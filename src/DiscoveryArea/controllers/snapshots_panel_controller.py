from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from DiscoveryArea.presets_repository import PresetEntry
from shared.qt.item_views import PANEL_ITEM_ROW_HEIGHT, configure_panel_item_view
from shared.qt.panel_controller import UiPanelController
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


SNAPSHOT_PREVIEW_IMAGE_SIZE = 96


class SnapshotsPanelController(UiPanelController):
    package = "DiscoveryArea"
    ui_path = "ui/snapshots_panel.ui"

    def _wire(self) -> None:
        self._list = require_child(self._widget, QtWidgets.QListWidget, "list_snapshots")
        configure_panel_item_view(self._list)
        self._add_button = require_child(self._widget, QtWidgets.QPushButton, "button_add_snapshot")
        self._options_button = require_child(self._widget, QtWidgets.QToolButton, "button_snapshots_options")
        self._size_spin = require_child(self._widget, QtWidgets.QSpinBox, "spin_snapshot_thumbnail_size")
        self._dialog: QtWidgets.QWidget | None = None
        self._entries: list[PresetEntry] = []
        self._preferred_snapshot_path: Path | None = None
        self._update_buttons: dict[str, QtWidgets.QPushButton] = {}
        self._row_icon_labels: dict[str, QtWidgets.QLabel] = {}
        self._update_enabled_path: Path | None = None
        self._has_update_changes = False
        self._save_current_callback: Callable[[str], bool] | None = None
        self._load_snapshot_callback: Callable[[Path], bool] | None = None
        self._update_snapshot_callback: Callable[[Path], bool] | None = None
        self._delete_snapshot_callback: Callable[[Path], bool] | None = None
        self._set_thumbnail_size(self._size_spin.value())
        self._size_spin.valueChanged.connect(self._set_thumbnail_size)
        self._add_button.clicked.connect(self._open_snapshot_save_dialog)
        self._list.itemActivated.connect(self._on_item_activated)
        self._list.currentRowChanged.connect(self._on_current_row_changed)
        self._list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_snapshot_context_menu)
        self._configure_options_menu()

    def set_project(self, project) -> None:
        super().set_project(project)
        enabled = project is not None
        self._add_button.setEnabled(enabled)
        self._options_button.setEnabled(enabled)
        if not enabled:
            self.set_snapshot_entries([])

    def shutdown(self) -> None:
        if self._dialog is not None:
            self._dialog.close()

    def set_snapshot_entries(self, entries: list[PresetEntry]) -> None:
        self._entries = list(entries)
        selected_path = self._preferred_snapshot_path or self.selected_snapshot_path()
        with QtCore.QSignalBlocker(self._list):
            self._list.clear()
            self._update_buttons.clear()
            self._row_icon_labels.clear()
            icon_size = self._list.iconSize()
            if self._entries:
                for entry in self._entries:
                    item = QtWidgets.QListWidgetItem()
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, str(entry.path))
                    item.setData(QtCore.Qt.ItemDataRole.UserRole + 1, entry.label)
                    item.setToolTip(self._snapshot_timestamp_tooltip(entry))
                    self._list.addItem(item)
                    self._list.setItemWidget(item, self._build_snapshot_row_widget(entry))
                    icon_label = self._row_icon_labels[str(entry.path)]
                    item.setSizeHint(
                        QtCore.QSize(
                            icon_size.width() + 8,
                            max(PANEL_ITEM_ROW_HEIGHT, icon_label.height() + 8),
                        )
                    )
                next_row = 0
                if selected_path is not None:
                    for row, entry in enumerate(self._entries):
                        if entry.path == selected_path:
                            next_row = row
                            break
                self._list.setCurrentRow(next_row)
        if selected_path is not None and any(entry.path == selected_path for entry in self._entries):
            self._preferred_snapshot_path = None
        self._refresh_menu()
        self._refresh_update_buttons()

    def prefer_snapshot_path(self, path: Path) -> None:
        """Keep this path selected through the next list refresh without loading it."""

        self._preferred_snapshot_path = Path(path)

    def set_save_current_callback(self, callback: Callable[[str], bool] | None) -> None:
        self._save_current_callback = callback

    def set_load_snapshot_callback(self, callback: Callable[[Path], bool] | None) -> None:
        self._load_snapshot_callback = callback

    def set_update_snapshot_callback(self, callback: Callable[[Path], bool] | None) -> None:
        self._update_snapshot_callback = callback

    def set_delete_snapshot_callback(self, callback: Callable[[Path], bool] | None) -> None:
        self._delete_snapshot_callback = callback

    def set_update_enabled_state(self, path: Path | None, has_changes: bool) -> None:
        self._update_enabled_path = path
        self._has_update_changes = bool(has_changes)
        self._refresh_menu()
        self._refresh_update_buttons()

    def selected_snapshot_path(self) -> Path | None:
        item = self._list.currentItem()
        if item is None:
            return None
        raw = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if not raw or raw == "empty":
            return None
        return Path(str(raw))

    def _set_thumbnail_size(self, value: int) -> None:
        size = max(16, int(value))
        self._list.setIconSize(QtCore.QSize(size, size))
        for row in range(self._list.count()):
            item = self._list.item(row)
            item.setSizeHint(QtCore.QSize(size + 8, max(PANEL_ITEM_ROW_HEIGHT, size + 8)))
            path = self._snapshot_path_from_item(item)
            label = None if path is None else self._row_icon_labels.get(str(path))
            if label is not None:
                pixmap = self._snapshot_pixmap(path, size)
                label.setFixedSize(pixmap.size())
                label.setPixmap(pixmap)
                item.setSizeHint(
                    QtCore.QSize(size + 8, max(PANEL_ITEM_ROW_HEIGHT, pixmap.height() + 8))
                )

    def _build_snapshot_row_widget(self, entry: PresetEntry) -> QtWidgets.QWidget:
        row = QtWidgets.QWidget(self._list)
        row.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        row.customContextMenuRequested.connect(
            lambda position, row=row, path=entry.path, label=entry.label: self._show_snapshot_context_menu(
                path,
                label,
                row.mapToGlobal(position),
            )
        )
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(6, 0, 6, 0)
        layout.setSpacing(6)
        icon_size = self._list.iconSize()
        icon_label = QtWidgets.QLabel(row)
        pixmap = self._snapshot_pixmap(entry.path, icon_size.width())
        icon_label.setFixedSize(pixmap.size())
        icon_label.setPixmap(pixmap)
        icon_label.setScaledContents(False)
        icon_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        label = QtWidgets.QLabel(entry.label, row)
        label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.NoTextInteraction)
        label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        button = QtWidgets.QPushButton("update", row)
        button.setObjectName(f"button_update_snapshot_{len(self._update_buttons)}")
        button.setEnabled(False)
        button.setToolTip("Update this snapshot with the current settings")
        button.clicked.connect(lambda _checked=False, path=entry.path: self._update_snapshot_from_path(path))
        layout.addWidget(icon_label)
        layout.addWidget(label)
        layout.addWidget(button)
        self._row_icon_labels[str(entry.path)] = icon_label
        self._update_buttons[str(entry.path)] = button
        return row

    def _cite_snapshot(self, path: Path | None) -> None:
        if self._project is None or path is None:
            return
        from DiscoveryArea.snapshot_provenance import SnapshotMethodsService
        from shared.qt.methods_citations_dialog import MethodsCitationsDialogController
        try:
            report = SnapshotMethodsService().load(self._project, path)
        except (OSError, ValueError) as exc:
            self._show_status(f"Methods report unavailable: {exc}")
            return
        MethodsCitationsDialogController().exec(report)

    def _snapshot_pixmap(self, snapshot_path: Path, max_edge: int) -> QtGui.QPixmap:
        png = snapshot_path.with_suffix(".png")
        if png.exists():
            pix = QtGui.QPixmap(str(png))
            if not pix.isNull():
                return pix.scaled(
                    max(1, int(max_edge)),
                    max(1, int(max_edge)),
                    QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                    QtCore.Qt.TransformationMode.SmoothTransformation,
                )
        fallback = QtGui.QPixmap(max(1, int(max_edge)), max(1, int(max_edge)))
        fallback.fill(QtGui.QColor("#d9dde3"))
        return fallback

    def _snapshot_icon(self, snapshot_path: Path, icon_size: QtCore.QSize) -> QtGui.QIcon:
        return QtGui.QIcon(self._snapshot_pixmap(snapshot_path, min(icon_size.width(), icon_size.height())))

    def _open_snapshot_save_dialog(self) -> None:
        self._dialog = load_ui(self.package, "ui/save_preset_dialog.ui", self._widget)
        self._dialog.setWindowTitle("save new snapshot")
        warning = self._dialog.findChild(QtWidgets.QLabel, "label_preset_exists_warning")
        edit = self._dialog.findChild(QtWidgets.QLineEdit, "edit_preset_name")
        button_box = self._dialog.findChild(QtWidgets.QDialogButtonBox, "button_box_preset_dialog")
        ok_button = button_box.button(QtWidgets.QDialogButtonBox.StandardButton.Ok) if button_box is not None else None
        if warning is not None:
            warning.setText("snapshot label exists already")
            warning.hide()

        def _validate() -> None:
            label = str(edit.text() if edit is not None else "").strip()
            exists = any(entry.label.lower() == label.lower() for entry in self._entries)
            if warning is not None:
                warning.setVisible(bool(label) and exists)
            if ok_button is not None:
                ok_button.setEnabled(bool(label) and not exists)

        if edit is not None:
            edit.textChanged.connect(lambda _text: _validate())
        if button_box is not None:
            button_box.accepted.connect(self._accept_snapshot_save)
            button_box.rejected.connect(self._dialog.close)
        _validate()
        self._dialog.show()

    def _accept_snapshot_save(self) -> None:
        label = ""
        if self._dialog is not None:
            edit = self._dialog.findChild(QtWidgets.QLineEdit, "edit_preset_name")
            label = str(edit.text() if edit is not None else "").strip()
            self._dialog.close()
        if not label:
            return
        if callable(self._save_current_callback):
            ok = bool(self._save_current_callback(label))
            if ok:
                self._show_status(f"Saved snapshot '{label}'.")

    def _on_item_activated(self, item: QtWidgets.QListWidgetItem) -> None:
        path = Path(str(item.data(QtCore.Qt.ItemDataRole.UserRole) or ""))
        if not path.exists():
            return
        if callable(self._load_snapshot_callback):
            self._load_snapshot_callback(path)

    def _on_current_row_changed(self, row: int) -> None:
        if row >= 0:
            self._show_status(f"Selected snapshot {row + 1}.")
            item = self._list.item(row)
            if item is not None:
                self._on_item_activated(item)
        self._refresh_menu()
        self._refresh_update_buttons()

    def _configure_options_menu(self) -> None:
        self._menu = QtWidgets.QMenu(self._options_button)
        self._load_recent_menu = self._menu.addMenu("Load Recent")
        self._action_cite = self._menu.addAction("Citations…")
        self._action_cite.triggered.connect(lambda: self._cite_snapshot(self.selected_snapshot_path()))
        self._action_save_current = self._menu.addAction("Save Snapshot…")
        self._action_save_current.triggered.connect(self._open_snapshot_save_dialog)
        self._action_update_selected = self._menu.addAction("Overwrite Snapshot")
        self._action_update_selected.triggered.connect(self._update_selected_snapshot)
        self._options_button.setMenu(self._menu)
        self._options_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self._options_button.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
        self._refresh_menu()

    def _refresh_menu(self) -> None:
        self._load_recent_menu.clear()
        recent = self._entries[:10]
        if not recent:
            action = self._load_recent_menu.addAction("No Presets Saved")
            action.setEnabled(False)
        for entry in recent:
            action = self._load_recent_menu.addAction(entry.label)
            action.triggered.connect(lambda _checked=False, path=entry.path: self._load_from_path(path))
        selected_path = self.selected_snapshot_path()
        self._action_update_selected.setEnabled(self._can_update_path(selected_path))
        self._action_cite.setEnabled(selected_path is not None)

    def _refresh_update_buttons(self) -> None:
        for raw_path, button in self._update_buttons.items():
            button.setEnabled(self._can_update_path(Path(raw_path)))

    def _can_update_path(self, path: Path | None) -> bool:
        selected_path = self.selected_snapshot_path()
        if path is None or selected_path is None:
            return False
        return (
            self._has_update_changes
            and self._update_enabled_path == path
            and selected_path == path
            and path.exists()
        )

    def _load_from_path(self, path: Path) -> None:
        if callable(self._load_snapshot_callback):
            self._load_snapshot_callback(path)

    def _update_selected_snapshot(self) -> None:
        path = self.selected_snapshot_path()
        if path is None:
            return
        self._update_snapshot_from_path(path)

    def _update_snapshot_from_path(self, path: Path) -> None:
        if callable(self._update_snapshot_callback):
            ok = bool(self._update_snapshot_callback(path))
            if ok:
                self._show_status(f"Updated snapshot '{path.stem}'.")

    def _on_snapshot_context_menu(self, position: QtCore.QPoint) -> None:
        item = self._list.itemAt(position)
        if item is None:
            return
        path = self._snapshot_path_from_item(item)
        label = str(item.data(QtCore.Qt.ItemDataRole.UserRole + 1) or item.text()).strip()
        self._show_snapshot_context_menu(path, label, self._list.viewport().mapToGlobal(position))

    def _show_snapshot_context_menu(
        self,
        path: Path | None,
        label: str,
        global_position: QtCore.QPoint,
    ) -> None:
        menu = self._snapshot_context_menu_for_path(path, label)
        if menu is not None:
            menu.exec(global_position)

    def _snapshot_context_menu(self, item: QtWidgets.QListWidgetItem) -> QtWidgets.QMenu | None:
        path = self._snapshot_path_from_item(item)
        label = str(item.data(QtCore.Qt.ItemDataRole.UserRole + 1) or item.text()).strip()
        return self._snapshot_context_menu_for_path(path, label)

    def _snapshot_context_menu_for_path(self, path: Path | None, label: str) -> QtWidgets.QMenu | None:
        if path is None or not label:
            return None
        menu = QtWidgets.QMenu(self._list)
        menu.addAction("Citations…").triggered.connect(lambda: self._cite_snapshot(path))
        menu.addSeparator()
        action = menu.addAction(f"Delete {label}")
        action.triggered.connect(lambda _checked=False, path=path, label=label: self._delete_snapshot(path, label))
        return menu

    def _delete_snapshot(self, path: Path, label: str) -> None:
        if callable(self._delete_snapshot_callback) and self._delete_snapshot_callback(path):
            self._show_status(f"Deleted snapshot '{label}'.")

    @staticmethod
    def _snapshot_path_from_item(item: QtWidgets.QListWidgetItem | None) -> Path | None:
        if item is None:
            return None
        raw = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if not raw or raw == "empty":
            return None
        return Path(str(raw))

    @staticmethod
    def _snapshot_timestamp_tooltip(entry: PresetEntry) -> str:
        timestamp = entry.created_at if entry.created_at is not None else entry.modified_at
        return f"Created: {datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')}"
