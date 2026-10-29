from __future__ import annotations

import datetime as dt
import html
import sqlite3
import threading
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid

from EmbeddingsArea.domain import EmbeddingRunState
from EmbeddingsArea.repository import EmbeddingRunRepository
from DiscoveryArea.controllers.area_export_dialog_controller import (
    AreaExportDialogController,
)
from DiscoveryArea.controllers.save_metadata_groups_dialog_controller import SaveMetadataGroupsDialogController
from DiscoveryArea.controllers.layers_setting_panel_controller import LayersSettingPanelController
from DiscoveryArea.controllers.appearance_panel_controller import AppearancePanelController
from DiscoveryArea.controllers.canvas_layers_panel_controller import CanvasLayersPanelController
from DiscoveryArea.controllers.canvas_tools_panel_controller import CanvasToolsPanelController
from DiscoveryArea.controllers.selection_actions_dialog_controller import (
    SelectionActionsDialogData,
    SelectionActionsDialogController,
)
from DiscoveryArea.controllers.nearest_neighbor_coordinator import (
    NearestNeighborCoordinator,
)
from DiscoveryArea.controllers.filters_panel_controller import FiltersPanelController
from DiscoveryArea.controllers.discovery_panel_controller import DiscoveryPanelController
from DiscoveryArea.controllers.transform_panel_controller import TransformPanelController
from DiscoveryArea.controllers.transform_coordinator import TransformCoordinator
from DiscoveryArea.controllers.snapshot_write_coordinator import (
    run_snapshot_write_responsive,
)
from DiscoveryArea.controllers.slicer_panel_controller import SlicerPanelController
from DiscoveryArea.controllers.snapshots_panel_controller import SNAPSHOT_PREVIEW_IMAGE_SIZE, SnapshotsPanelController
from DiscoveryArea.controllers.thumbnail_plan_coordinator import (
    ThumbnailPlanCoordinator,
)
from DiscoveryArea.canvas.thumbnail_planner import (
    MAXIMUM_THUMBNAIL_LIMIT,
    MINIMUM_THUMBNAIL_LIMIT,
    ThumbnailPlanRequest,
    ThumbnailViewportPlan,
    expand_thumbnail_viewport,
)
from DiscoveryArea.canvas.style_preparation import (
    AreaColorOverlay,
    CanvasStyleRequest,
    CanvasStyleResult,
    StyleBuildCancelled,
    build_canvas_style,
    color_style_metadata_columns,
    custom_color_labels,
    mapped_colors,
    required_metadata_columns,
)
from DiscoveryArea.metadata_access import DiscoveryMetadataAccess
from DiscoveryArea.settings_repository import DiscoverySettingsRepository
from DiscoveryArea.nearest_neighbors import NeighborSearchRequest, NeighborSearchResult
from DiscoveryArea.thumbnail_config import (
    THUMBNAIL_CONFIG,
    automatic_thumbnail_limit_for_size,
    automatic_thumbnail_runtime_policy,
)
from DiscoveryArea.canvas.widget import DiscoveryCanvasWidget
from DiscoveryArea.canvas.export import SUPPORTED_CANVAS_EXPORT_FORMATS, save_canvas_pixmap
from DiscoveryArea.domain import PolygonArea, DiscoveryDataset, DiscoveryOption
from DiscoveryArea.filters import (
    FilterMatching,
    build_mask,
    required_filter_columns,
)
from DiscoveryArea.palettes import next_unused_area_color
from DiscoveryArea.presets_repository import PresetRepository
from DiscoveryArea.discovery_execution import DiscoveryRunBridge, DiscoveryRunWorker
from DiscoveryArea.service import DiscoveryService
from DiscoveryArea.transform_service import DiscoveryTransformService
from DiscoveryArea.transforms import (
    TransformResult,
    TransformSettings,
    source_fingerprint,
)
from DiscoveryArea.snapshots import (
    DiscoverySnapshot,
    DiscoverySnapshotProjection,
    DiscoverySnapshotRefs,
    metadata_signature_for_ids,
    relative_path_or_none,
)
from DiscoveryArea.thumbnails.resolver import ThumbnailResolver
from DiscoveryArea.thumbnails.service import (
    DecodedThumbnail,
    DecodeRequest,
    ThumbnailDecodeFailure,
    ThumbnailService,
    ThumbnailSourceBatch,
)
from shared.qt.ui_loader import load_ui
from shared.qt.deferred import defer_to_event_loop
from shared.qt.widgets import add_widget_to_container, require_child
from shared.persistence import json_store


@dataclass
class _QueuedDiscoveryRun:
    run_id: str
    project: "Project"
    payload: dict[str, object]
    metadata_ids: np.ndarray
    artifact: Path | None
    selection_before_run: str

if TYPE_CHECKING:
    from project.domain import Project
    from shared.qt.activity import ActivityManager
    from shared.qt.panel_controller import UiPanelController


class _CanvasSpaceKeyFilter(QtCore.QObject):
    def __init__(self, owner: "DiscoveryAreaController") -> None:
        super().__init__(owner.widget)
        self._owner = owner

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        return self._owner._handle_space_key_event(obj, event)


class _DiscoveryVisibilityFilter(QtCore.QObject):
    def __init__(self, owner: "DiscoveryAreaController") -> None:
        super().__init__(owner.widget)
        self._owner = owner

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() == QtCore.QEvent.Type.Hide:
            self._owner.deactivate()
        return False


class _ThumbnailReadyBridge(QtCore.QObject):
    ready = QtCore.Signal(object)


class _CanvasStyleBuildSignals(QtCore.QObject):
    finished = QtCore.Signal(int, object)
    failed = QtCore.Signal(int, str)
    cancelled = QtCore.Signal(int)


class _CanvasStyleBuildTask(QtCore.QRunnable):
    def __init__(
        self,
        request: CanvasStyleRequest,
        cancelled: threading.Event,
        metadata_access: DiscoveryMetadataAccess | None,
    ) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.request = request
        self.cancelled = cancelled
        self.metadata_access = metadata_access
        self.signals = _CanvasStyleBuildSignals()

    @QtCore.Slot()
    def run(self) -> None:
        try:
            prepared = _canvas_style_request_with_metadata(
                self.request,
                self.metadata_access,
            )
            result = build_canvas_style(prepared, cancelled=self.cancelled.is_set)
        except StyleBuildCancelled:
            self.signals.cancelled.emit(self.request.revision)
            return
        except Exception as exc:
            self.signals.failed.emit(self.request.revision, f"{type(exc).__name__}: {exc}")
            return
        if self.cancelled.is_set():
            self.signals.cancelled.emit(self.request.revision)
        else:
            self.signals.finished.emit(self.request.revision, result)


def _canvas_style_request_with_metadata(
    request: CanvasStyleRequest,
    metadata_access: DiscoveryMetadataAccess | None,
) -> CanvasStyleRequest:
    if request.metadata_rows or metadata_access is None:
        return request
    columns, numeric_columns = required_metadata_columns(request)
    if not columns:
        return request
    text_values, numeric_values = metadata_access.values_for_ids(
        request.metadata_ids,
        columns=columns,
        numeric_columns=numeric_columns,
    )
    return replace(
        request,
        metadata_values=text_values,
        metadata_numeric_values=numeric_values,
    )


class _CanvasTooltipPopup(QtWidgets.QFrame):
    _MAX_TEXT_WIDTH = 360
    _MAX_VALUE_CHARACTERS = 300

    def __init__(self, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent, QtCore.Qt.WindowType.ToolTip)
        content = load_ui("DiscoveryArea", "ui/canvas_tooltip.ui", None)
        self._content = content
        content.setParent(self)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(content)
        self._thumbnail_label = require_child(content, QtWidgets.QLabel, "label_tooltip_thumbnail")
        self._text_label = require_child(content, QtWidgets.QLabel, "label_tooltip_identifier")
        self._text_panel = require_child(content, QtWidgets.QWidget, "panel_tooltip_identifier")
        self._text_panel.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Minimum,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self._text_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self._text_label.setWordWrap(True)
        self._text_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop
        )
        self._text_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.NoTextInteraction)
        self._text_label.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Minimum,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self._thumbnail_label.setFixedSize(96, 96)
        self._thumbnail_label.setScaledContents(False)
        self._thumbnail_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignHCenter | QtCore.Qt.AlignmentFlag.AlignTop
        )
        self._thumbnail_label.hide()
        self._thumbnail_max_edge = 96
        self._thumbnail_alignment = ""
        self._text_layout_key: tuple[tuple[str, ...], str] | None = None
        self._text_height = 0

    def set_thumbnail_alignment(self, value: str) -> None:
        alignment = str(value or "bottom").strip().lower()
        if alignment not in {"left", "right", "top", "bottom"}:
            alignment = "bottom"
        if alignment == self._thumbnail_alignment:
            return
        self._thumbnail_alignment = alignment
        content_layout = self._content.layout()
        if not isinstance(content_layout, QtWidgets.QBoxLayout):
            return
        content_layout.setDirection(
            QtWidgets.QBoxLayout.Direction.LeftToRight
            if alignment in {"left", "right"}
            else QtWidgets.QBoxLayout.Direction.TopToBottom
        )
        first, second = (
            (self._thumbnail_label, self._text_panel)
            if alignment in {"left", "top"}
            else (self._text_panel, self._thumbnail_label)
        )
        content_layout.insertWidget(0, first)
        content_layout.insertWidget(1, second)
        content_layout.setAlignment(
            self._text_panel,
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop,
        )
        self._text_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop
        )
        self._fit_to_content()

    def set_thumbnail_size(self, size_px: int) -> None:
        size = max(25, min(500, int(size_px)))
        self._thumbnail_max_edge = size
        if self._thumbnail_label.size() == QtCore.QSize(size, size):
            return
        self._thumbnail_label.setFixedSize(size, size)
        self._fit_to_content()

    def set_lines(self, lines: list[str]) -> None:
        layout_key = (tuple(lines), self._text_label.font().toString())
        if layout_key == self._text_layout_key:
            return
        has_lines = any(str(line) for line in lines)
        self._text_panel.setVisible(has_lines)
        if has_lines:
            margins = self._text_panel.layout().contentsMargins()
            width = min(
                self._minimum_text_width(lines),
                self._MAX_TEXT_WIDTH - margins.left() - margins.right(),
            )
            self._text_label.setFixedWidth(width)
            self._text_label.setText(
                self._lines_to_html(lines, font=self._text_label.font(), width=width)
            )
            self._text_height = self._text_label.heightForWidth(width)
        else:
            self._text_label.clear()
            self._text_label.setFixedWidth(0)
            self._text_height = 0
        self._text_layout_key = layout_key
        self._fit_to_content()

    def _minimum_text_width(self, lines: list[str]) -> int:
        metrics = QtGui.QFontMetricsF(self._text_label.font())
        label_width = value_width = plain_width = 0.0
        for line in lines:
            label, separator, value = str(line).partition("\t")
            if separator:
                label_width = max(label_width, metrics.horizontalAdvance(label))
                value_width = max(value_width, metrics.horizontalAdvance(self._truncate_value(value)))
            else:
                plain_width = max(plain_width, metrics.horizontalAdvance(self._truncate_value(label)))
        return int(max(plain_width, label_width + value_width + 10)) + 22

    @classmethod
    def _truncate_value(cls, value: str) -> str:
        if len(value) <= cls._MAX_VALUE_CHARACTERS:
            return value
        prefix = value[: cls._MAX_VALUE_CHARACTERS - 1]
        # Prefer a nearby word boundary without discarding most of a long token.
        if not value[len(prefix)].isspace():
            for index in range(len(prefix) - 1, max(0, len(prefix) - 40) - 1, -1):
                if prefix[index].isspace():
                    prefix = prefix[:index]
                    break
        return prefix.rstrip() + "…"

    @staticmethod
    def _wrapped_html(text: str, font: QtGui.QFont, width: int) -> str:
        option = QtGui.QTextOption()
        option.setWrapMode(QtGui.QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        wrapped: list[str] = []
        for paragraph in text.split("\n"):
            encoded = paragraph.encode("utf-16-le")
            layout = QtGui.QTextLayout(paragraph, font)
            layout.setTextOption(option)
            layout.beginLayout()
            parts: list[str] = []
            while True:
                line = layout.createLine()
                if not line.isValid():
                    break
                line.setLineWidth(max(1, width))
                # QTextLayout positions count UTF-16 units, including emoji pairs.
                start = line.textStart() * 2
                end = start + line.textLength() * 2
                parts.append(html.escape(encoded[start:end].decode("utf-16-le")))
            layout.endLayout()
            wrapped.append("<br>".join(parts))
        return "<br>".join(wrapped)

    @classmethod
    def _lines_to_html(
        cls, lines: list[str], *, font: QtGui.QFont | None = None, width: int = 352
    ) -> str:
        font = font if font is not None else QtWidgets.QApplication.font()
        # Explicit breaks also bound unspaced URLs/IDs in Qt's rich-text tables.
        content_width = max(1, width - 18)
        if all("\t" not in str(line) for line in lines):
            return "".join(
                "<div style='text-align: left; padding: 0 0 2px 0;'>"
                f"{cls._wrapped_html(cls._truncate_value(str(line)), font, content_width)}</div>"
                for line in lines
            )
        metrics = QtGui.QFontMetricsF(font)
        label_width = min(
            int(max(metrics.horizontalAdvance(str(line).partition("\t")[0]) for line in lines)) + 2,
            (cls._MAX_TEXT_WIDTH - 26) // 3,
        )
        value_width = max(1, content_width - label_width - 10)
        rows: list[str] = []
        for line in lines:
            label, separator, value = str(line).partition("\t")
            if separator:
                rows.append(
                    "<tr>"
                    "<td align='left' valign='top' style='text-align: left; padding: 0 10px 2px 0;'>"
                    f"{cls._wrapped_html(label, font, label_width)}</td>"
                    "<td align='left' valign='top' style='text-align: left; padding: 0 0 2px 0;'>"
                    f"{cls._wrapped_html(cls._truncate_value(value), font, value_width)}</td>"
                    "</tr>"
                )
            else:
                rows.append(
                    "<tr>"
                    "<td colspan='2' align='left' valign='top' style='text-align: left; padding: 0 0 2px 0;'>"
                    f"{cls._wrapped_html(cls._truncate_value(label), font, content_width)}</td>"
                    "</tr>"
                )
        return (
            "<table cellspacing='0' cellpadding='0'>"
            + "".join(rows)
            + "</table>"
        )

    def set_placeholder(self) -> None:
        self._thumbnail_label.clear()
        self._thumbnail_label.setText("...")
        self._thumbnail_label.show()
        self._fit_to_content()

    def clear_thumbnail(self) -> None:
        self._thumbnail_label.clear()
        self._thumbnail_label.hide()
        self._fit_to_content()

    def set_thumbnail_image(self, image: QtGui.QImage) -> None:
        pixmap = QtGui.QPixmap.fromImage(image)
        fitted = pixmap.scaled(
            QtCore.QSize(self._thumbnail_max_edge, self._thumbnail_max_edge),
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
        self._thumbnail_label.setFixedSize(fitted.size())
        self._thumbnail_label.setPixmap(fitted)
        self._thumbnail_label.show()
        self._fit_to_content()

    def _fit_to_content(self) -> None:
        text_visible = not self._text_panel.isHidden()
        if text_visible:
            text_margins = self._text_panel.layout().contentsMargins() if self._text_panel.layout() is not None else QtCore.QMargins()
            text_width = self._text_label.width() + text_margins.left() + text_margins.right()
            text_height = (
                self._text_height
                + text_margins.top() + text_margins.bottom()
            )
        else:
            text_width = 0
            text_height = 0
        thumbnail_enabled = not self._thumbnail_label.isHidden()
        thumbnail_width = self._thumbnail_label.width() if thumbnail_enabled else 0
        thumbnail_height = self._thumbnail_label.height() if thumbnail_enabled else 0
        if self._thumbnail_alignment in {"top", "bottom"}:
            width = max(thumbnail_width, text_width)
            height = thumbnail_height + text_height
        else:
            width = thumbnail_width + text_width
            height = max(thumbnail_height, text_height)
        if text_visible:
            self._text_panel.setFixedHeight(text_height)
        self._content.setFixedSize(max(1, width), max(1, height))
        self.setFixedSize(self._content.size())
        self.updateGeometry()


class DiscoveryAreaController:
    """UI binder for the discovery area."""

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        status_callback: Callable[[str], None] | None = None,
        activity_manager: ActivityManager | None = None,
    ) -> None:
        self._widget = load_ui("DiscoveryArea", "ui/discovery_area.ui", parent)
        self._project: Project | None = None
        self._status_callback = status_callback
        self._activity_manager = activity_manager
        self._metadata_changed_callback: Callable[[], None] | None = None
        self._groups_changed_callback: Callable[[], None] | None = None
        self._snapshots_changed_callback: Callable[[], None] | None = None
        self._open_metadata_callback: Callable[[int], None] | None = None
        self._create_thumbnails_requested_callback: Callable[[], None] | None = None
        self._panels: list[UiPanelController] = []
        self._panel_lookup: dict[type[UiPanelController], UiPanelController] = {}
        self._canvas_widget: DiscoveryCanvasWidget | None = None
        self._canvas_style_revision = 0
        self._canvas_style_tasks: dict[int, _CanvasStyleBuildTask] = {}
        self._canvas_style_cancel_events: dict[int, threading.Event] = {}
        self._canvas_style_render_options: dict[int, tuple[DiscoveryDataset, bool, bool]] = {}
        self._service = DiscoveryService()
        self._transform_service = DiscoveryTransformService()
        self._discovery_options: dict[str, DiscoveryOption] = {}
        self._active_discovery_key: str = "random"
        self._active_dataset: DiscoveryDataset | None = None
        self._source_dataset: DiscoveryDataset | None = None
        self._active_transform: TransformResult | None = None
        self._active_arrangement_ref = "original"
        self._transform_request_key: str | None = None
        self._transform_request_fingerprint: str | None = None
        self._transform_coordinator = TransformCoordinator(self._widget)
        self._transform_coordinator.ready.connect(self._handle_transform_ready)
        self._transform_coordinator.failed.connect(self._handle_transform_failed)
        self._transform_coordinator.cancelled.connect(self._handle_transform_cancelled)
        self._transform_coordinator.progress.connect(self._handle_transform_progress)
        self._filtered_opacity = 1.0
        self._show_marked_areas = True
        self._polygon_areas: list[PolygonArea] = []
        self._selected_polygon_area_id: str | None = None
        self._selected_polygon_metadata_ids: set[int] = set()
        self._marked_area_opacity = 0.2
        self._tooltip_layer_enabled = True
        self._metadata_access: DiscoveryMetadataAccess | None = None
        # Compatibility-only test seam. Production metadata is column-oriented
        # through _metadata_access and this mapping remains empty.
        self._metadata_row_map: dict[int, dict[str, str]] = {}
        self._metadata_columns_cache: list[str] = ["id"]
        self._metadata_array_cache: dict[
            tuple[int, tuple[int, ...], tuple[str, ...], tuple[str, ...]],
            tuple[np.ndarray, dict[str, np.ndarray], dict[str, np.ndarray]],
        ] = {}
        self._thumbnail_ready_bridge = _ThumbnailReadyBridge(self._widget)
        self._discovery_run_bridge = DiscoveryRunBridge(self._widget)
        self._queued_discovery_runs: deque[_QueuedDiscoveryRun] = deque()
        self._active_discovery_run: _QueuedDiscoveryRun | None = None
        self._discovery_run_thread: QtCore.QThread | None = None
        self._discovery_run_worker: DiscoveryRunWorker | None = None
        self._discovery_admission = None
        self._pending_discovery_option_key: str | None = None
        self._pending_discovery_error: str | None = None
        self._pending_discovery_canvas_key: str | None = None
        self._pending_discovery_canvas_ready = False
        self._pending_discovery_reload_complete = False
        self._discovery_cancel_requested = False
        self._discovery_selection_before_run: str | None = None
        self._shutting_down = False
        self._tooltip_popup: _CanvasTooltipPopup | None = None
        self._selection_actions_dialog_controller: SelectionActionsDialogController | None = None
        self._selection_neighbor_group_changed = False
        self._neighbor_search_coordinator = NearestNeighborCoordinator(self._widget)
        self._neighbor_search_coordinator.result_ready.connect(
            self._handle_selection_neighbor_result
        )
        self._neighbor_search_coordinator.search_failed.connect(
            self._handle_selection_neighbor_failure
        )
        self._settings_preview_tooltip_active = False
        self._hovered_metadata_id: int | None = None
        self._hovered_thumbnail_variant_id: str | None = None
        self._tooltip_thumbnail_pending_request: tuple[int, str, int] | None = None
        self._thumbnail_render_config = THUMBNAIL_CONFIG
        self._canvas_thumbnail_variant_id = "none"
        self._canvas_render_mode = "points_fast"
        self._canvas_thumbnail_size_px = 64
        self._thumbnail_runtime_policy = automatic_thumbnail_runtime_policy()
        self._canvas_thumbnail_limit = LayersSettingPanelController.normalize_thumbnail_limit(
            automatic_thumbnail_limit_for_size(self._canvas_thumbnail_size_px)
        )
        self._canvas_thumbnail_limit_automatic = True
        self._thumbnail_preview_visibility = np.empty((0,), dtype=np.float32)
        self._thumbnail_plan_coordinator = ThumbnailPlanCoordinator(self._widget)
        self._thumbnail_plan_coordinator.plan_ready.connect(
            self._on_thumbnail_plan_ready
        )
        self._thumbnail_plan_coordinator.plan_failed.connect(
            self._on_thumbnail_plan_failed
        )
        self._thumbnail_plan_coordinator.stale_result_discarded.connect(
            self._on_thumbnail_plan_stale_result
        )
        self._active_thumbnail_plan: ThumbnailViewportPlan | None = None
        self._active_thumbnail_metadata_id_set: set[int] = set()
        self._thumbnail_decode_px = 32
        self._thumbnail_fast_frames = 0
        self._thumbnail_slow_frames = 0
        self._thumbnail_missing_ids: set[int] = set()
        self._thumbnail_failed_ids: set[int] = set()
        self._thumbnail_stale_results = 0
        self._thumbnail_native_status: dict[str, object] = {}
        self._applying_camera_preset = False
        self._canvas_thumbnail_zoom_tier = "placeholders"
        self._thumbnail_service: ThumbnailService | None = None
        self._thumbnail_request_generation = 0
        self._thumbnail_pending_entries: list[dict[str, object]] = []
        self._thumbnail_loading_pending_ids: set[int] = set()
        self._pin_thumbnail_generation = 0
        self._pin_thumbnail_pending_requests: dict[tuple[int, str, int], int] = {}
        self._thumbnail_request_timer = QtCore.QTimer(self._widget)
        self._thumbnail_request_timer.setSingleShot(True)
        self._thumbnail_request_timer.setInterval(self._thumbnail_render_config.request_debounce_ms)
        self._thumbnail_request_timer.timeout.connect(self._request_visible_canvas_thumbnails)
        self._thumbnail_rolling_request_timer = QtCore.QTimer(self._widget)
        self._thumbnail_rolling_request_timer.setSingleShot(True)
        self._thumbnail_rolling_request_timer.setInterval(
            self._thumbnail_render_config.rolling_request_interval_ms
        )
        self._thumbnail_rolling_request_timer.timeout.connect(self._request_visible_canvas_thumbnails)
        self._thumbnail_progressive_timer = QtCore.QTimer(self._widget)
        self._thumbnail_progressive_timer.setSingleShot(True)
        self._thumbnail_progressive_timer.setInterval(
            self._thumbnail_runtime_policy.progressive_interval_ms
        )
        self._thumbnail_progressive_timer.timeout.connect(self._progress_thumbnail_plan)
        self._thumbnail_push_timer = QtCore.QTimer(self._widget)
        self._thumbnail_push_timer.setSingleShot(True)
        self._thumbnail_push_timer.setInterval(self._thumbnail_render_config.push_interval_ms)
        self._thumbnail_push_timer.timeout.connect(self._push_pending_canvas_thumbnails)
        self._thumbnail_ready_bridge.ready.connect(self._handle_thumbnail_ready)
        self._discovery_run_bridge.progress.connect(self._handle_discovery_run_progress)
        self._discovery_run_bridge.finished.connect(self._handle_discovery_run_finished)
        self._discovery_run_bridge.failed.connect(self._handle_discovery_run_failed)
        self._discovery_run_bridge.cancelled.connect(self._handle_discovery_run_cancelled)
        self._discovery_run_bridge.thread_finished.connect(self._handle_discovery_thread_finished)
        self._applying_pin_display = False
        self._persist_pins_timer = QtCore.QTimer(self._widget)
        self._persist_pins_timer.setSingleShot(True)
        self._persist_pins_timer.setInterval(120)
        self._persist_pins_timer.timeout.connect(self._persist_canvas_pins)
        self._pending_appearance_state: dict[str, object] | None = None
        self._persist_appearance_timer = QtCore.QTimer(self._widget)
        self._persist_appearance_timer.setSingleShot(True)
        self._persist_appearance_timer.setInterval(120)
        self._persist_appearance_timer.timeout.connect(self._persist_project_appearance_state)
        self._current_snapshot_path: Path | None = None
        self._current_snapshot_baseline: dict[str, object] | None = None
        self._active_pin_annotation_id: str | None = None
        self._applying_project_layer_state = False
        self._restoring_project_state = False
        self._space_key_filter = _CanvasSpaceKeyFilter(self)
        self._visibility_filter = _DiscoveryVisibilityFilter(self)
        self._widget.installEventFilter(self._visibility_filter)
        self._load_panels()
        self._sync_initial_areas_layer_visibility()
        self._install_canvas_host()
        self._wire_interactions()
        self._default_exploration_settings = self._exploration_settings_payload()
        self._persist_settings_timer = QtCore.QTimer(self._widget)
        self._persist_settings_timer.setSingleShot(True)
        self._persist_settings_timer.setInterval(120)
        self._persist_settings_timer.timeout.connect(self._persist_project_exploration_settings)

    @property
    def widget(self) -> QtWidgets.QWidget:
        return self._widget

    def activate(self) -> None:
        if self._canvas_widget is not None:
            self._canvas_widget.activate_render_host()

    def deactivate(self) -> None:
        """Close transient Discovery windows when its stacked page is left."""

        canvas_layers = self._panel(CanvasLayersPanelController)
        if canvas_layers is not None:
            canvas_layers.close_layer_settings_popups()
        self._hide_tooltip_popup()

    def _sync_initial_areas_layer_visibility(self) -> None:
        panel = self._panel(CanvasLayersPanelController)
        checkbox = (
            None
            if panel is None
            else panel.widget.findChild(QtWidgets.QCheckBox, "checkbox_show_marked_areas")
        )
        self._show_marked_areas = bool(checkbox is not None and checkbox.isChecked())

    def _handle_space_key_event(self, _obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() not in (
            QtCore.QEvent.Type.ShortcutOverride,
            QtCore.QEvent.Type.KeyPress,
            QtCore.QEvent.Type.KeyRelease,
        ):
            return False
        if self._shutting_down or self._canvas_widget is None or not isValid(self._widget):
            return False
        if self._focus_widget_accepts_text_input():
            return False
        if event.type() == QtCore.QEvent.Type.ShortcutOverride:
            key_event = event
            if int(key_event.key()) == int(QtCore.Qt.Key.Key_Space):
                key_event.accept()
                return True
        if event.type() == QtCore.QEvent.Type.KeyPress:
            key_event = event
            if int(key_event.key()) == int(QtCore.Qt.Key.Key_Space):
                if self._is_cursor_over_canvas():
                    self._canvas_widget.begin_temporary_pan()
                key_event.accept()
                return True
        if event.type() == QtCore.QEvent.Type.KeyRelease:
            key_event = event
            if int(key_event.key()) == int(QtCore.Qt.Key.Key_Space):
                if self._canvas_widget.end_temporary_pan():
                    key_event.accept()
                    return True
                key_event.accept()
                return True
        return False

    @staticmethod
    def _focus_widget_accepts_text_input() -> bool:
        focus_widget = QtWidgets.QApplication.focusWidget()
        return isinstance(
            focus_widget,
            (
                QtWidgets.QLineEdit,
                QtWidgets.QTextEdit,
                QtWidgets.QPlainTextEdit,
                QtWidgets.QAbstractSpinBox,
            ),
        )

    def set_project(self, project: Project | None) -> None:
        if self._activity_manager is not None:
            with self._activity_manager.busy("Refreshing Discovery…"):
                self._set_project(project)
            return
        self._set_project(project)

    def _set_project(self, project: Project | None) -> None:
        self._flush_pin_persistence()
        self._persist_project_appearance_state()
        self._persist_project_exploration_settings()
        if project is not self._project:
            self._cancel_all_discovery_runs()
            self._transform_coordinator.cancel_pending()
            self._transform_request_key = None
            self._transform_request_fingerprint = None
        self._restoring_project_state = True
        try:
            self._disable_previews_layer()
            self._clear_current_snapshot_update_state()
            self._clear_pins_for_project_switch()
            self._project = project
            self._metadata_access = (
                None if project is None else DiscoveryMetadataAccess(project)
            )
            self._reset_thumbnail_service(project)
            for panel in self._panels:
                metadata_setter = getattr(panel, "set_metadata_access", None)
                if callable(metadata_setter):
                    metadata_setter(self._metadata_access)
                panel.set_project(project)
            if project is not None:
                self._load_project_appearance_state()
            # Saved field selections must be validated against this project's
            # columns, rather than the panel's startup placeholders.
            if project is not None:
                self._refresh_metadata_cache(project)
            else:
                self._metadata_columns_cache = ["id"]
            self._sync_pin_metadata_fields()
            self._load_project_exploration_settings()
            label = self._widget.findChild(QtWidgets.QLabel, "label_project_status")
            if label is not None:
                label.setText(project.name if project is not None else "No project loaded")
            self._reload_discovery_options()
        finally:
            self._restoring_project_state = False

    def refresh(self) -> None:
        if self._activity_manager is None:
            self._reload_discovery_options()
            return
        with self._activity_manager.busy("Refreshing Discovery…"):
            self._reload_discovery_options()

    def refresh_metadata(self) -> None:
        """Reload metadata-backed state before rebuilding Discovery options."""

        if self._project is not None:
            self._rebuild_metadata_access()
        self.refresh()

    def refresh_metadata_groups(self) -> None:
        """Invalidate virtual group fields without reloading the active projection."""

        if self._project is None:
            return
        self._rebuild_metadata_access()
        if self._active_dataset is not None:
            self._render_active_dataset(preserve_view=True, fit_view=False)

    def _rebuild_metadata_access(self) -> None:
        """Replace caches whose row IDs or columns may have changed on disk."""

        if self._project is None:
            return
        self._metadata_access = DiscoveryMetadataAccess(self._project)
        for controller_type in (AppearancePanelController, FiltersPanelController, SlicerPanelController):
            panel = self._panel(controller_type)
            if panel is None:
                continue
            panel.set_metadata_access(self._metadata_access)
            panel.set_project(self._project)
        self._refresh_metadata_cache(self._project)

    def refresh_thumbnail_variants(self) -> None:
        """Reload thumbnail choices without resetting the active projection."""

        layers_panel = self._panel(LayersSettingPanelController)
        if layers_panel is not None:
            layers_panel.refresh_thumbnail_variants()

    def set_metadata_changed_callback(self, callback: Callable[[], None] | None) -> None:
        self._metadata_changed_callback = callback

    def set_groups_changed_callback(self, callback: Callable[[], None] | None) -> None:
        self._groups_changed_callback = callback

    def set_snapshots_changed_callback(self, callback: Callable[[], None] | None) -> None:
        self._snapshots_changed_callback = callback

    def set_create_thumbnails_requested_callback(
        self,
        callback: Callable[[], None] | None,
    ) -> None:
        self._create_thumbnails_requested_callback = callback
        layers_panel = self._panel(LayersSettingPanelController)
        if layers_panel is not None:
            layers_panel.set_create_thumbnails_requested_callback(callback)

    def _notify_snapshots_changed(self) -> None:
        if self._snapshots_changed_callback is not None:
            self._snapshots_changed_callback()

    def shutdown(self) -> None:
        self._shutting_down = True
        self._flush_pin_persistence()
        self._persist_project_appearance_state()
        self._persist_project_exploration_settings()
        self._canvas_style_revision += 1
        for event in self._canvas_style_cancel_events.values():
            event.set()
        self._canvas_style_render_options.clear()
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.removeEventFilter(self._space_key_filter)
        self._widget.removeEventFilter(self._space_key_filter)
        self._thumbnail_request_timer.stop()
        self._thumbnail_rolling_request_timer.stop()
        self._thumbnail_progressive_timer.stop()
        self._thumbnail_push_timer.stop()
        self._thumbnail_plan_coordinator.cancel_pending()
        self._transform_coordinator.cancel_pending()
        self._neighbor_search_coordinator.cancel_pending(clear_cache=True)
        self._persist_appearance_timer.stop()
        self._persist_settings_timer.stop()
        if self._thumbnail_service is not None:
            self._thumbnail_service.unsubscribe_thumbnail_ready(self._on_thumbnail_ready_from_worker)
            self._thumbnail_service.unsubscribe_source_batch_ready(
                self._on_thumbnail_source_batch_from_worker
            )
            self._thumbnail_service.shutdown()
            self._thumbnail_service = None
        self._cancel_all_discovery_runs()
        if self._discovery_run_worker is not None:
            self._discovery_run_thread.quit() if self._discovery_run_thread is not None else None
            self._discovery_run_thread.wait(1500) if self._discovery_run_thread is not None else None
            self._discovery_run_worker = None
            self._discovery_run_thread = None
        for panel in self._panels:
            panel.shutdown()
        if self._tooltip_popup is not None:
            self._tooltip_popup.hide()
            self._tooltip_popup.deleteLater()
            self._tooltip_popup = None
        if self._canvas_widget is not None:
            self._canvas_widget.close()
            self._canvas_widget.deleteLater()
            self._canvas_widget = None

    def _flush_pin_persistence(self) -> None:
        if not self._project_has_storage():
            return
        if self._persist_pins_timer.isActive():
            self._persist_pins_timer.stop()
        self._persist_canvas_pins()

    def _clear_pins_for_project_switch(self) -> None:
        if self._persist_pins_timer.isActive():
            self._persist_pins_timer.stop()
        self._active_pin_annotation_id = None
        self._pin_thumbnail_generation += 1
        self._pin_thumbnail_pending_requests.clear()
        self._tooltip_thumbnail_pending_request = None
        if self._canvas_widget is None:
            return
        self._applying_pin_display = True
        try:
            self._canvas_widget.set_pinned_annotations([])
        finally:
            self._applying_pin_display = False

    def _project_has_storage(self) -> bool:
        return self._project is not None and hasattr(self._project, "paths")

    def _reset_thumbnail_service(self, project: Project | None) -> None:
        if self._thumbnail_service is not None:
            self._thumbnail_service.unsubscribe_thumbnail_ready(self._on_thumbnail_ready_from_worker)
            self._thumbnail_service.unsubscribe_source_batch_ready(
                self._on_thumbnail_source_batch_from_worker
            )
            self._thumbnail_service.shutdown()
            self._thumbnail_service = None
        self._thumbnail_request_timer.stop()
        self._thumbnail_rolling_request_timer.stop()
        self._thumbnail_progressive_timer.stop()
        self._thumbnail_pending_entries.clear()
        self._thumbnail_loading_pending_ids.clear()
        self._reset_thumbnail_plan_state()
        self._pin_thumbnail_generation += 1
        self._pin_thumbnail_pending_requests.clear()
        self._tooltip_thumbnail_pending_request = None
        self._update_thumbnail_loading_indicator()
        self._thumbnail_request_generation += 1
        if project is None:
            return
        service = ThumbnailService(
            ThumbnailResolver(project),
            max_cache_bytes=max(1, int(self._thumbnail_runtime_policy.cpu_cache_mb)) * 1024 * 1024,
            max_workers=self._thumbnail_runtime_policy.decode_workers,
        )
        service.subscribe_thumbnail_ready(self._on_thumbnail_ready_from_worker)
        service.subscribe_source_batch_ready(self._on_thumbnail_source_batch_from_worker)
        self._thumbnail_service = service

    def _load_panels(self) -> None:
        panel_specs = [
            ("panel_projection_host", DiscoveryPanelController),
            ("panel_transform_host", TransformPanelController),
            ("panel_canvas_layers_host", CanvasLayersPanelController),
            ("panel_canvas_tools_host", CanvasToolsPanelController),
            ("panel_appearance_host", AppearancePanelController),
            ("panel_filters_host", FiltersPanelController),
            ("panel_slicer_host", SlicerPanelController),
            ("panel_snapshots_host", SnapshotsPanelController),
        ]
        for container_name, controller_type in panel_specs:
            container = require_child(self._widget, QtWidgets.QWidget, container_name)
            panel = controller_type(
                parent=self._widget,
                status_callback=self._show_status,
                activity_manager=self._activity_manager,
            )
            add_widget_to_container(container, panel.widget)
            self._panels.append(panel)
            self._panel_lookup[controller_type] = panel
        layer_settings = LayersSettingPanelController(
            parent=self._widget,
            status_callback=self._show_status,
            activity_manager=self._activity_manager,
        )
        layer_settings.widget.hide()
        self._panels.append(layer_settings)
        self._panel_lookup[LayersSettingPanelController] = layer_settings

    def _show_status(self, message: str) -> None:
        if self._status_callback is not None:
            self._status_callback(message)

    def _discovery_debug(self, event: str, **payload: object) -> None:
        _ = event, payload

    def _install_canvas_host(self) -> None:
        container = require_child(self._widget, QtWidgets.QWidget, "panel_canvas_host")
        self._canvas_widget = DiscoveryCanvasWidget(container)
        self._canvas_widget.setObjectName("discovery_canvas_widget")
        add_widget_to_container(container, self._canvas_widget)
        self._canvas_widget.preload_renderer()
        policy = self._thumbnail_runtime_policy
        self._canvas_widget.set_thumbnail_runtime_policy(
            cpu_cache_mb=policy.cpu_cache_mb,
            gpu_cache_mb=policy.gpu_cache_mb,
            atlas_page_px=policy.atlas_page_px,
            decode_workers=policy.decode_workers,
            upload_bytes_per_frame=policy.upload_bytes_per_frame,
            target_frame_ms=policy.target_frame_ms,
        )
        self._widget.installEventFilter(self._space_key_filter)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.installEventFilter(self._space_key_filter)

    def _is_cursor_over_canvas(self) -> bool:
        if self._canvas_widget is None:
            return False
        if not self._canvas_widget.isVisible():
            return False
        cursor_global = QtGui.QCursor.pos()
        local = self._canvas_widget.mapFromGlobal(cursor_global)
        return self._canvas_widget.rect().contains(local)

    def _wire_interactions(self) -> None:
        discovery_panel = self._panel(DiscoveryPanelController)
        if discovery_panel is not None:
            discovery_panel.set_discovery_selection_callback(self._handle_discovery_selection)
            discovery_panel.set_create_discovery_callback(self._handle_create_discovery_requested)
            discovery_panel.set_abort_discovery_callback(self._handle_abort_discovery_requested)
            discovery_panel.set_import_discovery_callback(self._handle_import_discovery_requested)
            discovery_panel.set_export_discovery_callback(self._handle_export_discovery_requested)
            discovery_panel.set_methods_report_callback(self._methods_report_for_projection)
            discovery_panel.set_rename_discovery_callback(self._handle_rename_discovery_requested)
            discovery_panel.set_delete_discovery_callback(self._handle_delete_discovery_requested)
            discovery_panel.set_discovery_sources_provider(self._discovery_source_options)
            discovery_panel.set_filter_presets_provider(self._projection_filter_preset_options)
            discovery_panel.set_discovery_metadata_ids_provider(
                lambda: self._project_metadata_ids(self._project) if self._project is not None else np.empty((0,), dtype=np.int64)
            )
            discovery_panel.set_discovery_metadata_columns_provider(self._discovery_import_metadata_columns)
            discovery_panel.set_discovery_metadata_values_provider(self._discovery_import_metadata_values)
            discovery_panel.set_discovery_methods(self._service.list_discovery_methods())
        transform_panel = self._panel(TransformPanelController)
        if transform_panel is not None:
            transform_panel.set_apply_callback(self._handle_transform_requested)
            transform_panel.set_cancel_callback(self._handle_transform_cancel_requested)
            transform_panel.set_save_preset_callback(self._save_transform_preset)
            transform_panel.set_load_preset_callback(self._load_transform_preset_by_label)
        tools_panel = self._panel(CanvasToolsPanelController)
        if tools_panel is not None:
            tools_panel.set_fit_callback(self._handle_fit_view)
            tools_panel.set_mode_changed_callback(self._handle_mode_changed)
            tools_panel.set_background_color_callback(self._handle_canvas_background_color_changed)
            tools_panel.set_current_background_color_callback(self._current_canvas_background_color)
            tools_panel.set_find_item_callback(self._handle_find_item_text_changed)
            tools_panel.set_clear_find_item_callback(self._clear_find_item)
            tools_panel.set_quick_export_callback(self._handle_quick_canvas_export)
        appearance_panel = self._panel(AppearancePanelController)
        if appearance_panel is not None:
            appearance_panel.set_style_changed_callback(self._handle_appearance_style_changed)
            appearance_panel.set_save_project_preset_callback(self._save_appearance_preset)
            appearance_panel.set_load_project_preset_callback(self._load_appearance_preset_by_label)
            appearance_panel.set_save_global_template_callback(self._save_global_appearance_template)
            appearance_panel.set_load_global_template_callback(self._load_global_appearance_template_by_label)
        filters_panel = self._panel(FiltersPanelController)
        if filters_panel is not None:
            filters_panel.set_filtered_opacity_changed_callback(self._handle_filtered_opacity_changed)
            filters_panel.set_hide_filtered_tooltips_changed_callback(
                self._handle_hide_filtered_tooltips_changed
            )
            filters_panel.set_filters_changed_callback(self._handle_filters_changed)
            filters_panel.set_save_preset_callback(self._save_filter_preset)
            filters_panel.set_load_preset_callback(self._load_filter_preset_by_label)
            self._filtered_opacity = filters_panel.filtered_opacity()
        slicer_panel = self._panel(SlicerPanelController)
        if slicer_panel is not None:
            slicer_panel.set_save_preset_callback(self._save_slicer_preset)
            slicer_panel.set_load_preset_callback(self._load_slicer_preset_by_label)
            slicer_panel.set_slicer_changed_callback(self._handle_slicer_changed)
        canvas_layers_panel = self._panel(CanvasLayersPanelController)
        if canvas_layers_panel is not None:
            canvas_layers_panel.set_areas_visibility_changed_callback(self._handle_areas_visibility_toggled)
            canvas_layers_panel.set_pins_visibility_changed_callback(self._handle_pins_visibility_toggled)
            canvas_layers_panel.set_tooltips_visibility_changed_callback(self._handle_tooltips_visibility_toggled)
            canvas_layers_panel.set_camera_save_callback(self._save_camera_preset)
            canvas_layers_panel.set_camera_load_callback(self._load_camera_preset_from_path)
            canvas_layers_panel.set_default_camera_callback(self._handle_fit_view)
            canvas_layers_panel.set_viewport_aspect_callback(self._handle_viewport_aspect_changed)
            canvas_layers_panel.set_crop_bars_hidden_callback(self._handle_crop_bars_hidden_changed)
            if self._canvas_widget is not None:
                self._canvas_widget.set_crop_bars_visible(not canvas_layers_panel.crop_bars_hidden())
        snapshots_panel = self._panel(SnapshotsPanelController)
        if snapshots_panel is not None:
            snapshots_panel.set_save_current_callback(self._save_snapshot_by_label)
            snapshots_panel.set_load_snapshot_callback(self._load_snapshot_from_path)
            snapshots_panel.set_update_snapshot_callback(self._update_snapshot_from_path)
            snapshots_panel.set_delete_snapshot_callback(self._delete_snapshot_from_path)
        layers_panel = self._panel(LayersSettingPanelController)
        if layers_panel is not None:
            layers_panel.set_selection_changed_callback(self._handle_area_tree_selection_changed)
            layers_panel.set_delete_area_callback(self._queue_area_tree_delete_requested)
            layers_panel.set_area_visibility_callback(self._queue_area_visibility_changed)
            layers_panel.set_points_visibility_callback(self._queue_area_points_visibility_changed)
            layers_panel.set_all_area_visibility_callback(self._queue_all_area_visibility_changed)
            layers_panel.set_all_points_visibility_callback(self._queue_all_area_points_visibility_changed)
            layers_panel.set_rename_callback(self._queue_area_renamed)
            layers_panel.set_color_changed_callback(self._queue_area_color_changed)
            layers_panel.set_export_area_requested_callback(self._queue_export_area_requested)
            layers_panel.set_marked_area_style_changed_callback(self._handle_marked_area_style_changed)
            layers_panel.set_pin_display_changed_callback(self._handle_pin_display_changed)
            layers_panel.set_tooltip_thumbnail_size_changed_callback(self._handle_tooltip_thumbnail_size_changed)
            layers_panel.set_tooltip_thumbnail_size_preview_hidden_callback(self._hide_settings_preview_tooltip)
            layers_panel.set_create_thumbnails_requested_callback(
                self._create_thumbnails_requested_callback
            )
            layers_panel.set_render_selection_changed_callback(self._handle_render_selection_changed)
            layers_panel.set_thumbnail_size_changed_callback(self._handle_thumbnail_size_changed)
            layers_panel.set_thumbnail_limit_changed_callback(
                self._handle_thumbnail_limit_changed
            )
            layers_panel.set_thumbnail_density_auto_requested_callback(
                self._handle_thumbnail_limit_auto_requested
            )
        if canvas_layers_panel is not None and layers_panel is not None:
            canvas_layers_panel.set_layer_settings_view_provider(
                layers_panel.layer_settings_view
            )
            thumb_toggle = canvas_layers_panel.widget.findChild(QtWidgets.QCheckBox, "checkbox_show_thumbnails")
            if thumb_toggle is not None:
                thumb_toggle.toggled.connect(
                    lambda checked: layers_panel.set_previews_enabled(bool(checked))
                )
                thumb_toggle.toggled.connect(
                    lambda checked: self._handle_render_selection_changed("points_fast")
                    if not checked
                    else None
                )
                layers_panel.set_previews_enabled(bool(thumb_toggle.isChecked()))
        if self._canvas_widget is not None:
            self._canvas_widget.set_lasso_selection_callback(self._handle_lasso_selection_changed)
            self._canvas_widget.lasso_polygon_created.connect(self._queue_lasso_polygon_created)
            self._canvas_widget.lasso_polygon_selected.connect(self._handle_lasso_polygon_selected)
            self._canvas_widget.lasso_polygon_context.connect(self._queue_lasso_polygon_context)
            self._canvas_widget.lasso_polygon_delete_requested.connect(self._queue_lasso_polygon_delete_requested)
            self._canvas_widget.lasso_polygon_removed.connect(self._handle_lasso_polygon_removed)
            self._canvas_widget.lasso_polygon_updated.connect(self._handle_lasso_polygon_updated)
            self._canvas_widget.lasso_status_hint_changed.connect(self._show_status)
            self._canvas_widget.pinned_annotations_changed.connect(self._handle_canvas_pins_changed)
            self._canvas_widget.pin_context_requested.connect(self._handle_pin_context_requested)
            self._canvas_widget.point_selection_requested.connect(
                self._handle_canvas_point_selection_requested
            )
            self._canvas_widget.camera_changed.connect(self._handle_canvas_camera_changed)
            self._canvas_widget.scene_build_finished.connect(
                self._handle_canvas_scene_build_finished
            )
            self._canvas_widget.scene_build_failed.connect(
                self._handle_canvas_scene_build_failed
            )
            self._canvas_widget.static_upload_finished.connect(
                self._handle_canvas_static_upload_finished
            )
            self._canvas_widget.render_timing_changed.connect(
                self._handle_canvas_render_timing
            )
            self._canvas_widget.thumbnail_runtime_status_changed.connect(
                self._handle_thumbnail_runtime_status
            )
            self._canvas_widget.set_point_hover_callback(self._handle_canvas_point_hovered)

    def _panel(self, panel_type):
        panel = self._panel_lookup.get(panel_type)
        return panel if panel is not None else None

    def _disable_previews_layer(self) -> None:
        canvas_panel = self._panel(CanvasLayersPanelController)
        checkbox = (
            None
            if canvas_panel is None
            else canvas_panel.widget.findChild(
                QtWidgets.QCheckBox,
                "checkbox_show_thumbnails",
            )
        )
        if checkbox is not None:
            with QtCore.QSignalBlocker(checkbox):
                checkbox.setChecked(False)
        layers_panel = self._panel(LayersSettingPanelController)
        if layers_panel is not None:
            layers_panel.set_previews_enabled(False)
        self._handle_render_selection_changed("points_fast")

    def _current_canvas_background_color(self) -> QtGui.QColor:
        if self._canvas_widget is None:
            return QtGui.QColor("#ffffff")
        return self._canvas_widget.background_color()

    def _handle_canvas_background_color_changed(self, color: QtGui.QColor) -> None:
        if self._canvas_widget is not None:
            self._canvas_widget.set_background_color(color)
            self._queue_project_exploration_settings_save()
            self._refresh_current_snapshot_update_state()

    def _handle_quick_canvas_export(self) -> bool:
        if self._canvas_widget is None:
            self._show_status("Canvas export is unavailable.")
            return False
        export_filters = (
            "PNG Image (*.png);;"
            "JPEG Image (*.jpg *.jpeg);;"
            "GIF Image (*.gif);;"
            "WebP Image (*.webp);;"
            "PDF Document (*.pdf)"
        )
        initial_dir = self._project.root if self._project is not None else Path.home()
        selected_path, selected_filter = QtWidgets.QFileDialog.getSaveFileName(
            self._widget,
            "Quick Export Cropped Canvas",
            str(initial_dir / "discovery_canvas"),
            export_filters,
        )
        if not selected_path:
            return False
        target = Path(selected_path)
        suffix = target.suffix.lower().lstrip(".")
        if suffix not in SUPPORTED_CANVAS_EXPORT_FORMATS:
            extension_by_filter = {
                "PNG Image": ".png",
                "JPEG Image": ".jpg",
                "GIF Image": ".gif",
                "WebP Image": ".webp",
                "PDF Document": ".pdf",
            }
            extension = next(
                (value for label, value in extension_by_filter.items() if selected_filter.startswith(label)),
                ".png",
            )
            target = target.with_suffix(extension)
        activity = (
            self._activity_manager.progress(
                "Preparing canvas export…", value=0, total=100
            )
            if self._activity_manager is not None
            else None
        )
        try:
            if activity is not None:
                activity.update(message="Capturing canvas…", value=20)
            pixmap = self._canvas_widget.capture_active_viewport_pixmap()
            if pixmap.isNull():
                message = "Canvas export failed: the canvas could not be captured."
                if activity is not None:
                    activity.retain_failed(message)
                else:
                    self._show_status(message)
                return False
            if activity is not None:
                activity.update(message="Writing canvas export…", value=60)
            save_canvas_pixmap(pixmap, target)
            if activity is not None:
                activity.update(message="Canvas export complete", value=100)
        except (OSError, ValueError) as exc:
            message = f"Canvas export failed: {exc}"
            if activity is not None:
                activity.retain_failed(message)
            else:
                self._show_status(message)
            return False
        finally:
            if activity is not None:
                activity.finish()
        self._show_status(f"Exported cropped canvas: {target.name}")
        return True

    def _handle_find_item_text_changed(self, text: str) -> bool:
        if self._canvas_widget is None:
            return False
        stripped = str(text or "").strip()
        if not stripped:
            self._canvas_widget.clear_find_item_blink()
            return False
        try:
            metadata_id = int(stripped)
        except ValueError:
            self._canvas_widget.clear_find_item_blink()
            return False
        found = self._canvas_widget.blink_metadata_id(metadata_id)
        if not found:
            self._canvas_widget.clear_find_item_blink()
        else:
            self._queue_canvas_thumbnail_requests()
        return found

    def _clear_find_item(self) -> None:
        if self._canvas_widget is not None:
            self._canvas_widget.clear_find_item_blink()

    def _reload_discovery_options(self, *, selected_key: str | None = None) -> None:
        discovery_panel = self._panel(DiscoveryPanelController)
        if discovery_panel is None:
            return
        self._discovery_debug("reload_discovery_options_start", has_project=self._project is not None)
        if self._project is None:
            self._metadata_row_map = {}
            self._metadata_columns_cache = ["id"]
            self._metadata_array_cache.clear()
            self._discovery_options = {}
            self._active_discovery_key = "random"
            discovery_panel.set_discovery_options([], enabled=False, selected_key="random")
            self._active_dataset = None
            self._source_dataset = None
            self._active_transform = None
            self._active_arrangement_ref = "original"
            self._reset_thumbnail_plan_state()
            self._polygon_areas = []
            self._selected_polygon_area_id = None
            self._selected_polygon_metadata_ids.clear()
            self._active_pin_annotation_id = None
            if self._canvas_widget is not None:
                self._canvas_widget.clear()
                self._canvas_widget.set_pinned_annotations([])
            self._clear_current_snapshot_update_state()
            transform_panel = self._panel(TransformPanelController)
            if transform_panel is not None:
                transform_panel.set_projection(
                    None, populated=False, transformable=False
                )
            self._sync_areas_panel()
            self._refresh_preset_menus()
            self._discovery_debug("reload_discovery_options_no_project")
            return
        metadata_ids = self._project_metadata_ids(self._project)
        self._discovery_debug("reload_discovery_options_metadata", metadata_count=int(metadata_ids.size))
        self._refresh_metadata_cache(self._project)
        self._sync_pin_metadata_fields()
        filters_panel = self._panel(FiltersPanelController)
        if filters_panel is not None:
            self._filtered_opacity = filters_panel.filtered_opacity()
        options = self._service.load_project_discovery_options(self._project, metadata_ids)
        self._discovery_debug(
            "reload_discovery_options_loaded",
            option_count=len(options),
            option_labels=[option.label for option in options],
            option_keys=[option.key for option in options],
        )
        self._discovery_options = {option.key: option for option in options}
        resolved_key = selected_key if selected_key in self._discovery_options else self._restored_discovery_key(options)
        discovery_panel.set_discovery_options(options, enabled=True, selected_key=resolved_key)
        # Load the same fallback selection that is highlighted in the list.
        resolved_key = discovery_panel.current_selection_key() or resolved_key
        self._active_discovery_key = resolved_key
        self._handle_discovery_selection(resolved_key)
        self._refresh_preset_menus()

    def _restored_discovery_key(self, options: list[DiscoveryOption]) -> str:
        available = {option.key for option in options}
        if not self._restoring_project_state and self._active_discovery_key in available:
            return self._active_discovery_key
        if self._project is not None:
            recent_scope = self._service.most_recent_pin_discovery_scope(project=self._project)
            if recent_scope:
                for option in options:
                    if option.key in available and self._service.discovery_scope_for_option(option) == recent_scope:
                        return option.key
        return next((option.key for option in options if not option.is_random), "random")

    def _refresh_preset_menus(self) -> None:
        if self._project is None:
            return
        appearance_panel = self._panel(AppearancePanelController)
        if appearance_panel is not None:
            labels = [entry.label for entry in self._service.list_appearance_preset_entries(self._project)]
            appearance_panel.set_recent_project_preset_labels(labels)
            appearance_panel.set_global_template_labels(
                [entry.label for entry in self._list_global_appearance_template_entries()]
            )
        filters_panel = self._panel(FiltersPanelController)
        if filters_panel is not None:
            labels = [entry.label for entry in self._service.list_filter_preset_entries(self._project)]
            filters_panel.set_recent_preset_labels(labels)
        slicer_panel = self._panel(SlicerPanelController)
        if slicer_panel is not None:
            labels = [entry.label for entry in self._service.list_slicer_preset_entries(self._project)]
            slicer_panel.set_recent_preset_labels(labels)
        transform_panel = self._panel(TransformPanelController)
        if transform_panel is not None:
            labels = [
                entry.label
                for entry in self._service.list_transform_preset_entries(self._project)
            ]
            transform_panel.set_recent_preset_labels(labels)
        canvas_layers_panel = self._panel(CanvasLayersPanelController)
        if canvas_layers_panel is not None:
            entries = self._service.list_camera_preset_entries(self._project)
            canvas_layers_panel.set_camera_presets(entries)
        layers_panel = self._panel(LayersSettingPanelController)
        if layers_panel is not None:
            variants = self._service.list_thumbnail_variants(self._project)
            self._disable_missing_thumbnail_variant(variants)
            selected = "none" if self._canvas_render_mode == "points_fast" else self._canvas_render_mode
            layers_panel.set_render_options(variants, selected=selected)
            layers_panel.set_thumbnail_size_px(self._canvas_thumbnail_size_px)
            layers_panel.set_thumbnail_limit(
                self._canvas_thumbnail_limit,
                automatic=self._canvas_thumbnail_limit_automatic,
            )
        snapshots_panel = self._panel(SnapshotsPanelController)
        if snapshots_panel is not None:
            snapshots_panel.set_snapshot_entries(self._service.list_snapshot_entries(self._project))

    def _handle_discovery_selection(self, selection_key: str) -> None:
        if self._activity_manager is not None:
            with self._activity_manager.busy("Loading discovery view…"):
                self._load_discovery_selection(selection_key)
            return
        self._load_discovery_selection(selection_key)

    def _load_discovery_selection(self, selection_key: str) -> None:
        self._discovery_debug("discovery_selection_start", selection_key=selection_key)
        if self._project is None:
            self._discovery_debug("discovery_selection_no_project", selection_key=selection_key)
            return
        option = self._discovery_options.get(selection_key)
        if option is None:
            self._discovery_debug("discovery_selection_missing_option", selection_key=selection_key)
            return
        metadata_ids = self._project_metadata_ids(self._project)
        if metadata_ids.size == 0:
            self._active_dataset = None
            self._source_dataset = None
            self._active_transform = None
            self._active_arrangement_ref = "original"
            transform_panel = self._panel(TransformPanelController)
            if transform_panel is not None:
                transform_panel.set_projection(
                    None,
                    populated=False,
                    transformable=self._active_projection_supports_arrangement_transform(),
                )
            self._reset_thumbnail_plan_state()
            if self._canvas_widget is not None:
                self._canvas_widget.clear()
            self._show_status("No items available. Import metadata or run an analysis on assets.")
            self._discovery_debug("discovery_selection_no_metadata", selection_key=selection_key)
            return
        if selection_key != self._active_discovery_key:
            self._flush_pin_persistence()
        self._active_discovery_key = selection_key
        discovery_panel = self._panel(DiscoveryPanelController)
        if discovery_panel is not None and discovery_panel.current_selection_key() != selection_key:
            discovery_panel.set_selected_key(selection_key, emit_signal=False)
        self._discovery_debug(
            "discovery_dataset_load_start",
            selection_key=selection_key,
            option_label=option.label,
            discovery_file=str(option.discovery_file) if option.discovery_file is not None else None,
            is_random=option.is_random,
        )
        self._set_pending_discovery_display_progress(96, "Loading saved projection coordinates...")
        self._transform_coordinator.cancel_pending()
        self._transform_request_key = None
        self._transform_request_fingerprint = None
        dataset = self._service.load_discovery_dataset_for_option(self._project, option, metadata_ids)
        self._discovery_debug(
            "discovery_dataset_loaded",
            coordinate_shape=tuple(dataset.coordinates.shape),
            metadata_shape=tuple(dataset.metadata_ids.shape),
            finite=bool(np.isfinite(dataset.coordinates).all()) if dataset.coordinates.size else True,
            min_xy=np.min(dataset.coordinates, axis=0).tolist() if dataset.coordinates.size else None,
            max_xy=np.max(dataset.coordinates, axis=0).tolist() if dataset.coordinates.size else None,
        )
        self._begin_thumbnail_scene_transition()
        source_coordinates = np.asarray(dataset.coordinates, dtype=np.float32).copy()
        source_metadata_ids = np.asarray(dataset.metadata_ids, dtype=np.int64).copy()
        source_coordinates.setflags(write=False)
        source_metadata_ids.setflags(write=False)
        self._source_dataset = DiscoveryDataset(
            coordinates=source_coordinates,
            metadata_ids=source_metadata_ids,
        )
        scope = self._service.discovery_scope_for_option(option)
        transformable = self._service.discovery_supports_arrangement_transform(
            option.discovery_type
        )
        if not transformable and self._project_has_storage():
            self._transform_service.repository(self._project).clear_current(scope)
        had_saved_transform = (
            transformable
            and self._transform_service.repository(self._project).has_current(scope)
            if self._project_has_storage()
            else False
        )
        restored_transform = (
            self._transform_service.load_current(self._project, scope, self._source_dataset)
            if transformable and self._project_has_storage()
            else None
        )
        self._active_transform = restored_transform
        if restored_transform is None:
            self._active_dataset = self._source_dataset
            self._active_arrangement_ref = "original"
        else:
            self._active_dataset = self._transform_service.dataset_for_result(restored_transform)
            self._active_arrangement_ref = (
                self._transform_service.relative_ref(self._project, restored_transform) or "original"
            )
        transform_panel = self._panel(TransformPanelController)
        if transform_panel is not None:
            transform_panel.set_projection(
                restored_transform.settings if restored_transform is not None else None,
                populated=bool(self._active_dataset.coordinates.shape[0]),
                transformable=transformable,
            )
        self._selected_polygon_area_id = None
        self._selected_polygon_metadata_ids.clear()
        self._active_pin_annotation_id = None
        self._load_polygon_areas_for_active_discovery()
        self._load_pins_for_active_discovery()
        self._discovery_debug("discovery_render_call", preserve_view=False, fit_view=True)
        self._set_pending_discovery_display_progress(97, "Preparing projection styles...")
        self._render_active_dataset(preserve_view=False, fit_view=True)
        if had_saved_transform and restored_transform is None:
            self._show_status("Projection coordinates changed; saved arrangement was reset.")
        else:
            self._show_status(f"Discovery loaded: {option.label}")
        self._discovery_debug("discovery_selection_done", selection_key=selection_key, option_label=option.label)
        self._refresh_current_snapshot_update_state()
        if self._pending_discovery_canvas_key == selection_key and self._canvas_widget is None:
            self._pending_discovery_canvas_ready = True
            self._complete_pending_discovery_display_if_ready()

    def _active_projection_supports_arrangement_transform(self) -> bool:
        option = self._discovery_options.get(self._active_discovery_key)
        return bool(
            option is not None
            and self._service.discovery_supports_arrangement_transform(
                option.discovery_type
            )
        )

    def _handle_transform_requested(self, settings: TransformSettings) -> None:
        if self._project is None or self._source_dataset is None or self._source_dataset.coordinates.shape[0] == 0:
            return
        option = self._discovery_options.get(self._active_discovery_key)
        if option is None:
            return
        if not self._service.discovery_supports_arrangement_transform(
            option.discovery_type
        ):
            self._show_status(
                "Transformations are unavailable for this coordinate-truthful projection."
            )
            return
        self._flush_pin_persistence()
        normalized = settings.normalized()
        panel = self._panel(TransformPanelController)
        if (
            normalized.snap_percent == 0
            and normalized.minimum_spacing == 0.0
        ):
            self._transform_coordinator.cancel_pending()
            self._transform_request_key = None
            self._transform_request_fingerprint = None
            self._transform_service.repository(self._project).clear_current(
                self._service.discovery_scope_for_option(option)
            )
            self._active_transform = None
            self._active_arrangement_ref = "original"
            self._active_dataset = self._source_dataset
            if panel is not None:
                panel.set_applied(normalized)
            self._begin_thumbnail_scene_transition()
            self._load_pins_for_active_discovery()
            self._render_active_dataset(preserve_view=True, fit_view=False)
            self._show_status("Original projection restored.")
            self._refresh_current_snapshot_update_state()
            return
        self._transform_request_key = self._active_discovery_key
        self._transform_request_fingerprint = source_fingerprint(
            self._source_dataset.coordinates, self._source_dataset.metadata_ids
        )
        if panel is not None:
            panel.set_processing(
                calculate_overlap=(
                    normalized.minimum_spacing > 0.0
                    and normalized.snap_percent < 100
                )
            )
        cached_grid = (
            self._active_transform.grid_coordinates
            if self._active_transform is not None
            and self._active_transform.settings.grid_aspect_preset == normalized.grid_aspect_preset
            else None
        )
        self._transform_coordinator.submit(
            self._project,
            self._service.discovery_scope_for_option(option),
            self._source_dataset,
            normalized,
            cached_grid,
        )

    def _handle_transform_cancel_requested(self) -> None:
        self._transform_coordinator.cancel_pending()
        self._transform_request_key = None
        self._transform_request_fingerprint = None
        panel = self._panel(TransformPanelController)
        if panel is not None:
            panel.set_projection(
                self._active_transform.settings if self._active_transform is not None else None,
                populated=self._source_dataset is not None and bool(self._source_dataset.coordinates.shape[0]),
                transformable=self._active_projection_supports_arrangement_transform(),
            )
        self._show_status("Rearrangement cancelled.")

    @QtCore.Slot(int, object)
    def _handle_transform_ready(self, _revision: int, payload: object) -> None:
        if not isinstance(payload, TransformResult) or self._project is None:
            return
        if (
            self._transform_request_key != self._active_discovery_key
            or self._source_dataset is None
            or payload.source_fingerprint != self._transform_request_fingerprint
        ):
            return
        option = self._discovery_options.get(self._active_discovery_key)
        if option is None:
            return
        try:
            self._transform_service.repository(self._project).set_current(
                self._service.discovery_scope_for_option(option), payload
            )
        except Exception as exc:
            self._handle_transform_failed(_revision, f"Could not publish arrangement: {exc}")
            return
        self._transform_request_key = None
        self._transform_request_fingerprint = None
        self._begin_thumbnail_scene_transition()
        self._active_transform = payload
        self._active_arrangement_ref = self._transform_service.relative_ref(self._project, payload) or "original"
        self._active_dataset = self._transform_service.dataset_for_result(payload)
        panel = self._panel(TransformPanelController)
        if panel is not None:
            panel.set_applied(payload.settings)
        self._load_pins_for_active_discovery()
        self._render_active_dataset(preserve_view=True, fit_view=False)
        self._show_status("Projection rearranged.")
        self._refresh_current_snapshot_update_state()

    @QtCore.Slot(int, int)
    def _handle_transform_progress(self, _revision: int, value: int) -> None:
        panel = self._panel(TransformPanelController)
        if panel is not None:
            panel.set_calculation_progress(value)

    @QtCore.Slot(int, str)
    def _handle_transform_failed(self, _revision: int, message: str) -> None:
        self._transform_request_key = None
        self._transform_request_fingerprint = None
        panel = self._panel(TransformPanelController)
        if panel is not None:
            panel.set_failure(f"Failed: {message}")
        self._show_status(f"Projection rearrangement failed: {message}")

    @QtCore.Slot(int)
    def _handle_transform_cancelled(self, _revision: int) -> None:
        self._handle_transform_cancel_requested()

    def _render_active_dataset(self, *, preserve_view: bool, fit_view: bool) -> None:
        if self._canvas_widget is not None:
            dataset = self._active_dataset
            if dataset is None:
                self._canvas_style_revision += 1
                for event in self._canvas_style_cancel_events.values():
                    event.set()
                self._canvas_style_render_options.clear()
                self._reset_thumbnail_plan_state()
                self._canvas_widget.clear()
                filters_panel = self._panel(FiltersPanelController)
                if filters_panel is not None:
                    filters_panel.set_filtered_item_counts(0, 0)
                self._discovery_debug("render_active_dataset_clear")
                return
            self._discovery_debug(
                "render_active_dataset_start",
                preserve_view=preserve_view,
                fit_view=fit_view,
                coordinate_shape=tuple(dataset.coordinates.shape),
                metadata_shape=tuple(dataset.metadata_ids.shape),
            )
            self._canvas_style_revision += 1
            revision = self._canvas_style_revision
            for event in self._canvas_style_cancel_events.values():
                event.set()
            request = self._canvas_style_request(dataset, revision=revision)
            self._canvas_style_render_options[revision] = (dataset, preserve_view, fit_view)
            if dataset.coordinates.shape[0] <= 10_000:
                try:
                    prepared = _canvas_style_request_with_metadata(
                        request,
                        self._metadata_access,
                    )
                    result = build_canvas_style(prepared)
                except Exception as exc:
                    self._on_canvas_style_failed(revision, f"{type(exc).__name__}: {exc}")
                    return
                self._on_canvas_style_ready(revision, result)
                return
            cancelled = threading.Event()
            task = _CanvasStyleBuildTask(
                request,
                cancelled,
                self._metadata_access,
            )
            task.signals.finished.connect(self._on_canvas_style_ready)
            task.signals.failed.connect(self._on_canvas_style_failed)
            task.signals.cancelled.connect(self._on_canvas_style_cancelled)
            self._canvas_style_cancel_events[revision] = cancelled
            self._canvas_style_tasks[revision] = task
            QtCore.QThreadPool.globalInstance().start(task)
            return

    def _canvas_style_request(self, dataset: DiscoveryDataset, *, revision: int) -> CanvasStyleRequest:
        filters_panel = self._panel(FiltersPanelController)
        filters = tuple(filters_panel.active_filters()) if filters_panel is not None else ()
        filter_matching = (
            filters_panel.filter_matching()
            if filters_panel is not None and hasattr(filters_panel, "filter_matching")
            else FilterMatching.ALL
        )
        slicer_panel = self._panel(SlicerPanelController)
        slicer = slicer_panel.active_selection() if slicer_panel is not None else {}
        slicer_enabled = bool(slicer.get("enabled"))
        slicer_column = str(slicer.get("column") or "").strip() if slicer_enabled else ""
        raw_slicer_values = slicer.get("values") if slicer_enabled else []
        slicer_values = (
            tuple(str(value) for value in raw_slicer_values)
            if isinstance(raw_slicer_values, list)
            else ()
        )
        visible_areas = [area for area in self._polygon_areas if area.points_visible] if self._show_marked_areas else []
        visible_areas.sort(key=lambda area: area.area_id == self._selected_polygon_area_id)
        overlays = tuple(
            AreaColorOverlay(
                metadata_ids=np.asarray(area.metadata_ids, dtype=np.int64),
                color_hex=area.color_hex,
            )
            for area in visible_areas
        )
        return CanvasStyleRequest(
            revision=int(revision),
            metadata_ids=dataset.metadata_ids,
            metadata_rows=self._metadata_row_map,
            point_style=self._current_point_style(),
            color_style=self._current_point_color_style(),
            border_style=self._current_border_style(),
            filters=filters,
            slicer_column=slicer_column,
            slicer_values=slicer_values,
            filtered_opacity=self._filtered_opacity,
            area_overlays=overlays,
            filter_matching=filter_matching,
        )

    @QtCore.Slot(int, object)
    def _on_canvas_style_ready(self, revision: int, result: object) -> None:
        self._canvas_style_tasks.pop(int(revision), None)
        self._canvas_style_cancel_events.pop(int(revision), None)
        options = self._canvas_style_render_options.pop(int(revision), None)
        if int(revision) != self._canvas_style_revision or options is None:
            return
        dataset, preserve_view, fit_view = options
        if dataset is not self._active_dataset or not isinstance(result, CanvasStyleResult):
            return
        canvas = self._canvas_widget
        if canvas is None:
            return
        self._set_pending_discovery_display_progress(98, "Building projection canvas...")
        self._thumbnail_preview_visibility = result.preview_visibility
        filters_panel = self._panel(FiltersPanelController)
        if filters_panel is not None:
            filters_panel.set_filtered_item_counts(
                int(np.count_nonzero(result.preview_visibility >= 0.5)),
                int(dataset.metadata_ids.size),
            )
        self._update_appearance_color_counts(dataset, result.preview_visibility)
        canvas.set_points(
            dataset.coordinates,
            colors=result.colors,
            size=result.point_size,
            visibility=result.visibility,
            metadata_ids=dataset.metadata_ids,
            edge_color=result.edge_colors,
            edge_width=result.edge_widths,
            preserve_view=preserve_view,
        )
        self._apply_tooltip_hover_eligibility()
        if fit_view:
            canvas.fit_view()
        self._apply_canvas_polygons()

    def _update_appearance_color_counts(
        self,
        dataset: DiscoveryDataset,
        preview_visibility: np.ndarray,
    ) -> None:
        panel = self._panel(AppearancePanelController)
        if panel is None:
            return
        visible = np.asarray(preview_visibility, dtype=np.float32).reshape(-1) >= 0.5
        visible_total = int(np.count_nonzero(visible))
        for target, style in (
            ("point", self._current_point_color_style()),
            ("border", self._current_border_style()),
        ):
            columns, numeric_columns = color_style_metadata_columns(style)
            values, _numeric = self._metadata_values_for_ids(
                dataset.metadata_ids,
                columns=columns,
                numeric_columns=numeric_columns,
            )
            if bool(style.get("custom")):
                labels = custom_color_labels(dataset.metadata_ids.size, style, values)
                total_counts = {
                    str(label): int(np.count_nonzero(labels == label))
                    for label in np.unique(labels).tolist()
                }
                visible_counts = {
                    str(label): int(np.count_nonzero((labels == label) & visible))
                    for label in np.unique(labels).tolist()
                }
                panel.set_conditional_color_counts(
                    target,
                    total_counts,
                    int(labels.size),
                    visible_counts,
                    visible_total,
                )
                continue
            column = str(style.get("column") or "").strip()
            column_values = values.get(column)
            if column_values is None:
                continue
            visible_counts = {
                (str(label).strip() or "(missing)"): int(np.count_nonzero((column_values == label) & visible))
                for label in np.unique(column_values).tolist()
            }
            panel.set_mapped_color_view_counts(target, column, visible_counts, visible_total)


    @QtCore.Slot(int, str)
    def _on_canvas_style_failed(self, revision: int, message: str) -> None:
        self._canvas_style_tasks.pop(int(revision), None)
        self._canvas_style_cancel_events.pop(int(revision), None)
        self._canvas_style_render_options.pop(int(revision), None)
        if int(revision) == self._canvas_style_revision:
            self._show_status(f"Canvas style preparation failed: {message}")
            if self._pending_discovery_canvas_key == self._active_discovery_key:
                self._fail_pending_discovery_display(
                    f"Projection was saved but its canvas style could not be prepared: {message}"
                )

    @QtCore.Slot(int)
    def _on_canvas_style_cancelled(self, revision: int) -> None:
        self._canvas_style_tasks.pop(int(revision), None)
        self._canvas_style_cancel_events.pop(int(revision), None)
        self._canvas_style_render_options.pop(int(revision), None)

    def _reset_thumbnail_plan_state(self, *, preserve_preview_visibility: bool = False) -> None:
        self._thumbnail_plan_coordinator.cancel_pending()
        self._active_thumbnail_plan = None
        self._active_thumbnail_metadata_id_set.clear()
        self._thumbnail_decode_px = 32
        self._thumbnail_fast_frames = 0
        self._thumbnail_slow_frames = 0
        if not preserve_preview_visibility:
            self._thumbnail_preview_visibility = np.empty((0,), dtype=np.float32)
        self._thumbnail_loading_pending_ids.clear()
        self._thumbnail_missing_ids.clear()
        self._thumbnail_failed_ids.clear()
        self._thumbnail_progressive_timer.stop()
        layers_panel = self._panel(LayersSettingPanelController)
        if layers_panel is not None:
            layers_panel.set_thumbnail_rendered_count(0)
            layers_panel.set_thumbnail_potential_count(0)

    def _begin_thumbnail_scene_transition(self) -> None:
        """Retire old preview membership before replacing canvas coordinates."""

        if (
            self._canvas_render_mode == "points_fast"
            or self._canvas_thumbnail_variant_id == "none"
        ):
            return
        self._thumbnail_request_timer.stop()
        self._thumbnail_rolling_request_timer.stop()
        self._thumbnail_progressive_timer.stop()
        self._thumbnail_push_timer.stop()
        self._thumbnail_request_generation += 1
        self._reset_thumbnail_plan_state()
        self._thumbnail_pending_entries.clear()
        canvas = self._canvas_widget
        if canvas is not None:
            canvas.set_thumbnail_candidates([], target_px=self._thumbnail_decode_px)
        self._update_thumbnail_loading_indicator()

    def _handle_render_selection_changed(self, value: str) -> None:
        render_value = str(value or "points_fast")
        if render_value == "none":
            render_value = "points_fast"
        self._canvas_render_mode = render_value
        if render_value == "points_fast":
            self._canvas_thumbnail_variant_id = "none"
            self._thumbnail_request_timer.stop()
            self._thumbnail_rolling_request_timer.stop()
            self._thumbnail_progressive_timer.stop()
            self._thumbnail_push_timer.stop()
            self._thumbnail_request_generation += 1
            self._reset_thumbnail_plan_state()
            self._thumbnail_pending_entries.clear()
            if self._canvas_widget is not None:
                self._canvas_widget.set_thumbnail_layer_config(enabled=False, variant_id="none")
                self._canvas_widget.set_thumbnail_candidates([], target_px=32)
                self._update_thumbnail_loading_indicator()
            # Ensure marker geometry is rebuilt after leaving thumbnail mode.
            self._render_active_dataset(preserve_view=True, fit_view=False)
            self._refresh_current_snapshot_update_state()
            return
        if self._canvas_thumbnail_variant_id != render_value:
            self._reset_thumbnail_plan_state()
            self._thumbnail_pending_entries.clear()
            self._update_thumbnail_loading_indicator()
            if self._thumbnail_service is not None:
                self._thumbnail_service.clear_source_cache()
        self._canvas_thumbnail_variant_id = render_value
        if self._canvas_widget is not None:
            zoom_far, zoom_close = self._thumbnail_zoom_thresholds()
            self._canvas_widget.set_thumbnail_layer_config(
                enabled=True,
                variant_id=self._canvas_thumbnail_variant_id,
                zoom_mid_threshold=zoom_far,
                zoom_close_threshold=zoom_close,
                thumbnail_size_px=float(self._canvas_thumbnail_size_px),
                max_visible_mid=self._canvas_thumbnail_limit,
                max_visible_close=self._canvas_thumbnail_limit,
                coloring=self._thumbnail_coloring_mode(),
                colors=self._thumbnail_colors_for_active_dataset(),
                border_colors=self._thumbnail_border_colors_for_active_dataset(),
                border_width_px=float(self._current_preview_style()["frame_width"]),
                color_opacity=float(self._current_preview_style()["body_opacity"]),
            )
        # Keep mode changes deterministic by rebinding point payload once.
        self._render_active_dataset(preserve_view=True, fit_view=False)
        self._queue_canvas_thumbnail_requests()
        self._refresh_current_snapshot_update_state()

    def _disable_missing_thumbnail_variant(self, variants: list[object]) -> None:
        if self._canvas_thumbnail_variant_id == "none":
            return
        available_ids = {str(getattr(variant, "variant_id", "") or "") for variant in variants}
        if self._canvas_thumbnail_variant_id in available_ids:
            return
        self._handle_render_selection_changed("points_fast")

    def _handle_fit_view(self) -> None:
        if self._canvas_widget is None:
            return
        self._canvas_widget.fit_view()
        self._show_status("Canvas fit to view.")
        self._refresh_current_snapshot_update_state()

    def _handle_mode_changed(self, mode: str) -> None:
        if self._canvas_widget is not None:
            self._canvas_widget.set_interaction_mode(mode)
            if mode != "selection":
                self._canvas_widget.clear_selected_metadata_id()
        if mode == "lasso":
            self._ensure_canvas_layer_checked("checkbox_show_marked_areas")
        if mode == "lasso":
            self._hide_tooltip_popup()

    def _ensure_canvas_layer_checked(self, checkbox_name: str) -> None:
        panel = self._panel(CanvasLayersPanelController)
        checkbox = None if panel is None else panel.widget.findChild(QtWidgets.QCheckBox, checkbox_name)
        if checkbox is not None and not checkbox.isChecked():
            checkbox.setChecked(True)

    def _handle_lasso_selection_changed(self, payload: dict) -> None:
        metadata_ids = payload.get("metadata_ids") if isinstance(payload, dict) else None
        count = len(metadata_ids) if isinstance(metadata_ids, list) else 0
        self._selected_polygon_metadata_ids = {int(value) for value in (metadata_ids or []) if isinstance(value, int)}
        self._show_status(f"Lasso selected {count} item(s).")
        if self._active_dataset is not None:
            self._render_active_dataset(preserve_view=True, fit_view=False)

    def _handle_canvas_pins_changed(self, _payload: object) -> None:
        if not self._applying_pin_display:
            self._apply_pin_display_elements()
        if self._applying_pin_display:
            return
        if self._restoring_project_state:
            return
        self._persist_pins_timer.start()
        self._refresh_current_snapshot_update_state()

    def _handle_canvas_point_selection_requested(
        self,
        metadata_id: int,
        global_pos: object,
    ) -> None:
        if self._selection_actions_dialog_controller is not None:
            return
        canvas = self._canvas_widget
        if canvas is None or not canvas.select_metadata_id(metadata_id):
            return
        self._hide_tooltip_popup()
        position = global_pos if isinstance(global_pos, QtCore.QPoint) else None
        defer_to_event_loop(
            lambda target=int(metadata_id), pos=position: self._show_selection_actions(
                target,
                pos,
            )
        )

    def _show_selection_actions(
        self,
        metadata_id: int,
        global_pos: QtCore.QPoint | None,
    ) -> None:
        canvas = self._canvas_widget
        if canvas is None or canvas.selected_metadata_id() != int(metadata_id):
            return
        controller = SelectionActionsDialogController()
        self._selection_actions_dialog_controller = controller
        self._selection_neighbor_group_changed = False
        tools_panel = self._panel(CanvasToolsPanelController)
        if tools_panel is not None:
            tools_panel.set_active_mode("pan")
        else:
            canvas.set_interaction_mode("pan")
        pin_changed = False

        def set_pin_scope(scope_mode: str) -> bool:
            nonlocal pin_changed
            changed = self._set_selection_pin_scope(metadata_id, scope_mode)
            pin_changed = pin_changed or changed
            return changed

        try:
            result = controller.exec(
                self._selection_actions_dialog_data(metadata_id),
                global_pos,
                self._request_selection_dialog_thumbnail,
                self._request_selection_dialog_neighbors,
                self._neighbor_search_coordinator.cancel_pending,
                self._save_selection_neighbor_group,
                self._focus_selection_neighbor,
                canvas.refresh_cursor,
                set_pin_scope,
            )
            action = result if isinstance(result, str) else getattr(result, "action", None)
            if action != "metadata":
                if not self._selection_neighbor_group_changed and not pin_changed:
                    self._show_status("Item selection cancelled.")
                return
            if self._open_metadata_callback is not None:
                self._open_metadata_callback(int(metadata_id))
        finally:
            self._neighbor_search_coordinator.cancel_pending(clear_cache=True)
            controller.close()
            self._selection_actions_dialog_controller = None
            if tools_panel is not None:
                tools_panel.set_active_mode("selection")
            else:
                canvas.set_interaction_mode("selection")
            canvas.clear_find_item_blink()
            canvas.clear_selected_metadata_id()

    def _set_selection_pin_scope(self, metadata_id: int, scope_mode: str) -> bool:
        canvas = self._canvas_widget
        scope = self._active_discovery_scope()
        requested_scope = str(scope_mode).strip().lower()
        if requested_scope not in {"", "project", "projection"}:
            return False
        if canvas is None or (requested_scope == "projection" and scope is None):
            self._show_status("A projection must be active to create an exclusive pin.")
            return False
        scope = scope or ""
        existing = next(
            (
                entry
                for entry in canvas.pinned_annotations_payload()
                if int(entry.get("metadata_id", -1)) == int(metadata_id)
            ),
            None,
        )
        if not requested_scope:
            if existing is None:
                return False
            changed = canvas.remove_pinned_annotation(
                str(existing.get("annotation_id") or "")
            )
            status = "Pin removed."
        elif existing is not None:
            changed = canvas.set_pinned_annotation_scope(
                str(existing.get("annotation_id") or ""),
                requested_scope,
                projection_scope=scope if requested_scope == "projection" else None,
            )
            status = (
                "Pin limited to this projection."
                if requested_scope == "projection"
                else "Pin shown in all projections."
            )
        else:
            annotation_id = (
                f"Pin_{metadata_id}_{uuid.uuid5(uuid.NAMESPACE_URL, scope).hex[:12]}"
                if requested_scope == "projection"
                else f"Pin_{metadata_id}"
            )
            changed = canvas.create_pinned_annotation(
                metadata_id,
                annotation_id=annotation_id,
                scope_mode=requested_scope,
                projection_scope=scope if requested_scope == "projection" else None,
                replace_existing=False,
            )
            status = (
                "Pin created for this projection."
                if requested_scope == "projection"
                else "Collection-wide pin created."
            )
        if not changed:
            return False
        if requested_scope:
            self._ensure_canvas_layer_checked("checkbox_show_pins")
            self._apply_pin_display_elements()
        if self._persist_pins_timer.isActive():
            self._persist_pins_timer.stop()
        self._persist_canvas_pins()
        self._show_status(status)
        return True

    def set_open_metadata_callback(
        self,
        callback: Callable[[int], None] | None,
    ) -> None:
        self._open_metadata_callback = callback

    def _selection_actions_dialog_data(self, metadata_id: int) -> SelectionActionsDialogData:
        columns = self._metadata_columns()
        requested = {column for column in columns if column != "id"}
        metadata_text, _numeric = self._metadata_values_for_ids(
            np.asarray([int(metadata_id)], dtype=np.int64),
            columns=requested,
            numeric_columns=set(),
        )
        rows: list[tuple[str, str]] = []
        for column in columns:
            if column == "id":
                rows.append(("ID", str(metadata_id)))
                continue
            values = metadata_text.get(column)
            rows.append((column, "" if values is None or values.size == 0 else str(values[0])))
        if not any(column == "id" for column in columns):
            rows.insert(0, ("ID", str(metadata_id)))
        panel = self._panel(LayersSettingPanelController)
        display = panel.pin_display_settings_payload() if panel is not None else {}
        variants = self._service.list_thumbnail_variants(self._project) if self._project is not None else []
        source_file_path = ""
        source_content_type = ""
        source_file_unavailable_reason = ""
        if self._project is not None:
            try:
                media_asset = self._service.linked_media_asset(
                    self._project,
                    int(metadata_id),
                )
            except (OSError, sqlite3.Error, ValueError):
                media_asset = None
            if media_asset is not None:
                source_file_path = str(media_asset.absolute_path)
                source_content_type = media_asset.content_type.value
                if not media_asset.absolute_path.is_file():
                    source_file_unavailable_reason = (
                        "The linked source file is no longer available."
                    )
        dataset = self._active_dataset
        option = self._discovery_options.get(self._active_discovery_key)
        neighbor_source_label = ""
        neighbor_unavailable_reason = ""
        neighbor_candidate_ids: tuple[int, ...] = ()
        neighbor_visible_ids: tuple[int, ...] = ()
        neighbor_embedding_options: tuple[
            tuple[str, str, int], ...
        ] = ()
        neighbor_default_embedding_id = ""
        if self._project is None or dataset is None or option is None:
            neighbor_unavailable_reason = "No active projection is available."
        else:
            try:
                embedding_sources = self._service.list_neighbor_embedding_sources(
                    self._project
                )
            except (OSError, sqlite3.Error, ValueError):
                embedding_sources = ()
            source_key = str(option.source.get("key") or "")
            current_run_id = (
                source_key.removeprefix("run:").strip()
                if source_key.startswith("run:")
                else source_key if source_key.startswith("run_") else ""
            )
            neighbor_embedding_options = tuple(
                (source.run_id, source.label, source.item_count)
                for source in embedding_sources
            )
            available_ids = {source.run_id for source in embedding_sources}
            if current_run_id in available_ids:
                neighbor_default_embedding_id = current_run_id
            elif embedding_sources:
                neighbor_default_embedding_id = embedding_sources[0].run_id
            if not embedding_sources:
                neighbor_unavailable_reason = "No completed embeddings are available."
            else:
                selected_source = next(
                    source
                    for source in embedding_sources
                    if source.run_id == neighbor_default_embedding_id
                )
                neighbor_source_label = selected_source.label
                neighbor_candidate_ids = tuple(
                    int(value) for value in dataset.metadata_ids.tolist()
                )
                visibility = self._preview_visibility_for_dataset(dataset)
                neighbor_visible_ids = tuple(
                    int(value)
                    for value in dataset.metadata_ids[
                        np.asarray(visibility, dtype=np.float32) >= 0.5
                    ].tolist()
                )
        return SelectionActionsDialogData(
            metadata_id=int(metadata_id),
            metadata_rows=tuple(rows),
            thumbnail_options=tuple((variant.variant_id, variant.label) for variant in variants),
            pin_scope_mode=self._metadata_pin_scope_mode(metadata_id),
            neighbor_source_label=neighbor_source_label,
            neighbor_unavailable_reason=neighbor_unavailable_reason,
            neighbor_candidate_ids=neighbor_candidate_ids,
            neighbor_visible_ids=neighbor_visible_ids,
            neighbor_embedding_options=neighbor_embedding_options,
            neighbor_default_embedding_id=neighbor_default_embedding_id,
            neighbor_thumbnail_variant_id=str(
                display.get("tooltip_thumbnail_variant_id") or "none"
            ),
            source_file_path=source_file_path,
            source_content_type=source_content_type,
            source_file_unavailable_reason=source_file_unavailable_reason,
        )

    def _metadata_pin_scope_mode(self, metadata_id: int) -> str:
        target = int(metadata_id)
        if self._canvas_widget is not None:
            for entry in self._canvas_widget.pinned_annotations_payload():
                try:
                    if int(entry.get("metadata_id")) == target:
                        return (
                            "projection"
                            if str(entry.get("scope_mode") or "").strip().lower()
                            == "projection"
                            else "project"
                        )
                except (TypeError, ValueError):
                    continue
        return ""

    def _request_selection_dialog_thumbnail(
        self,
        target: str,
        metadata_id: int,
        variant_id: str,
        target_px: int,
        prefer_original: bool = False,
    ) -> None:
        del target
        if self._thumbnail_service is None:
            return
        self._thumbnail_service.request(
            DecodeRequest(
                metadata_id=int(metadata_id),
                target_px=max(1, int(target_px)),
                variant_id=str(variant_id),
                prefer_original=bool(prefer_original),
            )
        )

    def _request_selection_dialog_neighbors(
        self,
        request: NeighborSearchRequest,
    ) -> int:
        project = self._project
        dataset = self._active_dataset
        option = self._discovery_options.get(self._active_discovery_key)
        if project is None or dataset is None or option is None:
            raise ValueError("No active projection is available.")
        if request.embedding_run_id:
            return self._neighbor_search_coordinator.submit(
                cache_key=f"embedding:{request.embedding_run_id}",
                loader=lambda: self._service.prepare_embedding_neighbor_index_for_run(
                    project,
                    request.embedding_run_id,
                ),
                request=request,
            )
        candidate_ids = np.asarray(dataset.metadata_ids, dtype=np.int64).copy()
        return self._neighbor_search_coordinator.submit(
            cache_key=str(self._active_discovery_key),
            loader=lambda: self._service.prepare_embedding_neighbor_index(
                project,
                option,
                candidate_ids,
            ),
            request=request,
        )

    def _save_selection_neighbor_group(
        self,
        label: str,
        member_metadata_ids: tuple[int, ...],
    ) -> str:
        if self._project is None:
            raise ValueError("Open a project before creating a custom group.")
        set_name, group_name, member_count = self._service.save_neighbor_snapshot_group(
            project=self._project,
            group_name=label,
            member_metadata_ids=member_metadata_ids,
        )
        self._selection_neighbor_group_changed = True
        self._notify_custom_groups_changed()
        message = f"Saved {member_count} nearest neighbors to '{set_name} / {group_name}'."
        self._show_status(message)
        return message

    def _focus_selection_neighbor(self, metadata_id: int | None) -> None:
        if metadata_id is None:
            if self._canvas_widget is not None:
                self._canvas_widget.clear_find_item_blink()
            return
        if self._handle_find_item_text_changed(str(int(metadata_id))):
            self._show_status(f"Centered nearest neighbor {int(metadata_id)} on the canvas.")
        else:
            self._show_status(
                f"Nearest neighbor {int(metadata_id)} is unavailable on the active canvas."
            )

    @QtCore.Slot(int, object)
    def _handle_selection_neighbor_result(self, revision: int, payload: object) -> None:
        controller = self._selection_actions_dialog_controller
        if controller is not None and isinstance(payload, NeighborSearchResult):
            controller.apply_neighbor_result(int(revision), payload)

    @QtCore.Slot(int, str)
    def _handle_selection_neighbor_failure(self, revision: int, message: str) -> None:
        controller = self._selection_actions_dialog_controller
        if controller is not None:
            controller.apply_neighbor_failure(int(revision), str(message))

    def _handle_pin_context_requested(self, annotation_id: str, global_pos: object) -> None:
        canvas = self._canvas_widget
        annotation_key = str(annotation_id or "").strip()
        if canvas is None or not annotation_key:
            return
        pin_payload = next(
            (
                entry
                for entry in canvas.pinned_annotations_payload()
                if str(entry.get("annotation_id") or "").strip() == annotation_key
            ),
            None,
        )
        if pin_payload is None:
            return
        projection_only = (
            str(pin_payload.get("scope_mode") or "").strip().lower() == "projection"
        )
        menu = QtWidgets.QMenu(self._widget)
        action_show_item = menu.addAction("Show Item…")
        action_all_projections = menu.addAction("In All Projections")
        action_this_projection = menu.addAction("This Projection Only")
        menu.addSeparator()
        action_remove = menu.addAction("Remove Pin")
        action_all_projections.setCheckable(True)
        action_this_projection.setCheckable(True)
        action_all_projections.setChecked(not projection_only)
        action_this_projection.setChecked(projection_only)
        scope = self._active_discovery_scope()
        action_this_projection.setEnabled(scope is not None)
        position = global_pos if isinstance(global_pos, QtCore.QPoint) else QtGui.QCursor.pos()
        selected_action = menu.exec(position)
        if selected_action == action_show_item:
            try:
                metadata_id = int(pin_payload.get("metadata_id"))
            except (TypeError, ValueError):
                return
            if not canvas.select_metadata_id(metadata_id):
                self._show_status("The pinned item is unavailable in this projection.")
                return
            defer_to_event_loop(
                lambda target=metadata_id, pos=position: self._show_selection_actions(
                    target,
                    pos,
                )
            )
            return
        if selected_action == action_remove:
            if canvas.remove_pinned_annotation(annotation_key):
                self._show_status("Removed pin.")
            return
        if selected_action == action_all_projections:
            changed = canvas.set_pinned_annotation_scope(annotation_key, "project")
            status = "Pin shown in all projections."
        elif selected_action == action_this_projection and scope is not None:
            changed = canvas.set_pinned_annotation_scope(
                annotation_key,
                "projection",
                projection_scope=scope,
            )
            status = "Pin shown in this projection only."
        else:
            return
        if not changed:
            return
        if self._persist_pins_timer.isActive():
            self._persist_pins_timer.stop()
        self._persist_canvas_pins()
        self._show_status(status)

    def _handle_pins_visibility_toggled(self, visible: bool) -> None:
        if self._canvas_widget is not None:
            self._canvas_widget.set_pinned_annotations_visible(bool(visible))
        if not self._applying_project_layer_state and not self._restoring_project_state:
            self._persist_canvas_pins()
        self._show_status(f"Pins layer {'shown' if visible else 'hidden'}.")
        self._refresh_current_snapshot_update_state()

    def _handle_tooltips_visibility_toggled(self, visible: bool) -> None:
        self._queue_project_exploration_settings_save()
        self._tooltip_layer_enabled = bool(visible)
        if self._canvas_widget is not None:
            self._canvas_widget.set_hover_tooltips_enabled(self._tooltip_layer_enabled)
        if not self._tooltip_layer_enabled:
            self._hide_tooltip_popup()
        self._refresh_current_snapshot_update_state()

    def _handle_canvas_camera_changed(self, payload: object) -> None:
        del payload
        self._sync_camera_combo_to_canvas()
        self._sync_viewport_aspect_to_canvas()
        self._queue_canvas_thumbnail_requests(rolling=True)
        self._refresh_current_snapshot_update_state()

    def _handle_viewport_aspect_changed(self, preset: str) -> None:
        if self._canvas_widget is None:
            return
        self._canvas_widget.set_viewport_aspect_preset(preset)
        self._sync_viewport_aspect_to_canvas()
        self._refresh_current_snapshot_update_state()

    def _handle_crop_bars_hidden_changed(self, hidden: bool) -> None:
        if self._canvas_widget is not None:
            self._canvas_widget.set_crop_bars_visible(not bool(hidden))
        self._refresh_current_snapshot_update_state()

    def _handle_canvas_scene_build_finished(self, _revision: int) -> None:
        if (
            self._canvas_render_mode != "points_fast"
            and self._canvas_thumbnail_variant_id != "none"
        ):
            self._configure_canvas_thumbnail_layer()
            self._queue_canvas_thumbnail_requests()
        if self._pending_discovery_canvas_key == self._active_discovery_key:
            canvas = self._canvas_widget
            wait_for_native_upload = bool(
                canvas is not None and canvas.native_upload_completion_available()
            )
            if wait_for_native_upload:
                self._set_pending_discovery_display_progress(99, "Uploading projection to the GPU...")
            else:
                self._mark_pending_discovery_canvas_ready()

    def _handle_canvas_scene_build_failed(self, _revision: int, message: str) -> None:
        if self._pending_discovery_canvas_key != self._active_discovery_key:
            return
        self._fail_pending_discovery_display(f"Projection was saved but could not be displayed: {message}")

    def _handle_canvas_static_upload_finished(self, point_count: int) -> None:
        if self._pending_discovery_canvas_key != self._active_discovery_key:
            return
        dataset = self._active_dataset
        if dataset is None or int(point_count) != int(dataset.coordinates.shape[0]):
            return
        self._mark_pending_discovery_canvas_ready()

    def _handle_canvas_render_timing(self, frame_ms: float) -> None:
        plan = self._active_thumbnail_plan
        canvas = self._canvas_widget
        if plan is None or canvas is None or canvas.camera_interaction_active():
            return
        policy = self._thumbnail_runtime_policy
        frame = max(0.0, float(frame_ms))
        if frame > policy.reduce_frame_ms:
            self._thumbnail_slow_frames += 1
            self._thumbnail_fast_frames = 0
            if self._thumbnail_slow_frames >= policy.reduce_after_frames:
                self._thumbnail_slow_frames = 0
                if self._thumbnail_decode_px > 32:
                    self._thumbnail_decode_px = 32
                    self._thumbnail_request_timer.start()
            return
        if frame < policy.increase_frame_ms:
            self._thumbnail_fast_frames += 1
            self._thumbnail_slow_frames = 0
            if self._thumbnail_fast_frames >= policy.increase_after_frames:
                self._thumbnail_fast_frames = 0
                self._progress_thumbnail_plan()
            return
        self._thumbnail_fast_frames = 0
        self._thumbnail_slow_frames = 0

    def _handle_thumbnail_runtime_status(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        self._thumbnail_native_status = dict(payload)
        active = self._active_thumbnail_plan
        if active is None or int(payload.get("revision", -1)) != active.request_revision:
            return
        layers_panel = self._panel(LayersSettingPanelController)
        if layers_panel is not None:
            layers_panel.set_thumbnail_rendered_count(
                max(0, int(payload.get("drawn", 0)))
            )
        self._update_thumbnail_loading_indicator()
        if (
            int(payload.get("queued", 0)) <= 0
            and int(payload.get("decoding", 0)) <= 0
        ):
            self._thumbnail_progressive_timer.start()

    def _progress_thumbnail_plan(self) -> None:
        plan = self._active_thumbnail_plan
        canvas = self._canvas_widget
        if plan is None or canvas is None or canvas.camera_interaction_active():
            self._thumbnail_progressive_timer.start()
            return
        if plan.planned_visible_count <= 0:
            return
        desired_decode = self._desired_thumbnail_decode_px()
        resident = int(self._thumbnail_native_status.get("resident", plan.planned_visible_count))
        if self._thumbnail_decode_px < desired_decode and resident >= max(
            1,
            int(plan.planned_visible_count * 0.8),
        ):
            self._thumbnail_decode_px = desired_decode
            self._thumbnail_request_timer.start()

    def _desired_thumbnail_decode_px(self) -> int:
        dpr = 1.0
        if self._canvas_widget is not None:
            try:
                dpr = max(1.0, float(self._canvas_widget.devicePixelRatioF()))
            except (AttributeError, TypeError, ValueError):
                dpr = 1.0
        physical_px = max(1.0, float(self._canvas_thumbnail_size_px) * dpr)
        if physical_px <= 32.0:
            desired = 32
        elif physical_px <= 64.0:
            desired = 64
        else:
            desired = 128
        plan = self._active_thumbnail_plan
        if plan is None:
            return desired
        planned_count = max(1, int(plan.renderer_indices.size))
        atlas_budget_bytes = (
            max(16, int(self._thumbnail_runtime_policy.gpu_cache_mb))
            * 1024
            * 1024
        )
        usable_bytes = int(float(atlas_budget_bytes) * 0.95)
        for tier in (desired, 64, 32):
            if tier > desired:
                continue
            estimated_bytes = planned_count * tier * tier * 4
            if estimated_bytes <= usable_bytes:
                return tier
        return 32

    def _forced_thumbnail_metadata_ids(self) -> list[int]:
        output: list[int] = []
        canvas = self._canvas_widget
        if canvas is not None:
            found_id = canvas.active_find_metadata_id()
            if found_id is not None:
                output.append(int(found_id))
            for item in canvas.pinned_annotations_payload():
                if not bool(item.get("visible", True)):
                    continue
                try:
                    output.append(int(item.get("metadata_id")))
                except (TypeError, ValueError):
                    continue
        if self._hovered_metadata_id is not None:
            output.append(int(self._hovered_metadata_id))
        return list(dict.fromkeys(output))

    def _sync_camera_combo_to_canvas(self) -> None:
        if self._applying_camera_preset or self._canvas_widget is None:
            return
        is_default_camera = getattr(self._canvas_widget, "is_default_camera", None)
        if not callable(is_default_camera):
            return
        canvas_layers_panel = self._panel(CanvasLayersPanelController)
        if canvas_layers_panel is None:
            return
        if is_default_camera():
            canvas_layers_panel.show_default_camera()
        else:
            canvas_layers_panel.show_new_camera()

    def _sync_viewport_aspect_to_canvas(self) -> None:
        if self._canvas_widget is None:
            return
        preset_getter = getattr(self._canvas_widget, "viewport_aspect_preset", None)
        if not callable(preset_getter):
            return
        canvas_layers_panel = self._panel(CanvasLayersPanelController)
        if canvas_layers_panel is not None:
            canvas_layers_panel.set_viewport_aspect_preset(preset_getter())

    def _queue_canvas_thumbnail_requests(self, *, rolling: bool = False) -> None:
        if self._canvas_render_mode == "points_fast":
            return
        if self._canvas_thumbnail_variant_id == "none":
            return
        if self._thumbnail_service is None or self._canvas_widget is None:
            return
        if rolling:
            self._thumbnail_progressive_timer.stop()
        self._thumbnail_request_timer.start()

    def _request_visible_canvas_thumbnails(self) -> None:
        service = self._thumbnail_service
        canvas = self._canvas_widget
        dataset = self._active_dataset
        if service is None or canvas is None or dataset is None:
            return
        if self._canvas_render_mode == "points_fast" or self._canvas_thumbnail_variant_id == "none":
            return
        if canvas.camera_interaction_active():
            return
        scene = canvas.thumbnail_scene_snapshot()
        if scene is None or scene.point_count != dataset.coordinates.shape[0]:
            return
        state_getter = getattr(canvas, "camera_preset_state", None)
        viewport_state = state_getter() if callable(state_getter) else canvas.camera_state()
        try:
            viewport = (
                float(viewport_state["x_min"]),
                float(viewport_state["x_max"]),
                float(viewport_state["y_min"]),
                float(viewport_state["y_max"]),
            )
        except (KeyError, TypeError, ValueError):
            return
        visibility = self._thumbnail_preview_visibility
        if visibility.shape[0] != scene.point_count:
            visibility = np.asarray(scene.visibility > 0.0, dtype=np.float32)
        planning_viewport = expand_thumbnail_viewport(
            viewport,
            self._thumbnail_render_config.prefetch_margin_fraction,
        )
        frame_getter = getattr(canvas, "active_viewport_frame", None)
        if callable(frame_getter):
            viewport_frame = frame_getter()
            canvas_size = (
                float(max(1.0, viewport_frame.width())),
                float(max(1.0, viewport_frame.height())),
            )
        else:
            canvas_size = (float(max(1, canvas.width())), float(max(1, canvas.height())))
        revision = self._thumbnail_plan_coordinator.begin_request()
        request = ThumbnailPlanRequest(
            scene_revision=int(scene.revision),
            request_revision=revision,
            points=scene.points,
            metadata_ids=scene.metadata_ids,
            visibility=visibility,
            spatial_index=scene.spatial_index,
            viewport=viewport,
            planning_viewport=planning_viewport,
            canvas_size=canvas_size,
            device_pixel_ratio=float(max(0.5, canvas.devicePixelRatioF())),
            display_px=max(1, int(self._canvas_thumbnail_size_px)),
            decode_px=max(1, int(self._thumbnail_decode_px)),
            screen_coverage_budget=self._thumbnail_runtime_policy.screen_coverage_budget,
            thumbnail_limit=self._canvas_thumbnail_limit,
            minimum_thumbnail_limit=self._thumbnail_limit_bounds()[0],
            maximum_thumbnail_limit=self._thumbnail_limit_bounds()[1],
            forced_metadata_ids=tuple(self._forced_thumbnail_metadata_ids()),
            thumbnail_lod_levels=scene.thumbnail_lod_levels,
            world_bounds=scene.world_bounds,
        )
        self._thumbnail_plan_coordinator.submit(request)

    @QtCore.Slot(int, object)
    def _on_thumbnail_plan_ready(self, revision: int, payload: object) -> None:
        if not isinstance(payload, ThumbnailViewportPlan):
            return
        canvas = self._canvas_widget
        service = self._thumbnail_service
        scene = None if canvas is None else canvas.thumbnail_scene_snapshot()
        if (
            canvas is None
            or service is None
            or scene is None
            or payload.scene_revision != scene.revision
            or self._canvas_thumbnail_variant_id == "none"
        ):
            self._thumbnail_stale_results += 1
            return
        if canvas.camera_interaction_active():
            self._thumbnail_stale_results += 1
            return
        previous_plan = self._active_thumbnail_plan
        reusable_sources = bool(
            previous_plan is not None
            and previous_plan.scene_revision == payload.scene_revision
            and previous_plan.decode_px == payload.decode_px
        )
        previous_metadata_ids = (
            self._active_thumbnail_metadata_id_set.copy()
            if reusable_sources
            else set()
        )
        self._active_thumbnail_plan = payload
        layers_panel = self._panel(LayersSettingPanelController)
        if layers_panel is not None:
            layers_panel.set_thumbnail_rendered_count(payload.planned_visible_count)
            layers_panel.set_thumbnail_potential_count(payload.desired_visible_count)
        cache_metadata_ids = [int(value) for value in payload.metadata_ids.tolist()]
        forced_ids = [int(value) for value in payload.forced_metadata_ids.tolist()]
        visible_ids = [int(value) for value in payload.visible_metadata_ids.tolist()]
        display_metadata_ids = list(dict.fromkeys((*forced_ids, *visible_ids)))
        self._active_thumbnail_metadata_id_set = set(cache_metadata_ids)
        self._thumbnail_loading_pending_ids = set(display_metadata_ids)
        if reusable_sources:
            self._thumbnail_missing_ids.intersection_update(self._active_thumbnail_metadata_id_set)
            self._thumbnail_failed_ids.intersection_update(self._active_thumbnail_metadata_id_set)
        else:
            self._thumbnail_missing_ids.clear()
            self._thumbnail_failed_ids.clear()
        canvas.set_thumbnail_viewport_plan(
            revision=payload.request_revision,
            metadata_ids=display_metadata_ids,
            forced_metadata_ids=forced_ids,
            planning_viewport=payload.planning_viewport,
            display_px=payload.display_px,
            decode_px=payload.decode_px,
        )
        canvas.set_thumbnail_plan_diagnostics(
            desired_visible=payload.desired_visible_count,
            planned_visible=payload.planned_visible_count,
            planned_prefetch=payload.planned_prefetch_count,
            budget_suppressed=payload.budget_suppressed_count,
            estimated_screen_cost=payload.estimated_screen_cost,
            adaptive_limit=payload.adaptive_limit,
            minimum_spacing_px=payload.minimum_spacing_px,
            decode_px=payload.decode_px,
            stale_discarded=self._thumbnail_stale_results,
            missing=len(self._thumbnail_missing_ids),
            failed=len(self._thumbnail_failed_ids),
        )
        if not cache_metadata_ids:
            self._update_thumbnail_loading_indicator()
            return
        if canvas.supports_native_thumbnail_sources():
            added_metadata_ids = [
                metadata_id
                for metadata_id in cache_metadata_ids
                if metadata_id not in previous_metadata_ids
            ]
            if added_metadata_ids:
                service.request_sources(
                    revision=payload.request_revision,
                    metadata_ids=added_metadata_ids,
                    variant_id=self._canvas_thumbnail_variant_id,
                    target_px=payload.decode_px,
                )
        else:
            missing_ids = canvas.missing_thumbnail_metadata_ids(
                display_metadata_ids,
                target_px=payload.decode_px,
            )
            self._thumbnail_loading_pending_ids = set(missing_ids)
            for metadata_id in missing_ids:
                service.request(
                    DecodeRequest(
                        metadata_id=int(metadata_id),
                        target_px=payload.decode_px,
                        variant_id=self._canvas_thumbnail_variant_id,
                    )
                )
        self._update_thumbnail_loading_indicator()
        self._thumbnail_progressive_timer.start()

    @QtCore.Slot(int, str)
    def _on_thumbnail_plan_failed(self, revision: int, message: str) -> None:
        del revision
        self._show_status(f"Thumbnail planning failed: {message}")

    @QtCore.Slot()
    def _on_thumbnail_plan_stale_result(self) -> None:
        self._thumbnail_stale_results += 1

    def _update_thumbnail_loading_indicator(self) -> None:
        if self._canvas_widget is None:
            return
        active_plan = self._active_thumbnail_plan
        native_revision = int(self._thumbnail_native_status.get("revision", -1))
        if active_plan is not None and native_revision == active_plan.request_revision:
            pending_count = max(
                0,
                int(self._thumbnail_native_status.get("queued", 0))
                + int(self._thumbnail_native_status.get("decoding", 0)),
            )
        else:
            pending_count = len(self._thumbnail_loading_pending_ids)
        active = bool(
            pending_count > 0
            and self._canvas_render_mode != "points_fast"
            and self._canvas_thumbnail_variant_id != "none"
        )
        if active:
            self._canvas_widget.set_thumbnail_loading_indicator(
                True,
                f"Preparing thumbnails ({pending_count})",
            )
        else:
            self._canvas_widget.set_thumbnail_loading_indicator(False)

    def _configure_canvas_thumbnail_layer(self) -> None:
        if self._canvas_widget is None:
            return
        if self._canvas_thumbnail_variant_id == "none":
            self._canvas_widget.set_thumbnail_layer_config(enabled=False, variant_id="none")
            return
        zoom_far, zoom_close = self._thumbnail_zoom_thresholds()
        self._canvas_widget.set_thumbnail_layer_config(
            enabled=True,
            variant_id=self._canvas_thumbnail_variant_id,
            zoom_mid_threshold=zoom_far,
            zoom_close_threshold=zoom_close,
            thumbnail_size_px=float(self._canvas_thumbnail_size_px),
            max_visible_mid=self._canvas_thumbnail_limit,
            max_visible_close=self._canvas_thumbnail_limit,
            coloring=self._thumbnail_coloring_mode(),
            colors=self._thumbnail_colors_for_active_dataset(),
            border_colors=self._thumbnail_border_colors_for_active_dataset(),
            border_width_px=float(self._current_preview_style()["frame_width"]),
            color_opacity=float(self._current_preview_style()["body_opacity"]),
            render_margin_fraction=self._thumbnail_render_config.prefetch_margin_fraction,
        )

    def _thumbnail_colors_for_active_dataset(self) -> np.ndarray | None:
        preview_style = self._current_preview_style()
        source = str(preview_style["fill_color_source"])
        if source == "none" or self._active_dataset is None:
            return None
        return self._thumbnail_colors_for_source(
            source,
            opacity=float(preview_style["fill_opacity"]),
        )

    def _thumbnail_border_colors_for_active_dataset(self) -> np.ndarray | None:
        preview_style = self._current_preview_style()
        source = str(preview_style["border_color_source"])
        if source == "none" or self._active_dataset is None:
            return None
        return self._thumbnail_colors_for_source(
            source,
            opacity=float(preview_style["frame_opacity"]),
        )

    def _thumbnail_coloring_mode(self) -> str:
        preview_style = self._current_preview_style()
        has_fill = preview_style["fill_color_source"] != "none"
        has_border = preview_style["border_color_source"] != "none"
        if has_fill and has_border:
            return "tint_border"
        if has_fill:
            return "tint"
        if has_border:
            return "border"
        return "none"

    def _current_preview_style(self) -> dict[str, object]:
        panel = self._panel(AppearancePanelController)
        if panel is None:
            return {
                "fill_color_source": "none",
                "border_color_source": "point",
                "fill_opacity": 0.7,
                "body_opacity": 1.0,
                "frame_width": 4.0,
                "frame_opacity": 1.0,
            }
        return panel.preview_style()

    def _thumbnail_colors_for_source(self, source: str, *, opacity: float) -> np.ndarray | None:
        dataset = self._active_dataset
        if dataset is None or source == "none":
            return None
        alpha = float(np.clip(float(opacity), 0.0, 1.0))
        if source == "border":
            return self._compute_border_colors(dataset, alpha=alpha)
        colors = self._compute_point_colors(dataset, alpha=alpha)
        return self._apply_polygon_point_colors(dataset, colors)

    def _thumbnail_zoom_thresholds(self) -> tuple[float, float]:
        return self._thumbnail_render_config.zoom_thresholds_for_thumbnail_size(
            float(self._canvas_thumbnail_size_px)
        )

    def _on_thumbnail_ready_from_worker(
        self,
        payload: DecodedThumbnail | ThumbnailDecodeFailure | None,
    ) -> None:
        self._thumbnail_ready_bridge.ready.emit(payload)

    def _on_thumbnail_source_batch_from_worker(self, payload: ThumbnailSourceBatch) -> None:
        self._thumbnail_ready_bridge.ready.emit(payload)

    def _handle_thumbnail_ready(self, payload: object) -> None:
        if isinstance(payload, ThumbnailSourceBatch):
            self._handle_thumbnail_source_batch(payload)
            return
        dialog_controller = self._selection_actions_dialog_controller
        if dialog_controller is not None and isinstance(
            payload,
            (DecodedThumbnail, ThumbnailDecodeFailure),
        ):
            dialog_controller.apply_thumbnail(payload)
        if isinstance(payload, ThumbnailDecodeFailure):
            self._handle_thumbnail_decode_failure(payload)
            return
        if not isinstance(payload, DecodedThumbnail):
            return
        if self._canvas_widget is None:
            return
        self._apply_ready_pin_thumbnail(payload)
        self._apply_ready_tooltip_thumbnail(payload)
        if self._canvas_render_mode == "points_fast":
            return
        if str(payload.variant_id or "none") != self._canvas_thumbnail_variant_id:
            return
        active_plan = self._active_thumbnail_plan
        if (
            active_plan is None
            or int(payload.target_px) != active_plan.decode_px
            or int(payload.metadata_id) not in self._active_thumbnail_metadata_id_set
        ):
            self._thumbnail_stale_results += 1
            return
        self._thumbnail_loading_pending_ids.discard(int(payload.metadata_id))
        self._update_thumbnail_loading_indicator()
        self._thumbnail_pending_entries.append(
            {
                "metadata_id": int(payload.metadata_id),
                "target_px": int(payload.target_px),
                "width": int(payload.width),
                "height": int(payload.height),
                "rgba": payload.rgba,
            }
        )
        self._thumbnail_push_timer.start()

    def _handle_thumbnail_source_batch(self, payload: ThumbnailSourceBatch) -> None:
        plan = self._active_thumbnail_plan
        canvas = self._canvas_widget
        if (
            plan is None
            or canvas is None
            or payload.revision != plan.request_revision
            or str(payload.variant_id or "none") != self._canvas_thumbnail_variant_id
            or int(payload.target_px) != plan.decode_px
        ):
            self._thumbnail_stale_results += 1
            return
        missing = {int(value) for value in payload.missing_metadata_ids}
        self._thumbnail_missing_ids.update(missing)
        self._thumbnail_loading_pending_ids.difference_update(missing)
        entries = [
            {
                "metadata_id": int(source.metadata_id),
                "path": str(source.path),
                "mtime_ns": source.mtime_ns,
                "target_px": int(payload.target_px),
            }
            for source in payload.sources
        ]
        entries.extend(
            {
                "metadata_id": int(metadata_id),
                "path": "",
                "mtime_ns": None,
                "target_px": int(payload.target_px),
                "missing": True,
            }
            for metadata_id in payload.missing_metadata_ids
        )
        canvas.push_thumbnail_sources(
            self._canvas_thumbnail_variant_id,
            entries,
            revision=payload.revision,
        )
        if payload.failed_message:
            self._show_status(f"Thumbnail source lookup failed: {payload.failed_message}")
        canvas.set_thumbnail_plan_diagnostics(
            desired_visible=plan.desired_visible_count,
            planned_visible=plan.planned_visible_count,
            planned_prefetch=plan.planned_prefetch_count,
            budget_suppressed=plan.budget_suppressed_count,
            estimated_screen_cost=plan.estimated_screen_cost,
            adaptive_limit=plan.adaptive_limit,
            minimum_spacing_px=plan.minimum_spacing_px,
            decode_px=plan.decode_px,
            stale_discarded=self._thumbnail_stale_results,
            missing=len(self._thumbnail_missing_ids),
            failed=len(self._thumbnail_failed_ids),
        )
        self._update_thumbnail_loading_indicator()

    def _handle_thumbnail_decode_failure(self, payload: ThumbnailDecodeFailure) -> None:
        metadata_id = int(payload.metadata_id)
        self._thumbnail_loading_pending_ids.discard(metadata_id)
        self._thumbnail_failed_ids.add(metadata_id)
        key = (
            metadata_id,
            str(payload.variant_id or "none"),
            int(payload.target_px),
        )
        self._pin_thumbnail_pending_requests.pop(key, None)
        if self._tooltip_thumbnail_pending_request == key:
            self._tooltip_thumbnail_pending_request = None
            if self._tooltip_popup is not None:
                self._tooltip_popup.clear_thumbnail()
        self._update_thumbnail_loading_indicator()

    def _push_pending_canvas_thumbnails(self) -> None:
        if self._canvas_widget is None or not self._thumbnail_pending_entries:
            self._thumbnail_pending_entries.clear()
            return
        batch_size = max(1, int(self._thumbnail_render_config.push_batch_size))
        entries = self._thumbnail_pending_entries[:batch_size]
        del self._thumbnail_pending_entries[:batch_size]
        self._canvas_widget.push_thumbnail_batch(entries)
        if self._thumbnail_pending_entries:
            self._thumbnail_push_timer.start()

    def _handle_canvas_point_hovered(self, payload: object) -> None:
        if not self._tooltip_layer_enabled:
            self._hide_tooltip_popup()
            return
        if not isinstance(payload, dict):
            self._hide_tooltip_popup()
            return
        metadata_id = payload.get("metadata_id")
        global_pos = payload.get("global_pos")
        if not isinstance(metadata_id, int) or not isinstance(global_pos, QtCore.QPoint):
            self._hide_tooltip_popup()
            return
        self._show_tooltip_for_metadata_id(int(metadata_id), global_pos + QtCore.QPoint(12, 16))

    def _show_tooltip_for_metadata_id(
        self,
        metadata_id: int,
        global_pos: QtCore.QPoint,
        *,
        settings_preview: bool = False,
    ) -> None:
        panel = self._panel(LayersSettingPanelController)
        display_payload = panel.pin_display_settings_payload() if panel is not None else {}
        variant_id = (
            display_payload.get("tooltip_thumbnail_variant_id", "none")
            if bool(display_payload.get("tooltip_thumbnail_visible", True))
            else "none"
        )
        variant_id = str(variant_id or "none")
        lines = self._tooltip_lines_for_metadata_id(metadata_id)
        if not lines and variant_id == "none":
            self._hide_tooltip_popup()
            return
        popup = self._ensure_tooltip_popup()
        popup.set_thumbnail_alignment(str(display_payload.get("tooltip_thumbnail_alignment") or "bottom"))
        popup.set_lines(lines)
        tooltip_thumbnail_size = self._tooltip_thumbnail_size_from_payload(display_payload)
        self._hovered_metadata_id = int(metadata_id)
        self._hovered_thumbnail_variant_id = variant_id
        self._request_tooltip_thumbnail(
            metadata_id=int(metadata_id),
            variant_id=variant_id,
            target_px=tooltip_thumbnail_size,
            popup=popup,
        )
        self._settings_preview_tooltip_active = bool(settings_preview)
        if settings_preview:
            global_pos = self._settings_preview_tooltip_position(popup)
        popup.move(global_pos)
        popup.show()

    def _handle_pin_display_changed(self) -> None:
        self._queue_project_exploration_settings_save()
        self._apply_pin_display_elements()
        if self._restoring_project_state:
            return
        self._persist_canvas_pins()
        self._refresh_current_snapshot_update_state()

    def _handle_tooltip_thumbnail_size_changed(self, _size_px: int) -> None:
        self._show_settings_preview_tooltip()

    def _hide_settings_preview_tooltip(self) -> None:
        self._hide_tooltip_popup()

    def _show_settings_preview_tooltip(self) -> None:
        if self._restoring_project_state or not self._tooltip_layer_enabled:
            return
        if self._canvas_widget is None:
            return
        metadata_id = self._tooltip_preview_metadata_id()
        if metadata_id is None:
            return
        self._show_tooltip_for_metadata_id(
            metadata_id,
            self._canvas_widget.mapToGlobal(QtCore.QPoint(0, 0)),
            settings_preview=True,
        )

    def _settings_preview_tooltip_position(
        self,
        popup: QtWidgets.QWidget,
    ) -> QtCore.QPoint:
        if self._canvas_widget is None:
            return popup.pos()
        margin = 16
        local_x = max(margin, self._canvas_widget.width() - popup.width() - margin)
        local_y = max(margin, (self._canvas_widget.height() - popup.height()) // 2)
        return self._canvas_widget.mapToGlobal(QtCore.QPoint(local_x, local_y))

    def _tooltip_preview_metadata_id(self) -> int | None:
        if self._hovered_metadata_id is not None:
            return int(self._hovered_metadata_id)
        if self._active_dataset is not None and self._active_dataset.metadata_ids.size > 0:
            return int(self._active_dataset.metadata_ids[0])
        if self._metadata_row_map:
            return int(next(iter(self._metadata_row_map)))
        if self._metadata_access is not None:
            metadata_ids = self._metadata_access.metadata_ids()
            if metadata_ids.size:
                return int(metadata_ids[0])
        return None

    def _apply_pin_display_elements(self) -> None:
        if self._canvas_widget is None:
            return
        panel = self._panel(LayersSettingPanelController)
        if panel is None:
            return
        style = panel.pin_display_style()
        if style:
            self._canvas_widget.set_pin_style(style)
        fields = panel.selected_pin_fields()
        display_payload = panel.pin_display_settings_payload()
        variant_id = str(display_payload.get("thumbnail_variant_id") or "none")
        thumbnails_enabled = bool(style.get("thumbnail_visible", False)) and variant_id != "none"
        original_payload = self._canvas_widget.pinned_annotations_payload()
        if not original_payload:
            return
        payload = self._pin_display_entries(
            original_payload,
            fields=fields,
            variant_id=variant_id,
            thumbnail_size=int(style.get("thumbnail_size", 96)),
        )
        if payload != original_payload:
            self._applying_pin_display = True
            try:
                self._canvas_widget.set_pinned_annotations(payload)
            finally:
                self._applying_pin_display = False
        self._queue_pin_thumbnail_requests(payload, variant_id=variant_id, enabled=thumbnails_enabled)

    def _pin_display_entries(
        self,
        entries: list[dict[str, object]],
        *,
        fields: list[str],
        variant_id: str,
        thumbnail_size: int,
    ) -> list[dict[str, object]]:
        payload = [dict(item) for item in entries]
        if not payload:
            return payload
        metadata_text, _metadata_numeric = self._metadata_values_for_ids(
            np.asarray([int(item.get("metadata_id", -1)) for item in payload], dtype=np.int64),
            columns=set(fields) - {"id"},
            numeric_columns=set(),
        )
        for idx, item in enumerate(payload):
            metadata_id = int(item.get("metadata_id", -1))
            lines: list[str] = []
            for field in fields:
                if field == "id":
                    lines.append(f"ID\t{metadata_id}")
                    continue
                values = metadata_text.get(field)
                value = values[idx] if values is not None and idx < values.shape[0] else ""
                lines.append(f"{field}\t{value}")
            item["lines"] = lines
            item["thumbnail_variant_id"] = variant_id
            item["thumbnail_size"] = int(thumbnail_size)
            item.pop("thumbnail_rgba", None)
            item.pop("thumbnail_width", None)
            item.pop("thumbnail_height", None)
        return payload

    def _queue_pin_thumbnail_requests(
        self,
        entries: list[dict],
        *,
        variant_id: str,
        enabled: bool,
    ) -> None:
        self._pin_thumbnail_generation += 1
        self._pin_thumbnail_pending_requests.clear()
        if not enabled or self._thumbnail_service is None:
            return
        target_px = max(32, int(entries[0].get("thumbnail_size", 96))) if entries else 96
        for item in entries:
            try:
                metadata_id = int(item.get("metadata_id"))
            except (TypeError, ValueError):
                continue
            key = (metadata_id, variant_id, target_px)
            self._pin_thumbnail_pending_requests[key] = self._pin_thumbnail_generation
            ref = self._thumbnail_service.request(
                DecodeRequest(
                    metadata_id=metadata_id,
                    target_px=target_px,
                    variant_id=variant_id,
                )
            )
            if ref is None:
                self._pin_thumbnail_pending_requests.pop(key, None)

    def _apply_ready_pin_thumbnail(self, payload: DecodedThumbnail) -> bool:
        if self._canvas_widget is None:
            return False
        key = (
            int(payload.metadata_id),
            str(payload.variant_id or "none"),
            int(payload.target_px),
        )
        generation = self._pin_thumbnail_pending_requests.get(key)
        if generation is None:
            return False
        if generation != self._pin_thumbnail_generation:
            self._pin_thumbnail_pending_requests.pop(key, None)
            return True
        self._pin_thumbnail_pending_requests.pop(key, None)
        annotations = self._canvas_widget.pinned_annotations_payload()
        changed = False
        for item in annotations:
            try:
                metadata_id = int(item.get("metadata_id"))
            except (TypeError, ValueError):
                continue
            if metadata_id != int(payload.metadata_id):
                continue
            if str(item.get("thumbnail_variant_id") or "none") != str(payload.variant_id or "none"):
                continue
            item["thumbnail_rgba"] = payload.rgba
            item["thumbnail_width"] = int(payload.width)
            item["thumbnail_height"] = int(payload.height)
            changed = True
        if changed:
            self._applying_pin_display = True
            try:
                self._canvas_widget.set_pinned_annotations(annotations)
            finally:
                self._applying_pin_display = False
        return True

    def _request_tooltip_thumbnail(
        self,
        *,
        metadata_id: int,
        variant_id: str,
        target_px: int,
        popup: _CanvasTooltipPopup,
    ) -> None:
        target_px = max(25, min(500, int(target_px)))
        popup.set_thumbnail_size(target_px)
        self._tooltip_thumbnail_pending_request = None
        if variant_id == "none" or self._thumbnail_service is None:
            popup.clear_thumbnail()
            return
        self._tooltip_thumbnail_pending_request = (int(metadata_id), str(variant_id), target_px)
        popup.set_placeholder()
        ref = self._thumbnail_service.request(
            DecodeRequest(
                metadata_id=int(metadata_id),
                target_px=target_px,
                variant_id=str(variant_id),
            )
        )
        if ref is None:
            self._tooltip_thumbnail_pending_request = None
            popup.clear_thumbnail()

    def _apply_ready_tooltip_thumbnail(self, payload: DecodedThumbnail) -> bool:
        pending = self._tooltip_thumbnail_pending_request
        if pending is None or self._tooltip_popup is None:
            return False
        key = (
            int(payload.metadata_id),
            str(payload.variant_id or "none"),
            int(payload.target_px),
        )
        if key != pending:
            return False
        if (
            self._hovered_metadata_id != int(payload.metadata_id)
            or self._hovered_thumbnail_variant_id != str(payload.variant_id or "none")
        ):
            return False
        image = QtGui.QImage(
            payload.rgba,
            int(payload.width),
            int(payload.height),
            QtGui.QImage.Format.Format_RGBA8888,
        ).copy()
        self._tooltip_popup.set_thumbnail_image(image)
        if self._settings_preview_tooltip_active:
            self._tooltip_popup.move(
                self._settings_preview_tooltip_position(self._tooltip_popup)
            )
        self._tooltip_thumbnail_pending_request = None
        return True

    @staticmethod
    def _tooltip_thumbnail_size_from_payload(payload: dict[str, object]) -> int:
        try:
            value = int(round(float(payload.get("tooltip_thumbnail_size", 250))))
        except (TypeError, ValueError):
            value = 250
        return max(25, min(500, value))

    def _ensure_tooltip_popup(self) -> _CanvasTooltipPopup:
        if self._tooltip_popup is None:
            self._tooltip_popup = _CanvasTooltipPopup(self._widget.window())
        return self._tooltip_popup

    def _hide_tooltip_popup(self) -> None:
        if self._tooltip_popup is not None:
            self._tooltip_popup.hide()
        self._settings_preview_tooltip_active = False
        self._hovered_metadata_id = None
        self._hovered_thumbnail_variant_id = None
        self._tooltip_thumbnail_pending_request = None

    def _sync_pin_metadata_fields(self) -> None:
        panel = self._panel(LayersSettingPanelController)
        if panel is None:
            return
        fields = self._metadata_columns()
        panel.set_metadata_fields(
            fields,
            tooltip_checked=panel.selected_tooltip_fields(),
            pin_checked=panel.selected_pin_fields(),
        )

    def _tooltip_lines_for_metadata_id(self, metadata_id: int) -> list[str]:
        panel = self._panel(LayersSettingPanelController)
        if panel is None:
            return [f"ID\t{metadata_id}"]
        display_payload = panel.pin_display_settings_payload()
        if not bool(display_payload.get("tooltip_info_field_visible", True)):
            return []
        fields = panel.selected_tooltip_fields()
        show_label = bool(display_payload.get("tooltip_show_label", True))
        requested_fields = {str(field).strip() for field in fields} - {"id", ""}
        metadata_text, _numeric = self._metadata_values_for_ids(
            np.asarray([int(metadata_id)], dtype=np.int64),
            columns=requested_fields,
            numeric_columns=set(),
        )
        lines: list[str] = []
        for field in fields:
            key = str(field).strip()
            if key == "id":
                lines.append(f"ID\t{metadata_id}" if show_label else str(metadata_id))
            else:
                values = metadata_text.get(key)
                value = (
                    ""
                    if values is None or values.size == 0
                    else _CanvasTooltipPopup._truncate_value(str(values[0]))
                )
                lines.append(f"{key}\t{value}" if show_label else value)
        return lines

    def _refresh_metadata_cache(self, project: Project) -> None:
        del project
        self._metadata_row_map = {}
        self._metadata_array_cache.clear()
        try:
            columns = (
                ["id"]
                if self._metadata_access is None
                else list(self._metadata_access.column_names())
            )
        except Exception:
            columns = ["id"]
        self._metadata_columns_cache = columns or ["id"]

    def _metadata_columns(self) -> list[str]:
        return list(self._metadata_columns_cache or ["id"])

    def _load_pins_for_active_discovery(self) -> None:
        if self._project is None or self._canvas_widget is None:
            return
        scope = self._active_discovery_scope()
        if scope is None:
            self._canvas_widget.set_pinned_annotations([])
            return
        entries: list[dict[str, object]] = []
        points_by_metadata_id: dict[int, tuple[float, float]] = {}
        if self._active_dataset is not None:
            for idx, metadata_id in enumerate(self._active_dataset.metadata_ids.tolist()):
                if idx >= self._active_dataset.coordinates.shape[0]:
                    continue
                point = self._active_dataset.coordinates[idx]
                points_by_metadata_id[int(metadata_id)] = (float(point[0]), float(point[1]))
        pin_payloads = self._service.list_pin_payloads(project=self._project, discovery_scope=scope)
        display_payload = self._display_payload_from_pin_payloads(pin_payloads)
        if display_payload is not None:
            if not self._restoring_project_state or self._has_saved_tooltip_settings:
                display_payload.update(self._exploration_settings_payload()["tooltip"])
            self._apply_pin_display_payload(display_payload)
        for payload in pin_payloads:
            entries.append(
                self._canvas_entry_from_pin_payload(
                    payload,
                    fallback_points=points_by_metadata_id,
                    display_payload=display_payload,
                )
            )
        panel = self._panel(LayersSettingPanelController)
        variant_id = "none"
        thumbnails_enabled = False
        if panel is not None:
            style = panel.pin_display_style()
            if style:
                self._canvas_widget.set_pin_style(style)
            display_payload = panel.pin_display_settings_payload()
            variant_id = str(display_payload.get("thumbnail_variant_id") or "none")
            thumbnails_enabled = bool(style.get("thumbnail_visible", False)) and variant_id != "none"
            entries = self._pin_display_entries(
                entries,
                fields=panel.selected_pin_fields(),
                variant_id=variant_id,
                thumbnail_size=int(style.get("thumbnail_size", 96)),
            )
        self._applying_pin_display = True
        try:
            self._canvas_widget.set_pinned_annotations(entries)
        finally:
            self._applying_pin_display = False
        self._queue_pin_thumbnail_requests(entries, variant_id=variant_id, enabled=thumbnails_enabled)

    def _display_payload_from_pin_payloads(self, payloads: list[dict[str, object]]) -> dict[str, object] | None:
        for payload in payloads:
            display = payload.get("display")
            if isinstance(display, dict):
                return display
        return None

    def _canvas_entry_from_pin_payload(
        self,
        payload: dict[str, object],
        *,
        fallback_points: dict[int, tuple[float, float]],
        display_payload: dict[str, object] | None,
    ) -> dict[str, object]:
        metadata_id = int(payload.get("metadata_id") or 0)
        active_point = fallback_points.get(metadata_id)
        point_values = (
            [float("nan"), float("nan")]
            if active_point is None
            else [float(active_point[0]), float(active_point[1])]
        )
        offset = payload.get("screen_offset", [48.0, -36.0])
        if not (isinstance(offset, (list, tuple)) and len(offset) >= 2):
            offset = [48.0, -36.0]
        display = payload.get("display") if isinstance(payload.get("display"), dict) else display_payload or {}
        style = display.get("style") if isinstance(display, dict) else {}
        thumbnail_size = 96
        if isinstance(style, dict):
            try:
                thumbnail_size = int(style.get("thumbnail_size", 96))
            except (TypeError, ValueError):
                thumbnail_size = 96
        lines = payload.get("lines")
        entry: dict[str, object] = {
            "annotation_id": str(payload.get("annotation_id") or ""),
            "metadata_id": metadata_id,
            "available_in_projection": active_point is not None,
            "scope_mode": (
                "projection"
                if str(payload.get("scope_mode") or "").strip().lower() == "projection"
                else "project"
            ),
            "point": point_values,
            "screen_offset": [float(offset[0]), float(offset[1])],
            "visible": bool(payload.get("visible", True)),
            "created_at": str(payload.get("created_at") or ""),
            "lines": [str(line) for line in lines] if isinstance(lines, list) else [],
            "thumbnail_variant_id": str(display.get("thumbnail_variant_id") or "none") if isinstance(display, dict) else "none",
            "thumbnail_size": thumbnail_size,
        }
        if entry["scope_mode"] == "projection":
            entry["projection_scope"] = str(payload.get("projection_scope") or "").strip()
        box_anchor = payload.get("box_anchor")
        if isinstance(box_anchor, (list, tuple)) and len(box_anchor) >= 2:
            try:
                old_point = payload.get("point")
                if active_point is not None and isinstance(old_point, (list, tuple)) and len(old_point) >= 2:
                    delta_x = float(active_point[0]) - float(old_point[0])
                    delta_y = float(active_point[1]) - float(old_point[1])
                else:
                    delta_x = delta_y = 0.0
                entry["box_anchor"] = [
                    float(box_anchor[0]) + delta_x,
                    float(box_anchor[1]) + delta_y,
                ]
            except (TypeError, ValueError):
                pass
        return entry

    def _apply_pin_display_payload(self, payload: dict[str, object]) -> None:
        panel = self._panel(LayersSettingPanelController)
        if panel is None:
            return
        panel.apply_pin_display_settings_payload(payload)
        self._apply_project_pin_layer_state(payload.get("layer_state"))

    def _current_project_pin_layer_state(self) -> dict[str, object]:
        panel = self._panel(CanvasLayersPanelController)
        if panel is None:
            return {}
        checkbox = panel.widget.findChild(QtWidgets.QCheckBox, "checkbox_show_pins")
        if checkbox is None:
            return {}
        return {"pins_visible": bool(checkbox.isChecked())}

    def _apply_project_pin_layer_state(self, payload: object) -> None:
        if not isinstance(payload, dict) or "pins_visible" not in payload:
            return
        visible = bool(payload.get("pins_visible"))
        panel = self._panel(CanvasLayersPanelController)
        checkbox = None if panel is None else panel.widget.findChild(QtWidgets.QCheckBox, "checkbox_show_pins")
        self._applying_project_layer_state = True
        try:
            if checkbox is not None:
                with QtCore.QSignalBlocker(checkbox):
                    checkbox.setChecked(visible)
            if self._canvas_widget is not None:
                self._canvas_widget.set_pinned_annotations_visible(visible)
        finally:
            self._applying_project_layer_state = False

    def _persist_canvas_pins(self) -> None:
        if self._restoring_project_state:
            return
        if not self._project_has_storage() or self._canvas_widget is None:
            return
        scope = self._active_discovery_scope()
        if scope is None:
            return
        payload = self._canvas_widget.pinned_annotations_payload()
        display_payload = self._current_pin_display_payload()
        pin_payloads: list[dict[str, object]] = []
        for entry in payload:
            annotation_id = str(entry.get("annotation_id") or "").strip()
            if not annotation_id:
                continue
            try:
                metadata_id = int(entry.get("metadata_id"))
            except (TypeError, ValueError):
                continue
            point = entry.get("point")
            offset = entry.get("screen_offset", [48.0, -36.0])
            if not (isinstance(point, (list, tuple)) and len(point) >= 2):
                continue
            if not (isinstance(offset, (list, tuple)) and len(offset) >= 2):
                offset = [48.0, -36.0]
            pin_payload: dict[str, object] = {
                "annotation_id": annotation_id,
                "discovery_scope": scope,
                "metadata_id": metadata_id,
                "scope_mode": (
                    "projection"
                    if str(entry.get("scope_mode") or "").strip().lower() == "projection"
                    else "project"
                ),
                "point": [float(point[0]), float(point[1])],
                "screen_offset": [float(offset[0]), float(offset[1])],
                "visible": bool(entry.get("visible", True)),
                "created_at": str(entry.get("created_at") or ""),
                "display": display_payload,
                "lines": [str(line) for line in entry.get("lines", [])]
                if isinstance(entry.get("lines"), list)
                else [],
            }
            if pin_payload["scope_mode"] == "projection":
                pin_payload["projection_scope"] = str(
                    entry.get("projection_scope") or scope
                ).strip()
            box_anchor = entry.get("box_anchor")
            if isinstance(box_anchor, (list, tuple)) and len(box_anchor) >= 2:
                try:
                    pin_payload["box_anchor"] = [float(box_anchor[0]), float(box_anchor[1])]
                except (TypeError, ValueError):
                    pass
            pin_payloads.append(pin_payload)
        self._service.replace_pin_payloads(project=self._project, discovery_scope=scope, payloads=pin_payloads)

    def _current_pin_display_payload(self) -> dict[str, object]:
        panel = self._panel(LayersSettingPanelController)
        payload = panel.pin_display_settings_payload() if panel is not None else {}
        payload["layer_state"] = self._current_project_pin_layer_state()
        return payload

    def _handle_appearance_style_changed(self, style: dict[str, object]) -> None:
        self._queue_project_appearance_state_save(style)
        if self._active_dataset is not None:
            self._render_active_dataset(preserve_view=True, fit_view=False)
        if self._canvas_render_mode != "points_fast":
            self._configure_canvas_thumbnail_layer()
        self._refresh_current_snapshot_update_state()

    def _handle_thumbnail_size_changed(self, size_px: int) -> None:
        previous_thumbnail_size_px = self._canvas_thumbnail_size_px
        self._canvas_thumbnail_size_px = max(1, int(size_px))
        if previous_thumbnail_size_px == self._canvas_thumbnail_size_px:
            return
        if self._canvas_thumbnail_limit_automatic:
            point_count = (
                0
                if self._active_dataset is None
                else int(self._active_dataset.coordinates.shape[0])
            )
            self._apply_recommended_thumbnail_limit(point_count)
        self._reset_thumbnail_plan_state(preserve_preview_visibility=True)
        self._thumbnail_pending_entries.clear()
        self._update_thumbnail_loading_indicator()
        if self._canvas_render_mode != "points_fast":
            self._configure_canvas_thumbnail_layer()
            self._queue_canvas_thumbnail_requests()
        self._refresh_current_snapshot_update_state()

    def _handle_thumbnail_limit_changed(self, thumbnail_limit: int) -> None:
        minimum, maximum = self._thumbnail_limit_bounds()
        normalized = LayersSettingPanelController.normalize_thumbnail_limit(
            max(minimum, min(maximum, int(thumbnail_limit)))
        )
        layers_panel = self._panel(LayersSettingPanelController)
        automatic = bool(
            layers_panel is not None
            and layers_panel.thumbnail_density_is_automatic()
        )
        if (
            normalized == self._canvas_thumbnail_limit
            and automatic == self._canvas_thumbnail_limit_automatic
        ):
            return
        self._canvas_thumbnail_limit = normalized
        self._canvas_thumbnail_limit_automatic = automatic
        self._reset_thumbnail_plan_state(preserve_preview_visibility=True)
        self._thumbnail_pending_entries.clear()
        self._update_thumbnail_loading_indicator()
        if self._canvas_render_mode != "points_fast":
            self._configure_canvas_thumbnail_layer()
            self._queue_canvas_thumbnail_requests()
        self._refresh_current_snapshot_update_state()

    def _handle_thumbnail_limit_auto_requested(self) -> None:
        point_count = (
            0
            if self._active_dataset is None
            else int(self._active_dataset.coordinates.shape[0])
        )
        self._apply_recommended_thumbnail_limit(point_count)
        self._reset_thumbnail_plan_state(preserve_preview_visibility=True)
        self._thumbnail_pending_entries.clear()
        self._update_thumbnail_loading_indicator()
        if self._canvas_render_mode != "points_fast":
            self._configure_canvas_thumbnail_layer()
            self._queue_canvas_thumbnail_requests()
        self._refresh_current_snapshot_update_state()

    def _apply_recommended_thumbnail_limit(self, _point_count: int) -> None:
        minimum, maximum = self._thumbnail_limit_bounds()
        recommended = LayersSettingPanelController.normalize_thumbnail_limit(
            max(
                minimum,
                min(
                    maximum,
                    automatic_thumbnail_limit_for_size(self._canvas_thumbnail_size_px),
                ),
            )
        )
        self._canvas_thumbnail_limit = recommended
        self._canvas_thumbnail_limit_automatic = True
        layers_panel = self._panel(LayersSettingPanelController)
        if layers_panel is not None:
            layers_panel.set_thumbnail_limit(
                recommended,
                automatic=True,
            )

    def _handle_filtered_opacity_changed(self, opacity: float) -> None:
        self._queue_project_exploration_settings_save()
        self._filtered_opacity = float(max(0.0, min(1.0, opacity)))
        if self._restoring_project_state or self._active_dataset is None:
            return
        self._render_active_dataset(preserve_view=True, fit_view=False)
        self._refresh_current_snapshot_update_state()

    def _handle_hide_filtered_tooltips_changed(self, _checked: bool) -> None:
        self._queue_project_exploration_settings_save()
        self._apply_tooltip_hover_eligibility()
        self._refresh_current_snapshot_update_state()

    def _apply_tooltip_hover_eligibility(self) -> None:
        canvas = self._canvas_widget
        filters_panel = self._panel(FiltersPanelController)
        if canvas is None:
            return
        mask = None
        if filters_panel is not None and filters_panel.hide_filtered_tooltips():
            mask = self._thumbnail_preview_visibility >= 0.5
        canvas.set_hover_eligibility_mask(mask)

    def _handle_filters_changed(self) -> None:
        self._queue_project_exploration_settings_save()
        if self._restoring_project_state or self._active_dataset is None:
            return
        self._render_active_dataset(preserve_view=True, fit_view=False)
        self._refresh_current_snapshot_update_state()

    def _handle_slicer_changed(self) -> None:
        if self._active_dataset is None:
            return
        self._render_active_dataset(preserve_view=True, fit_view=False)
        self._refresh_current_snapshot_update_state()

    def _save_filter_preset(self, label: str, payload: dict) -> bool:
        if self._project is None:
            return False
        self._service.save_filter_preset(self._project, label, payload)
        self._refresh_preset_menus()
        return True

    def _save_appearance_preset(self, label: str, payload: dict) -> bool:
        if self._project is None:
            return False
        self._service.save_appearance_preset(self._project, label, self._named_appearance_payload(label, payload))
        self._refresh_preset_menus()
        return True

    def _load_appearance_preset_by_label(self, label: str) -> dict | None:
        if self._project is None:
            return None
        for entry in self._service.list_appearance_preset_entries(self._project):
            if entry.label == label:
                return self._service.load_appearance_preset(self._project, entry.path)
        return None

    def _save_global_appearance_template(self, label: str, payload: dict) -> bool:
        try:
            repository = PresetRepository(self._global_appearance_preset_root())
            repository.save_payload(label, self._named_appearance_payload(label, payload), allow_overwrite=True)
            self._refresh_preset_menus()
            return True
        except OSError:
            self._show_status("Failed to save global appearance template.")
            return False

    def _load_global_appearance_template_by_label(self, label: str) -> dict | None:
        root = self._global_appearance_preset_root()
        if not root.exists():
            return None
        try:
            repository = PresetRepository(root)
            for entry in self._list_global_appearance_template_entries():
                if entry.label == label:
                    return repository.load_payload_from_path(entry.path)
        except OSError:
            return None
        return None

    def _exploration_settings_payload(self) -> dict:
        layers = self._panel(LayersSettingPanelController)
        filters = self._panel(FiltersPanelController)
        canvas_layers = self._panel(CanvasLayersPanelController)
        checkbox = (
            canvas_layers.widget.findChild(QtWidgets.QCheckBox, "checkbox_show_tooltips")
            if canvas_layers is not None else None
        )
        display = layers.pin_display_settings_payload() if layers is not None else {}
        return {
            "canvas": self._canvas_widget.background_state() if self._canvas_widget is not None else {},
            "tooltip": {key: value for key, value in display.items() if key.startswith("tooltip_")},
            "tooltips_visible": checkbox.isChecked() if checkbox is not None else True,
            "filters": filters.filter_state_payload() if filters is not None else {},
        }

    def _queue_project_exploration_settings_save(self) -> None:
        if self._restoring_project_state or not self._project_has_storage():
            return
        self._persist_settings_timer.start()

    def _persist_project_exploration_settings(self) -> None:
        if self._restoring_project_state or not self._project_has_storage():
            return
        self._persist_settings_timer.stop()
        try:
            DiscoverySettingsRepository(self._project.paths.state).save(self._exploration_settings_payload())
        except OSError:
            self._show_status("Failed to save Discovery settings.")

    def _load_project_exploration_settings(self) -> None:
        payload = (
            DiscoverySettingsRepository(self._project.paths.state).load()
            if self._project_has_storage() else {}
        )
        if self._canvas_widget is not None:
            self._canvas_widget.restore_background_state(payload.get("canvas", {}))
        self._has_saved_tooltip_settings = "tooltip" in payload
        defaults = self._default_exploration_settings
        layers = self._panel(LayersSettingPanelController)
        if layers is not None:
            display = layers.pin_display_settings_payload()
            display.update(defaults["tooltip"])
            display.update(payload.get("tooltip", {}))
            layers.apply_pin_display_settings_payload(display)
        filters = self._panel(FiltersPanelController)
        if filters is not None:
            filters.apply_filter_state_payload(payload.get("filters", defaults["filters"]))
            self._filtered_opacity = filters.filtered_opacity()
        canvas_layers = self._panel(CanvasLayersPanelController)
        if canvas_layers is not None:
            checkbox = canvas_layers.widget.findChild(QtWidgets.QCheckBox, "checkbox_show_tooltips")
            if checkbox is not None:
                checkbox.setChecked(bool(payload.get("tooltips_visible", defaults["tooltips_visible"])))

    def _queue_project_appearance_state_save(self, payload: dict[str, object]) -> None:
        if self._project is None:
            return
        self._pending_appearance_state = dict(payload)
        self._persist_appearance_timer.start()

    def _persist_project_appearance_state(self) -> None:
        if self._project is None or self._pending_appearance_state is None:
            return
        try:
            path = self._project_appearance_state_path(self._project)
            path.parent.mkdir(parents=True, exist_ok=True)
            json_store.write_json(path, self._pending_appearance_state)
            self._pending_appearance_state = None
        except OSError:
            self._show_status("Failed to save appearance state.")

    def _load_project_appearance_state(self) -> None:
        if self._project is None:
            return
        try:
            payload = json_store.read_json(self._project_appearance_state_path(self._project), default=None)
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        panel = self._panel(AppearancePanelController)
        if panel is not None:
            panel.apply_preset_payload(payload)

    @staticmethod
    def _project_appearance_state_path(project: "Project") -> Path:
        return project.paths.state / "discovery_appearance_state.json"

    @staticmethod
    def _named_appearance_payload(label: str, payload: dict) -> dict:
        named = dict(payload)
        named["name"] = str(label).strip()
        return named

    def _global_appearance_preset_root(self) -> Path:
        app_data = QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.StandardLocation.AppDataLocation
        )
        root = Path(app_data) if app_data else Path.home() / ".eidora"
        return root / "presets" / "appearance"

    def _list_global_appearance_template_entries(self) -> list:
        root = self._global_appearance_preset_root()
        if not root.exists():
            return []
        try:
            return PresetRepository(root).list_entries()
        except OSError:
            return []

    def _load_filter_preset_by_label(self, label: str) -> dict | None:
        if self._project is None:
            return None
        for entry in self._service.list_filter_preset_entries(self._project):
            if entry.label == label:
                return self._service.load_filter_preset(self._project, entry.path)
        return None

    def _save_slicer_preset(self, label: str, payload: dict) -> bool:
        if self._project is None:
            return False
        self._service.save_slicer_preset(self._project, label, payload)
        self._refresh_preset_menus()
        return True

    def _load_slicer_preset_by_label(self, label: str) -> dict | None:
        if self._project is None:
            return None
        for entry in self._service.list_slicer_preset_entries(self._project):
            if entry.label == label:
                return self._service.load_slicer_preset(self._project, entry.path)
        return None

    def _save_transform_preset(self, label: str, payload: dict) -> bool:
        if self._project is None:
            return False
        self._service.save_transform_preset(self._project, label, payload)
        self._refresh_preset_menus()
        return True

    def _load_transform_preset_by_label(self, label: str) -> dict | None:
        if self._project is None:
            return None
        for entry in self._service.list_transform_preset_entries(self._project):
            if entry.label == label:
                return self._service.load_transform_preset(self._project, entry.path)
        return None

    def _save_camera_preset(self, label: str) -> bool:
        if self._project is None or self._canvas_widget is None:
            return False
        state_getter = getattr(self._canvas_widget, "camera_preset_state", None)
        camera = state_getter() if callable(state_getter) else self._canvas_widget.camera_state()
        payload = {"camera": camera}
        self._service.save_camera_preset(self._project, label, payload)
        self._refresh_preset_menus()
        return True

    def _load_camera_preset_from_path(self, path: Path) -> bool:
        if self._project is None or self._canvas_widget is None:
            return False
        payload = self._service.load_camera_preset(self._project, path)
        if not isinstance(payload, dict):
            return False
        camera = payload.get("camera")
        if not isinstance(camera, dict):
            return False
        self._applying_camera_preset = True
        try:
            state_setter = getattr(self._canvas_widget, "set_camera_preset_state", None)
            if callable(state_setter):
                return bool(state_setter(camera, preserve_aspect=True))
            return self._canvas_widget.set_camera_state(camera, preserve_aspect=True)
        finally:
            self._applying_camera_preset = False

    def _save_snapshot_by_label(self, label: str) -> bool:
        if self._project is None or self._active_dataset is None:
            return False
        refs = self._save_component_presets_for_snapshot(label)
        snapshot = self._build_snapshot_payload(label, refs=refs)
        projection = snapshot.projection or self._snapshot_projection_payload()
        project = self._project
        dataset = self._active_dataset
        try:
            path = run_snapshot_write_responsive(
                lambda: self._service.save_snapshot(
                    project,
                    label,
                    snapshot,
                    dataset,
                    projection,
                )
            )
        except FileExistsError:
            self._show_status(f"Snapshot '{label}' already exists.")
            return False
        except Exception as exc:
            self._show_status(f"Could not save snapshot '{label}': {exc}")
            return False
        self._save_snapshot_thumbnail(path)
        self._current_snapshot_path = path
        self._current_snapshot_baseline = self._current_snapshot_comparison_state(path.stem)
        snapshots_panel = self._panel(SnapshotsPanelController)
        if snapshots_panel is not None:
            snapshots_panel.prefer_snapshot_path(path)
        self._refresh_current_snapshot_update_state()
        self._refresh_preset_menus()
        self._notify_snapshots_changed()
        return True

    def _update_snapshot_from_path(self, path: Path) -> bool:
        if self._project is None or self._active_dataset is None or not path.exists():
            return False
        refs = self._save_component_presets_for_snapshot(path.stem)
        snapshot = self._build_snapshot_payload(path.stem, refs=refs)
        projection = snapshot.projection or self._snapshot_projection_payload()
        dataset = self._active_dataset
        project = self._project
        try:
            run_snapshot_write_responsive(
                lambda: self._service.update_snapshot(
                    project,
                    path.stem,
                    snapshot,
                    dataset,
                    projection,
                )
            )
        except Exception as exc:
            self._show_status(f"Could not update snapshot '{path.stem}': {exc}")
            return False
        self._save_snapshot_thumbnail(path)
        self._refresh_preset_menus()
        self._current_snapshot_path = path
        self._current_snapshot_baseline = self._current_snapshot_comparison_state(path.stem)
        self._refresh_current_snapshot_update_state()
        self._notify_snapshots_changed()
        return True

    def _delete_snapshot_from_path(self, path: Path) -> bool:
        if self._project is None or not self._service.delete_snapshot(self._project, path):
            return False
        if self._current_snapshot_path == path:
            self._clear_current_snapshot_update_state()
        self._refresh_preset_menus()
        self._notify_snapshots_changed()
        return True

    def _load_snapshot_from_path(self, path: Path) -> bool:
        if self._activity_manager is not None:
            with self._activity_manager.busy("Loading snapshot…"):
                return self._restore_snapshot_from_path(path)
        return self._restore_snapshot_from_path(path)

    def _restore_snapshot_from_path(self, path: Path) -> bool:
        if self._project is None:
            return False
        snapshot = self._service.load_snapshot(self._project, path)
        if snapshot is None:
            return False
        failures: list[str] = []
        discovery_panel = self._panel(DiscoveryPanelController)
        if snapshot.discovery_key in self._discovery_options:
            if discovery_panel is not None:
                discovery_panel.set_selected_key(snapshot.discovery_key, emit_signal=False)
            self._handle_discovery_selection(snapshot.discovery_key)
            if self._source_dataset is not None:
                frozen = self._service.load_snapshot_coordinates(self._project, snapshot)
                projection = snapshot.projection
                if frozen is not None and projection is not None:
                    settings = TransformSettings.from_dict(projection.transform)
                    self._active_transform = None
                    self._active_dataset = frozen
                    self._active_arrangement_ref = projection.arrangement_identity or "original"
                    transform_panel = self._panel(TransformPanelController)
                    if transform_panel is not None:
                        transform_panel.set_projection(
                            settings if projection.coordinate_space == "transformed" else None,
                            populated=bool(frozen.coordinates.shape[0]),
                            transformable=self._active_projection_supports_arrangement_transform(),
                        )
                else:
                    if snapshot.projection is not None:
                        failures.append("snapshot coordinates")
                    restored = self._transform_service.load_reference(
                        self._project, snapshot.arrangement_ref, self._source_dataset
                    )
                    if snapshot.arrangement_ref and restored is None:
                        failures.append("arrangement")
                    self._active_transform = restored
                    if restored is None:
                        self._active_dataset = self._source_dataset
                        self._active_arrangement_ref = "original"
                    else:
                        self._active_dataset = self._transform_service.dataset_for_result(restored)
                        self._active_arrangement_ref = snapshot.arrangement_ref or "original"
                    transform_panel = self._panel(TransformPanelController)
                    if transform_panel is not None:
                        transform_panel.set_projection(
                            restored.settings if restored is not None else None,
                            populated=bool(self._active_dataset.coordinates.shape[0]),
                            transformable=self._active_projection_supports_arrangement_transform(),
                        )
        else:
            failures.append("discovery")
        appearance_panel = self._panel(AppearancePanelController)
        if appearance_panel is not None:
            ref_path = self._resolve_snapshot_ref(snapshot.refs.appearance)
            if ref_path is not None and ref_path.exists():
                appearance_panel.load_preset_path(ref_path)
            if snapshot.appearance_state:
                appearance_panel.apply_preset_payload(snapshot.appearance_state)
        filters_panel = self._panel(FiltersPanelController)
        if filters_panel is not None:
            applied = False
            ref_path = self._resolve_snapshot_ref(snapshot.refs.filters)
            if ref_path is not None and ref_path.exists():
                payload = self._service.load_filter_preset(self._project, ref_path)
                if isinstance(payload, dict):
                    filters_panel.apply_filter_state_payload(payload)
                    applied = True
            if snapshot.filters_state or not applied:
                filters_panel.apply_filter_state_payload(snapshot.filters_state)
        slicer_panel = self._panel(SlicerPanelController)
        if slicer_panel is not None:
            applied = False
            ref_path = self._resolve_snapshot_ref(snapshot.refs.slicer)
            if ref_path is not None and ref_path.exists():
                payload = self._service.load_slicer_preset(self._project, ref_path)
                if isinstance(payload, dict):
                    slicer_panel.apply_slicer_state_payload(payload)
                    applied = True
            if snapshot.slicer_state or not applied:
                slicer_panel.apply_slicer_state_payload(snapshot.slicer_state)
        if self._active_dataset is not None:
            # Select the saved arrangement for rendering before restoring its
            # coordinate-dependent camera, pins, and overlays.
            self._render_active_dataset(preserve_view=True, fit_view=False)
            self._load_pins_for_active_discovery()
        if self._canvas_widget is not None:
            ref_path = self._resolve_snapshot_ref(snapshot.refs.camera)
            if ref_path is not None and ref_path.exists():
                payload = self._service.load_camera_preset(self._project, ref_path)
                if isinstance(payload, dict) and isinstance(payload.get("camera"), dict):
                    state_setter = getattr(self._canvas_widget, "set_camera_preset_state", None)
                    if callable(state_setter):
                        state_setter(payload["camera"], preserve_aspect=True)
                    else:
                        self._canvas_widget.set_camera_state(payload["camera"], preserve_aspect=True)
            if snapshot.camera_state:
                self._canvas_widget.set_camera_state(snapshot.camera_state, preserve_aspect=True)
        if self._canvas_widget is not None:
            self._canvas_widget.restore_background_state(snapshot.canvas_state)
            self._queue_project_exploration_settings_save()
        self._apply_snapshot_layer_state(snapshot.layer_state)
        self._apply_snapshot_pinboard_state(snapshot.pinboard_state)
        if failures:
            self._show_status(f"Snapshot loaded with partial fallback: {', '.join(failures)}")
        else:
            self._show_status(f"Snapshot loaded: {snapshot.name}")
        self._current_snapshot_path = path
        self._current_snapshot_baseline = self._current_snapshot_comparison_state(path.stem)
        self._refresh_current_snapshot_update_state()
        return True

    def _resolve_snapshot_ref(self, ref: str | None) -> Path | None:
        if self._project is None or not ref:
            return None
        path = Path(ref)
        if path.is_absolute():
            return path
        return (self._project.root / path).resolve()

    def _build_snapshot_payload(self, label: str, *, refs: DiscoverySnapshotRefs | None = None) -> DiscoverySnapshot:
        assert self._project is not None
        appearance_panel = self._panel(AppearancePanelController)
        filters_panel = self._panel(FiltersPanelController)
        slicer_panel = self._panel(SlicerPanelController)
        layers_panel = self._panel(LayersSettingPanelController)
        appearance_state = appearance_panel.appearance_state_payload() if appearance_panel is not None else {}
        filters_state = filters_panel.filter_state_payload() if filters_panel is not None else {}
        slicer_state = slicer_panel.slicer_state_payload() if slicer_panel is not None else {}
        layer_state = self._current_layer_state()
        pinboard_state = self._current_pin_display_payload()
        if self._canvas_widget is not None:
            pinboard_state["annotations"] = self._snapshot_pin_annotations_payload(
                self._canvas_widget.pinned_annotations_payload()
            )
        camera_state = self._canvas_widget.camera_state() if self._canvas_widget is not None else {}
        metadata_ids = self._active_dataset.metadata_ids if self._active_dataset is not None else np.empty((0,), dtype=np.int64)
        snapshot_refs = refs or DiscoverySnapshotRefs(
            appearance=None,
            filters=self._relative_latest_path(self._service.list_filter_preset_entries(self._project)),
            camera=self._relative_latest_path(self._service.list_camera_preset_entries(self._project)),
            slicer=self._relative_latest_path(self._service.list_slicer_preset_entries(self._project)),
            transform=None,
        )
        option = self._discovery_options.get(self._active_discovery_key)
        discovery_ref = str(option.discovery_file.resolve()) if option and option.discovery_file is not None else None
        return DiscoverySnapshot(
            name=label,
            created_at=dt.datetime.now().isoformat(timespec="seconds"),
            discovery_key=self._active_discovery_key,
            discovery_ref=discovery_ref,
            arrangement_ref=None,
            projection=self._snapshot_projection_payload(),
            refs=snapshot_refs,
            appearance_state=appearance_state,
            filters_state=filters_state,
            canvas_state=self._canvas_widget.background_state() if self._canvas_widget is not None else {},
            camera_state=camera_state,
            slicer_state=slicer_state,
            layer_state=layer_state,
            pinboard_state=pinboard_state,
            transform_state=(
                self._snapshot_applied_transform_settings().to_dict()
            ),
            metadata_signature=metadata_signature_for_ids(metadata_ids),
        )

    def _snapshot_projection_payload(self) -> DiscoverySnapshotProjection:
        settings = self._snapshot_applied_transform_settings()
        transformed = (
            settings.snap_percent > 0
            or settings.minimum_spacing > 0.0
        )
        fingerprint = ""
        if self._source_dataset is not None:
            fingerprint = source_fingerprint(
                self._source_dataset.coordinates,
                self._source_dataset.metadata_ids,
            )
        return DiscoverySnapshotProjection(
            coordinates_ref="",
            coordinate_space="transformed" if transformed else "original",
            source_fingerprint=fingerprint,
            arrangement_identity=self._active_arrangement_ref or "original",
            transform=settings.to_dict(),
        )

    def _snapshot_applied_transform_settings(self) -> TransformSettings:
        if self._active_transform is not None:
            return self._active_transform.settings
        panel = self._panel(TransformPanelController)
        if panel is not None and self._active_arrangement_ref != "original":
            return panel.applied_settings()
        return TransformSettings()

    def _current_snapshot_comparison_state(self, label: str) -> dict[str, object] | None:
        if self._project is None:
            return None
        return self._snapshot_comparison_state(self._build_snapshot_payload(label))

    @staticmethod
    def _snapshot_comparison_state(snapshot: DiscoverySnapshot) -> dict[str, object]:
        return {
            "discovery_key": snapshot.discovery_key,
            "discovery_ref": snapshot.discovery_ref,
            "arrangement_ref": snapshot.arrangement_ref,
            "projection": (
                {
                    "coordinate_space": snapshot.projection.coordinate_space,
                    "source_fingerprint": snapshot.projection.source_fingerprint,
                    "arrangement_identity": snapshot.projection.arrangement_identity,
                    "transform": snapshot.projection.transform,
                }
                if snapshot.projection is not None
                else None
            ),
            "transform_state": snapshot.transform_state,
            "appearance_state": snapshot.appearance_state,
            "filters_state": snapshot.filters_state,
            "canvas_state": snapshot.canvas_state,
            "camera_state": snapshot.camera_state,
            "slicer_state": snapshot.slicer_state,
            "layer_state": snapshot.layer_state,
            "pinboard_state": snapshot.pinboard_state,
            "metadata_signature": snapshot.metadata_signature,
        }

    def _refresh_current_snapshot_update_state(self) -> None:
        snapshots_panel = self._panel(SnapshotsPanelController)
        if snapshots_panel is None:
            return
        path = self._current_snapshot_path
        if self._restoring_project_state or path is None or self._current_snapshot_baseline is None:
            snapshots_panel.set_update_enabled_state(None, False)
            return
        current = self._current_snapshot_comparison_state(path.stem)
        snapshots_panel.set_update_enabled_state(path, current != self._current_snapshot_baseline)

    def _clear_current_snapshot_update_state(self) -> None:
        self._current_snapshot_path = None
        self._current_snapshot_baseline = None
        snapshots_panel = self._panel(SnapshotsPanelController)
        if snapshots_panel is not None:
            snapshots_panel.set_update_enabled_state(None, False)

    @staticmethod
    def _snapshot_pin_annotations_payload(entries: list[dict[str, object]]) -> list[dict[str, object]]:
        output: list[dict[str, object]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            annotation_id = str(entry.get("annotation_id") or "").strip()
            if not annotation_id:
                continue
            try:
                metadata_id = int(entry.get("metadata_id"))
            except (TypeError, ValueError):
                continue
            point = entry.get("point")
            box_anchor = entry.get("box_anchor")
            offset = entry.get("screen_offset", [48.0, -36.0])
            if not (isinstance(point, (list, tuple)) and len(point) >= 2):
                continue
            if not (isinstance(offset, (list, tuple)) and len(offset) >= 2):
                offset = [48.0, -36.0]
            try:
                point_values = [float(point[0]), float(point[1])]
                offset_values = [float(offset[0]), float(offset[1])]
            except (TypeError, ValueError):
                continue
            box_anchor_values = None
            if isinstance(box_anchor, (list, tuple)) and len(box_anchor) >= 2:
                try:
                    box_anchor_values = [float(box_anchor[0]), float(box_anchor[1])]
                except (TypeError, ValueError):
                    box_anchor_values = None
            lines = entry.get("lines")
            payload = {
                "annotation_id": annotation_id,
                "metadata_id": metadata_id,
                "scope_mode": (
                    "projection"
                    if str(entry.get("scope_mode") or "").strip().lower() == "projection"
                    else "project"
                ),
                "point": point_values,
                "screen_offset": offset_values,
                "visible": bool(entry.get("visible", True)),
                "created_at": str(entry.get("created_at") or ""),
                "lines": [str(line) for line in lines] if isinstance(lines, list) else [],
            }
            if payload["scope_mode"] == "projection":
                payload["projection_scope"] = str(
                    entry.get("projection_scope") or ""
                ).strip()
            if box_anchor_values is not None:
                payload["box_anchor"] = box_anchor_values
            output.append(payload)
        return output

    def _save_snapshot_thumbnail(self, snapshot_path: Path) -> None:
        if self._canvas_widget is None:
            return
        icon_size = SNAPSHOT_PREVIEW_IMAGE_SIZE
        pix = self._canvas_widget.render_snapshot_pixmap(icon_size)
        pix.save(str(snapshot_path.with_suffix(".png")))

    def _save_component_presets_for_snapshot(self, label: str) -> DiscoverySnapshotRefs:
        assert self._project is not None
        appearance_ref = self._save_appearance_preset_for_snapshot(label)
        filters_ref = self._save_filter_preset_for_snapshot(label)
        camera_ref = self._save_camera_preset_for_snapshot(label)
        slicer_ref = self._save_slicer_preset_for_snapshot(label)
        transform_ref = self._save_transform_preset_for_snapshot(label)
        return DiscoverySnapshotRefs(
            appearance=appearance_ref,
            filters=filters_ref,
            camera=camera_ref,
            slicer=slicer_ref,
            transform=transform_ref,
        )

    def _save_transform_preset_for_snapshot(self, label: str) -> str | None:
        if self._project is None:
            return None
        settings = self._snapshot_applied_transform_settings()
        if settings.snap_percent <= 0 and settings.minimum_spacing <= 0.0:
            return None
        payload = {
            "name": label,
            "preset_version": 2,
            "transform": settings.to_preset_dict(),
        }
        path = self._service.ensure_transform_preset(self._project, label, payload)
        return relative_path_or_none(path, self._project.root)

    def _save_appearance_preset_for_snapshot(self, label: str) -> str | None:
        appearance_panel = self._panel(AppearancePanelController)
        if appearance_panel is None or self._project is None:
            return None
        payload = self._named_appearance_payload(label, appearance_panel.appearance_state_payload())
        path = self._service.ensure_appearance_preset(self._project, label, payload)
        return relative_path_or_none(path, self._project.root)

    def _save_filter_preset_for_snapshot(self, label: str) -> str | None:
        if self._project is None:
            return None
        panel = self._panel(FiltersPanelController)
        if panel is None:
            return None
        payload = panel.filter_state_payload()
        if not self._filter_snapshot_preset_is_meaningful(payload):
            return None
        path = self._service.ensure_filter_preset(self._project, label, payload)
        return relative_path_or_none(path, self._project.root)

    def _save_camera_preset_for_snapshot(self, label: str) -> str | None:
        if self._project is None or self._canvas_widget is None:
            return None
        state_getter = getattr(self._canvas_widget, "camera_preset_state", None)
        camera_state = state_getter() if callable(state_getter) else self._canvas_widget.camera_state()
        payload = {"camera": camera_state}
        path = self._service.ensure_camera_preset(self._project, label, payload)
        return relative_path_or_none(path, self._project.root)

    def _save_slicer_preset_for_snapshot(self, label: str) -> str | None:
        if self._project is None:
            return None
        panel = self._panel(SlicerPanelController)
        if panel is None:
            return None
        payload = panel.slicer_state_payload()
        if not self._slicer_snapshot_preset_is_meaningful(payload):
            return None
        path = self._service.ensure_slicer_preset(self._project, label, payload)
        return relative_path_or_none(path, self._project.root)

    @staticmethod
    def _filter_snapshot_preset_is_meaningful(payload: dict[str, object]) -> bool:
        rules = payload.get("rules")
        if isinstance(rules, list) and any(
            isinstance(rule, dict) and bool(rule.get("active")) for rule in rules
        ):
            return True
        return bool(
            payload.get("advanced_query_active")
            and str(payload.get("advanced_query") or "").strip()
        )

    @staticmethod
    def _slicer_snapshot_preset_is_meaningful(payload: dict[str, object]) -> bool:
        return bool(payload.get("enabled") and str(payload.get("column") or "").strip())

    def _relative_latest_path(self, entries: list) -> str | None:
        if self._project is None or not entries:
            return None
        return relative_path_or_none(entries[0].path, self._project.root)

    def _current_layer_state(self) -> dict[str, object]:
        panel = self._panel(CanvasLayersPanelController)
        if panel is None:
            return {}
        return {
            "tooltips_visible": bool(panel.widget.findChild(QtWidgets.QCheckBox, "checkbox_show_tooltips").isChecked()),
            "pins_visible": bool(panel.widget.findChild(QtWidgets.QCheckBox, "checkbox_show_pins").isChecked()),
            "areas_visible": bool(panel.widget.findChild(QtWidgets.QCheckBox, "checkbox_show_marked_areas").isChecked()),
            "viewport_aspect_preset": (
                self._canvas_widget.viewport_aspect_preset()
                if self._canvas_widget is not None
                else "free"
            ),
            "crop_bars_hidden": bool(panel.crop_bars_hidden()),
            "thumbnail_variant_id": (
                "none" if self._canvas_render_mode == "points_fast" else self._canvas_render_mode
            ),
            "thumbnail_size_px": int(self._canvas_thumbnail_size_px),
            "thumbnail_limit": int(self._canvas_thumbnail_limit),
            "thumbnail_limit_automatic": bool(
                self._canvas_thumbnail_limit_automatic
            ),
        }

    def _thumbnail_limit_bounds(self) -> tuple[int, int]:
        layers_panel = self._panel(LayersSettingPanelController)
        if layers_panel is None:
            return (MINIMUM_THUMBNAIL_LIMIT, MAXIMUM_THUMBNAIL_LIMIT)
        minimum, maximum = layers_panel.thumbnail_limit_bounds()
        minimum = max(1, int(minimum))
        return (minimum, max(minimum, int(maximum)))

    def _apply_snapshot_layer_state(self, payload: dict[str, object]) -> None:
        canvas_panel = self._panel(CanvasLayersPanelController)
        if canvas_panel is None or not isinstance(payload, dict):
            return
        self._disable_previews_layer()
        checkbox_map = {
            "tooltips_visible": "checkbox_show_tooltips",
            "pins_visible": "checkbox_show_pins",
            "areas_visible": "checkbox_show_marked_areas",
        }
        for key, name in checkbox_map.items():
            box = canvas_panel.widget.findChild(QtWidgets.QCheckBox, name)
            if box is not None and key in payload:
                box.setChecked(bool(payload.get(key)))
        if "viewport_aspect_preset" in payload:
            aspect_preset = str(payload.get("viewport_aspect_preset") or "free")
            canvas_panel.set_viewport_aspect_preset(aspect_preset)
            if self._canvas_widget is not None:
                self._canvas_widget.set_viewport_aspect_preset(aspect_preset)
        crop_bars_hidden = bool(payload.get("crop_bars_hidden", False))
        canvas_panel.set_crop_bars_hidden(crop_bars_hidden)
        if self._canvas_widget is not None:
            self._canvas_widget.set_crop_bars_visible(not crop_bars_hidden)
        layers_panel = self._panel(LayersSettingPanelController)
        if layers_panel is None:
            return
        variant_id = str(payload.get("thumbnail_variant_id") or "").strip()
        if variant_id:
            variants = self._service.list_thumbnail_variants(self._project) if self._project is not None else []
            layers_panel.set_render_options(variants, selected=variant_id)
        if "thumbnail_size_px" in payload:
            try:
                layers_panel.set_thumbnail_size_px(int(payload.get("thumbnail_size_px")))
            except (TypeError, ValueError):
                pass
        if "thumbnail_limit" in payload:
            try:
                limit_automatic = bool(
                    payload.get("thumbnail_limit_automatic", False)
                )
                if limit_automatic:
                    point_count = (
                        0
                        if self._active_dataset is None
                        else int(self._active_dataset.coordinates.shape[0])
                    )
                    self._apply_recommended_thumbnail_limit(point_count)
                    thumbnail_limit = self._canvas_thumbnail_limit
                else:
                    minimum, maximum = self._thumbnail_limit_bounds()
                    thumbnail_limit = max(
                        minimum,
                        min(maximum, int(payload.get("thumbnail_limit"))),
                    )
                    layers_panel.set_thumbnail_limit(
                        thumbnail_limit,
                        automatic=False,
                    )
                self._handle_thumbnail_limit_changed(thumbnail_limit)
            except (TypeError, ValueError):
                pass

    def _apply_snapshot_pinboard_state(self, payload: dict[str, object]) -> None:
        if not isinstance(payload, dict):
            return
        panel = self._panel(LayersSettingPanelController)
        if panel is None:
            return
        panel.apply_pin_display_settings_payload(payload)
        self._apply_pin_display_elements()

    def _handle_create_discovery_requested(self, payload: dict[str, object]) -> bool:
        if self._project is None:
            self._show_status("Open a project before creating a discovery.")
            return False
        discovery_name = str(payload.get("name") or "Discovery").strip() or "Discovery"
        if self._discovery_label_exists(discovery_name):
            self._show_status("Discovery name already exists.")
            return False
        method = str(payload.get("method") or "random").strip().lower()
        metadata_ids = self._project_metadata_ids(self._project)
        if metadata_ids.size == 0:
            self._show_status("Import metadata or run an analysis on assets before creating a projection.")
            return False
        filter_preset_key = str(payload.get("filter_preset_key") or "").strip()
        if filter_preset_key:
            try:
                filter_snapshot = self._service.load_filter_preset(
                    self._project, Path(filter_preset_key)
                )
                metadata_ids = self._service.metadata_ids_for_filter_preset(
                    self._project,
                    metadata_ids,
                    Path(filter_preset_key),
                )
                if isinstance(filter_snapshot, dict):
                    payload["filter_preset_snapshot"] = filter_snapshot
            except ValueError as exc:
                self._report_projection_request_error(str(exc))
                return False
            if metadata_ids.size < 2:
                self._report_projection_request_error(
                    "The selected filter preset leaves fewer than two items for projection."
                )
                return False
        artifact: Path | None = None
        if method not in {"random", "custom_coordinates"}:
            source_key = str(payload.get("source_key") or "")
            artifact = self._artifact_path_for_source_key(source_key)
            if artifact is None:
                self._show_status("Select a completed embeddings source.")
                return False
        run_id = uuid.uuid4().hex
        request = _QueuedDiscoveryRun(
            run_id=run_id,
            project=self._project,
            payload=dict(payload),
            metadata_ids=np.asarray(metadata_ids, dtype=np.int64),
            artifact=artifact,
            selection_before_run=self._active_discovery_key,
        )
        discovery_panel = self._panel(DiscoveryPanelController)
        if discovery_panel is not None:
            discovery_panel.add_queued_run(run_id, payload)
        self._queued_discovery_runs.append(request)
        self._start_next_discovery_run()
        return True

    def _start_next_discovery_run(self) -> None:
        if self._shutting_down or self._active_discovery_run is not None:
            return
        while self._queued_discovery_runs:
            request = self._queued_discovery_runs.popleft()
            if request.project is not self._project:
                panel = self._panel(DiscoveryPanelController)
                if panel is not None:
                    panel.remove_run(request.run_id)
                continue
            break
        else:
            return
        self._active_discovery_run = request
        self._discovery_cancel_requested = False
        self._discovery_selection_before_run = request.selection_before_run
        if self._activity_manager is not None and hasattr(self._activity_manager, "processes"):
            self._discovery_admission = self._activity_manager.processes.submit(
                panel="Projections",
                title=str(request.payload.get("name") or "Projection"),
                projection=True,
                start=lambda current=request: self._begin_discovery_run(current),
                cancel=self._cancel_admitted_discovery_run,
            )
            self._activity_manager.processes.dispatch()
            return
        self._begin_discovery_run(request)

    def _begin_discovery_run(self, request: _QueuedDiscoveryRun) -> None:
        if request is not self._active_discovery_run or self._discovery_cancel_requested:
            return
        panel = self._panel(DiscoveryPanelController)
        if panel is not None:
            panel.set_run_started(request.run_id)
        thread = QtCore.QThread(self._widget)
        worker = DiscoveryRunWorker(
            request.project,
            request.payload,
            request.metadata_ids,
            request.artifact,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._discovery_run_bridge.on_progress)
        worker.finished.connect(self._discovery_run_bridge.on_finished)
        worker.failed.connect(self._discovery_run_bridge.on_failed)
        worker.cancelled.connect(self._discovery_run_bridge.on_cancelled)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        worker.cancelled.connect(worker.deleteLater)
        thread.finished.connect(self._discovery_run_bridge.on_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._discovery_run_thread = thread
        self._discovery_run_worker = worker
        self._pending_discovery_option_key = None
        self._pending_discovery_error = None
        thread.start()

    def _cancel_admitted_discovery_run(self) -> None:
        self._discovery_cancel_requested = True
        if self._discovery_run_worker is not None:
            self._discovery_run_worker.request_cancel()

    def _report_projection_request_error(self, message: str) -> None:
        self._show_status(message)

    def _discovery_label_exists(self, label: str) -> bool:
        normalized = label.strip().casefold()
        if not normalized:
            return False
        saved_exists = any(
            option.label.strip().casefold() == normalized
            for option in self._discovery_options.values()
            if not option.is_random
        )
        panel = self._panel(DiscoveryPanelController)
        return saved_exists or (
            panel is not None and normalized in panel.active_run_names()
        )

    def _handle_import_discovery_requested(self, payload: dict[str, object]) -> bool:
        if self._project is None:
            self._show_status("Open a project before importing discovery data.")
            return False
        discovery_name = str(payload.get("name") or "Discovery").strip() or "Discovery"
        if self._discovery_label_exists(discovery_name):
            self._show_status("Discovery name already exists.")
            return False
        metadata_ids = self._project_metadata_ids(self._project)
        if metadata_ids.size == 0:
            self._show_status("Import metadata or run an analysis on assets before importing coordinates.")
            return False
        try:
            option = self._service.import_discovery_data(
                self._project,
                coordinate_file=Path(str(payload.get("coordinate_file") or "")),
                metadata_ids=metadata_ids,
                label=discovery_name,
                id_column=str(payload.get("id_column") or ""),
                metadata_column=str(payload.get("metadata_column") or ""),
                x_column=str(payload.get("x_column") or ""),
                y_column=str(payload.get("y_column") or ""),
            )
        except Exception as exc:
            self._show_status(f"Discovery import failed: {exc}")
            return False
        self._reload_discovery_options()
        discovery_panel = self._panel(DiscoveryPanelController)
        if discovery_panel is not None:
            discovery_panel.set_discovery_options(
                list(self._discovery_options.values()),
                enabled=True,
                selected_key=option.key,
            )
        QtCore.QTimer.singleShot(0, lambda key=option.key: self._handle_discovery_selection(key))
        self._show_status(f"Imported discovery: {option.label}")
        return True

    def _handle_export_discovery_requested(self, payload: dict[str, object]) -> bool:
        if self._project is None:
            self._show_status("Open a project before exporting discovery coordinates.")
            return False
        selection_key = str(payload.get("selection_key") or self._active_discovery_key or "random")
        option = self._discovery_options.get(selection_key)
        if option is None:
            self._show_status("Select a discovery before exporting coordinates.")
            return False
        metadata_ids = self._project_metadata_ids(self._project)
        if metadata_ids.size == 0:
            self._show_status("No items are available to export.")
            return False
        activity = (
            self._activity_manager.progress(
                "Preparing projection export…", value=0, total=100
            )
            if self._activity_manager is not None
            else None
        )
        try:
            if activity is not None:
                activity.update(message="Writing projection coordinates…", value=35)
            destination = self._service.export_discovery_coordinates(
                self._project,
                option,
                metadata_ids,
                Path(str(payload.get("destination") or "")),
                str(payload.get("export_format") or "csv"),
                "metadata_id",
                "current",
                self._active_arrangement_ref,
            )
            if activity is not None:
                activity.update(message="Projection export complete", value=100)
                activity.retain_completed()
        except Exception as exc:
            message = f"Projection export failed: {exc}"
            if activity is not None:
                activity.retain_failed(message)
            else:
                self._show_status(message)
            return False
        finally:
            if activity is not None:
                activity.finish()
        if activity is None:
            self._show_status(f"Exported discovery coordinates to {destination}")
        return True

    def _methods_report_for_projection(self, selection_key: str):
        option = self._discovery_options.get(str(selection_key))
        if option is None:
            self._show_status("Select a saved projection before opening methods and citations.")
            return None
        return self._service.methods_report(option)

    def _discovery_import_metadata_columns(self) -> list[str]:
        if self._project is None:
            return []
        return [column for column in self._metadata_columns_cache if column != "id"]

    def _discovery_import_metadata_values(self, column_name: str) -> dict[int, str] | None:
        if self._project is None:
            return None
        column = str(column_name or "").strip()
        if not column or column in {"metadata_id", "id"}:
            return None
        try:
            access = self._metadata_access or DiscoveryMetadataAccess(self._project)
            return access.values_by_id(column)
        except Exception:
            return None

    def _handle_discovery_run_progress(self, value: int, message: str) -> None:
        run = self._active_discovery_run
        if run is None:
            return
        discovery_panel = self._panel(DiscoveryPanelController)
        if discovery_panel is not None:
            discovery_panel.set_run_progress(run.run_id, value, message)

    def _handle_abort_discovery_requested(self, run_id: str) -> bool:
        for request in tuple(self._queued_discovery_runs):
            if request.run_id != run_id:
                continue
            self._queued_discovery_runs.remove(request)
            panel = self._panel(DiscoveryPanelController)
            if panel is not None:
                panel.remove_run(run_id)
            self._show_status(f"Cancelled queued projection: {request.payload.get('name', 'Projection')}")
            return True
        active_run = self._active_discovery_run
        if active_run is None or active_run.run_id != run_id:
            return False
        worker = self._discovery_run_worker
        if worker is None and self._discovery_admission is not None:
            self._discovery_admission.cancel()
            panel = self._panel(DiscoveryPanelController)
            if panel is not None:
                panel.remove_run(run_id)
            self._finish_active_discovery_run()
            return True
        has_pending_completion = bool(
            self._pending_discovery_option_key or self._pending_discovery_canvas_key
        )
        if worker is None and not has_pending_completion:
            return False
        if getattr(self, "_discovery_cancel_requested", False):
            return True
        self._discovery_cancel_requested = True
        self._show_status("Aborting projection at a safe checkpoint...")
        if worker is not None:
            worker.request_cancel()
        if self._discovery_run_thread is None or not self._discovery_run_thread.isRunning():
            self._process_pending_discovery_completion()
        return True

    def _cancel_all_discovery_runs(self) -> None:
        panel = self._panel(DiscoveryPanelController)
        self._queued_discovery_runs.clear()
        if panel is not None:
            panel.clear_runs()
        if self._active_discovery_run is None:
            return
        self._discovery_cancel_requested = True
        if self._discovery_admission is not None and self._discovery_run_worker is None:
            self._discovery_admission.cancel()
            self._finish_active_discovery_run()
            return
        if self._discovery_run_worker is not None:
            self._discovery_run_worker.request_cancel()
        if self._discovery_run_thread is None or not self._discovery_run_thread.isRunning():
            self._process_pending_discovery_completion()

    def _handle_discovery_run_finished(self, option_key: str) -> None:
        self._discovery_debug("discovery_worker_finished_signal", option_key=option_key)
        self._pending_discovery_option_key = option_key
        self._pending_discovery_error = None
        if self._discovery_run_thread is None or not self._discovery_run_thread.isRunning():
            self._process_pending_discovery_completion()

    def _handle_discovery_thread_finished(self) -> None:
        self._discovery_debug(
            "discovery_thread_finished",
            pending_key=self._pending_discovery_option_key,
            pending_error=self._pending_discovery_error,
        )
        self._discovery_run_worker = None
        self._discovery_run_thread = None
        self._process_pending_discovery_completion()

    def _process_pending_discovery_completion(self) -> None:
        self._discovery_debug(
            "discovery_completion_process_start",
            thread_running=self._discovery_run_thread is not None and self._discovery_run_thread.isRunning(),
            pending_key=self._pending_discovery_option_key,
            pending_error=self._pending_discovery_error,
        )
        if self._discovery_run_thread is not None and self._discovery_run_thread.isRunning():
            return
        message = self._pending_discovery_error
        option_key = self._pending_discovery_option_key
        run = self._active_discovery_run
        if run is None:
            return
        self._pending_discovery_error = None
        self._pending_discovery_option_key = None
        discovery_panel = self._panel(DiscoveryPanelController)
        project_changed = run.project is not self._project
        if getattr(self, "_discovery_cancel_requested", False) or project_changed:
            cancelled_key = option_key or self._pending_discovery_canvas_key
            was_loaded = bool(
                cancelled_key
                and (
                    cancelled_key == self._active_discovery_key
                    or cancelled_key in self._discovery_options
                )
            )
            self._pending_discovery_canvas_key = None
            self._pending_discovery_canvas_ready = False
            self._pending_discovery_reload_complete = False
            if cancelled_key:
                self._service.discard_discovery_result(run.project, cancelled_key)
            restore_key = getattr(self, "_discovery_selection_before_run", None) or "random"
            if was_loaded and not project_changed:
                self._reload_discovery_options(selected_key=restore_key)
            if discovery_panel is not None:
                discovery_panel.remove_run(run.run_id)
            if not project_changed:
                self._show_status("Projection run aborted safely.")
            self._discovery_debug("discovery_completion_aborted", option_key=cancelled_key)
            self._finish_active_discovery_run()
            return
        if message:
            if discovery_panel is not None:
                discovery_panel.set_run_failed(run.run_id, message)
            self._show_status(f"Discovery failed: {message}")
            self._discovery_debug("discovery_completion_failed", message=message)
            self._finish_active_discovery_run()
            return
        if not option_key:
            self._discovery_debug("discovery_completion_no_key")
            self._finish_active_discovery_run()
            return
        self._discovery_debug("discovery_completion_reload_start", option_key=option_key)
        self._pending_discovery_canvas_key = option_key
        self._pending_discovery_canvas_ready = False
        self._pending_discovery_reload_complete = False
        if discovery_panel is not None:
            discovery_panel.bind_run_to_projection(run.run_id, option_key)
            discovery_panel.set_run_progress(run.run_id, 95, "Reloading saved projection...")
        try:
            self._reload_discovery_options(selected_key=option_key)
        except Exception as exc:
            self._fail_pending_discovery_display(
                f"Projection was saved but could not be reloaded: {exc}"
            )
            return
        self._pending_discovery_reload_complete = True
        self._complete_pending_discovery_display_if_ready()

    def _handle_discovery_run_failed(self, message: str) -> None:
        self._discovery_debug("discovery_worker_failed_signal", message=message)
        self._pending_discovery_error = message
        self._pending_discovery_option_key = None
        if self._discovery_run_thread is None or not self._discovery_run_thread.isRunning():
            self._process_pending_discovery_completion()

    def _handle_discovery_run_cancelled(self) -> None:
        self._discovery_debug("discovery_worker_cancelled_signal")
        self._discovery_cancel_requested = True
        self._pending_discovery_error = None
        if self._discovery_run_thread is None or not self._discovery_run_thread.isRunning():
            self._process_pending_discovery_completion()

    def _set_pending_discovery_display_progress(self, value: int, message: str) -> None:
        if self._pending_discovery_canvas_key != self._active_discovery_key:
            return
        run = self._active_discovery_run
        if run is None:
            return
        discovery_panel = self._panel(DiscoveryPanelController)
        if discovery_panel is not None:
            discovery_panel.set_run_progress(run.run_id, value, message)

    def _mark_pending_discovery_canvas_ready(self) -> None:
        self._pending_discovery_canvas_ready = True
        self._set_pending_discovery_display_progress(99, "Projection is ready on the canvas.")
        self._complete_pending_discovery_display_if_ready()

    def _complete_pending_discovery_display_if_ready(self) -> None:
        if not (
            self._pending_discovery_canvas_key
            and self._pending_discovery_canvas_ready
            and self._pending_discovery_reload_complete
        ):
            return
        option_key = self._pending_discovery_canvas_key
        self._pending_discovery_canvas_key = None
        self._pending_discovery_canvas_ready = False
        self._pending_discovery_reload_complete = False
        run = self._active_discovery_run
        discovery_panel = self._panel(DiscoveryPanelController)
        if discovery_panel is not None and run is not None:
            discovery_panel.set_run_finished(run.run_id)
        option = self._discovery_options.get(option_key)
        if option is not None:
            self._show_status(f"Projection ready: {option.label}")
        self._finish_active_discovery_run()

    def _fail_pending_discovery_display(self, message: str) -> None:
        self._pending_discovery_canvas_key = None
        self._pending_discovery_canvas_ready = False
        self._pending_discovery_reload_complete = False
        run = self._active_discovery_run
        discovery_panel = self._panel(DiscoveryPanelController)
        if discovery_panel is not None and run is not None:
            discovery_panel.set_run_failed(run.run_id, message)
        self._show_status(message)
        self._finish_active_discovery_run()

    def _finish_active_discovery_run(self) -> None:
        admission = self._discovery_admission
        self._discovery_admission = None
        if admission is not None:
            admission.finish()
        self._active_discovery_run = None
        self._discovery_cancel_requested = False
        self._discovery_selection_before_run = None
        if not self._shutting_down:
            QtCore.QTimer.singleShot(0, self._start_next_discovery_run)

    def _handle_delete_discovery_requested(self, selection_key: str) -> None:
        if self._project is None:
            return
        option = self._discovery_options.get(selection_key)
        if option is None or option.is_random:
            return
        removed = self._service.delete_discovery(self._project, option)
        self._reload_discovery_options()
        self._show_status("Discovery deleted." if removed else "Discovery could not be deleted.")

    def _handle_rename_discovery_requested(self, selection_key: str, label: str) -> bool:
        if self._project is None:
            return False
        option = self._discovery_options.get(selection_key)
        if option is None or option.is_random:
            return False
        try:
            renamed = self._service.rename_discovery(self._project, option, label)
        except ValueError as exc:
            self._show_status(str(exc))
            return False
        if not renamed:
            return False
        self._reload_discovery_options()
        discovery_panel = self._panel(DiscoveryPanelController)
        if discovery_panel is not None:
            discovery_panel.set_discovery_options(
                list(self._discovery_options.values()),
                enabled=True,
                selected_key=selection_key,
            )
        self._handle_discovery_selection(selection_key)
        self._show_status(f"Discovery renamed to {label.strip()}.")
        return True

    def _load_polygon_areas_for_active_discovery(self) -> None:
        if self._project is None:
            self._polygon_areas = []
            self._sync_areas_panel()
            return
        scope = self._active_discovery_scope()
        if scope is None:
            self._polygon_areas = []
            self._sync_areas_panel()
            return
        self._polygon_areas = self._service.list_polygon_areas(self._project, scope)
        self._sync_areas_panel()

    def _active_discovery_scope(self) -> str | None:
        option = self._discovery_options.get(self._active_discovery_key)
        if option is None:
            return None
        return self._service.discovery_scope_for_option(option)

    def _apply_canvas_polygons(self) -> None:
        if self._canvas_widget is None:
            return
        if not self._show_marked_areas:
            self._canvas_widget.clear_lasso_polygons()
            return
        payloads = [
            {
                "area_id": area.area_id,
                "label": area.label,
                "color_hex": area.color_hex,
                "area_visible": area.area_visible,
                "points_visible": area.points_visible,
                "polygon": [[float(x), float(y)] for x, y in area.polygon],
            }
            for area in self._polygon_areas
            if area.area_visible and area.arrangement_ref == self._active_arrangement_ref
        ]
        self._canvas_widget.set_polygon_render_state(
            payloads,
            fill_opacity=self._marked_area_opacity,
        )
        if self._selected_polygon_area_id:
            index = self._polygon_index_by_area_id(self._selected_polygon_area_id)
            self._canvas_widget.set_lasso_selected_polygon_index(index)

    def _sync_areas_panel(self) -> None:
        panel = self._panel(LayersSettingPanelController)
        if panel is None:
            return
        total = 0
        if self._active_dataset is not None:
            total = int(self._active_dataset.metadata_ids.shape[0])
        visible_member_ids: set[int] = set()
        for area in self._polygon_areas:
            if area.points_visible:
                visible_member_ids.update(int(value) for value in area.metadata_ids)
        panel.set_marked_areas(
            [
                {
                    "area_id": area.area_id,
                    "label": area.label,
                    "color_hex": area.color_hex,
                    "area_visible": area.area_visible,
                    "points_visible": area.points_visible,
                    "share": f"{(len(area.metadata_ids) / total * 100.0):.1f}%" if total > 0 else "0.0%",
                }
                for area in self._polygon_areas
            ],
            selected_area_id=self._selected_polygon_area_id,
            all_areas_share=f"{(len(visible_member_ids) / total * 100.0):.1f}%" if total > 0 else "0.0%",
        )

    def _polygon_index_by_area_id(self, area_id: str) -> int | None:
        if self._canvas_widget is None:
            return None
        payloads = self._canvas_widget.lasso_area_payloads()
        for index, payload in enumerate(payloads):
            if str(payload.get("area_id") or "") == str(area_id):
                return index
        return None

    def _prompt_area_label(self) -> str | None:
        text, accepted = QtWidgets.QInputDialog.getText(self._widget, "Save Area", "Area label:")
        if not accepted:
            return None
        label = text.strip()
        return label or None

    def _handle_lasso_polygon_created(self, payload: dict) -> None:
        if self._project is None or self._active_dataset is None:
            return
        scope = self._active_discovery_scope()
        if scope is None:
            return
        label = self._prompt_area_label()
        polygon_points = self._service.normalize_polygon_points(payload.get("polygon"))
        if label is None or len(polygon_points) < 3:
            index = int(payload.get("polygon_index", -1))
            if index >= 0 and self._canvas_widget is not None:
                self._canvas_widget.remove_lasso_polygon(index)
            return
        metadata_ids = self._service.metadata_ids_in_polygon(
            polygon_points=polygon_points,
            dataset=self._active_dataset,
            visibility=self._canvas_widget.visibility_array() if self._canvas_widget is not None else None,
            visible_threshold=0.5,
        )
        area = self._service.save_polygon_area(
            project=self._project,
            discovery_scope=scope,
            label=label,
            color_hex=self._next_polygon_area_color(),
            polygon_points=polygon_points,
            metadata_ids=metadata_ids,
            arrangement_ref=self._active_arrangement_ref,
        )
        self._sync_polygon_area_custom_group(area)
        self._selected_polygon_area_id = area.area_id
        self._selected_polygon_metadata_ids = set(metadata_ids)
        self._load_polygon_areas_for_active_discovery()
        self._render_active_dataset(preserve_view=True, fit_view=False)

    def _next_polygon_area_color(self) -> str:
        return next_unused_area_color([area.color_hex for area in self._polygon_areas])

    def _queue_lasso_polygon_created(self, payload: dict) -> None:
        payload_copy = dict(payload or {})
        QtCore.QTimer.singleShot(0, lambda: self._handle_lasso_polygon_created(payload_copy))

    def _handle_lasso_polygon_selected(self, payload: dict) -> None:
        area_id = str(payload.get("area_id") or "").strip()
        if not area_id:
            return
        self._selected_polygon_area_id = area_id
        self._sync_areas_panel()

    def _handle_lasso_polygon_context(self, payload: dict) -> None:
        polygon_index = int(payload.get("polygon_index", -1))
        if polygon_index < 0 or self._canvas_widget is None:
            return
        area_id = str(payload.get("area_id") or "").strip()
        label = str(payload.get("label") or "").strip() or self._area_label_by_id(area_id)
        anchor_index = payload.get("anchor_index")
        menu = QtWidgets.QMenu(self._widget)
        action_rename = menu.addAction("Rename Area…")
        action_color = menu.addAction("Change Color…")
        menu.addSeparator()
        if anchor_index is not None:
            action_anchor_remove = menu.addAction("Remove Anchor")
        else:
            action_anchor_remove = None
        action_remove = menu.addAction("Remove Area…")
        chosen = menu.exec(QtGui.QCursor.pos())
        if chosen == action_rename and area_id:
            next_label, accepted = QtWidgets.QInputDialog.getText(
                self._widget,
                "Rename Area",
                "Area label:",
                text=label,
            )
            if accepted and next_label.strip():
                self._handle_area_renamed(area_id, next_label.strip())
            return
        if chosen == action_color and area_id:
            current_color = QtGui.QColor(str(payload.get("color_hex") or "#3b82f6"))
            if not current_color.isValid():
                current_color = QtGui.QColor("#3b82f6")
            color = QtWidgets.QColorDialog.getColor(current_color, self._widget, "Select area color")
            if color.isValid():
                self._handle_area_color_changed(area_id, color.name())
            return
        if chosen == action_anchor_remove and anchor_index is not None:
            try:
                vertex_index = int(anchor_index)
            except (TypeError, ValueError):
                return
            self._canvas_widget.remove_lasso_anchor(polygon_index, vertex_index)
            return
        if chosen == action_remove:
            self._request_remove_lasso_polygon(polygon_index, label=label)

    def _queue_lasso_polygon_context(self, payload: dict) -> None:
        payload_copy = dict(payload or {})
        QtCore.QTimer.singleShot(0, lambda: self._handle_lasso_polygon_context(payload_copy))

    def _handle_lasso_polygon_delete_requested(self, payload: dict) -> None:
        if self._canvas_widget is None:
            return
        polygon_index = int(payload.get("polygon_index", -1))
        if polygon_index < 0:
            return
        label = str(payload.get("label") or "").strip() or self._area_label_by_id(str(payload.get("area_id") or ""))
        self._request_remove_lasso_polygon(polygon_index, label=label)

    def _queue_lasso_polygon_delete_requested(self, payload: dict) -> None:
        payload_copy = dict(payload or {})
        QtCore.QTimer.singleShot(0, lambda: self._handle_lasso_polygon_delete_requested(payload_copy))

    def _area_label_by_id(self, area_id: str) -> str:
        area_key = str(area_id or "").strip()
        for area in self._polygon_areas:
            if area.area_id == area_key:
                return area.label
        return ""

    def _confirm_remove_area(self, label: str) -> bool:
        area_label = str(label or "selected area").strip() or "selected area"
        reply = QtWidgets.QMessageBox.question(
            self._widget,
            "Remove Area",
            f'Remove area "{area_label}"?',
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        return reply == QtWidgets.QMessageBox.StandardButton.Yes

    def _request_remove_lasso_polygon(self, polygon_index: int, *, label: str = "") -> None:
        if self._canvas_widget is None:
            return
        if not self._confirm_remove_area(label):
            self._show_status("Area removal cancelled.")
            return
        self._canvas_widget.remove_lasso_polygon(polygon_index)

    def _handle_lasso_polygon_removed(self, payload: dict) -> None:
        area_id = str(payload.get("area_id") or "").strip()
        if not area_id or self._project is None:
            return
        self._service.remove_polygon_area(project=self._project, area_id=area_id)
        self._service.remove_polygon_area_group(project=self._project, area_id=area_id)
        self._notify_custom_groups_changed()
        if self._selected_polygon_area_id == area_id:
            self._selected_polygon_area_id = None
            self._selected_polygon_metadata_ids.clear()
        self._load_polygon_areas_for_active_discovery()
        self._render_active_dataset(preserve_view=True, fit_view=False)

    def _handle_lasso_polygon_updated(self, payload: dict) -> None:
        if self._project is None or self._active_dataset is None:
            return
        area_id = str(payload.get("area_id") or "").strip()
        if not area_id:
            return
        polygon_points = self._service.normalize_polygon_points(payload.get("polygon"))
        if len(polygon_points) < 3:
            return
        metadata_ids = self._service.metadata_ids_in_polygon(
            polygon_points=polygon_points,
            dataset=self._active_dataset,
            visibility=self._canvas_widget.visibility_array() if self._canvas_widget is not None else None,
            visible_threshold=0.5,
        )
        self._service.update_polygon_area_geometry(
            project=self._project,
            area_id=area_id,
            polygon_points=polygon_points,
            metadata_ids=metadata_ids,
        )
        if self._selected_polygon_area_id == area_id:
            self._selected_polygon_metadata_ids = set(metadata_ids)
        self._load_polygon_areas_for_active_discovery()
        area = next((item for item in self._polygon_areas if item.area_id == area_id), None)
        if area is not None:
            self._sync_polygon_area_custom_group(area)
        self._render_active_dataset(preserve_view=True, fit_view=False)

    def _handle_areas_visibility_toggled(self, visible: bool) -> None:
        self._show_marked_areas = bool(visible)
        if self._active_dataset is not None:
            self._render_active_dataset(preserve_view=True, fit_view=False)
        else:
            self._apply_canvas_polygons()
        self._refresh_current_snapshot_update_state()

    def _handle_area_tree_selection_changed(self, area_id: str) -> None:
        self._selected_polygon_area_id = str(area_id)
        self._selected_polygon_metadata_ids.clear()
        for area in self._polygon_areas:
            if area.area_id == self._selected_polygon_area_id and area.points_visible:
                self._selected_polygon_metadata_ids = set(area.metadata_ids)
                break
        if self._canvas_widget is not None:
            self._canvas_widget.set_lasso_selected_polygon_index(self._polygon_index_by_area_id(area_id))
        if self._active_dataset is not None:
            self._render_active_dataset(preserve_view=True, fit_view=False)

    def _handle_area_tree_delete_requested(self, area_id: str) -> None:
        if self._project is None:
            return
        label = self._area_label_by_id(area_id)
        if not self._confirm_remove_area(label):
            self._show_status("Area removal cancelled.")
            return
        self._service.remove_polygon_area(project=self._project, area_id=area_id)
        self._service.remove_polygon_area_group(project=self._project, area_id=area_id)
        self._notify_custom_groups_changed()
        if self._selected_polygon_area_id == area_id:
            self._selected_polygon_area_id = None
            self._selected_polygon_metadata_ids.clear()
        self._load_polygon_areas_for_active_discovery()
        self._render_active_dataset(preserve_view=True, fit_view=False)

    def _queue_area_tree_delete_requested(self, area_id: str) -> None:
        area_key = str(area_id)
        defer_to_event_loop(lambda: self._handle_area_tree_delete_requested(area_key))

    def _handle_area_visibility_changed(self, area_id: str, visible: bool) -> None:
        if self._project is None:
            return
        self._service.set_polygon_area_visibility(project=self._project, area_id=area_id, area_visible=visible)
        self._load_polygon_areas_for_active_discovery()
        self._apply_canvas_polygons()
        self._refresh_current_snapshot_update_state()

    def _queue_area_visibility_changed(self, area_id: str, visible: bool) -> None:
        area_key = str(area_id)
        next_visible = bool(visible)
        defer_to_event_loop(lambda: self._handle_area_visibility_changed(area_key, next_visible))

    def _handle_area_points_visibility_changed(self, area_id: str, visible: bool) -> None:
        if self._project is None:
            return
        self._service.set_polygon_area_visibility(project=self._project, area_id=area_id, points_visible=visible)
        self._load_polygon_areas_for_active_discovery()
        if self._active_dataset is not None:
            self._render_active_dataset(preserve_view=True, fit_view=False)
        self._refresh_current_snapshot_update_state()

    def _queue_area_points_visibility_changed(self, area_id: str, visible: bool) -> None:
        area_key = str(area_id)
        next_visible = bool(visible)
        defer_to_event_loop(lambda: self._handle_area_points_visibility_changed(area_key, next_visible))

    def _handle_all_area_visibility_changed(self, visible: bool) -> None:
        if self._project is None:
            return
        self._service.set_polygon_areas_visibility(
            project=self._project,
            area_ids=[area.area_id for area in self._polygon_areas],
            area_visible=visible,
        )
        self._load_polygon_areas_for_active_discovery()
        self._apply_canvas_polygons()
        self._refresh_current_snapshot_update_state()

    def _queue_all_area_visibility_changed(self, visible: bool) -> None:
        next_visible = bool(visible)
        defer_to_event_loop(lambda: self._handle_all_area_visibility_changed(next_visible))

    def _handle_all_area_points_visibility_changed(self, visible: bool) -> None:
        if self._project is None:
            return
        self._service.set_polygon_areas_visibility(
            project=self._project,
            area_ids=[area.area_id for area in self._polygon_areas],
            points_visible=visible,
        )
        self._load_polygon_areas_for_active_discovery()
        if self._active_dataset is not None:
            self._render_active_dataset(preserve_view=True, fit_view=False)
        self._refresh_current_snapshot_update_state()

    def _queue_all_area_points_visibility_changed(self, visible: bool) -> None:
        next_visible = bool(visible)
        defer_to_event_loop(lambda: self._handle_all_area_points_visibility_changed(next_visible))

    def _handle_area_renamed(self, area_id: str, label: str) -> None:
        if self._project is None or not label:
            return
        self._service.rename_polygon_area(project=self._project, area_id=area_id, label=label)
        self._load_polygon_areas_for_active_discovery()
        area = next((item for item in self._polygon_areas if item.area_id == area_id), None)
        if area is not None:
            self._sync_polygon_area_custom_group(area, rename_existing=True)
        self._apply_canvas_polygons()

    def _sync_polygon_area_custom_group(
        self, area, *, rename_existing: bool = False
    ) -> None:
        if self._project is None:
            return
        option = self._discovery_options.get(self._active_discovery_key)
        discovery_label = option.label if option is not None else "Discovery"
        try:
            self._service.sync_polygon_area_group(
                project=self._project,
                area=area,
                discovery_label=discovery_label,
                rename_existing=rename_existing,
            )
        except Exception as exc:
            self._show_status(f"Area saved, but its custom group could not be updated: {exc}")
            return
        self._notify_custom_groups_changed()

    def _notify_custom_groups_changed(self) -> None:
        self.refresh_metadata_groups()
        if callable(self._groups_changed_callback):
            self._groups_changed_callback()

    def _queue_area_renamed(self, area_id: str, label: str) -> None:
        area_key = str(area_id)
        next_label = str(label)
        defer_to_event_loop(lambda: self._handle_area_renamed(area_key, next_label))

    def _handle_area_color_changed(self, area_id: str, color_hex: str) -> None:
        if self._project is None:
            return
        self._service.set_polygon_area_color(project=self._project, area_id=area_id, color_hex=color_hex)
        self._load_polygon_areas_for_active_discovery()
        self._apply_canvas_polygons()
        if self._active_dataset is not None:
            self._render_active_dataset(preserve_view=True, fit_view=False)

    def _queue_area_color_changed(self, area_id: str, color_hex: str) -> None:
        area_key = str(area_id)
        next_color = str(color_hex)
        defer_to_event_loop(lambda: self._handle_area_color_changed(area_key, next_color))

    def _queue_export_area_requested(self, area_id: str) -> None:
        area_key = str(area_id)
        defer_to_event_loop(lambda: self._handle_export_area_requested(area_key))

    def _handle_export_area_requested(self, area_id: str) -> None:
        if self._project is None:
            self._show_status("Open a project before exporting an area.")
            return
        area = next((item for item in self._polygon_areas if item.area_id == area_id), None)
        if area is None:
            self._show_status("The selected area is no longer available.")
            return
        payload = AreaExportDialogController().exec(area)
        if payload is None:
            self._show_status("Area export cancelled.")
            return
        activity = (
            self._activity_manager.progress(
                "Preparing area export…", value=0, total=100
            )
            if self._activity_manager is not None
            else None
        )
        try:
            if activity is not None:
                activity.update(message="Writing area members…", value=35)
            destination = self._service.export_polygon_area_members(
                project=self._project,
                area=area,
                destination=Path(str(payload.get("destination") or "")),
                export_format=str(payload.get("export_format") or "csv"),
                include_metadata=bool(payload.get("include_metadata", False)),
            )
            if activity is not None:
                activity.update(message="Area export complete", value=100)
        except Exception as exc:
            message = f"Area export failed: {exc}"
            if activity is not None:
                activity.retain_failed(message)
            else:
                self._show_status(message)
            return
        finally:
            if activity is not None:
                activity.finish()
        self._show_status(f"Exported area members to {destination}")

    def _handle_write_areas_to_metadata_requested(self) -> None:
        if self._project is None:
            self._show_status("Open a project before saving areas as groups.")
            return
        if not self._polygon_areas:
            self._show_status("Create an area before saving area memberships as groups.")
            return
        overlap_count = self._service.polygon_area_overlap_count(list(self._polygon_areas))
        payload = SaveMetadataGroupsDialogController().exec(
            self._service.metadata_group_service_for_project(self._project),
            area_labels=[(area.area_id, area.label) for area in self._polygon_areas],
            overlap_count=overlap_count,
        )
        if payload is None:
            self._show_status("Saving areas as groups cancelled.")
            return
        try:
            set_name, group_count, affected_count = self._service.save_polygon_areas_as_groups(
                project=self._project,
                areas=list(self._polygon_areas),
                group_set_id=payload.get("group_set_id"),
                new_group_set_name=str(payload.get("new_group_set_name") or ""),
                mode=str(payload.get("mode") or "overlapping"),
                priority_area_ids=[str(value) for value in payload.get("priority_area_ids", [])],
            )
        except Exception as exc:
            self._show_status(f"Saving areas as groups failed: {exc}")
            return
        self.refresh_metadata_groups()
        if callable(self._groups_changed_callback):
            self._groups_changed_callback()
        self._show_status(
            f"Saved {affected_count} item memberships across {group_count} groups in '{set_name}'."
        )

    def _handle_marked_area_style_changed(self, opacity: float) -> None:
        self._marked_area_opacity = float(np.clip(float(opacity), 0.0, 1.0))
        if self._active_dataset is not None:
            self._render_active_dataset(preserve_view=True, fit_view=False)
        self._refresh_current_snapshot_update_state()

    def _discovery_source_options(self) -> list[tuple[str, str]]:
        if self._project is None:
            return []
        runs_db = self._project.paths.embeddings / "embeddings.db"
        if not runs_db.exists():
            return []
        repository = EmbeddingRunRepository(runs_db)
        options: list[tuple[str, str]] = []
        for run in repository.list_runs():
            if run.state != EmbeddingRunState.COMPLETED or not run.artifact_path:
                continue
            artifact = Path(run.artifact_path)
            if not artifact.exists():
                continue
            options.append((run.label, f"run:{run.id}"))
        return options

    def _projection_filter_preset_options(self) -> list[tuple[str, str]]:
        if self._project is None:
            return []
        return [
            (entry.label, str(entry.path.resolve()))
            for entry in self._service.list_filter_preset_entries(self._project)
        ]

    def _artifact_path_for_source_key(self, source_key: str) -> Path | None:
        if self._project is None:
            return None
        if not source_key.startswith("run:"):
            return None
        run_id = source_key.split(":", 1)[1]
        runs_db = self._project.paths.embeddings / "embeddings.db"
        if not runs_db.exists():
            return None
        repository = EmbeddingRunRepository(runs_db)
        run = repository.get_run(run_id)
        if run is None or not run.artifact_path:
            return None
        artifact = Path(run.artifact_path)
        return artifact if artifact.exists() else None

    def _current_point_style(self) -> dict[str, float]:
        panel = self._panel(AppearancePanelController)
        if panel is None:
            return {"size": 5.0, "opacity": 1.0}
        return panel.point_style()

    def _current_point_color_style(self) -> dict[str, object]:
        panel = self._panel(AppearancePanelController)
        if panel is None:
            return {"mode": "categorical", "column": "Global", "palette": ["#000000"]}
        return panel.point_color_style()

    def _current_border_style(self) -> dict[str, object]:
        panel = self._panel(AppearancePanelController)
        if panel is None:
            return {"width": 0.0, "opacity": 1.0, "mode": "categorical", "column": "Global", "palette": ["#111111"]}
        return panel.border_style()

    def _compute_filter_style_for_dataset(
        self,
        dataset: DiscoveryDataset,
        *,
        base_size: float,
        base_edge_widths: np.ndarray | None,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
        del base_edge_widths
        sizes = np.full(dataset.coordinates.shape[0], base_size, dtype=np.float32)
        mask = self._filter_match_mask_for_dataset(dataset)
        if mask is None:
            visibility = np.ones(dataset.coordinates.shape[0], dtype=np.float32)
        else:
            opacity = float(np.clip(self._filtered_opacity, 0.0, 1.0))
            visibility = np.where(mask, 1.0, opacity).astype(np.float32)
        return visibility, sizes, None

    def _preview_visibility_for_dataset(self, dataset: DiscoveryDataset) -> np.ndarray:
        """Return thumbnail eligibility, independently from point opacity."""
        mask = self._filter_match_mask_for_dataset(dataset)
        if mask is None:
            return np.ones(dataset.coordinates.shape[0], dtype=np.float32)
        return np.asarray(mask, dtype=np.float32)

    def _filter_match_mask_for_dataset(self, dataset: DiscoveryDataset) -> np.ndarray | None:
        filters_panel = self._panel(FiltersPanelController)
        active_filters = filters_panel.active_filters() if filters_panel is not None else []
        filter_matching = (
            filters_panel.filter_matching()
            if filters_panel is not None and hasattr(filters_panel, "filter_matching")
            else FilterMatching.ALL
        )
        slicer_mask = self._slicer_mask_for_dataset(dataset)
        if not active_filters and slicer_mask is None:
            return None
        columns, numeric_columns = required_filter_columns(active_filters)
        metadata_values, metadata_numeric_values = self._metadata_values_for_ids(
            dataset.metadata_ids,
            columns=columns,
            numeric_columns=numeric_columns,
        )
        filter_mask = build_mask(
            active_filters,
            metadata_values,
            metadata_numeric_values,
            dataset.metadata_ids,
            filter_matching,
        )
        return self._combine_visibility_masks(filter_mask, slicer_mask)

    @staticmethod
    def _combine_visibility_masks(mask_a: np.ndarray | None, mask_b: np.ndarray | None) -> np.ndarray | None:
        if mask_a is None:
            return mask_b
        if mask_b is None:
            return mask_a
        if mask_a.shape != mask_b.shape:
            return mask_a
        combined = np.asarray(mask_a, dtype=bool) & np.asarray(mask_b, dtype=bool)
        if np.all(combined):
            return None
        return combined

    def _slicer_mask_for_dataset(self, dataset: DiscoveryDataset) -> np.ndarray | None:
        slicer_panel = self._panel(SlicerPanelController)
        if slicer_panel is None:
            return None
        selection = slicer_panel.active_selection()
        if not bool(selection.get("enabled")):
            return None
        column = str(selection.get("column") or "").strip()
        values = selection.get("values")
        if not column or not isinstance(values, list):
            return None
        if dataset.metadata_ids.size == 0:
            return None
        if not values:
            return np.zeros(dataset.metadata_ids.shape, dtype=bool)
        allowed = {str(value) for value in values}
        metadata_text, _numeric = self._metadata_values_for_ids(
            dataset.metadata_ids,
            columns={column},
            numeric_columns=set(),
        )
        column_values = metadata_text.get(column)
        if column_values is None:
            return None
        mask = np.isin(column_values, np.asarray(tuple(allowed), dtype=np.str_))
        if np.all(mask):
            return None
        return mask

    def _metadata_values_for_ids(
        self,
        metadata_ids: np.ndarray,
        *,
        columns: set[str] | None = None,
        numeric_columns: set[str] | None = None,
    ) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
        if metadata_ids.size == 0:
            return {}, {}
        if self._project is None and not self._metadata_row_map:
            return {}, {}
        row_map = self._metadata_row_map
        requested_columns = set(self._metadata_columns_cache) if columns is None else {str(value) for value in columns}
        requested_columns.discard("id")
        requested_numeric = set(requested_columns) if numeric_columns is None else {str(value) for value in numeric_columns}
        requested_numeric &= requested_columns
        if not row_map and self._metadata_access is not None:
            try:
                return self._metadata_access.values_for_ids(
                    metadata_ids,
                    columns=requested_columns,
                    numeric_columns=requested_numeric,
                )
            except Exception:
                return {}, {}
        cache_key = (
            id(metadata_ids),
            tuple(np.asarray(metadata_ids).shape),
            tuple(sorted(requested_columns)),
            tuple(sorted(requested_numeric)),
        )
        cached = self._metadata_array_cache.get(cache_key)
        if cached is not None and cached[0] is metadata_ids:
            return cached[1], cached[2]
        metadata_values: dict[str, np.ndarray] = {}
        metadata_numeric_values: dict[str, np.ndarray] = {}
        for column in sorted(requested_columns):
            values = [str(row_map.get(int(metadata_id), {}).get(column, "")) for metadata_id in metadata_ids]
            as_text = np.asarray(values, dtype=np.str_)
            metadata_values[column] = as_text
            if column not in requested_numeric:
                continue
            numeric = np.full(as_text.shape[0], np.nan, dtype=np.float32)
            try:
                numeric = np.asarray(values, dtype=np.float32)
            except (TypeError, ValueError):
                numeric = np.full(as_text.shape[0], np.nan, dtype=np.float32)
                for index, value in enumerate(values):
                    try:
                        numeric[index] = float(value)
                    except (TypeError, ValueError):
                        numeric[index] = np.nan
            metadata_numeric_values[column] = numeric
        if len(self._metadata_array_cache) >= 16:
            self._metadata_array_cache.clear()
        self._metadata_array_cache[cache_key] = (metadata_ids, metadata_values, metadata_numeric_values)
        return metadata_values, metadata_numeric_values

    def _compute_point_colors(self, dataset: DiscoveryDataset, *, alpha: float) -> np.ndarray:
        n = dataset.coordinates.shape[0]
        style = self._current_point_color_style()
        columns, numeric_columns = color_style_metadata_columns(style)
        metadata_values, metadata_numeric_values = self._metadata_values_for_ids(
            dataset.metadata_ids,
            columns=columns,
            numeric_columns=numeric_columns,
        )
        return mapped_colors(n, style, metadata_values, metadata_numeric_values, float(alpha), "#1f1f1f")

    def _compute_border_style_for_dataset(self, dataset: DiscoveryDataset) -> tuple[np.ndarray | None, np.ndarray | None]:
        style = self._current_border_style()
        width = float(style.get("width", 0.0))
        if width <= 0.0:
            return None, None
        rgba = self._compute_border_colors(dataset, alpha=float(np.clip(float(style.get("opacity", 1.0)), 0.0, 1.0)))
        return rgba, np.full(dataset.coordinates.shape[0], width, dtype=np.float32)

    def _compute_border_colors(self, dataset: DiscoveryDataset, *, alpha: float) -> np.ndarray:
        style = self._current_border_style()
        n = dataset.coordinates.shape[0]
        columns, numeric_columns = color_style_metadata_columns(style)
        metadata_values, metadata_numeric_values = self._metadata_values_for_ids(
            dataset.metadata_ids,
            columns=columns,
            numeric_columns=numeric_columns,
        )
        return mapped_colors(
            n,
            style,
            metadata_values,
            metadata_numeric_values,
            float(np.clip(float(alpha), 0.0, 1.0)),
            "#111111",
        )

    def _apply_polygon_point_colors(self, dataset: DiscoveryDataset, colors: np.ndarray) -> np.ndarray:
        """Apply marked-area colours to member point RGB without changing point alpha."""
        if not getattr(self, "_show_marked_areas", True):
            return np.asarray(colors, dtype=np.float32)
        visible_areas = [
            area
            for area in getattr(self, "_polygon_areas", [])
            if area.points_visible
        ]
        if not visible_areas:
            return np.asarray(colors, dtype=np.float32)
        selected_area_id = getattr(self, "_selected_polygon_area_id", None)
        visible_areas.sort(key=lambda area: area.area_id == selected_area_id)
        color_by_metadata_id: dict[int, tuple[float, float, float]] = {}
        for area in visible_areas:
            rgb = self._hex_to_rgb01(area.color_hex)
            color_by_metadata_id.update({int(metadata_id): rgb for metadata_id in area.metadata_ids})
        output = np.asarray(colors, dtype=np.float32).copy()
        for index, metadata_id in enumerate(dataset.metadata_ids.tolist()):
            rgb = color_by_metadata_id.get(int(metadata_id))
            if rgb is not None and index < output.shape[0]:
                output[index, :3] = rgb
        return output

    @staticmethod
    def _hex_to_rgb01(color: str) -> tuple[float, float, float]:
        text = color.strip().lstrip("#")
        if len(text) != 6:
            return 0.12, 0.12, 0.12
        try:
            r = int(text[0:2], 16) / 255.0
            g = int(text[2:4], 16) / 255.0
            b = int(text[4:6], 16) / 255.0
            return float(r), float(g), float(b)
        except ValueError:
            return 0.12, 0.12, 0.12

    def _project_metadata_ids(self, project: Project) -> np.ndarray:
        try:
            access = (
                self._metadata_access
                if project is self._project and self._metadata_access is not None
                else DiscoveryMetadataAccess(project)
            )
            return access.metadata_ids()
        except Exception:
            return np.empty((0,), dtype=np.int64)
