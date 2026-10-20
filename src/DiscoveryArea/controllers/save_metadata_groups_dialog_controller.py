from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from MetadataArea.domain import MetadataGroupSetMode
from MetadataArea.group_service import MetadataGroupService
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class SaveMetadataGroupsDialogController:
    """Collect a target set/group without owning group persistence."""

    def exec(
        self,
        service: MetadataGroupService,
        *,
        suggested_group_name: str = "",
        area_labels: list[tuple[str, str]] | None = None,
        overlap_count: int = 0,
    ) -> dict[str, object] | None:
        dialog = load_ui("DiscoveryArea", "ui/save_metadata_groups_dialog.ui", None)
        if not isinstance(dialog, QtWidgets.QDialog):
            return None
        dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        combo_set = require_child(dialog, QtWidgets.QComboBox, "combo_group_set")
        edit_set = require_child(dialog, QtWidgets.QLineEdit, "edit_new_group_set")
        combo_mode = require_child(dialog, QtWidgets.QComboBox, "combo_group_set_mode")
        combo_group = require_child(dialog, QtWidgets.QComboBox, "combo_group")
        edit_group = require_child(dialog, QtWidgets.QLineEdit, "edit_new_group")
        combo_action = require_child(dialog, QtWidgets.QComboBox, "combo_membership_action")
        priority_panel = require_child(dialog, QtWidgets.QWidget, "panel_group_priority")
        priority_list = require_child(dialog, QtWidgets.QListWidget, "list_group_priority")
        summary = require_child(dialog, QtWidgets.QLabel, "label_save_groups_summary")
        button_box = require_child(dialog, QtWidgets.QDialogButtonBox, "button_box_save_groups")
        areas = list(area_labels or [])
        sets = service.list_group_sets()
        combo_set.addItem("New group set…", None)
        for group_set in sets:
            combo_set.addItem(group_set.name, group_set.group_set_id)
            combo_set.setItemData(combo_set.count() - 1, group_set.mode.value, QtCore.Qt.ItemDataRole.UserRole + 1)
        edit_set.setText("Areas" if areas else "Nearest Neighbors")
        edit_group.setText(str(suggested_group_name))
        for area_id, label in areas:
            item = QtWidgets.QListWidgetItem(label)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, area_id)
            priority_list.addItem(item)
        is_batch = bool(areas)
        for widget in (
            require_child(dialog, QtWidgets.QLabel, "label_group"),
            combo_group,
            require_child(dialog, QtWidgets.QLabel, "label_new_group"),
            edit_group,
            require_child(dialog, QtWidgets.QLabel, "label_membership_action"),
            combo_action,
        ):
            widget.setVisible(not is_batch)

        def selected_mode() -> str:
            if combo_set.currentData() is None:
                return str(combo_mode.currentText()).lower()
            return str(combo_set.currentData(QtCore.Qt.ItemDataRole.UserRole + 1) or "overlapping")

        def update() -> None:
            new_set = combo_set.currentData() is None
            edit_set.setVisible(new_set)
            combo_mode.setVisible(new_set)
            require_child(dialog, QtWidgets.QLabel, "label_new_group_set").setVisible(new_set)
            require_child(dialog, QtWidgets.QLabel, "label_group_set_mode").setVisible(new_set)
            if not is_batch:
                current_group = combo_group.currentData()
                combo_group.blockSignals(True)
                combo_group.clear()
                combo_group.addItem("New group…", None)
                if not new_set:
                    for group in service.list_groups(str(combo_set.currentData())):
                        combo_group.addItem(group.name, group.group_id)
                if current_group is not None:
                    index = combo_group.findData(current_group)
                    if index >= 0:
                        combo_group.setCurrentIndex(index)
                combo_group.blockSignals(False)
                edit_group.setVisible(combo_group.currentData() is None)
                require_child(dialog, QtWidgets.QLabel, "label_new_group").setVisible(
                    combo_group.currentData() is None
                )
                combo_action.setEnabled(combo_group.currentData() is not None)
            show_priority = is_batch and selected_mode() == MetadataGroupSetMode.EXCLUSIVE.value and overlap_count > 0
            priority_panel.setVisible(show_priority)
            summary.setText(
                f"{len(areas)} groups; {overlap_count} overlapping items."
                if is_batch else "The selected items are stored as a snapshot."
            )

        def move(delta: int) -> None:
            row = priority_list.currentRow()
            target = row + delta
            if row < 0 or target < 0 or target >= priority_list.count():
                return
            item = priority_list.takeItem(row)
            priority_list.insertItem(target, item)
            priority_list.setCurrentRow(target)

        accepted: dict[str, object] = {}

        def accept() -> None:
            if combo_set.currentData() is None and not edit_set.text().strip():
                summary.setText("Choose a new group set name.")
                return
            if not is_batch and combo_group.currentData() is None and not edit_group.text().strip():
                summary.setText("Choose a new group name.")
                return
            accepted.update({
                "group_set_id": combo_set.currentData(),
                "new_group_set_name": edit_set.text().strip(),
                "mode": selected_mode(),
                "group_id": None if is_batch else combo_group.currentData(),
                "new_group_name": edit_group.text().strip(),
                "operation": str(combo_action.currentText()).lower(),
                "priority_area_ids": [
                    str(priority_list.item(index).data(QtCore.Qt.ItemDataRole.UserRole))
                    for index in range(priority_list.count())
                ],
            })
            dialog.accept()

        combo_set.currentIndexChanged.connect(lambda _index: update())
        combo_group.currentIndexChanged.connect(lambda _index: update())
        combo_mode.currentIndexChanged.connect(lambda _index: update())
        require_child(dialog, QtWidgets.QPushButton, "button_group_priority_up").clicked.connect(lambda: move(-1))
        require_child(dialog, QtWidgets.QPushButton, "button_group_priority_down").clicked.connect(lambda: move(1))
        button_box.accepted.connect(accept)
        button_box.rejected.connect(dialog.reject)
        update()
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return None
        return dict(accepted)
