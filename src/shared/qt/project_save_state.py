"""Observable pending project writes, owned by one area and project session."""
from PySide6 import QtCore


class ProjectSaveState(QtCore.QObject):
    changed = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pending: dict[str, str] = {}

    def begin(self, name: str) -> None:
        # Keep a failure visible until a write actually succeeds.
        self.pending.setdefault(name, "")
        self.changed.emit()

    def failed(self, name: str, error: Exception) -> None:
        self.pending[name] = str(error) or type(error).__name__
        self.changed.emit()

    def saved(self, name: str) -> None:
        self.pending.pop(name, None)
        self.changed.emit()

    def clear(self) -> None:
        self.pending.clear()
        self.changed.emit()
