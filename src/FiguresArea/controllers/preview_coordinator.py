"""Coalesce view edits and deliver background completion on the GUI thread."""
from __future__ import annotations

from concurrent.futures import CancelledError
import weakref

from PySide6 import QtCore


def _wake_preview(reference):
    """A future callback may only enqueue a signal, never touch GUI state."""
    coordinator = reference()
    if coordinator is not None:
        try:
            coordinator._completed.emit()
        except RuntimeError:
            pass  # The parent Qt widget may already have been destroyed.


class FigurePreviewCoordinator(QtCore.QObject):
    ready = QtCore.Signal(object)
    published = QtCore.Signal(object)
    failed = QtCore.Signal(str)
    _completed = QtCore.Signal()

    def __init__(self, service, parent=None, *, image_base=None):
        super().__init__(parent)
        self._service = service
        self._image_base = image_base
        self._future = None
        self._pending = None
        self._closed = False
        self._revision = 0
        self._inflight = {}
        self._owner = object()  # Scheduler owns a token, never a widget/controller.
        self._active = True
        self._scheduled = callable(getattr(service, 'set_preview_active', None))
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._poll)
        self._completed.connect(self._poll, QtCore.Qt.ConnectionType.QueuedConnection)

    def set_active(self, active):
        self._active = active
        if self._scheduled:
            self._service.set_preview_active(self._owner, active)

    def submit(self, composition):
        if self._closed:
            return
        self._revision += 1
        self._pending = composition
        if self._scheduled:
            self._service.cancel_preview(self._owner)
        self._timer.start()  # Coalesce this event-loop turn, without a 25 ms wait.

    def cancel(self):
        self._revision += 1
        self._pending = None
        self._timer.stop()
        if self._scheduled:
            self._service.cancel_preview(self._owner)
        for future in list(self._inflight):
            future.cancel()

    def shutdown(self):
        self._closed = True
        self.cancel()
        self.set_active(False)
        self._inflight.clear()
        self._future = None

    @QtCore.Slot()
    def _poll(self):
        if self._closed:
            return
        for future, revision in list(self._inflight.items()):
            if self._closed:
                return
            if not future.done():
                continue
            self._inflight.pop(future, None)
            if self._future is future:
                self._future = None
            try:
                result = future.result()
            except CancelledError:
                continue
            except Exception as error:
                if revision == self._revision:
                    self.failed.emit(str(error))
            else:
                if isinstance(result, dict):
                    # Data identity, not preview revision, determines whether
                    # a still-referenced dataset publication can be adopted.
                    self.published.emit(result.get('datasets', []))
                if revision == self._revision and not (isinstance(result, dict) and result.get('publication_only')):
                    self.ready.emit(result)
        if self._closed:
            return
        if self._pending is not None:
            composition, self._pending = self._pending, None
            options = {} if self._image_base is None else {'incremental': True, 'base_frame': self._image_base()}
            if self._scheduled:
                self.set_active(self._active)
                options['owner'] = self._owner
            try:
                future = self._service.preview(composition, **options)
            except Exception as error:
                self.failed.emit(str(error))
            else:
                self._future = future
                self._inflight[future] = self._revision
                reference = weakref.ref(self)
                future.add_done_callback(lambda _future, ref=reference: _wake_preview(ref))
        self._timer.stop()
