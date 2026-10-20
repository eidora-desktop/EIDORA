from __future__ import annotations

from collections.abc import Callable

from PySide6 import QtCore, QtWidgets


class _Signals(QtCore.QObject):
    finished = QtCore.Signal(object)
    failed = QtCore.Signal(object)


class _Task(QtCore.QRunnable):
    def __init__(self, operation: Callable[[], object]) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.operation = operation
        self.signals = _Signals()

    @QtCore.Slot()
    def run(self) -> None:
        try:
            result = self.operation()
        except Exception as exc:
            self.signals.failed.emit(exc)
        else:
            self.signals.finished.emit(result)


def run_snapshot_write_responsive(operation: Callable[[], object]) -> object:
    """Run snapshot compression off-thread while keeping the Qt event loop alive."""

    application = QtWidgets.QApplication.instance()
    if application is None or QtCore.QThread.currentThread() is not application.thread():
        return operation()
    loop = QtCore.QEventLoop()
    outcome: dict[str, object] = {}
    task = _Task(operation)

    def finished(value: object) -> None:
        outcome["value"] = value
        loop.quit()

    def failed(error: object) -> None:
        outcome["error"] = error
        loop.quit()

    task.signals.finished.connect(finished)
    task.signals.failed.connect(failed)
    QtCore.QThreadPool.globalInstance().start(task)
    loop.exec()
    error = outcome.get("error")
    if isinstance(error, BaseException):
        raise error
    return outcome.get("value")
