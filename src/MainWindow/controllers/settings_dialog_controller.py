"""Bind General settings; changes persist immediately through their owners."""
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from application_settings.service import SettingsService
from shared.env import get_env
from shared.paths import WORKSPACE_ROOT_ENV_VAR
from shared.qt.message_dialogs import show_error
from shared.qt.theme import install_theme
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class SettingsDialogController:
    def __init__(self, projects, *, service=None, project_open=False, current_project=None,
                 storage_busy=lambda: False, models_changed=lambda: None,
                 presets_changed=lambda project: None, required_preset_paths=lambda project: ()):
        self.service = service or SettingsService(projects)
        self._storage_busy = storage_busy
        self._models_changed = models_changed
        self._project_open = project_open
        self._connections = []
        self._presets_panel = None
        self._current_project = current_project
        self._presets_changed = presets_changed
        self._required_preset_paths = required_preset_paths
        self.dialog = load_ui("MainWindow", "ui/settings_dialog.ui", None)
        self.dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self._theme = install_theme(QtWidgets.QApplication.instance())
        self._pending_appearance = None
        self._appearance_timer = QtCore.QTimer(self.dialog)
        self._appearance_timer.setSingleShot(True)
        self._connect(self._appearance_timer.timeout, self._apply_pending_appearance)
        self._status = require_child(self.dialog, QtWidgets.QLabel, "label_settings_status")
        self._radios = {
            "appearance": {value: require_child(self.dialog, QtWidgets.QRadioButton, f"radio_appearance_{value}")
                           for value in ("light", "dark", "system")},
            "startup": {value: require_child(self.dialog, QtWidgets.QRadioButton, f"radio_startup_{value}")
                        for value in ("chooser", "last_project")},
        }
        self._refresh()
        for key, choices in self._radios.items():
            for value, button in choices.items():
                self._connect(button.clicked, lambda checked, key=key, value=value: self._save(key, value))
        for kind, suffix in (("workspace", "projects"), ("models", "models")):
            button = require_child(self.dialog, QtWidgets.QPushButton, f"button_browse_{suffix}")
            self._connect(button.clicked, lambda checked=False, kind=kind: self._browse(kind))
        projects_button = require_child(self.dialog, QtWidgets.QPushButton, "button_browse_projects")
        if get_env(WORKSPACE_ROOT_ENV_VAR).strip():
            projects_button.setEnabled(False)
            projects_button.setToolTip("The project location is controlled by EIDORA_WORKSPACE_ROOT.")
        buttons = require_child(self.dialog, QtWidgets.QDialogButtonBox, "buttons_dialog")
        reset = buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Reset)
        reset.setText("Restore Defaults…")
        reset.setAutoDefault(False)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Close).setDefault(True)
        reset.setToolTip("Restore System appearance and Project Chooser startup. Storage locations are preserved.")
        self._connect(reset.clicked, self._reset)
        self._tabs = require_child(self.dialog, QtWidgets.QTabWidget, "tabs_settings")
        self._connect(self._tabs.currentChanged, self._tab_changed)
        self._tab_changed(self._tabs.currentIndex())

    def _tab_changed(self, index):
        presets = self._tabs.widget(index).objectName() == "tab_presets"
        buttons = require_child(self.dialog, QtWidgets.QDialogButtonBox, "buttons_dialog")
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Reset).setVisible(not presets)
        self._status.setVisible(not presets and bool(self._status.text()))
        require_child(self.dialog, QtWidgets.QWidget, "panel_preset_actions").setVisible(presets)
        preset_status = require_child(self.dialog, QtWidgets.QLabel, "label_presets_status")
        preset_status.setVisible(presets and bool(preset_status.text()))
        if presets:
            if self._presets_panel is None:
                from MainWindow.controllers.presets_panel_controller import PresetsPanelController

                self._presets_panel = PresetsPanelController(
                    self.dialog, self.service.projects, current_project=self._current_project,
                    changed=self._presets_changed, required_paths=self._required_preset_paths,
                    busy_changed=self._preset_deletion_busy,
                )
            self._presets_panel.activate()

    def _preset_deletion_busy(self, busy):
        self._tabs.setEnabled(not busy)
        require_child(self.dialog, QtWidgets.QDialogButtonBox, "buttons_dialog").setEnabled(not busy)

    def _show_status(self, text):
        self._status.setText(text)
        self._status.setVisible(bool(text) and self._tabs.currentWidget().objectName() == "tab_general")

    def _connect(self, signal, slot):
        signal.connect(slot)
        self._connections.append((signal, slot))

    def select_tab(self, name: str) -> None:
        tabs = require_child(self.dialog, QtWidgets.QTabWidget, "tabs_settings")
        page = require_child(tabs, QtWidgets.QWidget, f"tab_{name}")
        tabs.setCurrentWidget(page)

    def exec(self):
        try:
            return self.dialog.exec()
        finally:
            self.dispose()

    def dispose(self):
        if self._presets_panel is not None:
            self._presets_panel.dispose()
            self._presets_panel = None
        # A saved choice still applies if the dialog closes before the timer fires.
        self._apply_pending_appearance()
        for signal, slot in self._connections:
            signal.disconnect(slot)
        self._connections.clear()

    def _refresh(self):
        preferences = self.service.preferences.read()
        for key, choices in self._radios.items():
            choices[preferences[key]].setChecked(True)
        for name, path in (("project", self.service.projects.workspace_root), ("model", self.service.storage.models)):
            edit = require_child(self.dialog, QtWidgets.QLineEdit, f"edit_{name}_location")
            edit.setText(str(path))
            edit.setCursorPosition(0)
            edit.setToolTip(str(path))

    def _save(self, key, value):
        try:
            self.service.preferences.update(**{key: value})
        except (OSError, ValueError) as exc:
            self._refresh()
            show_error(self.dialog, "Settings Could Not Be Saved", str(exc))
            return
        self._show_status("")
        if key == "appearance":
            self._queue_appearance(value)

    def _queue_appearance(self, value):
        self._pending_appearance = value
        # Paint the newly selected radio in the current theme before the costly
        # application-wide palette/style update. A queued timer alone does not
        # guarantee that Qt will deliver pending paint events first.
        require_child(self.dialog, QtWidgets.QWidget, "panel_appearance").repaint()
        self._appearance_timer.start(0)

    def _apply_pending_appearance(self):
        self._appearance_timer.stop()
        value, self._pending_appearance = self._pending_appearance, None
        if value is not None:
            self._theme.set_preference(value)

    def _browse(self, kind):
        if self._storage_busy():
            self._show_status("Finish or cancel background processes before changing storage locations.")
            return
        current = self.service.projects.workspace_root if kind == "workspace" else self.service.storage.models
        title = "Choose Default Project Location" if kind == "workspace" else "Choose Local Models Location"
        selected = QtWidgets.QFileDialog.getExistingDirectory(self.dialog, title, str(current))
        if not selected:
            return
        if Path(selected).resolve() == current.resolve():
            return
        # Jobs may have started while the native folder dialog was open.
        if self._storage_busy():
            self._show_status("Finish or cancel background processes before changing storage locations.")
            return
        try:
            if kind == "workspace" and self.service.has_workspace_projects():
                choice = self._choose_project_action()
                if choice == "cancel":
                    return
                if choice == "relocate":
                    from MainWindow.controllers.storage_dialog_controller import StorageDialogController

                    controller = StorageDialogController(self.service.projects)
                    controller.exec(relocate_projects_to=Path(selected))
                    self.service.storage.workspace = self.service.projects.workspace_root
                    self._refresh()
                    return
            self.service.change_destination(kind, Path(selected))
        except (OSError, ValueError) as exc:
            show_error(self.dialog, "Storage Location Could Not Be Changed", str(exc))
            return
        self._refresh()
        self._show_status("Location saved. Existing files remain in their original folders.")
        if kind == "models":
            self._models_changed()

    def _choose_project_action(self):
        message = QtWidgets.QMessageBox(self.dialog)
        message.setWindowTitle("Change Project Location")
        message.setText("Existing projects were found in the current default folder.")
        message.setInformativeText(
            "Change the destination for new projects, or copy and verify existing projects into the new folder. "
            "Original files are preserved."
            + ("\n\nTo relocate, restart EIDORA and open Settings from the startup project chooser before opening a project."
               if self._project_open else "")
        )
        change = message.addButton("Change destination only", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        relocate = message.addButton("Relocate existing projects…", QtWidgets.QMessageBox.ButtonRole.ActionRole)
        relocate.setEnabled(not self._project_open)
        message.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        message.setDefaultButton(change)
        message.exec()
        clicked = message.clickedButton()
        return "change" if clicked is change else "relocate" if clicked is relocate else "cancel"

    def _reset(self):
        answer = QtWidgets.QMessageBox.question(
            self.dialog, "Reset General Settings",
            "Restore System appearance and Project Chooser startup?\nStorage locations and all saved work are preserved.",
            QtWidgets.QMessageBox.StandardButton.Reset | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel,
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Reset:
            return
        try:
            self.service.preferences.reset()
        except OSError as exc:
            show_error(self.dialog, "Settings Could Not Be Reset", str(exc))
            return
        self._refresh()
        self._show_status("General defaults restored. Storage locations are unchanged.")
        self._queue_appearance("system")
