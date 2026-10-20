from __future__ import annotations

from collections import defaultdict

from PySide6 import QtCore, QtWidgets

from MetadataArea.group_service import MetadataGroupService
from shared.qt.widgets import require_child


class MetadataGroupsExportSelectionPanelController(QtCore.QObject):
    """Bind the group-selection controls embedded in the export dialog."""

    selection_changed = QtCore.Signal()

    _GROUP_ID_ROLE = QtCore.Qt.ItemDataRole.UserRole

    def __init__(
        self,
        root: QtWidgets.QWidget,
        service: MetadataGroupService,
    ) -> None:
        super().__init__(root)
        self._service = service
        self._tree = require_child(root, QtWidgets.QTreeWidget, "tree_export_groups")
        self._count_label = require_child(
            root, QtWidgets.QLabel, "label_export_group_selection_count"
        )
        self._select_all_button = require_child(
            root, QtWidgets.QPushButton, "button_select_all_export_groups"
        )
        self._clear_button = require_child(
            root, QtWidgets.QPushButton, "button_clear_export_groups"
        )
        self._tree.header().setStretchLastSection(False)
        self._tree.header().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        self._tree.header().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents
        )
        self._populate()
        self._tree.itemChanged.connect(self._on_item_changed)
        self._select_all_button.clicked.connect(self.select_all)
        self._clear_button.clicked.connect(self.clear_selection)

    @property
    def selected_group_ids(self) -> tuple[str, ...]:
        return tuple(
            str(item.data(0, self._GROUP_ID_ROLE))
            for item in self._children()
            if item.checkState(0) == QtCore.Qt.CheckState.Checked
        )

    def select_all(self, *_args) -> None:
        self._set_all(QtCore.Qt.CheckState.Checked)

    def clear_selection(self, *_args) -> None:
        self._set_all(QtCore.Qt.CheckState.Unchecked)

    def dispose(self) -> None:
        try:
            self._tree.itemChanged.disconnect(self._on_item_changed)
            self._select_all_button.clicked.disconnect(self.select_all)
            self._clear_button.clicked.disconnect(self.clear_selection)
        except (RuntimeError, TypeError):
            pass

    def _populate(self) -> None:
        groups_by_set = defaultdict(list)
        for group in self._service.list_groups():
            groups_by_set[group.group_set_id].append(group)

        with QtCore.QSignalBlocker(self._tree):
            self._tree.clear()
            for group_set in self._service.list_group_sets():
                groups = groups_by_set[group_set.group_set_id]
                parent = QtWidgets.QTreeWidgetItem([group_set.name, ""])
                parent.setFlags(
                    parent.flags() & ~QtCore.Qt.ItemFlag.ItemIsUserCheckable
                )
                font = parent.font(0)
                font.setBold(True)
                parent.setFont(0, font)
                parent.setToolTip(0, f"{len(groups)} subgroup(s)")
                if groups:
                    parent.setFlags(
                        parent.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable
                    )
                self._tree.addTopLevelItem(parent)
                for group in groups:
                    child = QtWidgets.QTreeWidgetItem(
                        parent, [group.name, f"{group.member_count:,}"]
                    )
                    child.setData(0, self._GROUP_ID_ROLE, group.group_id)
                    child.setFlags(
                        child.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable
                    )
                    child.setCheckState(0, QtCore.Qt.CheckState.Checked)
                    child.setToolTip(
                        0,
                        f"{group_set.name} / {group.name} — "
                        f"{group.member_count:,} items",
                    )
                    child.setTextAlignment(
                        1,
                        QtCore.Qt.AlignmentFlag.AlignRight
                        | QtCore.Qt.AlignmentFlag.AlignVCenter,
                    )
                if groups:
                    parent.setCheckState(0, QtCore.Qt.CheckState.Checked)
                parent.setExpanded(True)
        self._update_count()

    def _parents(self):
        for index in range(self._tree.topLevelItemCount()):
            yield self._tree.topLevelItem(index)

    def _children(self):
        for parent in self._parents():
            for index in range(parent.childCount()):
                yield parent.child(index)

    def _on_item_changed(self, item: QtWidgets.QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        with QtCore.QSignalBlocker(self._tree):
            if item.parent() is None:
                state = item.checkState(0)
                if state == QtCore.Qt.CheckState.PartiallyChecked:
                    state = QtCore.Qt.CheckState.Checked
                for index in range(item.childCount()):
                    item.child(index).setCheckState(0, state)
                self._update_parent(item)
            else:
                self._update_parent(item.parent())
        self._selection_did_change()

    def _set_all(self, state: QtCore.Qt.CheckState) -> None:
        with QtCore.QSignalBlocker(self._tree):
            for child in self._children():
                child.setCheckState(0, state)
            for parent in self._parents():
                self._update_parent(parent)
        self._selection_did_change()

    @staticmethod
    def _update_parent(parent: QtWidgets.QTreeWidgetItem) -> None:
        if not parent.childCount():
            return
        states = {
            parent.child(index).checkState(0)
            for index in range(parent.childCount())
        }
        state = (
            next(iter(states))
            if len(states) == 1
            else QtCore.Qt.CheckState.PartiallyChecked
        )
        parent.setCheckState(0, state)

    def _selection_did_change(self) -> None:
        self._update_count()
        self.selection_changed.emit()

    def _update_count(self) -> None:
        selected = len(self.selected_group_ids)
        total = sum(1 for _item in self._children())
        self._count_label.setText(f"{selected} of {total} groups selected")
