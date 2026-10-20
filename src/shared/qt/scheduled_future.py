from __future__ import annotations

from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from typing import Callable


class ScheduledFuture:
    """Submit work only after admission and deliver callbacks from poll()."""

    def __init__(
        self,
        manager,
        executor: ThreadPoolExecutor,
        function: Callable[[], object],
        *,
        panel: str,
        title: str,
        completed: Callable[[object], None] | None = None,
        failed: Callable[[str], None] | None = None,
        projection: bool = False,
    ) -> None:
        self._executor = executor
        self._function = function
        self._completed = completed
        self._failed = failed
        self._future: Future | None = None
        self._cancelled = False
        self._delivered = False
        self._admission = manager.processes.submit(
            panel=panel, title=title, projection=projection,
            start=self._start, cancel=self._cancel_worker,
        )

    def _start(self) -> None:
        if self._cancelled:
            return
        self._future = self._executor.submit(self._function)

    def _cancel_worker(self) -> None:
        if self._future is not None:
            self._future.cancel()

    def cancel(self) -> bool:
        if self.done():
            return False
        self._cancelled = True
        self._admission.cancel()
        self._cancel_worker()
        return True

    def cancelled(self) -> bool:
        return self._cancelled or bool(self._future and self._future.cancelled())

    def running(self) -> bool:
        return bool(self._future and self._future.running())

    def done(self) -> bool:
        return self.cancelled() or bool(self._future and self._future.done())

    def result(self, timeout: float | None = None):
        if self._cancelled:
            raise CancelledError()
        if self._future is None:
            raise RuntimeError("Background work is still queued.")
        return self._future.result(timeout=timeout)

    @property
    def future(self):
        return self

    def poll(self) -> None:
        if self._delivered or not self.done():
            return
        self._delivered = True
        try:
            value = self.result()
        except CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            if self._failed is not None:
                self._failed(str(exc))
        else:
            if self._completed is not None:
                self._completed(value)
        finally:
            self._admission.finish()
