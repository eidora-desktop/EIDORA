"""Project selector and grouped preset cleanup; filesystem work stays off Qt."""
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from application_settings.presets_service import PresetsService
from project.domain import Project
from shared.qt.background_tasks import BackgroundTasks
from shared.qt.item_views import configure_checkable_item_view
from shared.qt.widgets import require_child


class _PresetItemDelegate(QtWidgets.QStyledItemDelegate):
    """Roomy rows and palette-aware category headers, with native checkboxes."""

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(max(size.height(), 32))
        return size

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        if not index.parent().isValid():
            option.backgroundBrush = QtGui.QBrush(option.palette.color(QtGui.QPalette.ColorRole.AlternateBase))
            if index.column() == 1:
                option.palette.setColor(QtGui.QPalette.ColorRole.Text,
                                        option.palette.color(QtGui.QPalette.ColorRole.PlaceholderText))


class PresetsPanelController(QtCore.QObject):
    def __init__(self, dialog, projects, *, current_project=None, changed=lambda project: None,
                 required_paths=lambda project: (), busy_changed=lambda busy: None):
        super().__init__(dialog)
        self.dialog = dialog
        self.service = PresetsService(projects)
        self._current_project = current_project
        self._changed = changed
        self._required_paths = required_paths
        self._busy_changed = busy_changed
        self._project = None
        self._revision = 0
        self._started = False
        self._disposed = False
        self._deleting = False
        self._connections = []
        self.combo = require_child(dialog, QtWidgets.QComboBox, "combo_preset_project")
        self.tree = require_child(dialog, QtWidgets.QTreeWidget, "tree_presets")
        configure_checkable_item_view(self.tree)
        self.tree.setItemDelegate(_PresetItemDelegate(self.tree))
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.status = require_child(dialog, QtWidgets.QLabel, "label_presets_status")
        self.selection = require_child(dialog, QtWidgets.QLabel, "label_preset_selection")
        self.delete_button = require_child(dialog, QtWidgets.QPushButton, "button_delete_presets")
        self._tasks = BackgroundTasks(dialog)
        self._connect(self._tasks.finished, self._finished)
        self._connect(self._tasks.failed, self._failed)
        self._connect(self.combo.currentIndexChanged, self._project_changed)
        self._connect(self.tree.itemChanged, self._selection_changed)
        self._connect(self.delete_button.clicked, self._delete)
        dialog.installEventFilter(self)

    def _show_status(self, text):
        self.status.setText(text)
        tabs = require_child(self.dialog, QtWidgets.QTabWidget, "tabs_settings")
        self.status.setVisible(bool(text) and tabs.currentWidget().objectName() == "tab_presets")

    def _connect(self, signal, slot):
        signal.connect(slot)
        self._connections.append((signal, slot))

    def activate(self):
        if self._started:
            return
        self._started = True
        self.combo.setEnabled(False)
        self._show_status("Loading projects…")
        service, current = self.service, self._current_project
        self._request(lambda: ("projects", service.list_projects(current)))

    def _request(self, operation):
        self._revision += 1
        self._tasks.submit(self._revision, lambda progress: operation())

    def _clear(self):
        with QtCore.QSignalBlocker(self.tree):
            self.tree.clear()
        self._selection_changed()

    def _project_changed(self, index):
        value = self.combo.itemData(index)
        if value == "browse":
            with QtCore.QSignalBlocker(self.combo):
                self.combo.setCurrentIndex(self.combo.findData(self._project) if self._project else 0)
            QtCore.QTimer.singleShot(0, self.dialog, self._browse)
            return
        self._revision += 1  # Invalidate results even when choosing the empty row.
        self._project = value if isinstance(value, Project) else None
        self._clear()
        self.combo.setToolTip(str(self._project.root) if self._project else "")
        if self._project is None:
            self._show_status("Select a project to manage its presets.")
            return
        self._show_status("Loading presets…")
        service, project = self.service, self._project
        self._request(lambda: ("groups", service.list_groups(project)))

    def _browse(self):
        if self._disposed:
            return
        directory = QtWidgets.QFileDialog.getExistingDirectory(self.dialog, "Choose an EIDORA Project")
        if not directory:
            return
        self._clear()
        self._show_status("Checking project…")
        service = self.service
        self._request(lambda: ("browse", service.inspect_project(Path(directory))))

    def _add_project(self, project):
        # Paths distinguish projects with identical names, without filling the row.
        for index in range(self.combo.count()):
            existing = self.combo.itemData(index)
            if isinstance(existing, Project) and existing.root == project.root:
                return index
        index = self.combo.count() - 1
        self.combo.insertItem(index, project.name, project)
        self.combo.setItemData(index, str(project.root), QtCore.Qt.ItemDataRole.ToolTipRole)
        return index

    def _finished(self, revision, result):
        if self._disposed or revision != self._revision or result is None:
            return
        kind, value = result
        if kind == "projects":
            with QtCore.QSignalBlocker(self.combo):
                self.combo.clear()
                self.combo.addItem("Select a project…", None)
                self.combo.addItem("Browse for project…", "browse")
                selected = 0
                for project in value:
                    index = self._add_project(project)
                    if self._current_project is not None and project.root == self._current_project.root:
                        selected = index
                self.combo.setCurrentIndex(selected)
            self.combo.setEnabled(True)
            self._project_changed(selected)
        elif kind == "browse":
            with QtCore.QSignalBlocker(self.combo):
                index = self._add_project(value)
                self.combo.setCurrentIndex(index)
            self._project_changed(index)
        elif kind == "groups":
            self._populate(value)
        elif kind == "deleted":
            removed, failures = value
            self._clear()
            self._set_deleting(False)
            if removed:
                self._changed(self._project)
            # Always reload after partial failure; never retain deleted selections.
            service, project = self.service, self._project
            self._request(lambda: ("after_delete", (service.list_groups(project), len(removed), failures)))
        elif kind == "after_delete":
            groups, count, failures = value
            self._populate(groups)
            self._show_status(f"Deleted {count} preset(s)." + (" Could not delete:\n" + "\n".join(failures) if failures else ""))

    def _populate(self, groups):
        self._clear()
        with QtCore.QSignalBlocker(self.tree):
            for title, entries in groups:
                group = QtWidgets.QTreeWidgetItem(self.tree, [title, str(len(entries))])
                group.setFlags(group.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable | QtCore.Qt.ItemFlag.ItemIsAutoTristate)
                group.setCheckState(0, QtCore.Qt.CheckState.Unchecked)
                group.setTextAlignment(1, QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                font = group.font(0)
                font.setBold(True)
                group.setFont(0, font)
                for entry in entries:
                    item = QtWidgets.QTreeWidgetItem(group, [entry.label])
                    item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(0, QtCore.Qt.CheckState.Unchecked)
                    item.setData(0, QtCore.Qt.ItemDataRole.UserRole, str(entry.path))
                    item.setToolTip(0, str(entry.path))
                group.setExpanded(True)
        self._show_status("" if groups else "No presets saved in this project.")
        self._selection_changed()

    def _selected(self):
        return [self.tree.topLevelItem(i).child(j)
                for i in range(self.tree.topLevelItemCount())
                for j in range(self.tree.topLevelItem(i).childCount())
                if self.tree.topLevelItem(i).child(j).checkState(0) == QtCore.Qt.CheckState.Checked]

    def _selection_changed(self, *args):
        count = len(self._selected())
        self.selection.setText(f"{count} selected")
        self.delete_button.setEnabled(bool(count) and not self._deleting)

    def _delete(self):
        selected = self._selected()
        if not selected or self._project is None or self._deleting:
            return
        paths = tuple(Path(item.data(0, QtCore.Qt.ItemDataRole.UserRole)) for item in selected)
        message = QtWidgets.QMessageBox(self.dialog)
        message.setWindowTitle("Delete Presets")
        message.setText(f"Delete {len(paths)} preset(s) from ‘{self._project.name}’?")
        message.setInformativeText(f"{self._project.root}\n\nThis cannot be undone. Already-applied settings are preserved.")
        message.setDetailedText("\n".join(f"{item.parent().text(0)} — {item.text(0)}" for item in selected))
        message.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel)
        message.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        message.button(QtWidgets.QMessageBox.StandardButton.Yes).setText("Delete")
        if message.exec() != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        required = {Path(path).resolve() for path in self._required_paths(self._project)}
        if any(path.resolve() in required for path in paths):
            self._show_status("No presets were deleted. Selected presets are still used by an open animation.")
            return
        self._set_deleting(True)
        self._show_status("Checking references and deleting presets…")
        service, project = self.service, self._project
        self._request(lambda: ("deleted", service.delete(project, paths)))

    def _set_deleting(self, value):
        self._deleting = value
        self.combo.setEnabled(not value)
        self.tree.setEnabled(not value)
        self._busy_changed(value)
        self._selection_changed()

    def _failed(self, revision, message):
        if self._disposed or revision != self._revision:
            return
        self._set_deleting(False)
        self._show_status(message)

    def eventFilter(self, watched, event):
        if self._deleting and (event.type() == QtCore.QEvent.Type.Close or (
                event.type() == QtCore.QEvent.Type.KeyPress and event.key() == QtCore.Qt.Key.Key_Escape)):
            event.ignore()
            return True
        return False

    def dispose(self):
        self._disposed = True
        self._tasks.shutdown()
        self.dialog.removeEventFilter(self)
        for signal, slot in self._connections:
            signal.disconnect(slot)
        self._connections.clear()
