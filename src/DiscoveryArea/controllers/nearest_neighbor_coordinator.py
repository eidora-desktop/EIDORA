from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import threading

from PySide6 import QtCore

from DiscoveryArea.nearest_neighbors import (
    EmbeddingNeighborIndex,
    NeighborSearchCancelled,
    NeighborSearchRequest,
    NeighborSearchResult,
    search_neighbors,
)


NeighborIndexLoader = Callable[[], EmbeddingNeighborIndex]


@dataclass
class _SearchTask:
    revision: int
    cache_key: str
    loader: NeighborIndexLoader
    cached_index: EmbeddingNeighborIndex | None
    request: NeighborSearchRequest
    cancelled: threading.Event

    def run(self) -> tuple[EmbeddingNeighborIndex, NeighborSearchResult]:
        if self.cancelled.is_set():
            raise NeighborSearchCancelled()
        index = self.cached_index or self.loader()
        if self.cancelled.is_set():
            raise NeighborSearchCancelled()
        result = search_neighbors(index, self.request, cancelled=self.cancelled.is_set)
        return index, result


class NearestNeighborCoordinator(QtCore.QObject):
    """Keep one running search and one replaceable request; workers own no Qt objects."""

    result_ready = QtCore.Signal(int, object)
    search_failed = QtCore.Signal(int, str)

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._revision = 0
        self._active: _SearchTask | None = None
        self._pending: _SearchTask | None = None
        self._future: Future | None = None
        self._cached_key = ""
        self._cached_index: EmbeddingNeighborIndex | None = None
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="eidora-neighbors")
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._poll)

    def submit(
        self,
        *,
        cache_key: str,
        loader: NeighborIndexLoader,
        request: NeighborSearchRequest,
    ) -> int:
        if self._closed:
            raise RuntimeError("Nearest-neighbor searches have been shut down.")
        self.cancel_pending()
        revision = self._revision
        cached = self._cached_index if self._cached_key == str(cache_key) else None
        self._pending = _SearchTask(revision, str(cache_key), loader, cached, request, threading.Event())
        self._start_pending()
        return revision

    def _start_pending(self) -> None:
        if self._future is not None or self._pending is None or self._closed:
            return
        self._active, self._pending = self._pending, None
        self._future = self._executor.submit(self._active.run)
        self._timer.start()

    def cancel_pending(self, *, clear_cache: bool = False) -> None:
        self._revision += 1
        self._pending = None
        if self._active is not None:
            self._active.cancelled.set()
        if clear_cache:
            self._cached_key = ""
            self._cached_index = None

    @QtCore.Slot()
    def _poll(self) -> None:
        if self._future is None or not self._future.done():
            return
        future, self._future = self._future, None
        task, self._active = self._active, None
        self._timer.stop()
        try:
            index, result = future.result()
            if task.revision == self._revision and not task.cancelled.is_set():
                self._cached_key, self._cached_index = task.cache_key, index
                self.result_ready.emit(task.revision, result)
        except NeighborSearchCancelled:
            pass
        except Exception as exc:
            if task.revision == self._revision:
                self.search_failed.emit(task.revision, f"{type(exc).__name__}: {exc}")
        finally:
            self._start_pending()

    def shutdown(self) -> None:
        self._closed = True
        self.cancel_pending(clear_cache=True)
        self._timer.stop()
        self._active = self._future = None
        # A file read already in progress may finish, but cannot touch Qt or
        # publish a result. Never wait for that I/O on the GUI thread.
        self._executor.shutdown(wait=False, cancel_futures=True)
