"""Latest-only transformation jobs; workers never own Qt objects."""
from concurrent.futures import ThreadPoolExecutor
import threading

from PySide6 import QtCore
from DiscoveryArea.transforms import TransformCancelled


class FigureTransformCoordinator(QtCore.QObject):
    ready = QtCore.Signal(int, object)
    failed = QtCore.Signal(int, str)
    cancelled = QtCore.Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._revision = 0
        self._jobs = []
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='figure-transform')
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(25)
        self._timer.timeout.connect(self._poll)

    def submit(self, operation):
        self.cancel_pending()
        event = threading.Event()
        future = self._executor.submit(operation, event.is_set)
        self._jobs.append((self._revision, event, future))
        self._timer.start()
        return self._revision

    def cancel_pending(self):
        self._revision += 1
        for _, event, future in self._jobs:
            event.set()
            future.cancel()

    def _poll(self):
        for job in list(self._jobs):
            revision, _, future = job
            if not future.done():
                continue
            self._jobs.remove(job)
            if revision != self._revision:
                continue
            try:
                result = future.result()
            except TransformCancelled:
                self.cancelled.emit(revision)
            except Exception as error:
                self.failed.emit(revision, f'{type(error).__name__}: {error}')
            else:
                self.ready.emit(revision, result)
        if not self._jobs:
            self._timer.stop()

    def shutdown(self):
        self.cancel_pending()
        self._timer.stop()
        self._executor.shutdown(wait=True, cancel_futures=True)
        self._jobs.clear()
