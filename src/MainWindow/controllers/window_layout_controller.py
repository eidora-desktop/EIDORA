"""Bind main-window geometry and workspace splitters to local preferences."""
from PySide6 import QtCore, QtWidgets

from application_settings.session_repository import SessionPreferences


class WindowLayoutController(QtCore.QObject):
    def __init__(self, window, preferences=None):
        super().__init__(window)
        self._window = window
        self._preferences = preferences if preferences is not None else SessionPreferences()
        self._saved = self._preferences.read("window_layout")
        self._splitters = {}
        self._restored_splitters = set()
        self._shown = False
        self._restore(window.restoreGeometry, self._saved.get("geometry"))
        self._restore(window.restoreState, self._saved.get("state"))
        window.installEventFilter(self)

    @staticmethod
    def _restore(restore, value):
        if not isinstance(value, str):
            return False
        try:
            return restore(QtCore.QByteArray(bytes.fromhex(value)))
        except ValueError:
            return False

    def register_area(self, root):
        saved = self._saved.get("splitters", {})
        if not isinstance(saved, dict):
            saved = {}
        for splitter in root.findChildren(QtWidgets.QSplitter):
            key = f"{root.objectName()}/{splitter.objectName()}"
            if key in self._splitters:
                continue
            self._splitters[key] = splitter
            if self._restore(splitter.restoreState, saved.get(key)):
                self._restored_splitters.add(splitter)

    def has_saved_splitter(self, splitter):
        return splitter in self._restored_splitters

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.Show:
            self._shown = True
        elif event.type() == QtCore.QEvent.Type.Close:
            self.save()
        # QObject's default filter does nothing. During application teardown the
        # watched window's Python wrapper may already be invalid, so do not pass
        # it back through Shiboken just to obtain the default False result.
        return False

    def save(self):
        if not self._shown:
            return
        previous = self._saved.get("splitters", {})
        splitters = dict(previous) if isinstance(previous, dict) else {}
        splitters.update({key: bytes(widget.saveState()).hex() for key, widget in self._splitters.items()})
        self._saved = dict(
            geometry=bytes(self._window.saveGeometry()).hex(),
            state=bytes(self._window.saveState()).hex(),
            splitters=splitters,
        )
        self._preferences.update("window_layout", **self._saved)
