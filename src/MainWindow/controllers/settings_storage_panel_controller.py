"""Settings storage controls; measurement and maintenance run outside Qt."""
from pathlib import Path
from threading import Event

from PySide6 import QtCore, QtWidgets

from MainWindow.controllers.storage_dialog_controller import format_bytes
from shared.env import get_env
from shared.paths import WORKSPACE_ROOT_ENV_VAR
from shared.qt.background_tasks import BackgroundTasks
from shared.qt.message_dialogs import show_error
from shared.qt.widgets import require_child


class SettingsStoragePanelController(QtCore.QObject):
    def __init__(self, dialog, service, *, busy, before_project_storage_change, changed, status, busy_changed):
        super().__init__(dialog)
        self.dialog, self.service = dialog, service
        self._busy = busy
        self._before_project_storage_change = before_project_storage_change
        self._changed, self._status, self._busy_changed = changed, status, busy_changed
        self._tasks = BackgroundTasks(self)
        self._tasks.finished.connect(self._finished)
        self._tasks.failed.connect(self._failed)
        self._cancel = Event()
        self._running = False
        self._mutating = False
        self._started = False
        self._success = None
        self._connections = []
        self._sizes = {}
        self._widget("progress_storage").hide()
        self._progress_delay = QtCore.QTimer(self)
        self._progress_delay.setSingleShot(True)
        self._connect(self._progress_delay.timeout, self._show_progress)
        for kind, suffix in (("workspace", "projects"), ("models", "models")):
            self._connect(self._widget(f"button_browse_{suffix}").clicked,
                          lambda checked=False, kind=kind: self.browse(kind))
        self._connect(self._widget("button_remove_models").clicked, self._remove_models)
        self._connect(self._widget("button_remove_projects").clicked, self._remove_projects)
        self.dialog.installEventFilter(self)
        self.refresh_paths()
        self._update_controls()

    def _widget(self, name):
        return require_child(self.dialog, QtWidgets.QWidget, name)

    def _connect(self, signal, slot):
        signal.connect(slot)
        self._connections.append((signal, slot))

    def activate(self):
        if not self._started:
            self._started = True
            self._measure()

    def refresh_paths(self):
        storage = self.service.storage
        for name, path in (("edit_project_location", storage.workspace),
                           ("edit_model_location", storage.models), ("edit_cache", storage.cache)):
            edit = self._widget(name)
            edit.setText(str(path))
            edit.setCursorPosition(0)
            edit.setToolTip(str(path))

    def _update_controls(self):
        for name in ("button_browse_projects", "button_browse_models", "button_remove_models", "button_remove_projects"):
            self._widget(name).setEnabled(not self._running)
        if get_env(WORKSPACE_ROOT_ENV_VAR).strip():
            self._widget("button_browse_projects").setEnabled(False)
            self._widget("button_browse_projects").setToolTip("The project location is controlled by EIDORA_WORKSPACE_ROOT.")

    def _show_progress(self):
        self._widget("progress_storage").setVisible(self._running and self._mutating)

    def _run(self, operation, success, *, mutating=False):
        self._running, self._mutating = True, mutating
        self._success = success
        self._busy_changed(mutating)
        self._update_controls()
        self._progress_delay.stop()
        self._widget("progress_storage").hide()
        if mutating:
            self._progress_delay.start(500)
        self._tasks.submit(0, lambda progress: operation())

    def _settle(self):
        self._progress_delay.stop()
        self._widget("progress_storage").hide()
        self._running = self._mutating = False
        self._busy_changed(False)
        self._update_controls()

    def _finished(self, revision, result):
        success, self._success = self._success, None
        self._settle()
        success(result)

    def _failed(self, revision, message):
        self._success = None
        self._settle()
        self._status(f"Storage operation failed: {message}")
        for key in ("projects", "models", "cache"):
            if self._widget(f"label_{key}_size").text() == "Measuring…":
                self._widget(f"label_{key}_size").setText("Unavailable")

    def _measure(self, message=""):
        self._status(message or "Measuring storage…")
        for key in ("projects", "models", "cache"):
            self._widget(f"label_{key}_size").setText("Measuring…")
        storage, cancel = self.service.storage, self._cancel
        def measured(sizes):
            self._sizes = sizes
            for key, size in sizes.items():
                self._widget(f"label_{key}_size").setText(format_bytes(size))
            self._status(message)
        self._run(lambda: storage.inspect(cancelled=cancel.is_set), measured)

    def _available(self):
        if self._running:
            return False
        if self._busy():
            self._status("Finish or cancel background processes before changing storage.")
            return False
        return True

    def browse(self, kind):
        if not self._available():
            return
        storage = self.service.storage
        current = storage.workspace if kind == "workspace" else storage.models
        selected = QtWidgets.QFileDialog.getExistingDirectory(self.dialog, "Choose Storage Folder", str(current))
        if not selected or Path(selected).resolve() == current.resolve() or not self._available():
            return
        try:
            if not self.service.has_contents(kind):
                self.service.change_destination(kind, Path(selected))
                self.refresh_paths()
                self._changed(kind)
                self._measure("Location updated.")
                return
            destination = self.service.relocation_destination(kind, Path(selected))
            if not self._confirm_relocation() or not self._available():
                return
            wait_for_saves = self._before_project_storage_change()
        except (OSError, ValueError) as exc:
            show_error(self.dialog, "Storage Location Could Not Be Changed", str(exc))
            return
        self._status("Relocating storage…")
        def move():
            if wait_for_saves is not None:
                wait_for_saves()
            return storage.relocate(kind, destination, remove_source=True)
        def moved(message):
            if kind == "workspace":
                self.service.projects.use_workspace(storage.workspace)
            self.refresh_paths()
            self._changed(kind)
            self._measure(message or "Storage relocated. Close Settings to choose a project.")
        self._run(move, moved, mutating=True)

    def _confirm_relocation(self):
        message = QtWidgets.QMessageBox(self.dialog)
        message.setWindowTitle("Relocate Storage")
        message.setText("Move the current folder’s contents to the new location? The open project will close.")
        relocate = message.addButton("Relocate", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        keep = message.addButton("Keep Current Folder", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        message.setDefaultButton(keep)
        message.setEscapeButton(keep)
        message.exec()
        return message.clickedButton() is relocate

    def _remove_models(self):
        if not self._available():
            return
        answer = QtWidgets.QMessageBox.question(
            self.dialog, "Remove Downloaded Models",
            "Remove downloaded models? They will need to be downloaded again.\n"
            "Projects and manually installed models are kept.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes or not self._available():
            return
        storage = self.service.storage
        self._status("Removing downloaded models…")
        def cleaned(size):
            self._changed("models")
            self._measure(f"Removed {format_bytes(size)}. Projects were preserved.")
        self._run(lambda: storage.clean(models=True, cache=False), cleaned, mutating=True)

    def _remove_projects(self):
        if not self._available():
            return
        self._status("Checking projects…")
        self._run(self.service.projects_for_removal, self._confirm_project_removal)

    def _confirm_project_removal(self, projects):
        if not projects:
            self._status("No projects found in this location.")
            return
        if not self._available():
            return
        message = QtWidgets.QMessageBox(self.dialog)
        message.setWindowTitle("Remove All Projects")
        message.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        message.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        message.setText(f"Permanently delete all {len(projects)} projects in this location?")
        message.setInformativeText(
            f"{self.service.projects.workspace_root}\n\n"
            "All data stored in these project folders will be deleted. This cannot be undone. "
            "The open project will close. Projects elsewhere and external media are kept."
        )
        message.setDetailedText("\n".join(project.name for project in projects))
        message.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel)
        message.button(QtWidgets.QMessageBox.StandardButton.Yes).setText("Delete All Projects")
        message.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        if message.exec() != QtWidgets.QMessageBox.StandardButton.Yes or not self._available():
            self._status("Project removal cancelled.")
            return
        try:
            wait_for_saves = self._before_project_storage_change()
        except Exception as exc:
            self._status(f"Project removal could not start: {exc}")
            return
        service = self.service
        self._status("Removing projects…")
        def remove():
            if wait_for_saves is not None:
                wait_for_saves()
            return service.remove_projects(projects)
        def removed(result):
            count, failures = result
            status = f"Removed {count} of {len(projects)} projects. Close Settings to choose a project."
            if failures:
                status += "\nSome operations failed:\n" + "\n".join(failures)
            self._measure(status)
        self._run(remove, removed, mutating=True)

    def eventFilter(self, watched, event):
        if self._mutating and (event.type() == QtCore.QEvent.Type.Close or (
                event.type() == QtCore.QEvent.Type.KeyPress and event.key() == QtCore.Qt.Key.Key_Escape)):
            event.ignore()
            return True
        return False

    def dispose(self):
        self._progress_delay.stop()
        self._cancel.set()
        self._tasks.shutdown()
        self.dialog.removeEventFilter(self)
        for signal, slot in self._connections:
            signal.disconnect(slot)
        self._connections.clear()
