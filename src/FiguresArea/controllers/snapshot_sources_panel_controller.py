from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from DiscoveryArea.presets_repository import PresetEntry, PresetRepository
from shared.qt.item_views import PANEL_ITEM_ROW_HEIGHT, configure_panel_item_view
from shared.qt.panel_controller import UiPanelController
from shared.qt.widgets import require_child


class _SnapshotDragListWidget(QtWidgets.QListWidget):
    def mimeData(self, items: list[QtWidgets.QListWidgetItem]) -> QtCore.QMimeData:
        mime = super().mimeData(items)
        for item in items:
            raw = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if not raw or str(raw) == "empty":
                continue
            path = Path(str(raw))
            if path.suffix.lower() != ".json":
                continue
            mime.setUrls([QtCore.QUrl.fromLocalFile(str(path))])
            mime.setText(str(path))
            break
        return mime


class SnapshotSourcesPanelController(UiPanelController):
    package = "FiguresArea"
    ui_path = "ui/snapshot_sources_panel.ui"

    def _wire(self) -> None:
        self._list = self._replace_designer_snapshot_list(
            require_child(self._widget, QtWidgets.QListWidget, "list_snapshots")
        )
        configure_panel_item_view(self._list)
        self._options_button = require_child(self._widget, QtWidgets.QToolButton, "button_snapshot_sources_options")
        self._size_spin = require_child(self._widget, QtWidgets.QSpinBox, "spin_snapshot_thumbnail_size")
        self._entries: list[PresetEntry] = []
        self._snapshot_dir: Path | None = None
        self._watcher = QtCore.QFileSystemWatcher(self._widget)
        self._refresh_timer = QtCore.QTimer(self._widget)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(0)
        self._refresh_timer.timeout.connect(
            lambda: self.refresh_snapshots(show_activity=False)
        )
        self._watcher.directoryChanged.connect(lambda _path: self._schedule_snapshot_refresh())
        self._watcher.fileChanged.connect(lambda _path: self._schedule_snapshot_refresh())
        self._set_thumbnail_size(self._size_spin.value())
        self._size_spin.valueChanged.connect(self._set_thumbnail_size)
        self._configure_options_menu()
        self._configure_drag_list()

    def _replace_designer_snapshot_list(self, existing: QtWidgets.QListWidget) -> _SnapshotDragListWidget:
        if isinstance(existing, _SnapshotDragListWidget):
            return existing
        replacement = _SnapshotDragListWidget(existing.parentWidget())
        replacement.setObjectName(existing.objectName())
        replacement.setSizePolicy(existing.sizePolicy())
        replacement.setMinimumSize(existing.minimumSize())
        replacement.setMaximumSize(existing.maximumSize())
        replacement.setAlternatingRowColors(existing.alternatingRowColors())
        layout = existing.parentWidget().layout() if existing.parentWidget() is not None else None
        if layout is not None:
            index = layout.indexOf(existing)
            layout.removeWidget(existing)
            layout.insertWidget(max(index, 0), replacement)
        existing.setObjectName(f"{existing.objectName()}_designer_replaced")
        existing.deleteLater()
        return replacement

    def set_project(self, project) -> None:
        super().set_project(project)
        self._snapshot_dir = project.paths.snapshot_presets if project is not None else None
        self._watch_snapshot_dir()
        self._options_button.setEnabled(project is not None)
        self._size_spin.setEnabled(project is not None)
        self.refresh_snapshots()

    def shutdown(self) -> None:
        self._clear_watched_paths()

    def refresh_snapshots(self, *, show_activity: bool = True) -> None:
        if show_activity:
            with self._busy("Refreshing snapshots…"):
                self.refresh_snapshots(show_activity=False)
            return
        if self._project is None or self._snapshot_dir is None:
            self.set_snapshot_entries([])
            return
        self.set_snapshot_entries(PresetRepository(self._snapshot_dir).list_entries())
        self._watch_snapshot_dir()

    def set_snapshot_entries(self, entries: list[PresetEntry]) -> None:
        self._entries = list(entries)
        self._list.clear()
        icon_size = self._list.iconSize()
        if not self._entries:
            return
        for entry in self._entries:
            pixmap = self._snapshot_pixmap(entry.path, icon_size.width())
            item = QtWidgets.QListWidgetItem()
            item.setData(QtCore.Qt.ItemDataRole.UserRole, str(entry.path))
            item.setData(QtCore.Qt.ItemDataRole.UserRole + 1, entry.label)
            item.setData(QtCore.Qt.ItemDataRole.AccessibleTextRole, entry.label)
            item.setToolTip(self._snapshot_timestamp_tooltip(entry))
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsDragEnabled)
            item.setSizeHint(
                QtCore.QSize(
                    icon_size.width() + 8,
                    max(PANEL_ITEM_ROW_HEIGHT, pixmap.height() + 8),
                )
            )
            self._list.addItem(item)
            self._list.setItemWidget(item, self._build_snapshot_row_widget(entry, pixmap))
        self._list.setCurrentRow(0)

    def _build_snapshot_row_widget(
        self,
        entry: PresetEntry,
        pixmap: QtGui.QPixmap,
    ) -> QtWidgets.QWidget:
        row = QtWidgets.QWidget(self._list)
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(6, 0, 6, 0)
        layout.setSpacing(6)

        icon_label = QtWidgets.QLabel(row)
        icon_label.setObjectName("label_snapshot_source_preview")
        icon_label.setFixedSize(pixmap.size())
        icon_label.setPixmap(pixmap)
        icon_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        text_label = QtWidgets.QLabel(entry.label, row)
        text_label.setObjectName("label_snapshot_source_name")
        text_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.NoTextInteraction)
        text_label.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )

        layout.addWidget(icon_label)
        layout.addWidget(text_label)
        button = QtWidgets.QPushButton("cite", row)
        button.setObjectName("button_cite_snapshot")
        button.setToolTip("Methods, parameters, citations and caption text")
        button.clicked.connect(lambda: self._cite_snapshot(entry.path))
        layout.addWidget(button)
        return row

    def _cite_selected_snapshot(self) -> None:
        item = self._list.currentItem()
        if item is not None:
            self._cite_snapshot(Path(str(item.data(QtCore.Qt.ItemDataRole.UserRole))))

    def _configure_drag_list(self) -> None:
        self._list.setDragEnabled(True)
        self._list.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.DragOnly)
        self._list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)

    def _configure_options_menu(self) -> None:
        menu = QtWidgets.QMenu(self._options_button)
        cite = menu.addAction("Citations…")
        cite.triggered.connect(self._cite_selected_snapshot)
        menu.aboutToShow.connect(lambda: cite.setEnabled(self._list.currentItem() is not None))
        action_refresh = menu.addAction("Refresh")
        action_refresh.triggered.connect(self.refresh_snapshots)
        self._options_button.setMenu(menu)
        self._options_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self._options_button.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")

    def _set_thumbnail_size(self, value: int) -> None:
        size = max(16, int(value))
        self._list.setIconSize(QtCore.QSize(size, size))
        for row in range(self._list.count()):
            item = self._list.item(row)
            raw_path = item.data(QtCore.Qt.ItemDataRole.UserRole)
            pixmap = self._snapshot_pixmap(Path(str(raw_path)), size)
            row_widget = self._list.itemWidget(item)
            icon_label = (
                row_widget.findChild(QtWidgets.QLabel, "label_snapshot_source_preview")
                if row_widget is not None
                else None
            )
            if icon_label is not None:
                icon_label.setFixedSize(pixmap.size())
                icon_label.setPixmap(pixmap)
            item.setSizeHint(
                QtCore.QSize(size + 8, max(PANEL_ITEM_ROW_HEIGHT, pixmap.height() + 8))
            )

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

    def _watch_snapshot_dir(self) -> None:
        self._clear_watched_paths()
        if self._snapshot_dir is None:
            return
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._watcher.addPath(str(self._snapshot_dir))
        for path in self._snapshot_dir.glob("*.json"):
            self._watcher.addPath(str(path))
        for path in self._snapshot_dir.glob("*.png"):
            self._watcher.addPath(str(path))

    def _clear_watched_paths(self) -> None:
        paths = self._watcher.files() + self._watcher.directories()
        if paths:
            self._watcher.removePaths(paths)

    def _schedule_snapshot_refresh(self) -> None:
        self._refresh_timer.start()

    @staticmethod
    def _snapshot_timestamp_tooltip(entry: PresetEntry) -> str:
        timestamp = entry.created_at if entry.created_at is not None else entry.modified_at
        return f"Created: {datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')}"
