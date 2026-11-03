"""NVIDIA device discovery and explicit, cancellable GPU setup."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading

from PySide6 import QtCore, QtWidgets

from EmbeddingsArea.runtime.cuda_install import download_plan, enable_runtime, installed_runtime, platform_key
from shared.internal_workers import internal_worker_command
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class _InstallThread(QtCore.QThread):
    progress = QtCore.Signal(int, str)

    def __init__(self, cancelled, parent):
        super().__init__(parent)
        self.cancelled = cancelled
        self.error = ""

    def run(self):
        previous = None

        def progress(value, message):
            nonlocal previous
            if previous != (value, message):
                previous = (value, message)
                self.progress.emit(value, message)

        try:
            enable_runtime(cancelled=self.cancelled.is_set, progress=progress)
        except Exception as exc:
            self.error = str(exc)


class CudaSetupController(QtCore.QObject):
    ready = QtCore.Signal()

    def __init__(self, button, *, activity_manager=None, declared=True):
        super().__init__(button)
        self.button = button
        self.manager = activity_manager
        self.process = None
        self.temporary = None
        self._closed = False
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._probe_timeout)
        self.button.hide()
        self.button.clicked.connect(self._open_setup)
        if isinstance(button.window(), QtWidgets.QDialog):
            button.window().finished.connect(self.close)
        if declared and platform_key() is not None:
            self._probe()

    def _probe(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="eidora-cuda-device-")
        self.result = Path(self.temporary.name) / "result.json"
        self.process = QtCore.QProcess(self)
        self.process.setStandardOutputFile(QtCore.QProcess.nullDevice())
        self.process.setStandardErrorFile(QtCore.QProcess.nullDevice())
        self.process.finished.connect(self._probe_finished)
        self.process.errorOccurred.connect(self._probe_error)
        command = internal_worker_command("cuda-device", self.result)
        self.timer.start(15000)
        self.process.start(command[0], command[1:])

    def _probe_error(self, error):
        if error == QtCore.QProcess.ProcessError.FailedToStart:
            self._probe_finished(-1)

    def _probe_timeout(self):
        if self.process is not None:
            self.process.kill()

    def _probe_finished(self, exit_code=0, *_args):
        self.timer.stop()
        try:
            result = json.loads(self.result.read_text(encoding="utf-8"))
            visible = exit_code == 0 and result.get("ok") is True and bool(result.get("devices"))
        except (OSError, ValueError):
            visible = False
        self.button.setVisible(visible)
        if visible:
            self.button.setToolTip("NVIDIA device: " + ", ".join(result["devices"]))
            self.button.setText("Check NVIDIA GPU…" if installed_runtime() else "Enable NVIDIA GPU…")
        if self.temporary is not None:
            self.temporary.cleanup()
            self.temporary = None

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.timer.stop()
        if self.process is not None:
            self.process.finished.disconnect(self._probe_finished)
            self.process.errorOccurred.disconnect(self._probe_error)
            self.process.kill()
            self.process.waitForFinished(1000)
        if self.temporary is not None:
            self.temporary.cleanup()
            self.temporary = None

    def _open_setup(self):
        controller = _SetupDialog(self.manager)
        try:
            controller.dialog.exec()
            if controller.succeeded:
                self.button.setText("Check NVIDIA GPU…")
                self.ready.emit()
        finally:
            controller.dispose()


class _SetupDialog(QtCore.QObject):
    def __init__(self, manager):
        super().__init__()
        self.manager = manager
        self.dialog = load_ui("EmbeddingsArea", "ui/cuda_setup_dialog.ui")
        self.dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self.dialog.installEventFilter(self)
        self.status = require_child(self.dialog, QtWidgets.QLabel, "label_status")
        self.progress = require_child(self.dialog, QtWidgets.QProgressBar, "progress_download")
        self.download = require_child(self.dialog, QtWidgets.QPushButton, "button_download")
        self.cancel = require_child(self.dialog, QtWidgets.QPushButton, "button_cancel")
        self.thread = None
        self.admission = None
        self.cancelled = threading.Event()
        self.succeeded = False
        size = sum(w["size"] for w in download_plan()["wheels"])
        self.status.setText(
            "GPU support is already downloaded. Check that it works with your NVIDIA GPU."
            if installed_runtime() else
            f"Download {size / 1024**3:.2f} GiB of NVIDIA GPU support? "
            "Allow 5 GiB of free disk space during setup. CPU execution needs no download. "
            "Requires NVIDIA driver 570.65+ on Windows or 570.26+ on Linux. "
            "NVIDIA runtime license files are included with the download."
        )
        self.download.setText("Check GPU" if installed_runtime() else "Download and enable")
        self.download.clicked.connect(self._submit)
        self.cancel.clicked.connect(self._cancel_or_close)

    @property
    def busy(self):
        return self.admission is not None or self.thread is not None

    def eventFilter(self, watched, event):
        if watched is self.dialog and self.busy and (
                event.type() == QtCore.QEvent.Type.Close or
                event.type() == QtCore.QEvent.Type.KeyPress and event.key() == QtCore.Qt.Key.Key_Escape):
            self._cancel_or_close()
            event.ignore()
            return True
        return super().eventFilter(watched, event)

    def _submit(self):
        self.cancelled.clear()
        self.download.setEnabled(False)
        self.cancel.setText("Cancel download")
        self.status.setText("Waiting to set up NVIDIA GPU support…")
        if self.manager is not None:
            self.admission = self.manager.processes.submit(
                panel="Models", title="Set up NVIDIA GPU support", start=self._start,
                cancel=self._request_cancel,
            )
            self.manager.processes.dispatch()
        else:
            self._start()

    def _start(self):
        self.thread = _InstallThread(self.cancelled, self)
        self.thread.progress.connect(self._progress)
        self.thread.finished.connect(self._finished)
        self.thread.start()

    def _progress(self, value, message):
        self.progress.setValue(value)
        self.status.setText(message)

    def _request_cancel(self):
        self.cancelled.set()
        self.status.setText("Cancelling GPU setup…")
        self.cancel.setEnabled(False)
        if self.thread is None:
            self._finished()

    def _cancel_or_close(self):
        if not self.busy:
            self.dialog.reject()
        elif self.admission is not None:
            self.admission.cancel()
        else:
            self._request_cancel()

    def _finished(self):
        error = self.thread.error if self.thread is not None else ""
        if self.thread is not None:
            self.thread.deleteLater()
            self.thread = None
        admission, self.admission = self.admission, None
        if admission is not None:
            admission.finish()
        self.succeeded = not error and not self.cancelled.is_set()
        self.status.setText("GPU setup cancelled. You can retry." if self.cancelled.is_set() else
                            error or "NVIDIA GPU support is ready. Select CUDA in Execution device.")
        self.cancel.setEnabled(True)
        self.cancel.setText("Close")
        self.download.setEnabled(not self.succeeded)
        self.download.setText("Retry")
        if self.succeeded:
            self.progress.setValue(100)

    def dispose(self):
        if self.busy:
            self.cancelled.set()
            if self.admission is not None:
                self.admission.cancel()
            if self.thread is not None:
                # Application shutdown can end the nested event loop. Keep the
                # callback-owning controller alive until its worker really exits.
                self.thread.wait()
                self.thread.finished.disconnect(self._finished)
                self._finished()
        self.dialog.removeEventFilter(self)
        self.download.clicked.disconnect(self._submit)
        self.cancel.clicked.disconnect(self._cancel_or_close)
        self.dialog.deleteLater()
