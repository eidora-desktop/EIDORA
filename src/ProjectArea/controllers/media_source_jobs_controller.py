from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from queue import SimpleQueue, Empty
import threading
from uuid import uuid4

from PySide6 import QtCore
from EmbeddingsArea.domain import MediaProgress


@dataclass
class _Job:
    source_id: str
    operation: object
    finished: object
    failed: object
    generation: int
    cancel: threading.Event = field(default_factory=threading.Event)
    events: SimpleQueue = field(default_factory=SimpleQueue)
    future: object = None
    admission: object = None
    progress: MediaProgress = field(default_factory=lambda: MediaProgress("Queued"))
    cleanup: object = None


class MediaSourceJobsController:
    """Own workers until they stop; callbacks and progress run only on the GUI thread."""
    def __init__(self, activity_manager, changed):
        self._manager = getattr(activity_manager, "processes", None)
        self._changed = changed
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media-source")
        self._jobs = {}
        self._generation = 0
        self._closed = False
        # App-owned, so closing a dialog/panel never destroys a running worker's owner.
        self._timer = QtCore.QTimer(QtCore.QCoreApplication.instance())
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._poll)

    def submit(self, source_id, title, operation, finished, failed, *, cleanup=None):
        identity = uuid4().hex
        job = _Job(source_id, operation, finished, failed, self._generation, cleanup=cleanup)
        self._jobs[identity] = job
        def start():
            if job.cancel.is_set():
                return
            job.progress = MediaProgress(title)
            job.future = self._executor.submit(job.operation, job.events.put, job.cancel.is_set)
            self._notify(job)
        if self._manager is not None:
            job.admission = self._manager.submit(panel="Project Media Sources", title=title,
                                                start=start, cancel=job.cancel.set)
            self._manager.dispatch()
        else:
            start()
        self._notify(job)
        self._timer.start()
        return identity

    def busy(self, source_id=None):
        return any(job.generation == self._generation and (source_id is None or job.source_id == source_id)
                   for job in self._jobs.values())

    def cancel(self, identity):
        job = self._jobs.get(identity)
        if job:
            job.cancel.set()
            job.progress = MediaProgress("Cancelling")
            if job.admission:
                job.admission.cancel()
            self._notify(job)

    def cancel_source(self, source_id):
        for identity, job in list(self._jobs.items()):
            if job.source_id == source_id:
                self.cancel(identity)

    def reset(self):
        self._generation += 1
        for identity in tuple(self._jobs):
            self.cancel(identity)

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self.reset()
        self._executor.shutdown(wait=False, cancel_futures=True)
        if not self._jobs:
            self._dispose_timer()

    def _notify(self, job):
        if not self._closed and job.generation == self._generation:
            self._changed(job.source_id, job.progress, True)

    def _poll(self):
        for identity, job in list(self._jobs.items()):
            try:
                while True:
                    progress = job.events.get_nowait()
                    if not job.cancel.is_set():
                        job.progress = progress
            except Empty:
                pass
            self._notify(job)
            if job.future is None and not job.cancel.is_set():
                continue
            if job.future is not None and not job.future.done():
                continue
            self._jobs.pop(identity, None)
            current = not self._closed and job.generation == self._generation
            try:
                if job.future is not None and not job.future.cancelled():
                    result = job.future.result()
                    # Successful publication wins over a late cancel request.
                    if current:
                        job.finished(result)
                    elif callable(getattr(result, "cleanup", None)):
                        result.cleanup()
                    terminal = ("Failed" if getattr(result, "error_message", "") else
                                "Cancelled" if getattr(result, "cancelled", False) else "Complete")
                else:
                    terminal = "Cancelled"
            except Exception as exc:
                terminal = "Cancelled" if job.cancel.is_set() else "Failed"
                if current:
                    job.failed(str(exc), terminal == "Cancelled")
            finally:
                if current:
                    self._changed(job.source_id, MediaProgress(terminal), False)
                if callable(job.cleanup):
                    try:
                        job.cleanup()
                    except OSError:
                        pass
                if job.admission:
                    job.admission.finish()
        if not self._jobs:
            self._timer.stop()
            if self._closed:
                self._dispose_timer()

    def _dispose_timer(self):
        self._timer.stop()
        self._timer.timeout.disconnect(self._poll)
        self._timer.deleteLater()
