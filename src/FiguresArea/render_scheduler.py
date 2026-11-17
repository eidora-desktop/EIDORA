"""One render lane with visible-view priority and reliable export/load jobs."""
from collections import OrderedDict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import threading


@dataclass
class _Job:
    operation: object
    owner: object = None
    future: Future = field(default_factory=Future)
    obsolete: bool = False


class FigureRenderScheduler:
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='figure-preview')
        self._lock = threading.RLock()
        self._previews = OrderedDict()
        self._reliable = deque()
        self._active = None
        self._running = None
        self._draining = False
        self._closed = False
        self._last_preview = False

    def submit(self, operation, *args):
        """Exports and document loads are never superseded by preview edits."""
        job = _Job(lambda: operation(*args))
        with self._lock:
            self._check_open()
            self._reliable.append(job)
            self._start()
        return job.future

    def submit_preview(self, owner, operation):
        job = _Job(operation, owner)
        with self._lock:
            self._check_open()
            old = self._previews.pop(owner, None)
            if self._running is not None and self._running.owner is owner:
                self._running.obsolete = True
            self._previews[owner] = job
            self._start()
        if old is not None:
            old.future.cancel()
        return job.future

    def set_active(self, owner, active):
        with self._lock:
            if active:
                self._active = owner
            elif self._active is owner:
                self._active = None

    def cancel_preview(self, owner):
        with self._lock:
            old = self._previews.pop(owner, None)
            if self._running is not None and self._running.owner is owner:
                self._running.obsolete = True
        if old is not None:
            old.future.cancel()

    def _check_open(self):
        if self._closed:
            raise RuntimeError('Figure renderer has closed')

    def _start(self):
        if not self._draining:
            self._draining = True
            self._executor.submit(self._drain)

    def _may_draw(self, job):
        with self._lock:
            return not self._closed and not job.obsolete and self._active is job.owner

    def _drain(self):
        while True:
            with self._lock:
                active = self._active in self._previews
                # Alternate when both exist: the visible preview can bypass
                # hidden work, but repeated edits must not starve exports.
                if self._reliable and (not active or self._last_preview):
                    job = self._reliable.popleft()
                elif active:
                    job = self._previews.pop(self._active)
                elif self._previews:
                    _, job = self._previews.popitem(last=False)
                else:
                    self._draining = False
                    return
                self._running = job
                self._last_preview = job.owner is not None
            if job.future.set_running_or_notify_cancel():
                try:
                    result = job.operation(lambda job=job: self._may_draw(job)) if job.owner is not None else job.operation()
                except Exception as error:
                    job.future.set_exception(error)
                else:
                    job.future.set_result(result)
                    result = None
            with self._lock:
                self._running = None
            job = None  # Do not retain the last composition or reply while idle.

    def shutdown(self, *, wait=True, cancel_futures=True):
        with self._lock:
            self._closed = True
            jobs = list(self._previews.values()) + list(self._reliable)
            self._previews.clear()
            self._reliable.clear()
            if self._running is not None:
                self._running.obsolete = True
        for job in jobs:
            job.future.cancel()
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
