"""Storage maintenance UI; filesystem work runs outside the GUI thread."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event

from PySide6 import QtCore, QtGui, QtWidgets

from application_storage.service import StorageService
from shared.env import get_env
from shared.paths import WORKSPACE_ROOT_ENV_VAR, suggested_workspace_root
from shared.qt.background_processes import BackgroundProcessManager
from shared.qt.message_dialogs import show_error
from shared.qt.ui_loader import load_ui


def format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return ""


class StorageDialogController(QtCore.QObject):
    def __init__(self, project_service, *, read_only=False):
        super().__init__()
        self.dialog = load_ui("MainWindow", "ui/storage_dialog.ui", None)
        self.dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self.projects = project_service
        self.service = StorageService(project_service.workspace_root)
        self.read_only = read_only
        self._connections = []
        self._future = None
        self._executor = None
        self._cancel = Event()
        self._updates = SimpleQueue()
        self._manager = BackgroundProcessManager(changed=lambda: None)
        self._admission = None
        self._success = None
        self._sizes = {}
        self._timer = QtCore.QTimer(self.dialog)
        self._connect(self._timer.timeout, self._poll)
        self._connect(self._widget("button_close").clicked, self.dialog.reject)
        self._connect(self._widget("button_cancel").clicked, self._request_cancel)
        self._connect(self._widget("button_show_projects").clicked,
                      lambda: self._show_folder(self.service.workspace))
        self._connect(self._widget("button_show_models").clicked,
                      lambda: self._show_folder(self.service.models))
        self._connect(self._widget("button_relocate_projects").clicked, lambda: self._relocate("workspace"))
        self._connect(self._widget("button_relocate_models").clicked, lambda: self._relocate("models"))
        self._connect(self._widget("button_clean").clicked, self._clean)
        self.dialog.installEventFilter(self)
        self._refresh_paths()
        if read_only:
            self._widget("label_explanation").setText(
                "Close EIDORA and reopen Storage from the starting project chooser to relocate "
                "files or remove downloads. Projects are always preserved during cleanup."
            )

    def _widget(self, name):
        widget = self.dialog.findChild(QtWidgets.QWidget, name)
        if widget is None:
            raise RuntimeError(f"storage_dialog.ui is missing {name}")
        return widget

    def _connect(self, signal, slot):
        signal.connect(slot)
        self._connections.append((signal, slot))

    def exec(self):
        self._measure()
        try:
            self.dialog.exec()
        finally:
            self._cancel.set()
            if self._executor:
                self._executor.shutdown(wait=True)
            if self._admission:
                self._admission.finish()
            self._timer.stop()
            self.dialog.removeEventFilter(self)
            for signal, slot in self._connections:
                signal.disconnect(slot)
            self._connections.clear()

    def eventFilter(self, watched, event):
        if watched is self.dialog and self._future is not None:
            if event.type() == QtCore.QEvent.Type.Close or (
                event.type() == QtCore.QEvent.Type.KeyPress and event.key() == QtCore.Qt.Key.Key_Escape
            ):
                self._request_cancel()
                event.ignore()
                return True
        return False

    def _show_folder(self, path):
        if not path.is_dir():
            show_error(self.dialog, "Folder Not Created", f"This folder does not exist yet:\n{path}")
            return
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(path)))

    def _refresh_paths(self):
        for key, path in (("projects", self.service.workspace), ("models", self.service.models),
                          ("cache", self.service.cache)):
            self._widget(f"edit_{key}").setText(str(path))
            self._widget(f"edit_{key}").setCursorPosition(0)
        preferences = self.service.preferences.read()
        previous = [str(preferences[key]) for key in ("previous_workspace_root", "previous_models_root")
                    if preferences.get(key)]
        label = self._widget("label_previous")
        label.setVisible(bool(previous))
        label.setText("Previous folders retained for manual review (excluded from sizes above):\n"
                      + "\n".join(previous))

    def _set_busy(self, busy):
        for name in ("button_relocate_projects", "button_relocate_models", "button_clean",
                     "check_models", "check_cache"):
            self._widget(name).setEnabled(not busy and not self.read_only)
        self._widget("button_clean").setEnabled(not busy and not self.read_only and bool(self._sizes))
        if get_env(WORKSPACE_ROOT_ENV_VAR).strip():
            self._widget("button_relocate_projects").setEnabled(False)
            self._widget("button_relocate_projects").setToolTip(
                "The workspace location is controlled by EIDORA_WORKSPACE_ROOT."
            )
        self._widget("button_close").setEnabled(not busy)
        self._widget("button_cancel").setEnabled(busy)
        self._widget("progress_storage").setVisible(busy)

    def _run(self, operation, success):
        self._cancel.clear()
        self._success = success
        self._set_busy(True)
        def start():
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="storage")
            self._future = self._executor.submit(
                operation, cancelled=self._cancel.is_set, progress=self._updates.put
            )
        self._admission = self._manager.submit(
            panel="Storage", title="Storage maintenance", start=start, cancel=self._cancel.set
        )
        self._manager.dispatch()
        self._timer.start(60)

    def _request_cancel(self):
        self._cancel.set()
        self._widget("button_cancel").setEnabled(False)
        self._widget("label_status").setText("Cancelling after the current file operation…")

    def _poll(self):
        while True:
            try:
                status = self._updates.get_nowait()
            except Empty:
                break
            if not self._cancel.is_set():
                self._widget("label_status").setText(status)
        if self._future is None or not self._future.done():
            return
        future, success = self._future, self._success
        self._future = None
        self._success = None
        self._timer.stop()
        self._executor.shutdown(wait=True)
        self._executor = None
        self._admission.finish()
        self._admission = None
        self._set_busy(False)
        try:
            result = future.result()
        except InterruptedError:
            self._widget("label_status").setText(
                "Cancelled. Original projects are preserved. Resources already removed stay removed."
            )
        except Exception as exc:
            self._widget("label_status").setText("The operation did not complete.")
            show_error(self.dialog, "Storage Operation Failed", str(exc))
        else:
            success(result)

    def _measure(self, message=""):
        self._sizes = {}
        def measured(sizes):
            self._sizes = sizes
            for key in ("projects", "models", "cache"):
                self._widget(f"label_{key}_size").setText(format_bytes(sizes[key]))
            self._widget("label_status").setText(message or
                "Relocation verifies a copy and keeps the originals. Close other EIDORA instances first.")
            self._set_busy(False)
        self._run(self.service.inspect, measured)

    def _relocate(self, kind):
        if self.read_only:
            return
        if kind == "workspace" and get_env(WORKSPACE_ROOT_ENV_VAR).strip():
            return
        suggested = suggested_workspace_root() if kind == "workspace" else self.service.models
        selected = QtWidgets.QFileDialog.getExistingDirectory(
            self.dialog, "Choose a New or Empty Destination Folder", str(suggested.parent)
        )
        if not selected:
            return
        destination = Path(selected)
        answer = QtWidgets.QMessageBox.question(
            self.dialog, "Relocate Storage",
            f"Copy and verify all {'projects in this workspace' if kind == 'workspace' else 'installed models'}"
            f" into:\n{destination}\n\n"
            "EIDORA will use this location after verification. The original files will remain "
            "for you to review and remove manually. External media stays at its existing location.\n\n"
            "Close other EIDORA instances before continuing.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        def completed(_):
            if kind == "workspace":
                self.projects.use_workspace(self.service.workspace)
            self._refresh_paths()
            self._measure("Location updated. Verified originals remain in the previous folder.")
        self._run(lambda **kwargs: self.service.relocate(kind, destination, **kwargs), completed)

    def _clean(self):
        if self.read_only:
            return
        models = self._widget("check_models").isChecked()
        cache = self._widget("check_cache").isChecked()
        if not models and not cache:
            self._widget("label_status").setText("Select the resources to remove.")
            return
        answer = QtWidgets.QMessageBox.warning(
            self.dialog, "Remove Downloaded Resources",
            "Remove the selected EIDORA-managed resources?\n\n"
            f"Installed model folder: {format_bytes(self._sizes.get('models', 0))}\n"
            f"Cache folder: {format_bytes(self._sizes.get('cache', 0))}\n\n"
            "Only models with an EIDORA download receipt, unfinished downloads, and the selected "
            "font cache are removed. Other files are retained, so reclaimed space may be smaller. "
            "Projects, including MET, are preserved. Removed models must be downloaded again.\n\n"
            "Close other EIDORA instances before continuing.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._run(lambda **kwargs: self.service.clean(models=models, cache=cache, **kwargs),
                  lambda size: self._measure(f"Removed {format_bytes(size)}. Projects were preserved."))
