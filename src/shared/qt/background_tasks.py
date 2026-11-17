"""Collect plain Python task results on the owning GUI thread."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from PySide6 import QtCore


class BackgroundTasks(QtCore.QObject):
    finished = QtCore.Signal(int, object)
    failed = QtCore.Signal(int, str)
    progress = QtCore.Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="eidora-discovery")
        self._active = None
        self._queued = None
        self._skipped = []
        self._closed = False
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._poll)
        executor = self._executor
        self.destroyed.connect(lambda: executor.shutdown(wait=False, cancel_futures=True))

    def submit(self, revision: int, function) -> None:
        """Run function(report_progress); it must hold no widgets or Qt objects."""
        if self._closed:
            raise RuntimeError("Background tasks have been shut down.")
        # Keep just one replaceable request behind active work. Cancelled
        # executor work items would otherwise retain all their input arrays.
        if self._queued is not None:
            self._skipped.append(self._queued[0])
        self._queued = (revision, function)
        self._start_next()
        self._timer.start()

    def _start_next(self) -> None:
        if self._closed or self._active is not None or self._queued is None:
            return
        revision, function = self._queued
        self._queued = None
        updates = deque(maxlen=1)
        future = self._executor.submit(function, updates.append)
        self._active = (revision, future, updates)

    @QtCore.Slot()
    def _poll(self) -> None:
        skipped, self._skipped = self._skipped, []
        for revision in skipped:
            if self._closed:
                return
            self.finished.emit(revision, None)
        if not self._closed and self._active is not None:
            active = self._active
            revision, future, updates = active
            try:
                value = updates.popleft()
            except IndexError:
                pass
            else:
                self.progress.emit(revision, int(value))
            # Progress handlers can enter a nested Qt event loop. An inner poll
            # may already have collected this future and started another job.
            if self._closed or self._active is not active or not future.done():
                return
            self._active = None
            try:
                result = None if future.cancelled() else future.result()
            except Exception as exc:
                self.failed.emit(revision, f"{type(exc).__name__}: {exc}")
            else:
                self.finished.emit(revision, result)
        self._start_next()
        if self._active is None and not self._skipped:
            self._timer.stop()

    def shutdown(self) -> None:
        self._closed = True
        self._timer.stop()
        self._active = self._queued = None
        self._skipped.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)
