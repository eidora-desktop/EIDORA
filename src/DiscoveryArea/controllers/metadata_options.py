"""Load metadata choices without blocking or retaining obsolete panel requests."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from PySide6 import QtCore, QtWidgets
from shiboken6 import isValid


@dataclass
class _Request:
    signature: object
    load: object
    ready: object
    result: object = None


class MetadataOptions(QtCore.QObject):
    """One worker; one replaceable request per live control, delivered by polling."""

    def __init__(self, parent, failed):
        super().__init__(parent)
        self._failed = failed
        self._requests = {}
        self._failures = {}
        self._retry_button = None
        self._pending = OrderedDict()
        self._watched = set()
        self._active = None
        self._closed = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='metadata-options')
        executor = self._executor
        self.destroyed.connect(lambda: executor.shutdown(wait=False, cancel_futures=True))
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._poll)

    def get(self, owner, signature, load, ready):
        if self._closed:
            return None
        previous = self._requests.get(owner)
        if previous is not None and previous.signature == signature:
            previous.ready = ready
            return previous.result
        self._failures.pop(owner, None)
        self._update_retry_button()
        if isinstance(owner, QtCore.QObject) and owner not in self._watched:
            self._watched.add(owner)
            owner.destroyed.connect(lambda: self._forget(owner))
        request = _Request(signature, load, ready)
        self._requests[owner] = self._pending[owner] = request
        if isinstance(owner, QtCore.QObject):
            self._pending.move_to_end(owner, last=False)
        self._start()
        self._timer.start()
        return None

    def cancel(self, owner):
        self._requests.pop(owner, None)
        self._pending.pop(owner, None)
        self._failures.pop(owner, None)
        self._update_retry_button()

    def _forget(self, owner):
        self.cancel(owner)
        self._watched.discard(owner)

    def reset(self):
        self._requests.clear()
        self._pending.clear()
        self._failures.clear()
        self._update_retry_button()

    def retry_failed(self):
        for owner, request in list(self._failures.items()):
            self.get(owner, request.signature, request.load, request.ready)

    def is_pending(self, owner):
        request = self._requests.get(owner)
        return owner in self._failures or (request is not None and request.result is None)

    def _update_retry_button(self):
        # This transient action exists only after an actual read failure.
        if self._failures and self._retry_button is None:
            self._retry_button = QtWidgets.QPushButton('Retry metadata loading', self.parent())
            self._retry_button.setObjectName('button_retry_metadata')
            self._retry_button.clicked.connect(self.retry_failed)
            self.parent().layout().addWidget(self._retry_button)
        if self._retry_button is not None and isValid(self._retry_button):
            self._retry_button.setVisible(bool(self._failures))

    def _start(self):
        if self._active is None and self._pending and not self._closed:
            owner, request = self._pending.popitem(last=False)
            self._active = owner, request, self._executor.submit(request.load)

    def _poll(self):
        if self._active is None or not self._active[2].done():
            return
        owner, request, future = self._active
        self._active = None
        if not self._closed and self._requests.get(owner) is request:
            try:
                request.result = future.result()
            except Exception as exc:
                self._requests.pop(owner, None)
                self._failures[owner] = request
                self._update_retry_button()
                self._failed(f'Could not load metadata choices: {exc}')
            else:
                request.load = None
                request.ready()
        self._start()
        if self._active is None:
            self._timer.stop()

    def shutdown(self):
        self._closed = True
        self.reset()
        self._timer.stop()
        self._active = None
        self._executor.shutdown(wait=False, cancel_futures=True)
