from __future__ import annotations

from collections.abc import Callable
import threading

from PySide6 import QtCore

from DiscoveryArea.nearest_neighbors import (
    EmbeddingNeighborIndex,
    NeighborSearchCancelled,
    NeighborSearchRequest,
    search_neighbors,
)


NeighborIndexLoader = Callable[[], EmbeddingNeighborIndex]


class _SearchSignals(QtCore.QObject):
    finished = QtCore.Signal(int, str, object, object)
    failed = QtCore.Signal(int, str)
    cancelled = QtCore.Signal(int)


class _SearchTask(QtCore.QRunnable):
    def __init__(
        self,
        revision: int,
        cache_key: str,
        loader: NeighborIndexLoader,
        cached_index: EmbeddingNeighborIndex | None,
        request: NeighborSearchRequest,
        cancelled: threading.Event,
    ) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.revision = int(revision)
        self.cache_key = str(cache_key)
        self.loader = loader
        self.cached_index = cached_index
        self.request = request
        self.cancelled = cancelled
        self.signals = _SearchSignals()

    @QtCore.Slot()
    def run(self) -> None:
        try:
            index = self.cached_index or self.loader()
            result = search_neighbors(
                index,
                self.request,
                cancelled=self.cancelled.is_set,
            )
        except NeighborSearchCancelled:
            self.signals.cancelled.emit(self.revision)
            return
        except Exception as exc:
            self.signals.failed.emit(
                self.revision,
                f"{type(exc).__name__}: {exc}",
            )
            return
        if self.cancelled.is_set():
            self.signals.cancelled.emit(self.revision)
            return
        self.signals.finished.emit(self.revision, self.cache_key, index, result)


class NearestNeighborCoordinator(QtCore.QObject):
    """Own latest-only embedding load and nearest-neighbor search work."""

    result_ready = QtCore.Signal(int, object)
    search_failed = QtCore.Signal(int, str)

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._revision = 0
        self._tasks: dict[int, _SearchTask] = {}
        self._cancel_events: dict[int, threading.Event] = {}
        self._cached_key = ""
        self._cached_index: EmbeddingNeighborIndex | None = None

    def submit(
        self,
        *,
        cache_key: str,
        loader: NeighborIndexLoader,
        request: NeighborSearchRequest,
    ) -> int:
        self.cancel_pending()
        revision = self._revision
        cached = self._cached_index if self._cached_key == str(cache_key) else None
        cancelled = threading.Event()
        task = _SearchTask(
            revision,
            cache_key,
            loader,
            cached,
            request,
            cancelled,
        )
        task.signals.finished.connect(self._on_finished)
        task.signals.failed.connect(self._on_failed)
        task.signals.cancelled.connect(self._on_cancelled)
        self._tasks[revision] = task
        self._cancel_events[revision] = cancelled
        QtCore.QThreadPool.globalInstance().start(task)
        return revision

    def cancel_pending(self, *, clear_cache: bool = False) -> None:
        self._revision += 1
        for event in self._cancel_events.values():
            event.set()
        if clear_cache:
            self._cached_key = ""
            self._cached_index = None

    @QtCore.Slot(int, str, object, object)
    def _on_finished(
        self,
        revision: int,
        cache_key: str,
        index: object,
        result: object,
    ) -> None:
        self._forget(revision)
        if int(revision) != self._revision:
            return
        if isinstance(index, EmbeddingNeighborIndex):
            self._cached_key = str(cache_key)
            self._cached_index = index
        self.result_ready.emit(int(revision), result)

    @QtCore.Slot(int, str)
    def _on_failed(self, revision: int, message: str) -> None:
        self._forget(revision)
        if int(revision) == self._revision:
            self.search_failed.emit(int(revision), str(message))

    @QtCore.Slot(int)
    def _on_cancelled(self, revision: int) -> None:
        self._forget(revision)

    def _forget(self, revision: int) -> None:
        self._tasks.pop(int(revision), None)
        self._cancel_events.pop(int(revision), None)
