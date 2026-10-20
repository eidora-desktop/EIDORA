from __future__ import annotations

from PySide6 import QtCore


class WorkerSignals(QtCore.QObject):
    """Common signal set for future background workers."""

    finished = QtCore.Signal()
    failed = QtCore.Signal(str)
    progress = QtCore.Signal(int, str)

