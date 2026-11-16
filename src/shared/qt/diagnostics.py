"""Application-owned diagnostics, independent of project and window lifetimes."""
from __future__ import annotations

import atexit
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from shared.diagnostics.logging import DiagnosticSession
from shared.diagnostics.repository import ReportRepository
from shared.diagnostics.service import LOGGER
from shared.paths import application_data_root


def report_repository() -> ReportRepository:
    location = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.StandardLocation.AppLocalDataLocation)
    return ReportRepository((Path(location) if location else application_data_root()) / "diagnostics")


class DiagnosticsMonitor(QtCore.QObject):
    unexpected_error = QtCore.Signal(str)

    def __init__(self, app: QtWidgets.QApplication) -> None:
        super().__init__(app)
        self.session = None
        self._closed = False
        self._showing_error = False
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self._heartbeat)
        self.unexpected_error.connect(self._show_error, QtCore.Qt.ConnectionType.QueuedConnection)
        try:
            self.session = DiagnosticSession(report_repository(), self.unexpected_error.emit,
                                             qt_version=QtCore.qVersion())
        except (OSError, RuntimeError):
            LOGGER.warning("Local diagnostic storage is unavailable; EIDORA will continue.")
        if self.session is not None:
            LOGGER.info("Qt %s", QtCore.qVersion())
            self._heartbeat()
            self._timer.start()
            atexit.register(self.session.close)
        app.aboutToQuit.connect(self.stop)

    def _heartbeat(self) -> None:
        if self.session is not None:
            self.session.heartbeat()

    @QtCore.Slot(str)
    def _show_error(self, message: str) -> None:
        if self._closed or self._showing_error or QtCore.QCoreApplication.closingDown():
            return
        from shared.qt.message_dialogs import show_error

        self._showing_error = True
        try:
            show_error(None, "Unexpected EIDORA Error", message +
                       "\n\nIf EIDORA no longer works correctly, restart it. "
                       "Review the diagnostic report before sharing it with the developer.")
        finally:
            self._showing_error = False

    def stop(self) -> None:
        self._timer.stop()
        if self.session is not None:
            self.session.stop_heartbeat()

    @QtCore.Slot()
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop()
        if self.session is not None:
            self.session.close()
            atexit.unregister(self.session.close)
