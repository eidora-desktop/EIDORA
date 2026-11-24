"""Load visible text pins with cancellation and stale-result rejection."""
from functools import partial
import threading

from PySide6 import QtCore

from DiscoveryArea.pin_text_preview_service import load_pin_text_previews
from shared.qt.background_tasks import BackgroundTasks


class PinTextPreviewController(QtCore.QObject):
    previews_ready = QtCore.Signal(object)

    def __init__(self, canvas):
        super().__init__(canvas)
        self._canvas = canvas
        self._key = self._request_key = None
        self._entries = {}
        self._revision = 0
        self._cancelled = threading.Event()
        self._tasks = BackgroundTasks(self)
        self._tasks.finished.connect(self._finished)
        canvas.pin_media_visibility_changed.connect(self.refresh_visible)

    def configure(self, root, variant, entries):
        key = (root, variant)
        if key != self._key:
            self.reset()
            self._key = key
        self._entries = {str(entry["annotation_id"]): int(entry["metadata_id"]) for entry in entries}
        self._canvas.configure_pin_media("text_excerpt")
        self.refresh_visible(self._canvas.visible_pin_media_ids())

    def refresh_visible(self, visible):
        if self._key is None:
            return
        entries = tuple(sorted((key, self._entries[key]) for key in set(visible) & self._entries.keys()))
        request = (self._key, entries)
        if request == self._request_key:
            return
        self._request_key = request
        self._revision += 1
        self._cancelled.set()
        self._cancelled = threading.Event()
        if not entries:
            return
        root, variant = self._key
        job = partial(load_pin_text_previews, root, variant, entries, self._cancelled)
        self._tasks.submit(self._revision, lambda _progress: job())

    def _finished(self, revision, result):
        if revision == self._revision and result:
            self.previews_ready.emit(result)

    def reset(self):
        self._revision += 1
        self._cancelled.set()
        self._key = self._request_key = None
        self._entries = {}

    def shutdown(self):
        self.reset()
        self._tasks.shutdown()
