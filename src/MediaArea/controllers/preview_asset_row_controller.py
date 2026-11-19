from __future__ import annotations

from datetime import datetime

from PySide6 import QtCore, QtWidgets

from DiscoveryArea.thumbnails.repository import DEFAULT_THUMBNAIL_SIZES
from MediaArea.domain import THUMBNAIL_RESIZE_MODES, THUMBNAIL_VIDEO_FRAME_MODES, ProcessRecord, ProcessState
from shared.qt.icons import apply_icon_color
from shared.qt.ui_loader import load_ui
from shared.qt.panel_rows import PanelRowHeader, schedule_row_layout, section_widget
from shared.qt.widgets import require_child


_STATE_LABELS = {
    ProcessState.QUEUED: "Queued",
    ProcessState.RUNNING: "Running",
    ProcessState.PAUSING: "Pausing…",
    ProcessState.PAUSED: "Paused",
    ProcessState.CANCELLING: "Cancelling",
    ProcessState.COMPLETED: "Done",
    ProcessState.COMPLETED_WITH_WARNINGS: "Done with warnings",
    ProcessState.FAILED: "Failed",
    ProcessState.CANCELLED: "Canceled",
    ProcessState.INTERRUPTED: "Interrupted",
}

_DETAIL_LABELS = {
    "source": "Source", "type": "Preview type", "resize": "Image sizing",
    "frame": "Frame selection", "sizes": "Output sizes", "created": "Created",
    "results": "Results", "message": "Status", "errors": "Errors",
}


class PreviewAssetRowController(QtCore.QObject):
    """Bind the Designer card, including its inline details table."""

    def __init__(self, tree: QtWidgets.QTreeWidget, record: ProcessRecord) -> None:
        self.widget = load_ui("MediaArea", "ui/preview_asset_row.ui", tree)
        super().__init__(self.widget)
        self.record = record
        self.item = QtWidgets.QTreeWidgetItem([""])
        self.item.setFlags(self.item.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable)
        self.item.setData(0, QtCore.Qt.ItemDataRole.UserRole, record.id)
        # Keep expansion state on the item; the focusable disclosure button
        # owns keyboard interaction without selecting a row.
        self.item.setChildIndicatorPolicy(QtWidgets.QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        tree.addTopLevelItem(self.item)
        tree.setItemWidget(self.item, 0, self.widget)
        self.toggle = require_child(self.widget, QtWidgets.QToolButton, "button_toggle_details")
        self.toggle.toggled.connect(self.item.setExpanded)
        self._name = require_child(self.widget, QtWidgets.QLabel, "label_preview_name")
        self.status_label = require_child(self.widget, QtWidgets.QLabel, "label_preview_status")
        self.progress = require_child(self.widget, QtWidgets.QProgressBar, "progress_run")
        self._progress_panel = section_widget(self.widget, "layout_progress")
        self.cancel = require_child(self.widget, QtWidgets.QPushButton, "button_cancel_run")
        self.pause = require_child(self.widget, QtWidgets.QPushButton, "button_pause_run")
        self.options = require_child(self.widget, QtWidgets.QPushButton, "button_run_options")
        self.details = require_child(self.widget, QtWidgets.QTableWidget, "table_details")
        self.details.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.details.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.details.setRowCount(len(_DETAIL_LABELS))
        self.detail_values = {}
        for row, (key, title) in enumerate(_DETAIL_LABELS.items()):
            self.details.setItem(row, 0, QtWidgets.QTableWidgetItem(title))
            value = QtWidgets.QTableWidgetItem()
            self.details.setItem(row, 1, value)
            self.detail_values[key] = value
        self.header = PanelRowHeader(self.widget)
        self.summary = require_child(self.widget, QtWidgets.QLabel, "label_row_summary")
        self._body = section_widget(self.widget, "layout_row_details")
        self.details.hide()
        self._sizing = False
        self._texts = {}
        self._name.installEventFilter(self)
        self.widget.installEventFilter(self)
        self.details.viewport().installEventFilter(self)
        self.header.resized.connect(self._update_row_height)
        self.sync_expansion()

    def sync_expansion(self) -> None:
        expanded = self.item.isExpanded()
        with QtCore.QSignalBlocker(self.toggle):
            self.toggle.setChecked(expanded)
        self.toggle.setProperty("iconName", "down" if expanded else "next")
        text = "Hide preview details" if expanded else "Show preview details"
        self.toggle.setToolTip(text)
        self.toggle.setAccessibleName(text)
        self._body.setVisible(expanded)
        self.details.setVisible(expanded)
        apply_icon_color(self.widget)
        self._update_row_height()

    def update(self, record: ProcessRecord, *, cancellable: bool) -> None:
        self.record = record
        self.item.setData(0, QtCore.Qt.ItemDataRole.AccessibleTextRole, record.output_name)
        self._set_elided(self._name, record.output_name)
        active = not record.state.terminal
        self.options.setVisible(not active or record.state == ProcessState.PAUSED)
        show_progress = active
        self._progress_panel.setVisible(show_progress)
        self.summary.setVisible(not show_progress)
        sizes = record.parameters.get("sizes") or DEFAULT_THUMBNAIL_SIZES
        self.header.set_text(self.summary, f"{record.input.label} · {', '.join(map(str, sizes))} px")
        self.cancel.setVisible(active and cancellable)
        self.cancel.setEnabled(record.state != ProcessState.CANCELLING)
        self.pause.setVisible(active and cancellable)
        self.pause.setText("Resume" if record.state == ProcessState.PAUSED else "Pause")
        self.pause.setEnabled(record.state == ProcessState.PAUSED or (
            record.state == ProcessState.RUNNING and record.progress < 95
        ))
        self.pause.setToolTip("Resume creating previews" if record.state == ProcessState.PAUSED
                             else "Pause after active files finish")
        self.progress.setToolTip(record.message or _STATE_LABELS[record.state])
        self.status_label.setText(self._summary(record))
        self.status_label.setToolTip(record.message or _STATE_LABELS[record.state])
        if active:
            self.progress.setRange(0, 0 if record.state == ProcessState.RUNNING and record.progress == 0 else 100)
            self.progress.setValue(record.progress)
            self.progress.setFormat("%p%" if record.state == ProcessState.RUNNING and record.progress
                                    else _STATE_LABELS[record.state])

        details = self._details(record)
        for key, item in self.detail_values.items():
            value = details.get(key, "")
            item.setText(value)
            item.setToolTip(value)
            self.details.setRowHidden(item.row(), not bool(value))
        self._update_row_height()

    @staticmethod
    def _summary(record: ProcessRecord) -> str:
        if record.state in {ProcessState.COMPLETED, ProcessState.COMPLETED_WITH_WARNINGS}:
            count = record.generated_files
            summary = f"{count:,} {'item' if count == 1 else 'items'}"
            if record.state == ProcessState.COMPLETED_WITH_WARNINGS:
                skipped = record.failed_files + record.missing_files
                summary += f" · {skipped:,} skipped" if skipped else " · warnings"
            return summary
        if record.state == ProcessState.RUNNING:
            return f"Creating {record.progress}%" if record.progress else "Preparing…"
        return _STATE_LABELS[record.state]

    @staticmethod
    def _details(record: ProcessRecord) -> dict[str, str]:
        parameters = record.parameters
        media_type = record.input.media_type
        resize = str(parameters.get("resize_mode") or "original_aspect")
        sizes = parameters.get("sizes") or DEFAULT_THUMBNAIL_SIZES
        created = record.created_at
        if created:
            try:
                created = datetime.fromisoformat(created).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                pass
        details = {
            "source": record.input.label,
            "type": {"image": "Image thumbnails", "video": "Video frames", "audio": "Audio clips", "text": "Text labels"}.get(media_type, media_type),
            "resize": dict((value, label) for label, value in THUMBNAIL_RESIZE_MODES).get(resize, resize),
            "sizes": ", ".join(str(size) for size in sizes) + " px",
            "created": created,
        }
        if media_type == "video":
            frame = str(parameters.get("video_frame_mode") or "middle")
            details["frame"] = dict((value, label) for label, value in THUMBNAIL_VIDEO_FRAME_MODES).get(frame, frame)
        if record.state.terminal:
            results = [f"{record.generated_files:,} created"]
            if record.failed_files:
                results.append(f"{record.failed_files:,} failed")
            if record.missing_files:
                results.append(f"{record.missing_files:,} missing")
            details["results"] = " · ".join(results)
        if record.state != ProcessState.COMPLETED:
            details["message"] = record.message or _STATE_LABELS[record.state]
        if record.errors:
            details["errors"] = "\n".join(record.errors)
            # Failure messages often repeat the first error verbatim.
            if details.get("message") in record.errors:
                details.pop("message", None)
        return details

    def _set_elided(self, label: QtWidgets.QLabel, text: str) -> None:
        self._texts[label] = text
        label.setToolTip(text)
        self._elide(label)

    def _elide(self, label: QtWidgets.QLabel) -> None:
        label.setText(label.fontMetrics().elidedText(
            self._texts[label], QtCore.Qt.TextElideMode.ElideRight,
            max(0, label.contentsRect().width() - max(0, label.indent())),
        ))

    def _update_row_height(self) -> None:
        if self._sizing:
            return
        self._sizing = True
        try:
            self.widget.layout().activate()
            if not self.details.isHidden():
                self.details.resizeRowsToContents()
                height = sum(self.details.rowHeight(row) for row in range(self.details.rowCount()))
                self.details.setFixedHeight(height + 2 * self.details.frameWidth())
            height = self.header.collapsed_height
            if not self._body.isHidden():
                height += self._body.sizeHint().height()
            self.widget.setMinimumHeight(height)
            self.widget.resize(self.widget.width(), height)
            if self.item.sizeHint(0).height() != height:
                self.item.setSizeHint(0, QtCore.QSize(0, height))
                schedule_row_layout(self.item.treeWidget())
        finally:
            self._sizing = False

    def eventFilter(self, watched, event):
        if watched is self.widget and event.type() == QtCore.QEvent.Type.Show:
            # Hidden cards can leave the tree caching their initial, taller
            # size hint. Relayout after their visible layouts have settled.
            schedule_row_layout(self.item.treeWidget())
        if event.type() in {QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.FontChange} and watched in self._texts:
            self._elide(watched)
        if event.type() in {QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.LayoutRequest}:
            if watched is self.widget or watched is self.details.viewport():
                self._update_row_height()
        return super().eventFilter(watched, event)
