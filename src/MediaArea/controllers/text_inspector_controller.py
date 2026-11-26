"""Inspect saved plain text with one bounded background read at a time."""
from concurrent.futures import ThreadPoolExecutor

from PySide6 import QtCore, QtWidgets

from MediaArea.text_inspection_service import TextInspectionService
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class TextInspectorController:
    def __init__(self, repository, variant_id, label):
        self._service = TextInspectionService(repository, variant_id)
        self.dialog = load_ui("MediaArea", "ui/text_inspector.ui", None)
        self.dialog.setWindowTitle(f"Inspect {label}")
        self.dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self._text = require_child(self.dialog, QtWidgets.QPlainTextEdit, "edit_excerpt")
        self._item = require_child(self.dialog, QtWidgets.QLabel, "label_item")
        self._status = require_child(self.dialog, QtWidgets.QLabel, "label_status")
        self._previous = require_child(self.dialog, QtWidgets.QPushButton, "button_previous")
        self._next = require_child(self.dialog, QtWidgets.QPushButton, "button_next")
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="text-excerpt-inspector")
        self._future = self._preview = None
        self._timer = QtCore.QTimer(self.dialog)
        self._timer.setInterval(30)
        self._connections = [
            (self._timer.timeout, self._poll),
            (self._previous.clicked, lambda: self._load(True)),
            (self._next.clicked, lambda: self._load(False)),
            (require_child(self.dialog, QtWidgets.QPushButton, "button_close").clicked, self.dialog.accept),
        ]
        for signal, slot in self._connections:
            signal.connect(slot)
        self._load()

    def exec(self):
        try:
            self.dialog.exec()
        finally:
            self.close()

    def close(self):
        self._timer.stop()
        if self._future:
            self._future.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
        for signal, slot in self._connections:
            signal.disconnect(slot)
        self._connections.clear()

    def _load(self, previous=False):
        self._previous.setEnabled(False)
        self._next.setEnabled(False)
        self._text.clear()
        self._status.setText("Loading text excerpt…")
        metadata_id = self._preview.metadata_id if self._preview else None
        self._future = self._executor.submit(self._service.adjacent, metadata_id, previous=previous)
        self._timer.start()

    def _poll(self):
        if self._future is None or not self._future.done():
            return
        self._timer.stop()
        future, self._future = self._future, None
        try:
            self._preview, before, after = future.result()
            self._previous.setEnabled(before)
            self._next.setEnabled(after)
            if self._preview is None:
                self._item.clear()
                self._status.setText("No saved text excerpts are available.")
                return
            preview = self._preview
            prefix = "…" if preview.start_character else ""
            suffix = "…" if preview.end_character < preview.source_characters else ""
            self._text.setPlainText(prefix + preview.text + suffix)
            self._item.setText(f"Item {preview.metadata_id} · {len(preview.text):,} characters")
            self._status.setText(f"Characters {preview.start_character + 1:,}–{preview.end_character:,} of {preview.source_characters:,}. "
                                 "Ellipses indicate omitted text.")
        except Exception as exc:
            self._status.setText(f"Text excerpt unavailable: {exc}")
