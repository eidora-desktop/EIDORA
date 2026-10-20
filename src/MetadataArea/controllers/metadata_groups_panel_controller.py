from __future__ import annotations

from collections.abc import Callable
from collections import defaultdict
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtGui, QtWidgets

from MetadataArea.controllers.metadata_groups_export_dialog_controller import (
    MetadataGroupsExportDialogController,
)
from MetadataArea.domain import MetadataGroupExportDestination
from MetadataArea.group_repository import MetadataGroupRepository
from MetadataArea.group_service import MetadataGroupService
from MetadataArea.repository import MetadataRepository
from shared.qt.message_dialogs import show_error
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child

if TYPE_CHECKING:
    from shared.qt.activity import ActivityManager


class MetadataGroupsPanelController:
    _ID_ROLE = QtCore.Qt.ItemDataRole.UserRole
    _SET_ID_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        status_callback: Callable[[str], None] | None = None,
        activity_manager: ActivityManager | None = None,
    ) -> None:
        self.widget = load_ui("MetadataArea", "ui/metadata_groups_panel.ui", parent)
        self._project = None
        self._status_callback = status_callback
        self._activity_manager = activity_manager
        self._changed_callback: Callable[[], None] | None = None
        self._metadata_changed_callback: Callable[[], None] | None = None
        self._row_height: int | None = None
        self._tree = require_child(self.widget, QtWidgets.QTreeWidget, "tree_metadata_groups")
        self._tree.header().setStretchLastSection(False)
        self._tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self._tree.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self._tree.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._open_row_context_menu)
        self._export_button = require_child(self.widget, QtWidgets.QPushButton, "button_export_groups")
        self._export_button.clicked.connect(self._export)
        self._update_actions()

    def select_group_set(self, group_set_id):
        self.refresh()
        target = None
        for parent in self._parents():
            if parent.data(0, self._SET_ID_ROLE) == group_set_id:
                target = parent
                parent.setExpanded(True)
        if target is not None:
            self._tree.setCurrentItem(target)
            self._tree.scrollToItem(target)
            self._tree.setFocus()

    def set_changed_callback(self, callback: Callable[[], None] | None) -> None:
        self._changed_callback = callback

    def set_metadata_changed_callback(self, callback: Callable[[], None] | None) -> None:
        self._metadata_changed_callback = callback

    def set_row_height(self, height: int) -> None:
        self._row_height = height
        for parent in self._parents():
            parent.setSizeHint(0, QtCore.QSize(0, height))
        for child in self._children():
            child.setSizeHint(0, QtCore.QSize(0, height))

    def set_project(self, project) -> None:
        self._project = project
        with QtCore.QSignalBlocker(self._tree):
            self._tree.clear()
        self.refresh()

    def has_groups(self) -> bool:
        return self._project is not None and bool(self._service().list_groups())

    def clear_groups(self) -> None:
        if self._project is not None:
            self._service().clear()
            self.refresh()

    def refresh(self) -> None:
        expanded = {item.data(0, self._SET_ID_ROLE) for item in self._parents() if item.isExpanded()}
        selected = self._selected()
        selected_key = self._item_key(selected) if selected is not None else None
        with QtCore.QSignalBlocker(self._tree):
            self._tree.clear()
            if self._project is not None:
                service = self._service()
                groups = service.list_groups()
                children_by_set = defaultdict(list)
                for group in groups:
                    children_by_set[group.group_set_id].append(group)
                for group_set in service.list_group_sets():
                    children = children_by_set[group_set.group_set_id]
                    count = service.group_set_member_count(group_set.group_set_id)
                    parent = QtWidgets.QTreeWidgetItem([group_set.name, f"{count:,}"])
                    parent.setFlags(
                        parent.flags() & ~QtCore.Qt.ItemFlag.ItemIsUserCheckable
                    )
                    parent.setData(0, self._SET_ID_ROLE, group_set.group_set_id)
                    parent.setToolTip(0, f"{len(children)} subgroups · {count:,} distinct items · {group_set.mode.value}")
                    font = parent.font(0)
                    font.setBold(True)
                    parent.setFont(0, font)
                    self._tree.addTopLevelItem(parent)
                    for group in children:
                        child = QtWidgets.QTreeWidgetItem(parent, [group.name, f"{group.member_count:,}"])
                        child.setFlags(
                            child.flags() & ~QtCore.Qt.ItemFlag.ItemIsUserCheckable
                        )
                        child.setData(0, self._ID_ROLE, group.group_id)
                        child.setData(0, self._SET_ID_ROLE, group_set.group_set_id)
                        child.setToolTip(0, f"{group_set.name} / {group.name} — {group.member_count:,} items")
                    parent.setExpanded(group_set.group_set_id in expanded)
                for item in (*self._parents(), *self._children()):
                    if self._item_key(item) == selected_key:
                        self._tree.setCurrentItem(item)
                    item.setTextAlignment(1, QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                if self._row_height is not None:
                    self.set_row_height(self._row_height)
        self._update_actions()

    def _parents(self):
        for index in range(self._tree.topLevelItemCount()):
            yield self._tree.topLevelItem(index)

    def _children(self):
        for parent in self._parents():
            for index in range(parent.childCount()):
                yield parent.child(index)

    def _item_key(self, item):
        return item.data(0, self._SET_ID_ROLE), item.data(0, self._ID_ROLE)

    def _rename(self) -> None:
        selected = self._selected()
        if selected is None:
            return
        group_set_id, group_id = self._item_key(selected)
        kind = "Subgroup" if group_id else "Group"
        name, accepted = QtWidgets.QInputDialog.getText(
            self.widget, f"Rename {kind}", "New name:", text=selected.text(0)
        )
        if not accepted:
            return
        try:
            service = self._service()
            if group_id:
                service.rename_group(group_id, name)
            else:
                service.rename_group_set(group_set_id, name)
        except Exception as exc:
            show_error(self.widget, f"Rename {kind} Failed", str(exc))
            return
        self._changed(f"Renamed {kind.lower()}")

    def _delete(self) -> None:
        selected = self._selected()
        if selected is None:
            return
        group_set_id, group_id = self._item_key(selected)
        kind = "Subgroup" if group_id else "Group"
        contents = "its memberships" if group_id else "all its subgroups and memberships"
        if QtWidgets.QMessageBox.question(
            self.widget,
            f"Delete {kind}",
            f"Delete '{selected.text(0)}' and {contents}? This cannot be undone.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            service = self._service()
            if group_id:
                service.delete_group(group_id)
            else:
                service.delete_group_set(group_set_id)
        except Exception as exc:
            show_error(self.widget, f"Delete {kind} Failed", str(exc))
            return
        self._changed(f"Deleted {kind.lower()}")

    def _export(self) -> None:
        if self._project is None or not self.has_groups():
            return
        result = MetadataGroupsExportDialogController(
            self.widget,
            self._service(),
            activity_manager=self._activity_manager,
        ).exec()
        if result is None:
            return
        if result.destination == MetadataGroupExportDestination.METADATA:
            if self._metadata_changed_callback is not None:
                self._metadata_changed_callback()
            if self._activity_manager is None:
                self._show_status(
                    f"Wrote {len(result.output_columns)} group column(s) to project metadata"
                )
        elif self._activity_manager is None:
            self._show_status(f"Exported groups to {result.output_path}")

    def _selected(self):
        selected = self._tree.selectedItems()
        return selected[0] if selected else None

    def _open_row_context_menu(self, position: QtCore.QPoint) -> None:
        item = self._tree.itemAt(position)
        if item is None:
            return
        self._tree.setCurrentItem(item)
        menu = QtWidgets.QMenu(self.widget)
        action_rename = menu.addAction("Rename…")
        menu.addSeparator()
        action_remove = menu.addAction("Remove…")
        selected = self._exec_row_context_menu(menu, self._tree.viewport().mapToGlobal(position))
        if selected == action_rename:
            self._rename()
        elif selected == action_remove:
            self._delete()

    @staticmethod
    def _exec_row_context_menu(
        menu: QtWidgets.QMenu,
        global_position: QtCore.QPoint,
    ) -> QtGui.QAction | None:
        return menu.exec(global_position)

    def _update_actions(self) -> None:
        self._export_button.setEnabled(self.has_groups())

    def _changed(self, message: str) -> None:
        self.refresh()
        if self._changed_callback is not None:
            self._changed_callback()
        self._show_status(message)

    def _repository(self) -> MetadataGroupRepository:
        return MetadataGroupRepository(self._project.paths.metadata_groups_database)

    def _service(self) -> MetadataGroupService:
        return MetadataGroupService(
            self._repository(),
            MetadataRepository(self._project.paths.metadata_database, self._project.paths.metadata_columns),
        )

    def _show_status(self, message: str) -> None:
        if self._status_callback is not None:
            self._status_callback(message)
