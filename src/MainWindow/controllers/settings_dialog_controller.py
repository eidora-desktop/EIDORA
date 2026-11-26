"""Bind General settings; changes persist immediately through their owners."""
from PySide6 import QtCore, QtWidgets

from application_settings.service import SettingsService
from MainWindow.controllers.settings_storage_panel_controller import SettingsStoragePanelController
from shared.qt.message_dialogs import show_error
from shared.qt.theme import install_theme
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class SettingsDialogController:
    def __init__(self, projects, *, service=None, project_open=False, current_project=None,
                 storage_busy=lambda: False, models_changed=lambda: None, before_project_storage_change=lambda: None,
                 presets_changed=lambda project: None, required_preset_paths=lambda project: ()):
        self.service = service or SettingsService(projects)
        self._before_project_storage_change = before_project_storage_change
        self.project_closed_for_storage_change = False
        self._storage_status = ""
        self._general_status = ""
        self._storage_panel = None
        self._storage_mutating = self._presets_mutating = False
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
        buttons = require_child(self.dialog, QtWidgets.QDialogButtonBox, "buttons_dialog")
        reset = buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Reset)
        reset.setText("Restore Defaults…")
        reset.setAutoDefault(False)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Close).setDefault(True)
        reset.setToolTip("Restore System appearance and Project Chooser startup. Storage locations are preserved.")
        self._connect(reset.clicked, self._reset)
        self._tabs = require_child(self.dialog, QtWidgets.QTabWidget, "tabs_settings")
        # Designer's selected editing tab must not start hidden storage work.
        # Callers can choose a different initial tab through select_tab().
        self._tabs.setCurrentIndex(0)
        self._storage_panel = SettingsStoragePanelController(
            self.dialog, self.service, busy=lambda: self._storage_busy(),
            before_project_storage_change=self._prepare_project_storage_change, changed=self._storage_changed,
            status=self._show_storage_status, busy_changed=self._storage_operation_busy,
        )
        self._connect(self._tabs.currentChanged, self._tab_changed)
        self._tab_changed(self._tabs.currentIndex())

    def _tab_changed(self, index):
        name = self._tabs.widget(index).objectName()
        presets = name == "tab_presets"
        buttons = require_child(self.dialog, QtWidgets.QDialogButtonBox, "buttons_dialog")
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Reset).setVisible(name == "tab_general")
        self._status.setText(self._storage_status if name == "tab_storage" else self._general_status)
        self._status.setVisible(not presets and bool(self._status.text()))
        preset_status = require_child(self.dialog, QtWidgets.QLabel, "label_presets_status")
        preset_status.setVisible(presets and bool(preset_status.text()))
        if name == "tab_storage" and self._storage_panel is not None:
            self._storage_panel.activate()
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
        self._presets_mutating = busy
        self._update_busy()

    def _storage_operation_busy(self, busy):
        self._storage_mutating = busy
        self._update_busy()

    def _update_busy(self):
        busy = self._presets_mutating or self._storage_mutating
        self._tabs.setEnabled(not busy)
        require_child(self.dialog, QtWidgets.QDialogButtonBox, "buttons_dialog").setEnabled(not busy)

    def _show_status(self, text):
        self._general_status = text
        if self._tabs.currentWidget().objectName() == "tab_general":
            self._status.setText(text)
            self._status.setVisible(bool(text))

    def _show_storage_status(self, text):
        self._storage_status = text
        if self._tabs.currentWidget().objectName() == "tab_storage":
            self._status.setText(text)
            self._status.setVisible(bool(text))

    def _storage_changed(self, kind):
        if kind == "models":
            self._models_changed()

    def _prepare_project_storage_change(self):
        if self._presets_panel is not None:
            self._presets_panel.dispose()
            self._presets_panel = None
        wait_for_saves = self._before_project_storage_change()
        self.project_closed_for_storage_change = True
        self._project_open = False
        self._current_project = None
        return wait_for_saves

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
        if self._storage_panel is not None:
            self._storage_panel.dispose()
            self._storage_panel = None
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
        self._storage_panel.browse(kind)

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
