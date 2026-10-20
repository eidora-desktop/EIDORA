from __future__ import annotations

from collections import OrderedDict, deque
import datetime as dt
import math
import os
import threading
import time
from pathlib import Path

import numpy as np
from PySide6 import QtCore, QtGui, QtQml, QtQuick, QtQuickWidgets, QtWidgets

from DiscoveryArea.canvas.indexing import UniformGridIndex, Viewport, points_in_polygon
from DiscoveryArea.canvas.lod import AdaptiveRenderBudget, LodDecision, marker_coverage_ratio
from DiscoveryArea.canvas.native.runtime import detect_native_renderer_runtime
from DiscoveryArea.canvas.scene import (
    CanvasScene,
    CanvasSceneRequest,
    DensityLevel,
    SceneBuildCancelled,
    build_canvas_scene,
)
from DiscoveryArea.canvas.viewport import (
    centered_frame_rect,
    cropped_viewport_for_frame,
    normalize_viewport_aspect_preset,
    normalize_world_viewport,
    render_viewport_for_frame,
)
from DiscoveryArea.thumbnail_config import THUMBNAIL_CONFIG
from DiscoveryArea.canvas.render_backend import (
    NativeItemBackend,
    PaintedItemBackend,
    DiscoveryRenderBackend,
    RenderPayload,
    SceneGraphItemBackend,
    ThumbnailBatchEntry,
    ThumbnailAtlasPageEntry,
    ThumbnailAtlasSpriteEntry,
    ThumbnailTileDisplayKeyEntry,
    ThumbnailTileEntry,
    ThumbnailTileLayerConfigPayload,
    ThumbnailTilePolicyPayload,
    ThumbnailLayerConfigPayload,
    ThumbnailPlanPayload,
    ThumbnailRuntimePolicyPayload,
    ThumbnailSourceEntry,
)
from shared.cursors import (
    LASSO_ADD_POINT,
    LASSO_ADD_ANCHOR,
    LASSO_CLOSE_POINT,
    LASSO_MOVE_ANCHOR,
    LASSO_REMOVE_POINT,
    LASSO_SELECT_AREA,
    PAN_CLOSED,
    PAN_OPEN,
    PIN_ADD,
    PIN_MODE,
    PIN_MOVE,
    PIN_REMOVE,
    SELECTION_MODE,
    ZOOM_IN,
    ZOOM_OUT,
    CanvasCursorController,
)
from shared.env import env_flag, get_env


ENABLE_SCENEGRAPH_PYTHON_ENV_VAR = "EIDORA_ENABLE_SCENEGRAPH_PYTHON"
DISCOVERY_RENDER_BACKEND_ENV_VAR = "EIDORA_DISCOVERY_RENDER_BACKEND"
FORCE_SOFTWARE_QUICK_ENV_VAR = "EIDORA_FORCE_SOFTWARE_QUICK"
FORCE_WIDGET_CANVAS_ENV_VAR = "EIDORA_FORCE_WIDGET_CANVAS"
ALLOW_PAINTED_DISCOVERY_FALLBACK_ENV_VAR = "EIDORA_ALLOW_PAINTED_DISCOVERY_FALLBACK"
DISCOVERY_QUICK_HOST_ENV_VAR = "EIDORA_DISCOVERY_QUICK_HOST"


class _SceneBuildSignals(QtCore.QObject):
    finished = QtCore.Signal(int, object, float)
    failed = QtCore.Signal(int, str)
    cancelled = QtCore.Signal(int)


class _SceneBuildTask(QtCore.QRunnable):
    def __init__(self, request: CanvasSceneRequest, cancelled: threading.Event) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.request = request
        self.cancelled = cancelled
        self.signals = _SceneBuildSignals()

    @QtCore.Slot()
    def run(self) -> None:
        started = time.perf_counter()
        try:
            scene = build_canvas_scene(self.request, cancelled=self.cancelled.is_set)
        except SceneBuildCancelled:
            self.signals.cancelled.emit(int(self.request.revision))
            return
        except Exception as exc:
            self.signals.failed.emit(int(self.request.revision), f"{type(exc).__name__}: {exc}")
            return
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if self.cancelled.is_set():
            self.signals.cancelled.emit(int(self.request.revision))
        else:
            self.signals.finished.emit(int(self.request.revision), scene, float(elapsed_ms))


class _LassoSelectionSignals(QtCore.QObject):
    finished = QtCore.Signal(int, int, int, object, object)
    cancelled = QtCore.Signal(int)


class _LassoSelectionTask(QtCore.QRunnable):
    def __init__(
        self,
        *,
        scene_revision: int,
        query_revision: int,
        polygon_index: int,
        polygon: np.ndarray,
        points: np.ndarray,
        metadata_ids: np.ndarray,
        visibility: np.ndarray,
        spatial_index: UniformGridIndex | None,
        cancelled: threading.Event,
    ) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.scene_revision = int(scene_revision)
        self.query_revision = int(query_revision)
        self.polygon_index = int(polygon_index)
        self.polygon = np.asarray(polygon, dtype=np.float32).copy()
        self.points = points
        self.metadata_ids = metadata_ids
        self.visibility = visibility
        self.spatial_index = spatial_index
        self.cancelled = cancelled
        self.signals = _LassoSelectionSignals()

    @QtCore.Slot()
    def run(self) -> None:
        if self.cancelled.is_set():
            self.signals.cancelled.emit(self.query_revision)
            return
        if self.spatial_index is None:
            candidates = np.arange(self.points.shape[0], dtype=np.int64)
        else:
            candidates = self.spatial_index.indices_in_polygon_bounds(self.polygon)
        if self.cancelled.is_set():
            self.signals.cancelled.emit(self.query_revision)
            return
        if self.visibility.shape[0] == self.points.shape[0]:
            candidates = candidates[self.visibility[candidates] >= 0.5]
        inside = points_in_polygon(self.points[candidates], self.polygon) if candidates.size else np.empty((0,), dtype=bool)
        selected = self.metadata_ids[candidates[inside]] if candidates.size else np.empty((0,), dtype=np.int64)
        if self.cancelled.is_set():
            self.signals.cancelled.emit(self.query_revision)
            return
        self.signals.finished.emit(
            self.scene_revision,
            self.query_revision,
            self.polygon_index,
            self.polygon,
            selected,
        )


class _ScatterPaintItem(QtQuick.QQuickPaintedItem):
    def __init__(self, parent: QtQuick.QQuickItem | None = None) -> None:
        super().__init__(parent)
        self._points = np.empty((0, 2), dtype=np.float32)
        self._metadata_ids = np.empty((0,), dtype=np.int64)
        self._metadata_index_by_id: dict[int, int] = {}
        self._visibility = np.empty((0,), dtype=np.float32)
        self._colors: np.ndarray | None = None
        self._edge_colors: np.ndarray | None = None
        self._edge_widths: np.ndarray | None = None
        self._point_size = 5.0
        self._point_sizes: np.ndarray | None = None
        self._viewport: tuple[float, float, float, float] | None = None
        self._background_color = QtGui.QColor("#ffffff")
        self._lasso_polygons: list[np.ndarray] = []
        self._lasso_current_polygon: np.ndarray | None = None
        self._lasso_selected_polygon_index: int | None = None
        self._lasso_polygon_colors: list[str] = []
        self._lasso_fill_opacity = 0.2
        self._lasso_current_vertices: list[np.ndarray] = []
        self._lasso_hover_anchor: tuple[int, int, bool] | None = None
        self._lasso_hover_edge_insert: tuple[int, int, np.ndarray] | None = None
        self._lasso_hover_polygon_index: int | None = None
        self._lasso_alt_held = False
        self._lasso_shift_held = False
        self._lasso_ctrl_held = False
        self._thumb_layer_enabled = False
        self._thumb_variant_id = "none"
        self._thumb_zoom_mid_threshold = 40.0
        self._thumb_zoom_close_threshold = 110.0
        self._thumb_size_px = 64.0
        self._thumb_coloring = "none"
        self._thumb_colors: np.ndarray | None = None
        self._thumb_border_colors: np.ndarray | None = None
        self._thumb_border_width_px = 2.0
        self._thumb_color_opacity = 0.75
        self._thumb_max_visible_mid, self._thumb_max_visible_close = THUMBNAIL_CONFIG.native_draw_limits()
        self._thumbnail_images: dict[tuple[int, int, str], QtGui.QImage] = {}
        self._thumbnail_lru: OrderedDict[tuple[int, int, str], int] = OrderedDict()
        self._thumbnail_bytes = 0
        self._thumbnail_max_bytes = self._thumbnail_cache_budget_bytes()
        self._thumbnail_eviction_margin = 1.5
        self._thumbnail_candidate_metadata_ids: list[int] = []
        self._thumbnail_candidate_metadata_id_set: set[int] = set()
        self._thumbnail_candidate_target_px = 64
        self._thumbnail_eviction_keep_scan_limit = THUMBNAIL_CONFIG.eviction_keep_scan_limit
        self._max_marker_draw_count = 25000
        self._max_thumbnail_draw_count = THUMBNAIL_CONFIG.painted_draw_limit_detail
        self._fast_marker_threshold = 4000
        self._lod_far_zoom_threshold = 40.0
        self._lod_far_max_points = 6000
        self._lod_mode = "exact"
        self._representative_limit = self._lod_far_max_points
        self._representative_mask = np.empty((0,), dtype=bool)
        self._density_images: list[tuple[DensityLevel, QtGui.QImage]] = []
        self.setAntialiasing(True)

    def set_data(
        self,
        points: np.ndarray,
        metadata_ids: np.ndarray,
        visibility: np.ndarray,
        colors: np.ndarray | None,
        edge_colors: np.ndarray | None,
        edge_widths: np.ndarray | None,
        point_size: float | np.ndarray,
        viewport: tuple[float, float, float, float] | None,
        lasso_polygons: list[np.ndarray] | None = None,
        lasso_current_polygon: np.ndarray | None = None,
        lasso_selected_polygon_index: int | None = None,
    ) -> None:
        self._points = points
        self._metadata_ids = np.asarray(metadata_ids, dtype=np.int64)
        self._metadata_index_by_id = {
            int(metadata_id): int(index)
            for index, metadata_id in enumerate(self._metadata_ids)
        }
        self._visibility = visibility
        self._colors = colors
        self._edge_colors = edge_colors
        self._edge_widths = edge_widths
        if np.isscalar(point_size):
            self._point_size = max(1.0, float(point_size))
            self._point_sizes = None
        else:
            sizes = np.asarray(point_size, dtype=np.float32).reshape(-1)
            self._point_sizes = sizes
            self._point_size = max(1.0, float(np.nanmax(sizes))) if sizes.size > 0 else 1.0
        self._viewport = viewport
        self._lasso_polygons = lasso_polygons or []
        self._lasso_current_polygon = lasso_current_polygon
        self._lasso_selected_polygon_index = lasso_selected_polygon_index
        self._lasso_current_vertices = (
            []
            if lasso_current_polygon is None
            else [np.asarray(point, dtype=np.float32) for point in lasso_current_polygon]
        )
        if self._representative_mask.shape[0] != self._points.shape[0]:
            self._representative_mask = np.empty((0,), dtype=bool)
            self._density_images = []
        self.update()

    def set_lod_policy(self, mode: str, representative_limit: int) -> None:
        normalized = "hybrid" if str(mode) == "hybrid" else "exact"
        limit = max(1, int(representative_limit))
        if normalized == self._lod_mode and limit == self._representative_limit:
            return
        self._lod_mode = normalized
        self._representative_limit = limit
        self.update()

    def set_density_levels(
        self,
        levels: tuple[DensityLevel, ...],
        representative_indices: np.ndarray,
    ) -> None:
        images: list[tuple[DensityLevel, QtGui.QImage]] = []
        for level in levels:
            rgba = np.ascontiguousarray(level.rgba, dtype=np.uint8)
            image = QtGui.QImage(
                rgba.data,
                int(level.width),
                int(level.height),
                int(rgba.strides[0]),
                QtGui.QImage.Format.Format_RGBA8888_Premultiplied,
            ).copy()
            images.append((level, image))
        self._density_images = images
        self._representative_mask = np.zeros(self._points.shape[0], dtype=bool)
        indices = np.asarray(representative_indices, dtype=np.int64)
        indices = indices[(indices >= 0) & (indices < self._representative_mask.shape[0])]
        self._representative_mask[indices] = True
        self.update()

    def set_lasso_overlay_state(
        self,
        polygons: list[np.ndarray] | None,
        current_polygon: np.ndarray | None,
        selected_polygon_index: int | None,
        polygon_colors: list[str] | None,
        fill_opacity: float,
        hover_anchor: tuple[int, int, bool] | None,
        hover_edge_insert: tuple[int, int, np.ndarray] | None,
        hover_polygon_index: int | None,
        *,
        option_modifier_held: bool = False,
    ) -> None:
        self._lasso_polygons = [np.asarray(poly, dtype=np.float32) for poly in (polygons or [])]
        self._lasso_current_polygon = current_polygon
        self._lasso_selected_polygon_index = selected_polygon_index
        self._lasso_polygon_colors = [str(color or "#3b82f6") for color in (polygon_colors or [])]
        self._lasso_fill_opacity = float(np.clip(float(fill_opacity), 0.0, 1.0))
        self._lasso_current_vertices = (
            []
            if current_polygon is None
            else [np.asarray(point, dtype=np.float32) for point in current_polygon]
        )
        self._lasso_hover_anchor = hover_anchor
        self._lasso_hover_edge_insert = hover_edge_insert
        self._lasso_hover_polygon_index = hover_polygon_index
        held = bool(option_modifier_held)
        self._lasso_alt_held = held
        self._lasso_shift_held = False
        self._lasso_ctrl_held = False
        self.update()

    def _lasso_anchor_modifier_held(self) -> bool:
        return bool(self._lasso_alt_held or self._lasso_shift_held or self._lasso_ctrl_held)

    def set_thumbnail_layer_config(
        self,
        enabled: bool,
        variant_id: str,
        zoom_mid_threshold: float,
        zoom_close_threshold: float,
        thumbnail_size_px: float,
        max_visible_mid: int,
        max_visible_close: int,
        coloring: str = "none",
        border_width_px: float = 2.0,
        color_opacity: float = 0.75,
        colors: object | None = None,
        border_colors: object | None = None,
        render_margin_fraction: float = THUMBNAIL_CONFIG.prefetch_margin_fraction,
    ) -> None:
        previous_variant = self._thumb_variant_id
        self._thumb_layer_enabled = bool(enabled)
        self._thumb_variant_id = str(variant_id or "none")
        if not self._thumb_layer_enabled or self._thumb_variant_id != previous_variant:
            self._clear_thumbnail_memory()
        self._thumb_zoom_mid_threshold = max(1.0, float(zoom_mid_threshold))
        self._thumb_zoom_close_threshold = max(self._thumb_zoom_mid_threshold, float(zoom_close_threshold))
        self._thumb_size_px = max(1.0, float(thumbnail_size_px))
        self._thumb_max_visible_mid = max(1, int(max_visible_mid))
        self._thumb_max_visible_close = max(1, int(max_visible_close))
        normalized_coloring = str(coloring or "none").strip().lower()
        self._thumb_coloring = (
            normalized_coloring
            if normalized_coloring in {"none", "border", "tint", "tint_border"}
            else "none"
        )
        self._thumb_colors = self._normalized_thumbnail_colors(colors)
        self._thumb_border_colors = self._normalized_thumbnail_colors(border_colors)
        self._thumb_border_width_px = max(0.0, float(border_width_px))
        self._thumb_color_opacity = max(0.0, min(1.0, float(color_opacity)))
        self.update()

    def set_thumbnail_batch(self, entries: list[dict]) -> None:
        changed = False
        for entry in entries:
            try:
                metadata_id = int(entry.get("metadata_id"))
                width = int(entry.get("width"))
                height = int(entry.get("height"))
                target_px = int(entry.get("target_px"))
            except (TypeError, ValueError):
                continue
            if not self._thumbnail_candidate_metadata_id_set:
                continue
            if metadata_id not in self._thumbnail_candidate_metadata_id_set:
                continue
            if target_px > self._thumbnail_candidate_target_px:
                continue
            rgba = bytes(entry.get("rgba") or b"")
            if width <= 0 or height <= 0 or len(rgba) != width * height * 4:
                continue
            image = QtGui.QImage(rgba, width, height, width * 4, QtGui.QImage.Format.Format_RGBA8888).copy()
            self._store_thumbnail_image((metadata_id, target_px, self._thumb_variant_id), image)
            changed = True
        if changed:
            self._evict_thumbnail_cache()
            self.update()

    def set_viewport(self, viewport: tuple[float, float, float, float] | None) -> None:
        self._viewport = None if viewport is None else tuple(float(value) for value in viewport)
        self.update()

    def set_visibility(self, visibility: np.ndarray) -> None:
        values = np.asarray(visibility, dtype=np.float32).reshape(-1)
        if values.shape[0] != self._points.shape[0]:
            return
        self._visibility = values
        self._density_images = []
        self.update()

    def set_background_color(self, color: QtGui.QColor | str) -> None:
        next_color = QtGui.QColor(color)
        if not next_color.isValid() or next_color == self._background_color:
            return
        self._background_color = next_color
        self.update()

    def paint(self, painter: QtGui.QPainter) -> None:
        overlay_only = bool(self.property("overlay_only"))
        if not overlay_only and self.property("draw_background") is not False:
            painter.fillRect(self.boundingRect(), self._background_color)
        if self._points.size == 0 and not overlay_only:
            return
        width = float(max(1.0, self.width()))
        height = float(max(1.0, self.height()))
        if self._viewport is None:
            if self._points.size == 0:
                return
            x_min = float(np.min(self._points[:, 0]))
            x_max = float(np.max(self._points[:, 0]))
            y_min = float(np.min(self._points[:, 1]))
            y_max = float(np.max(self._points[:, 1]))
            if abs(x_max - x_min) < 1e-9:
                x_max = x_min + 1.0
            if abs(y_max - y_min) < 1e-9:
                y_max = y_min + 1.0
        else:
            x_min, x_max, y_min, y_max = self._viewport
        if abs(x_max - x_min) < 1e-9 or abs(y_max - y_min) < 1e-9:
            return
        sx = width / (x_max - x_min)
        sy = height / (y_max - y_min)
        draw_thumbnails = bool(self._thumb_layer_enabled and self._thumb_variant_id != "none")
        if draw_thumbnails:
            thumbnail_cap = min(
                self._thumb_max_visible_close,
                self._max_thumbnail_draw_count,
            )
        else:
            thumbnail_cap = 0
        if not overlay_only:
            indices = self._visible_indices(
                x_min=x_min,
                x_max=x_max,
                y_min=y_min,
                y_max=y_max,
            )
            if self._lod_mode == "hybrid":
                self._draw_precomputed_density(
                    painter,
                    x_min=x_min,
                    x_max=x_max,
                    y_min=y_min,
                    y_max=y_max,
                    width=width,
                    height=height,
                )
                sample_limit = min(self._representative_limit, self._max_marker_draw_count)
                if self._representative_mask.shape[0] == self._points.shape[0]:
                    indices = indices[self._representative_mask[indices]]
                if indices.size > sample_limit:
                    stride = max(1, int(np.ceil(indices.size / float(sample_limit))))
                    indices = indices[::stride][:sample_limit]
                if indices.size:
                    self._draw_markers_fast(
                        painter,
                        indices=indices,
                        x_min=x_min,
                        y_min=y_min,
                        sx=sx,
                        sy=sy,
                        height=height,
                        force_point_size=self._point_size,
                        force_alpha=0.82,
                    )
                    featured_count = min(2_000, int(indices.size))
                    featured_positions = np.linspace(
                        0,
                        int(indices.size) - 1,
                        num=featured_count,
                        dtype=np.int64,
                    )
                    indices = indices[featured_positions]
            if draw_thumbnails and indices.size:
                thumbnail_ids = self._thumbnail_candidate_metadata_id_set
                if thumbnail_ids:
                    indices = np.asarray(
                        [
                            index
                            for index in indices.tolist()
                            if int(self._metadata_ids[index]) not in thumbnail_ids
                        ],
                        dtype=np.int64,
                    )
            if indices.size >= self._fast_marker_threshold and not self._requires_detailed_marker_paint():
                self._draw_markers_fast(
                    painter,
                    indices=indices,
                    x_min=x_min,
                    y_min=y_min,
                    sx=sx,
                    sy=sy,
                    height=height,
                )
            else:
                for i in indices:
                    point = self._points[i]
                    alpha = 1.0
                    if i < self._visibility.shape[0]:
                        alpha = float(np.clip(self._visibility[i], 0.0, 1.0))
                    if alpha <= 0.0:
                        continue
                    x = (float(point[0]) - x_min) * sx
                    y = height - (float(point[1]) - y_min) * sy
                    if self._colors is not None and i < self._colors.shape[0]:
                        rgba = self._colors[i]
                        color = QtGui.QColor.fromRgbF(
                            float(np.clip(rgba[0], 0.0, 1.0)),
                            float(np.clip(rgba[1], 0.0, 1.0)),
                            float(np.clip(rgba[2], 0.0, 1.0)),
                            float(np.clip((rgba[3] if len(rgba) > 3 else 1.0) * alpha, 0.0, 1.0)),
                        )
                    else:
                        color = QtGui.QColor.fromRgbF(0.12, 0.12, 0.12, alpha)
                    edge_pen = QtCore.Qt.PenStyle.NoPen
                    if self._edge_colors is not None and i < self._edge_colors.shape[0]:
                        edge_rgba = self._edge_colors[i]
                        edge_color = QtGui.QColor.fromRgbF(
                            float(np.clip(edge_rgba[0], 0.0, 1.0)),
                            float(np.clip(edge_rgba[1], 0.0, 1.0)),
                            float(np.clip(edge_rgba[2], 0.0, 1.0)),
                            float(np.clip((edge_rgba[3] if len(edge_rgba) > 3 else 1.0) * alpha, 0.0, 1.0)),
                        )
                        pen_width = 1.0
                        if self._edge_widths is not None and i < self._edge_widths.shape[0]:
                            pen_width = max(0.0, float(self._edge_widths[i]))
                        edge_pen = QtGui.QPen(edge_color, pen_width)
                    painter.setPen(edge_pen)
                    painter.setBrush(color)
                    if self._point_sizes is not None and i < self._point_sizes.shape[0]:
                        radius = max(0.5, float(self._point_sizes[i]) * 0.5)
                    else:
                        radius = self._point_size * 0.5
                    self._draw_marker(painter, QtCore.QPointF(x, y), radius)
        rendered_any_thumbnails = False
        if not overlay_only and draw_thumbnails:
            self._draw_thumbnail_placeholder_layer(
                painter,
                x_min=x_min,
                x_max=x_max,
                y_min=y_min,
                y_max=y_max,
                sx=sx,
                sy=sy,
                height=height,
            )
            rendered_any_thumbnails = self._draw_thumbnails(
                painter,
                x_min=x_min,
                x_max=x_max,
                y_min=y_min,
                y_max=y_max,
                sx=sx,
                sy=sy,
                height=height,
                cap=thumbnail_cap,
            )

    def _draw_thumbnails(
        self,
        painter: QtGui.QPainter,
        *,
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
        sx: float,
        sy: float,
        height: float,
        cap: int,
    ) -> bool:
        if self._points.shape[0] == 0 or self._metadata_ids.shape[0] == 0:
            return False
        rendered = False
        indices = self._thumbnail_candidate_indices_from_plan(
            x_min=x_min,
            x_max=x_max,
            y_min=y_min,
            y_max=y_max,
            max_count=max(1, int(cap)),
        )
        for i in indices:
            point = self._points[i]
            if i >= self._metadata_ids.shape[0]:
                continue
            if i < self._visibility.shape[0] and float(self._visibility[i]) <= 0.0:
                continue
            metadata_id = int(self._metadata_ids[i])
            image = None
            desired_target_px = int(round(float(np.clip(self._thumb_size_px, 1.0, 512.0))))
            lookup_target_sizes = self._thumbnail_lookup_sizes(desired_target_px)
            for candidate_target_px in lookup_target_sizes:
                image = self._touch_thumbnail_image((metadata_id, candidate_target_px, self._thumb_variant_id))
                if image is not None:
                    break
            px = (float(point[0]) - x_min) * sx
            py = height - (float(point[1]) - y_min) * sy
            thumb_size = float(np.clip(self._thumb_size_px, 1.0, 512.0))
            rect = QtCore.QRectF(px - thumb_size * 0.5, py - thumb_size * 0.5, thumb_size, thumb_size)
            painter.save()
            painter.setOpacity(self._thumb_color_opacity)
            if image is None:
                self._draw_thumbnail_placeholder(painter, rect, metadata_id)
                self._draw_thumbnail_color_style(
                    painter, rect, i, draw_tint=True, draw_border=False
                )
                painter.restore()
                self._draw_thumbnail_color_style(
                    painter, rect, i, draw_tint=False, draw_border=True
                )
                rendered = True
                continue
            image_rect = self._thumbnail_display_rect(image, px, py, thumb_size)
            painter.drawImage(
                image_rect,
                image,
                self._thumbnail_full_source_rect(image),
            )
            self._draw_thumbnail_color_style(
                painter, image_rect, i, draw_tint=True, draw_border=False
            )
            painter.restore()
            self._draw_thumbnail_color_style(
                painter, image_rect, i, draw_tint=False, draw_border=True
            )
            rendered = True
        return rendered

    @staticmethod
    def _normalized_thumbnail_colors(colors: object | None) -> np.ndarray | None:
        if colors is None:
            return None
        try:
            if isinstance(colors, (bytes, bytearray, memoryview, QtCore.QByteArray)):
                array = np.frombuffer(bytes(colors), dtype=np.float32)
                if array.size == 0 or array.size % 4 != 0:
                    return None
                array = array.reshape((-1, 4))
            else:
                array = np.asarray(colors, dtype=np.float32)
        except (TypeError, ValueError, BufferError):
            return None
        if array.ndim != 2 or array.shape[1] < 3:
            return None
        rgba = np.ones((array.shape[0], 4), dtype=np.float32)
        rgba[:, : min(array.shape[1], 4)] = array[:, : min(array.shape[1], 4)]
        return np.clip(rgba, 0.0, 1.0)

    def _draw_thumbnail_color_style(
        self,
        painter: QtGui.QPainter,
        rect: QtCore.QRectF,
        index: int,
        *,
        draw_tint: bool = True,
        draw_border: bool = True,
    ) -> None:
        draw_tint = draw_tint and self._thumb_coloring in {"tint", "tint_border"}
        draw_border = draw_border and self._thumb_coloring in {"border", "tint_border"}
        fill_palette = self._thumb_colors
        border_palette = self._thumb_border_colors
        has_tint = draw_tint and fill_palette is not None and index < len(fill_palette)
        has_border = (
            draw_border
            and border_palette is not None
            and index < len(border_palette)
            and self._thumb_border_width_px > 0.0
        )
        if not has_tint and not has_border:
            return
        painter.save()
        if has_tint:
            red, green, blue, alpha = fill_palette[index]
            tint = QtGui.QColor.fromRgbF(float(red), float(green), float(blue), float(alpha))
            # Screen blending preserves the source thumbnail's texture and
            # contrast rather than replacing it with a flat color overlay.
            painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_Screen)
            painter.fillRect(rect, tint)
        if has_border:
            red, green, blue, alpha = border_palette[index]
            border = QtGui.QColor.fromRgbF(float(red), float(green), float(blue), float(alpha))
            painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.setPen(QtGui.QPen(border, self._thumb_border_width_px))
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawRect(rect)
        painter.restore()

    def _draw_thumbnail_placeholder_layer(
        self,
        painter: QtGui.QPainter,
        *,
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
        sx: float,
        sy: float,
        height: float,
    ) -> None:
        indices = self._thumbnail_candidate_indices_from_plan(
            x_min=x_min,
            x_max=x_max,
            y_min=y_min,
            y_max=y_max,
            max_count=max(1, len(self._thumbnail_candidate_metadata_ids)),
        )
        if indices.size:
            points = self._points[indices]
            in_viewport = (
                (points[:, 0] >= x_min)
                & (points[:, 0] <= x_max)
                & (points[:, 1] >= y_min)
                & (points[:, 1] <= y_max)
            )
            indices = indices[in_viewport]
        if indices.size == 0:
            return
        zoom_px_per_world = float(sx)
        if zoom_px_per_world >= self._thumb_zoom_close_threshold:
            size_px = 4.5
            alpha = 0.44
        elif zoom_px_per_world >= self._thumb_zoom_mid_threshold:
            size_px = 3.5
            alpha = 0.38
        else:
            size_px = 2.25
            alpha = 0.32
        self._draw_markers_fast(
            painter,
            indices=indices,
            x_min=x_min,
            y_min=y_min,
            sx=sx,
            sy=sy,
            height=height,
            force_point_size=size_px,
            force_alpha=alpha,
            force_color=QtGui.QColor(128, 142, 156),
        )

    def set_thumbnail_candidates(self, metadata_ids: list[int], target_px: int) -> None:
        self._thumbnail_candidate_metadata_ids = [int(value) for value in metadata_ids]
        self._thumbnail_candidate_metadata_id_set = set(self._thumbnail_candidate_metadata_ids)
        self._thumbnail_candidate_target_px = max(1, int(target_px))
        self._evict_thumbnail_cache(target_px=self._thumbnail_candidate_target_px)
        self.update()

    def evict_thumbnails_outside_viewport(
        self,
        viewport: tuple[float, float, float, float] | None,
        *,
        target_px: int,
    ) -> None:
        if viewport is None or self._points.shape[0] == 0 or self._metadata_ids.shape[0] == 0:
            self._evict_thumbnail_cache(target_px=target_px)
            return
        x_min, x_max, y_min, y_max = [float(value) for value in viewport]
        span_x = max(1e-9, x_max - x_min)
        span_y = max(1e-9, y_max - y_min)
        margin_x = span_x * self._thumbnail_eviction_margin
        margin_y = span_y * self._thumbnail_eviction_margin
        keep_mask = (
            (self._points[:, 0] >= x_min - margin_x)
            & (self._points[:, 0] <= x_max + margin_x)
            & (self._points[:, 1] >= y_min - margin_y)
            & (self._points[:, 1] <= y_max + margin_y)
        )
        keep_indices = np.where(keep_mask)[0]
        if keep_indices.size <= self._thumbnail_eviction_keep_scan_limit:
            keep_ids: set[int] | None = {int(value) for value in self._metadata_ids[keep_indices].tolist()}
        else:
            keep_ids = None
        remove_keys: list[tuple[int, int, str]] = []
        for key in self._thumbnail_images:
            metadata_id, size_px, variant_id = key
            if variant_id != self._thumb_variant_id:
                remove_keys.append(key)
                continue
            if keep_ids is not None and int(metadata_id) not in keep_ids:
                remove_keys.append(key)
                continue
            if int(size_px) > int(target_px):
                remove_keys.append(key)
        for key in remove_keys:
            self._remove_thumbnail_key(key)
        self._evict_thumbnail_cache(target_px=target_px)

    def _store_thumbnail_image(self, key: tuple[int, int, str], image: QtGui.QImage) -> None:
        self._remove_thumbnail_key(key)
        estimated = max(0, int(image.sizeInBytes()))
        self._thumbnail_images[key] = image
        self._thumbnail_lru[key] = estimated
        self._thumbnail_bytes += estimated

    def _touch_thumbnail_image(self, key: tuple[int, int, str]) -> QtGui.QImage | None:
        image = self._thumbnail_images.get(key)
        if image is None:
            return None
        size = self._thumbnail_lru.pop(key, max(0, int(image.sizeInBytes())))
        self._thumbnail_lru[key] = size
        return image

    def _remove_thumbnail_key(self, key: tuple[int, int, str]) -> None:
        image = self._thumbnail_images.pop(key, None)
        size = self._thumbnail_lru.pop(key, None)
        if size is None and image is not None:
            size = max(0, int(image.sizeInBytes()))
        if size is not None:
            self._thumbnail_bytes = max(0, self._thumbnail_bytes - int(size))

    def _clear_thumbnail_memory(self) -> None:
        self._thumbnail_images.clear()
        self._thumbnail_lru.clear()
        self._thumbnail_bytes = 0
        self._clear_thumbnail_candidates()

    def _clear_thumbnail_candidates(self) -> None:
        self._thumbnail_candidate_metadata_ids = []
        self._thumbnail_candidate_metadata_id_set.clear()

    def _evict_thumbnail_cache(
        self,
        *,
        target_px: int | None = None,
        keep_metadata_ids: set[int] | None = None,
    ) -> None:
        if keep_metadata_ids is not None:
            stale = [key for key in self._thumbnail_images if int(key[0]) not in keep_metadata_ids]
            for key in stale:
                self._remove_thumbnail_key(key)
        if target_px is not None:
            oversized = [key for key in self._thumbnail_images if int(key[1]) > int(target_px)]
            for key in oversized:
                self._remove_thumbnail_key(key)
        while self._thumbnail_bytes > self._thumbnail_max_bytes and self._thumbnail_lru:
            key, _size = next(iter(self._thumbnail_lru.items()))
            self._remove_thumbnail_key(key)

    @staticmethod
    def _thumbnail_cache_budget_bytes() -> int:
        try:
            pages = int(os.sysconf("SC_PHYS_PAGES"))
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            total = pages * page_size
        except (AttributeError, OSError, ValueError):
            return 128 * 1024 * 1024
        budget = int(total * 0.03)
        return int(np.clip(budget, 64 * 1024 * 1024, 256 * 1024 * 1024))

    def _draw_thumbnail_placeholder(
        self,
        painter: QtGui.QPainter,
        rect: QtCore.QRectF,
        metadata_id: int,
    ) -> None:
        hue = abs(int(metadata_id)) % 360
        fill = QtGui.QColor.fromHsv(hue, 42, 225, 185)
        border = QtGui.QColor.fromHsv(hue, 58, 135, 210)
        painter.setPen(QtGui.QPen(border, 1.0))
        painter.setBrush(QtGui.QBrush(fill))
        painter.drawRect(rect)

    @staticmethod
    def _thumbnail_full_source_rect(image: QtGui.QImage) -> QtCore.QRectF:
        width = float(max(1, image.width()))
        height = float(max(1, image.height()))
        return QtCore.QRectF(0.0, 0.0, width, height)

    @staticmethod
    def _thumbnail_display_rect(
        image: QtGui.QImage,
        center_x: float,
        center_y: float,
        max_size_px: float,
    ) -> QtCore.QRectF:
        source_width = float(max(1, image.width()))
        source_height = float(max(1, image.height()))
        max_size = float(max(1.0, max_size_px))
        scale = min(max_size / source_width, max_size / source_height)
        width = max(1.0, source_width * scale)
        height = max(1.0, source_height * scale)
        return QtCore.QRectF(
            float(center_x) - width * 0.5,
            float(center_y) - height * 0.5,
            width,
            height,
        )

    @staticmethod
    def _thumbnail_lookup_sizes(desired_target_px: int) -> tuple[int, ...]:
        available = (256, 128, 64, 32)
        desired = max(1, int(desired_target_px))
        larger_or_equal = [size for size in available if size >= desired]
        smaller = [size for size in available if size < desired]
        ordered = sorted(larger_or_equal) + sorted(smaller, reverse=True)
        return tuple(dict.fromkeys((desired, *ordered)))

    def _thumbnail_candidate_indices_from_plan(
        self,
        *,
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
        max_count: int,
    ) -> np.ndarray:
        if not self._thumbnail_candidate_metadata_ids or max_count <= 0:
            return np.empty((0,), dtype=np.int64)
        selected: list[int] = []
        for metadata_id in self._thumbnail_candidate_metadata_ids:
            index = self._metadata_index_by_id.get(int(metadata_id))
            if index is None or index >= self._points.shape[0]:
                continue
            if index < self._visibility.shape[0] and float(self._visibility[index]) <= 0.0:
                continue
            selected.append(int(index))
            if len(selected) >= max_count:
                break
        return np.asarray(selected, dtype=np.int64)

    def _draw_markers_fast(
        self,
        painter: QtGui.QPainter,
        *,
        indices: np.ndarray,
        x_min: float,
        y_min: float,
        sx: float,
        sy: float,
        height: float,
        force_point_size: float | None = None,
        force_alpha: float | None = None,
        force_color: QtGui.QColor | None = None,
    ) -> None:
        if indices.size == 0:
            return
        sampled_points = self._points[indices]
        x = (sampled_points[:, 0] - float(x_min)) * float(sx)
        y = float(height) - ((sampled_points[:, 1] - float(y_min)) * float(sy))
        width = max(
            1.0,
            float(self._point_size if force_point_size is None else force_point_size),
        )

        def draw_batch(batch_positions: np.ndarray, color: QtGui.QColor) -> None:
            if batch_positions.size == 0 or color.alphaF() <= 0.0:
                return
            polygon = QtGui.QPolygonF()
            polygon.reserve(int(batch_positions.size))
            for position in batch_positions:
                polygon.append(QtCore.QPointF(float(x[position]), float(y[position])))
            pen = QtGui.QPen(color, width)
            pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawPoints(polygon)

        has_point_colors = (
            self._colors is not None
            and self._colors.ndim == 2
            and self._colors.shape[1] >= 3
        )
        if force_color is not None or not has_point_colors:
            color = QtGui.QColor(force_color) if force_color is not None else QtGui.QColor("#222222")
            if force_alpha is not None:
                color.setAlphaF(float(np.clip(force_alpha, 0.0, 1.0)))
            draw_batch(np.arange(indices.size, dtype=np.int64), color)
            return

        rgba = np.ones((indices.size, 4), dtype=np.float32)
        source_colors = np.asarray(self._colors[indices], dtype=np.float32)
        rgba[:, :3] = np.clip(source_colors[:, :3], 0.0, 1.0)
        if source_colors.shape[1] >= 4:
            rgba[:, 3] = np.clip(source_colors[:, 3], 0.0, 1.0)
        if force_alpha is not None:
            rgba[:, 3] = float(np.clip(force_alpha, 0.0, 1.0))

        rgba8 = np.rint(rgba * 255.0).astype(np.uint8)
        exact_keys = (
            (rgba8[:, 0].astype(np.uint32) << 24)
            | (rgba8[:, 1].astype(np.uint32) << 16)
            | (rgba8[:, 2].astype(np.uint32) << 8)
            | rgba8[:, 3].astype(np.uint32)
        )
        keys, inverse = np.unique(exact_keys, return_inverse=True)
        if keys.size > 256:
            # Keep the diagnostic fallback responsive for continuous mappings:
            # 3/3/2-bit RGB buckets bound the number of QPainter draw calls.
            quantized_keys = (
                ((rgba8[:, 0] >> 5).astype(np.uint16) << 5)
                | ((rgba8[:, 1] >> 5).astype(np.uint16) << 2)
                | (rgba8[:, 2] >> 6).astype(np.uint16)
            )
            keys, inverse = np.unique(quantized_keys, return_inverse=True)

        for group_index in range(int(keys.size)):
            positions = np.flatnonzero(inverse == group_index)
            group_rgba = np.mean(rgba[positions], axis=0)
            color = QtGui.QColor.fromRgbF(*(float(value) for value in group_rgba))
            draw_batch(positions, color)

    def _requires_detailed_marker_paint(self) -> bool:
        if self._edge_colors is None or self._edge_widths is None:
            return False
        if self._edge_widths.size == 0:
            return False
        return bool(np.nanmax(np.asarray(self._edge_widths, dtype=np.float32)) > 0.0)

    def _draw_precomputed_density(
        self,
        painter: QtGui.QPainter,
        *,
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
        width: float,
        height: float,
    ) -> bool:
        if not self._density_images:
            return False
        level, image = self._density_images[-1]
        bounds = level.world_bounds
        sx = float(width) / max(1e-12, float(x_max - x_min))
        sy = float(height) / max(1e-12, float(y_max - y_min))
        left = (float(bounds[0]) - float(x_min)) * sx
        top = float(height) - ((float(bounds[3]) - float(y_min)) * sy)
        raster_width = (float(bounds[1]) - float(bounds[0])) * sx
        raster_height = (float(bounds[3]) - float(bounds[2])) * sy
        if raster_width <= 0.0 or raster_height <= 0.0:
            return False
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setOpacity(0.10)
        painter.drawImage(QtCore.QRectF(left, top, raster_width, raster_height), image)
        painter.restore()
        return True

    def _sample_visible_indices(
        self,
        *,
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
        max_count: int,
    ) -> np.ndarray:
        if self._points.shape[0] == 0 or max_count <= 0:
            return np.empty((0,), dtype=np.int64)
        mask = (
            (self._points[:, 0] >= float(x_min))
            & (self._points[:, 0] <= float(x_max))
            & (self._points[:, 1] >= float(y_min))
            & (self._points[:, 1] <= float(y_max))
        )
        if self._visibility.shape[0] == self._points.shape[0]:
            mask = mask & (self._visibility > 0.0)
        indices = np.where(mask)[0]
        if indices.size <= max_count:
            return indices.astype(np.int64, copy=False)
        stride = int(np.ceil(indices.size / float(max_count)))
        sampled = indices[::max(1, stride)]
        if sampled.size > max_count:
            sampled = sampled[:max_count]
        return sampled.astype(np.int64, copy=False)

    def _visible_indices(
        self,
        *,
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
    ) -> np.ndarray:
        if self._points.shape[0] == 0:
            return np.empty((0,), dtype=np.int64)
        mask = (
            (self._points[:, 0] >= float(x_min))
            & (self._points[:, 0] <= float(x_max))
            & (self._points[:, 1] >= float(y_min))
            & (self._points[:, 1] <= float(y_max))
        )
        if self._visibility.shape[0] == self._points.shape[0]:
            mask = mask & (self._visibility > 0.0)
        return np.where(mask)[0].astype(np.int64, copy=False)

    @staticmethod
    def _draw_marker(painter: QtGui.QPainter, center: QtCore.QPointF, radius: float) -> None:
        painter.drawEllipse(center, radius, radius)

    def _draw_lasso_overlay(
        self,
        painter: QtGui.QPainter,
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
        width: float,
        height: float,
        *,
        draw_fill: bool,
        draw_lines: bool,
    ) -> None:
        sx = width / (x_max - x_min)
        sy = height / (y_max - y_min)
        pen = QtGui.QPen(QtGui.QColor.fromRgbF(0.1, 0.6, 0.9, 0.95))
        pen.setWidthF(2.0)
        painter.setPen(pen)
        for polygon_index, polygon in enumerate(self._lasso_polygons):
            if polygon.ndim != 2 or polygon.shape[0] < 3:
                continue
            stroke = QtGui.QColor(
                str(
                    self._lasso_polygon_colors[polygon_index]
                    if polygon_index < len(self._lasso_polygon_colors)
                    else "#3b82f6"
                )
            )
            if not stroke.isValid():
                stroke = QtGui.QColor.fromRgbF(0.1, 0.6, 0.9, 0.95)
            fill_color = stroke.lighter(125)
            fill_color.setAlphaF(self._lasso_fill_opacity)
            stroke.setAlpha(255)
            is_selected = polygon_index == self._lasso_selected_polygon_index
            is_hovered = polygon_index == self._lasso_hover_polygon_index
            pen.setColor(stroke.lighter(120) if is_selected else stroke)
            pen.setWidthF(3.0 if is_selected else 2.0)
            painter.setPen(pen)
            fill = QtGui.QBrush(fill_color)
            path = QtGui.QPainterPath()
            for index, point in enumerate(polygon):
                x = (float(point[0]) - x_min) * sx
                y = height - (float(point[1]) - y_min) * sy
                if index == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
            path.closeSubpath()
            if draw_lines and is_hovered and not is_selected:
                hover_pen = QtGui.QPen(stroke.lighter(135))
                hover_pen.setWidthF(4.0)
                hover_color = hover_pen.color()
                hover_pen.setColor(
                    QtGui.QColor(
                        hover_color.red(),
                        hover_color.green(),
                        hover_color.blue(),
                        130,
                    )
                )
                painter.setPen(hover_pen)
                painter.drawPath(path)
            if draw_fill:
                painter.fillPath(path, fill)
            if draw_lines:
                painter.setPen(pen)
                painter.drawPath(path)
        if draw_lines and self._lasso_selected_polygon_index is not None:
            index = int(self._lasso_selected_polygon_index)
            if 0 <= index < len(self._lasso_polygons):
                self._draw_lasso_anchors(painter, self._lasso_polygons[index], x_min, y_min, sx, sy, height)
        if draw_lines and self._lasso_current_polygon is not None and self._lasso_current_polygon.shape[0] > 0:
            current_pen = QtGui.QPen(QtGui.QColor.fromRgbF(0.9, 0.5, 0.1, 0.95))
            current_pen.setWidthF(2.0)
            painter.setPen(current_pen)
            polyline = QtGui.QPolygonF()
            for point in self._lasso_current_polygon:
                x = (float(point[0]) - x_min) * sx
                y = height - (float(point[1]) - y_min) * sy
                polyline.append(QtCore.QPointF(x, y))
            painter.drawPolyline(polyline)
            self._draw_lasso_anchors(
                painter,
                self._lasso_current_polygon,
                x_min,
                y_min,
                sx,
                sy,
                height,
                edge_color=QtGui.QColor.fromRgbF(0.9, 0.5, 0.1, 1.0),
            )
        self._draw_lasso_hover_affordances(painter, x_min, y_min, sx, sy, height)

    def _draw_lasso_anchors(
        self,
        painter: QtGui.QPainter,
        polygon: np.ndarray,
        x_min: float,
        y_min: float,
        sx: float,
        sy: float,
        height: float,
        edge_color: QtGui.QColor | None = None,
    ) -> None:
        color = edge_color or QtGui.QColor.fromRgbF(0.05, 0.4, 0.7, 1.0)
        painter.setPen(QtGui.QPen(color, 1.5))
        painter.setBrush(QtGui.QBrush(QtGui.QColor.fromRgbF(1.0, 1.0, 1.0, 0.85)))
        for point in polygon:
            x = (float(point[0]) - x_min) * sx
            y = height - (float(point[1]) - y_min) * sy
            painter.drawEllipse(QtCore.QPointF(x, y), 4.0, 4.0)

    def _draw_lasso_hover_affordances(
        self,
        painter: QtGui.QPainter,
        x_min: float,
        y_min: float,
        sx: float,
        sy: float,
        height: float,
    ) -> None:
        if self._lasso_hover_edge_insert is not None:
            _polygon_index, _insert_index, world_point = self._lasso_hover_edge_insert
            x = (float(world_point[0]) - x_min) * sx
            y = height - (float(world_point[1]) - y_min) * sy
            center = QtCore.QPointF(x, y)
            painter.setPen(QtGui.QPen(QtGui.QColor("#1d4ed8"), 1.8))
            painter.setBrush(QtGui.QBrush(QtGui.QColor("#ffffff")))
            painter.drawEllipse(center, 5.0, 5.0)
            painter.drawLine(QtCore.QPointF(x - 3.0, y), QtCore.QPointF(x + 3.0, y))
            painter.drawLine(QtCore.QPointF(x, y - 3.0), QtCore.QPointF(x, y + 3.0))
        if self._lasso_hover_anchor is None:
            return
        polygon_index, vertex_index, is_current = self._lasso_hover_anchor
        point: np.ndarray | None = None
        if is_current and 0 <= vertex_index < len(self._lasso_current_vertices):
            point = self._lasso_current_vertices[vertex_index]
        elif 0 <= polygon_index < len(self._lasso_polygons):
            polygon = self._lasso_polygons[polygon_index]
            if 0 <= vertex_index < polygon.shape[0]:
                point = polygon[vertex_index]
        if point is None:
            return
        x = (float(point[0]) - x_min) * sx
        y = height - (float(point[1]) - y_min) * sy
        deleting = self._lasso_anchor_modifier_held() and not is_current
        color = QtGui.QColor("#dc2626" if deleting else "#1d4ed8")
        painter.setPen(QtGui.QPen(color, 2.0))
        painter.setBrush(QtGui.QBrush(QtGui.QColor(255, 255, 255, 230)))
        painter.drawEllipse(QtCore.QPointF(x, y), 6.0, 6.0)


class _LassoPaintItem(_ScatterPaintItem):
    """Dedicated polygon layer for the painted fallback.

    This item intentionally owns no point data. Keeping it separate from
    ``_ScatterPaintItem`` ensures point opacity and polygon fill opacity are
    composited by different Qt Quick items.
    """

    def __init__(self, parent: QtQuick.QQuickItem | None = None) -> None:
        super().__init__(parent)
        self._draw_fill = True

    def set_draw_fill(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if self._draw_fill == enabled:
            return
        self._draw_fill = enabled
        self.update()

    def paint(self, painter: QtGui.QPainter) -> None:
        if self._viewport is None:
            return
        x_min, x_max, y_min, y_max = self._viewport
        if abs(x_max - x_min) < 1e-9 or abs(y_max - y_min) < 1e-9:
            return
        self._draw_lasso_overlay(
            painter,
            x_min,
            x_max,
            y_min,
            y_max,
            float(max(1.0, self.width())),
            float(max(1.0, self.height())),
            draw_fill=self._draw_fill,
            draw_lines=not self._draw_fill,
        )


class _ScatterSceneGraphItem(QtQuick.QQuickItem):
    """Fast point cloud path using Qt Quick scene graph nodes."""

    def __init__(self, parent: QtQuick.QQuickItem | None = None) -> None:
        super().__init__(parent)
        self.setFlag(QtQuick.QQuickItem.Flag.ItemHasContents, True)
        self._points = np.empty((0, 2), dtype=np.float32)
        self._visibility = np.empty((0,), dtype=np.float32)
        self._colors: np.ndarray | None = None
        self._viewport: tuple[float, float, float, float] | None = None
        self._background_color = QtGui.QColor("#ffffff")
        self._max_draw_count = 200000
        self._point_size = 2.0

    def set_data(
        self,
        points: np.ndarray,
        _metadata_ids: np.ndarray,
        visibility: np.ndarray,
        colors: np.ndarray | None,
        _edge_colors: np.ndarray | None,
        _edge_widths: np.ndarray | None,
        point_size: float | np.ndarray,
        viewport: tuple[float, float, float, float] | None,
        _lasso_polygons: list[np.ndarray] | None = None,
        _lasso_current_polygon: np.ndarray | None = None,
        _lasso_selected_polygon_index: int | None = None,
    ) -> None:
        self._points = np.asarray(points, dtype=np.float32)
        self._visibility = np.asarray(visibility, dtype=np.float32)
        self._colors = None if colors is None else np.asarray(colors, dtype=np.float32)
        self._viewport = viewport
        if np.isscalar(point_size):
            self._point_size = max(1.0, float(point_size))
        else:
            sizes = np.asarray(point_size, dtype=np.float32).reshape(-1)
            self._point_size = max(1.0, float(np.nanmean(sizes))) if sizes.size else 1.0
        self.update()

    def updatePaintNode(self, old_node: QtQuick.QSGNode | None, _data):  # noqa: N802
        node = old_node
        if node is None:
            node = QtQuick.QSGGeometryNode()
            geometry = QtQuick.QSGGeometry(QtQuick.QSGGeometry.defaultAttributes_Point2D(), 0)
            geometry.setDrawingMode(QtQuick.QSGGeometry.DrawingMode.DrawPoints)
            geometry.setLineWidth(float(max(1.0, self._point_size)))
            node.setGeometry(geometry)
            node.setFlag(QtQuick.QSGNode.Flag.OwnsGeometry, True)
            material = QtQuick.QSGFlatColorMaterial()
            material.setColor(QtGui.QColor("#222222"))
            node.setMaterial(material)
            node.setFlag(QtQuick.QSGNode.Flag.OwnsMaterial, True)
        assert isinstance(node, QtQuick.QSGGeometryNode)
        geometry = node.geometry()
        if geometry is None:
            return node
        indices = self._sample_visible_indices(max_count=self._max_draw_count)
        screen = self._world_to_screen(self._points[indices]) if indices.size else np.empty((0, 2), dtype=np.float32)
        geometry.allocate(int(screen.shape[0]))
        geometry.setLineWidth(float(max(1.0, self._point_size)))
        vertices = geometry.vertexDataAsPoint2D()
        for i, point in enumerate(screen):
            vertices[i].set(float(point[0]), float(point[1]))
        material = node.material()
        if isinstance(material, QtQuick.QSGFlatColorMaterial):
            material.setColor(self._resolve_base_color())
        node.markDirty(QtQuick.QSGNode.DirtyStateBit.DirtyGeometry | QtQuick.QSGNode.DirtyStateBit.DirtyMaterial)
        return node

    def set_thumbnail_layer_config(
        self,
        _enabled: bool,
        _variant_id: str,
        _zoom_mid_threshold: float,
        _zoom_close_threshold: float,
        _thumbnail_size_px: float,
        _max_visible_mid: int,
        _max_visible_close: int,
        _coloring: str = "none",
        _colors: object | None = None,
        _border_width_px: float = 2.0,
        _color_opacity: float = 0.75,
    ) -> None:
        return

    def set_thumbnail_batch(self, _entries: list[dict]) -> None:
        return

    def set_viewport(self, viewport: tuple[float, float, float, float] | None) -> None:
        self._viewport = None if viewport is None else tuple(float(value) for value in viewport)
        self.update()

    def set_background_color(self, color: QtGui.QColor | str) -> None:
        next_color = QtGui.QColor(color)
        if not next_color.isValid() or next_color == self._background_color:
            return
        self._background_color = next_color
        self.update()

    def _resolve_base_color(self) -> QtGui.QColor:
        if self._colors is None or self._colors.size == 0:
            return QtGui.QColor("#222222")
        if self._colors.ndim != 2 or self._colors.shape[1] < 3:
            return QtGui.QColor("#222222")
        rgb = np.clip(np.nanmean(self._colors[:, :3], axis=0), 0.0, 1.0)
        alpha = 1.0
        if self._colors.shape[1] >= 4:
            alpha = float(np.clip(np.nanmean(self._colors[:, 3]), 0.05, 1.0))
        return QtGui.QColor.fromRgbF(float(rgb[0]), float(rgb[1]), float(rgb[2]), alpha)

    def _effective_viewport(self) -> tuple[float, float, float, float] | None:
        if self._viewport is not None:
            return self._viewport
        if self._points.shape[0] == 0:
            return None
        x_min = float(np.min(self._points[:, 0]))
        x_max = float(np.max(self._points[:, 0]))
        y_min = float(np.min(self._points[:, 1]))
        y_max = float(np.max(self._points[:, 1]))
        if abs(x_max - x_min) < 1e-9:
            x_max = x_min + 1.0
        if abs(y_max - y_min) < 1e-9:
            y_max = y_min + 1.0
        return (x_min, x_max, y_min, y_max)

    def _world_to_screen(self, points: np.ndarray) -> np.ndarray:
        viewport = self._effective_viewport()
        if viewport is None or points.shape[0] == 0:
            return np.empty((0, 2), dtype=np.float32)
        x_min, x_max, y_min, y_max = viewport
        width = float(max(1.0, self.width()))
        height = float(max(1.0, self.height()))
        sx = width / max(1e-9, (x_max - x_min))
        sy = height / max(1e-9, (y_max - y_min))
        screen_x = (points[:, 0] - x_min) * sx
        screen_y = height - (points[:, 1] - y_min) * sy
        return np.column_stack((screen_x, screen_y)).astype(np.float32, copy=False)

    def _sample_visible_indices(self, *, max_count: int) -> np.ndarray:
        if self._points.shape[0] == 0:
            return np.empty((0,), dtype=np.int64)
        viewport = self._effective_viewport()
        if viewport is None:
            return np.empty((0,), dtype=np.int64)
        x_min, x_max, y_min, y_max = viewport
        mask = (
            (self._points[:, 0] >= float(x_min))
            & (self._points[:, 0] <= float(x_max))
            & (self._points[:, 1] >= float(y_min))
            & (self._points[:, 1] <= float(y_max))
        )
        if self._visibility.shape[0] == self._points.shape[0]:
            mask = mask & (self._visibility > 0.0)
        indices = np.where(mask)[0]
        if indices.size <= max_count:
            return indices.astype(np.int64, copy=False)
        stride = int(np.ceil(indices.size / float(max_count)))
        sampled = indices[::max(1, stride)]
        if sampled.size > max_count:
            sampled = sampled[:max_count]
        return sampled.astype(np.int64, copy=False)


class _ZoomDragOverlay(QtWidgets.QWidget):
    """Transparent widget overlay for the zoom-in drag rectangle."""

    PEN_STYLE = QtCore.Qt.PenStyle.DotLine

    def __init__(self, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self._selection_rect: QtCore.QRectF | None = None
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.hide()

    @property
    def selection_rect(self) -> QtCore.QRectF | None:
        return None if self._selection_rect is None else QtCore.QRectF(self._selection_rect)

    def set_selection_rect(self, rect: QtCore.QRectF | None) -> None:
        self._selection_rect = None if rect is None else rect.normalized()
        self.setVisible(self._selection_rect is not None)
        if self._selection_rect is not None:
            self.raise_()
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        del event
        if self._selection_rect is None:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        fill = QtGui.QColor("#3b82f6")
        fill.setAlpha(24)
        painter.fillRect(self._selection_rect, fill)
        pen = QtGui.QPen(QtGui.QColor("#2563eb"), 1.5, self.PEN_STYLE)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawRect(self._selection_rect)


class _FindItemBlinkOverlay(QtWidgets.QWidget):
    def __init__(self, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self._center: QtCore.QPointF | None = None
        self._phase = 0.0
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.hide()

    def set_center(self, center: QtCore.QPointF | None) -> None:
        self._center = None if center is None else QtCore.QPointF(center)
        self.setVisible(self._center is not None)
        if self._center is not None:
            self.raise_()
        self.update()

    def set_phase(self, phase: float) -> None:
        self._phase = float(phase)
        if self._center is not None:
            self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        del event
        if self._center is None:
            return
        pulse = (math.sin(self._phase) + 1.0) * 0.5
        color = QtGui.QColor.fromRgbF(1.0 - (0.78 * pulse), pulse, 0.0, 0.95)
        radius = 10.0 + 14.0 * pulse
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        pen = QtGui.QPen(color, 3.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        fill = QtGui.QColor(color)
        fill.setAlpha(42)
        painter.setBrush(fill)
        painter.drawEllipse(self._center, radius, radius)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawEllipse(self._center, 4.0, 4.0)


class _PointSelectionOverlay(QtWidgets.QWidget):
    def __init__(self, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self._center: QtCore.QPointF | None = None
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.hide()

    def set_center(self, center: QtCore.QPointF | None) -> None:
        self._center = None if center is None else QtCore.QPointF(center)
        self.setVisible(self._center is not None)
        if self._center is not None:
            self.raise_()
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        del event
        if self._center is None:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        pen = QtGui.QPen(QtGui.QColor("#2563eb"), 3.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        fill = QtGui.QColor("#3b82f6")
        fill.setAlpha(42)
        painter.setBrush(fill)
        painter.drawEllipse(self._center, 13.0, 13.0)


class _PinOverlay(QtWidgets.QWidget):
    def __init__(self, canvas: "DiscoveryCanvasWidget") -> None:
        super().__init__(canvas)
        self._canvas = canvas
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.hide()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        del event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        self._canvas._draw_pins_overlay(painter)


class DiscoveryCanvasWidget(QtWidgets.QWidget):
    """Qt Quick-hosted discovery canvas with adapter API parity."""

    point_context_requested = QtCore.Signal(int, object)
    point_selection_requested = QtCore.Signal(int, object)
    pin_context_requested = QtCore.Signal(str, object)
    point_clicked = QtCore.Signal(int)
    point_hovered = QtCore.Signal(object)
    lasso_selection_changed = QtCore.Signal(object)
    lasso_polygon_created = QtCore.Signal(object)
    lasso_polygon_selected = QtCore.Signal(object)
    lasso_polygon_context = QtCore.Signal(object)
    lasso_polygon_delete_requested = QtCore.Signal(object)
    lasso_polygon_removed = QtCore.Signal(object)
    lasso_polygon_updated = QtCore.Signal(object)
    lasso_status_hint_changed = QtCore.Signal(str)
    pinned_annotations_changed = QtCore.Signal(object)
    camera_changed = QtCore.Signal(object)
    scene_build_started = QtCore.Signal(int, int)
    scene_build_finished = QtCore.Signal(int)
    scene_build_failed = QtCore.Signal(int, str)
    static_upload_finished = QtCore.Signal(int)
    render_mode_changed = QtCore.Signal(str)
    render_timing_changed = QtCore.Signal(float)
    thumbnail_runtime_status_changed = QtCore.Signal(object)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._quick: QtQuickWidgets.QQuickWidget | None = None
        self._quick_view: QtQuick.QQuickView | None = None
        self._quick_container: QtWidgets.QWidget | None = None
        self._quick_host_name = "unavailable"
        self._quick_shutdown = False
        self._qml_component: QtQml.QQmlComponent | None = None
        self._qml_root_object: QtCore.QObject | None = None
        self._qml_zoom_drag_item: QtCore.QObject | None = None
        self._qml_find_blink_item: QtCore.QObject | None = None
        self._qml_selection_item: QtCore.QObject | None = None
        self._background_color = QtGui.QColor("#ffffff")
        self._quick_available = False
        self._quick_init_attempted = False
        self._backend_placeholder: QtWidgets.QLabel | None = None
        self._painted_lasso_fill_item: _LassoPaintItem | None = None
        self._painted_lasso_overlay_item: _LassoPaintItem | None = None
        self._native_lasso_fill_item: QtCore.QObject | None = None
        self._native_lasso_overlay_item: QtCore.QObject | None = None
        self._native_pin_overlay_item: QtCore.QObject | None = None
        self._enable_python_scenegraph = env_flag(
            ENABLE_SCENEGRAPH_PYTHON_ENV_VAR,
        )
        placeholder = QtWidgets.QLabel("Discovery canvas loading…", self)
        placeholder.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        placeholder.setObjectName("label_canvas_backend_placeholder")
        self._layout.addWidget(placeholder)
        self._backend_placeholder = placeholder
        self._thumbnail_loading_label = QtWidgets.QLabel(self)
        self._thumbnail_loading_label.setObjectName("label_thumbnail_loading_indicator")
        self._thumbnail_loading_label.setText("Preparing thumbnails")
        self._thumbnail_loading_label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignVCenter | QtCore.Qt.AlignmentFlag.AlignLeft
        )
        self._thumbnail_loading_label.setStyleSheet(
            """
            QLabel#label_thumbnail_loading_indicator {
                background-color: rgba(24, 30, 38, 210);
                color: white;
                border-radius: 4px;
                padding: 6px 10px;
                font-size: 11px;
            }
            """
        )
        self._thumbnail_loading_label.hide()
        self._zoom_drag_overlay = _ZoomDragOverlay(self)
        self._zoom_drag_overlay.setObjectName("overlay_zoom_drag_rectangle")
        self._find_item_overlay = _FindItemBlinkOverlay(self)
        self._find_item_overlay.setObjectName("overlay_find_item_blink")
        self._find_item_overlay.setGeometry(self.rect())
        self._selection_overlay = _PointSelectionOverlay(self)
        self._selection_overlay.setObjectName("overlay_selected_point")
        self._selection_overlay.setGeometry(self.rect())
        self._pin_overlay = _PinOverlay(self)
        self._pin_overlay.setObjectName("overlay_pinned_annotations")
        self._pin_overlay.setGeometry(self.rect())
        self._find_item_timer = QtCore.QTimer(self)
        self._find_item_timer.setInterval(45)
        self._find_item_timer.timeout.connect(self._on_find_item_blink_tick)
        self._find_item_metadata_id: int | None = None
        self._selected_metadata_id: int | None = None
        self._find_item_phase = 0.0
        self._points = np.empty((0, 2), dtype=np.float32)
        self._metadata_ids = np.empty((0,), dtype=np.int64)
        self._metadata_index_by_id = {}
        self._colors: np.ndarray | None = None
        self._edge_colors: np.ndarray | None = None
        self._edge_widths: np.ndarray | None = None
        self._visibility = np.empty((0,), dtype=np.float32)
        self._point_size = 5.0
        self._point_sizes: np.ndarray | None = None
        self._active_scene: CanvasScene | None = None
        self._representative_pick_mask = np.empty((0,), dtype=bool)
        self._representative_pick_limit = -1
        self._scene_revision = 0
        self._active_scene_revision = 0
        self._scene_thread_pool = QtCore.QThreadPool.globalInstance()
        self._scene_tasks: dict[int, _SceneBuildTask] = {}
        self._scene_cancel_events: dict[int, threading.Event] = {}
        self._scene_preserve_view: dict[int, bool] = {}
        self._scene_source_tokens: dict[int, tuple[int, int | None]] = {}
        self._latest_scene_request: CanvasSceneRequest | None = None
        self._active_position_source_token: tuple[int, int | None] | None = None
        self._pending_scene_fit_view = False
        self._last_scene_build_ms = 0.0
        self._last_render_frame_ms = 0.0
        self._render_frame_samples_ms: deque[float] = deque(maxlen=240)
        self._last_native_upload_preparation_ms = 0.0
        self._last_native_upload_point_count = 0
        self._last_scene_error = ""
        self._density_levels: tuple[DensityLevel, ...] = ()
        self._position_revision = 0
        self._style_revision = 0
        self._visibility_revision = 0
        self._density_revision = 0
        self._camera_revision = 0
        self._render_mode = "exact"
        self._lod_decision: LodDecision | None = None
        self._adaptive_render_budget = AdaptiveRenderBudget()
        self._max_edge_width = 0.0
        self._viewport: tuple[float, float, float, float] | None = None
        self._viewport_aspect_preset = "free"
        self._crop_bars_visible = False
        self._pending_initial_fit_view = False
        self._render_backend: DiscoveryRenderBackend | None = None
        self._render_backend_name = "unavailable"
        self._native_upload_signal_connected = False
        self._static_payload_revision = 0
        self._uploaded_static_payload_revision = -1
        self._uploaded_position_revision = -1
        self._uploaded_style_revision = -1
        self._uploaded_visibility_revision = -1
        self._uploaded_density_revision = -1
        self._last_lod_payload: tuple[str, int] | None = None
        self._last_viewport_payload: tuple[float, float, float, float] | None = None
        self._thumbnail_layer_config = ThumbnailLayerConfigPayload(
            enabled=False,
            variant_id="none",
            zoom_mid_threshold=THUMBNAIL_CONFIG.zoom_mid_threshold,
            zoom_close_threshold=THUMBNAIL_CONFIG.zoom_close_threshold,
            thumbnail_size_px=64.0,
            max_visible_mid=THUMBNAIL_CONFIG.native_draw_limits()[0],
            max_visible_close=THUMBNAIL_CONFIG.native_draw_limits()[1],
        )
        self._thumbnail_tile_layer_config = ThumbnailTileLayerConfigPayload(
            enabled=False,
            variant_id="none",
            discovery_cache_id="",
            tile_size_px=THUMBNAIL_CONFIG.tile_size_px,
        )
        self._uploaded_thumbnail_tile_layer_config: ThumbnailTileLayerConfigPayload | None = None
        self._thumbnail_tile_policy = ThumbnailTilePolicyPayload(
            max_cache_mb=THUMBNAIL_CONFIG.tile_cache_mb,
            decode_workers=THUMBNAIL_CONFIG.tile_decode_workers,
            max_decode_jobs=THUMBNAIL_CONFIG.tile_max_decode_jobs,
        )
        self._uploaded_thumbnail_tile_policy: ThumbnailTilePolicyPayload | None = None
        self._uploaded_thumbnail_layer_config: ThumbnailLayerConfigPayload | None = None
        self._native_thumbnail_layer_active = False
        self._thumbnail_runtime_policy: ThumbnailRuntimePolicyPayload | None = None
        self._uploaded_thumbnail_runtime_policy: ThumbnailRuntimePolicyPayload | None = None
        self._thumbnail_runtime_status: dict[str, object] = {
            "revision": 0,
            "desired": 0,
            "resident": 0,
            "drawn": 0,
            "queued": 0,
            "decoding": 0,
            "missing": 0,
            "failed": 0,
            "atlas_pages": 0,
            "atlas_bytes": 0,
            "decode_ms": 0.0,
            "upload_ms": 0.0,
        }
        self._requested_thumbnail_candidate_metadata_ids: list[int] = []
        self._requested_thumbnail_candidate_target_px = 32
        self._requested_backend = get_env(DISCOVERY_RENDER_BACKEND_ENV_VAR, "native").strip().lower()
        self._grid_index: UniformGridIndex | None = None
        self._pinned_annotations: list[dict] = []
        self._pins_enabled = True
        self._pins_visible = True
        self._pin_style: dict[str, object] = {
            "font_family": "Arial",
            "font_size": 11,
            "text_color": "#222222",
            "show_label": True,
            "show_value": True,
            "text_label_bold": False,
            "text_label_italic": False,
            "text_label_underline": False,
            "text_value_bold": False,
            "text_value_italic": False,
            "text_value_underline": False,
            "box_color": "#f0f0f0",
            "border_color": "#333333",
            "border_width": 1.0,
            "connector_color": "#333333",
            "connector_width": 1.5,
        }
        self._pin_drag_annotation_id: str | None = None
        self._pin_drag_anchor_world: tuple[float, float] | None = None
        self._pin_drag_cursor_to_box_delta: tuple[float, float] | None = None
        self._pin_ignore_next_release = False
        self._pin_box_min_width_px = 120.0
        self._pin_box_min_height_px = 36.0
        self._pin_box_padding_px = 6.0
        self._hover_enabled = True
        self._hover_eligibility_mask: np.ndarray | None = None
        self._last_hover_metadata_id: int | None = None
        self._point_hover_callback = None
        self._lasso_polygons: list[np.ndarray] = []
        self._lasso_polygon_ids: list[str] = []
        self._lasso_polygon_labels: list[str] = []
        self._lasso_polygon_colors: list[str] = []
        self._lasso_fill_opacity = 0.2
        self._lasso_polygon_area_visible: list[bool] = []
        self._lasso_polygon_points_visible: list[bool] = []
        self._lasso_current_vertices: list[np.ndarray] = []
        self._lasso_selected_polygon_index: int | None = None
        self._lasso_dragging_anchor: tuple[int, int, bool] | None = None
        self._lasso_pending_draw_start = False
        self._lasso_ignore_next_release = False
        self._lasso_anchor_hit_radius_px = 8.0
        self._lasso_hover_anchor: tuple[int, int, bool] | None = None
        self._lasso_hover_edge_insert: tuple[int, int, np.ndarray] | None = None
        self._lasso_hover_polygon_index: int | None = None
        self._lasso_alt_held = False
        self._lasso_shift_held = False
        self._lasso_ctrl_held = False
        self._last_lasso_status_hint = ""
        self._lasso_selection_callback = None
        self._lasso_query_revision = 0
        self._lasso_query_tasks: dict[int, _LassoSelectionTask] = {}
        self._lasso_query_cancel_events: dict[int, threading.Event] = {}
        self._space_pan_active = False
        self._space_pan_previous_mode: str | None = None
        self._interaction_mode = "pan"
        self._drag_start_pos: QtCore.QPointF | None = None
        self._drag_start_viewport: tuple[float, float, float, float] | None = None
        self._click_press_pos: QtCore.QPointF | None = None
        self._zoom_press_pos: QtCore.QPointF | None = None
        self._zoom_drag_current_pos: QtCore.QPointF | None = None
        self._zoom_drag_active = False
        self._zoom_drag_threshold_px = 4.0
        self._click_move_threshold_px = 3.0
        self._zoom_click_in_factor = 0.8
        self._zoom_click_out_factor = 1.25
        self._wheel_zoom_base = 0.92
        self._camera_interaction_active = False
        self._interaction_viewport_payload: tuple[float, float, float, float] | None = None
        self._interaction_settle_timer = QtCore.QTimer(self)
        self._interaction_settle_timer.setSingleShot(True)
        self._interaction_settle_timer.setInterval(45)
        self._interaction_settle_timer.timeout.connect(self._commit_camera_interaction)
        self._deferred_redraw_timer = QtCore.QTimer(self)
        self._deferred_redraw_timer.setSingleShot(True)
        self._deferred_redraw_timer.setInterval(50)
        self._deferred_redraw_timer.timeout.connect(self._request_backend_redraw)
        self._cursor_canvas_position: QtCore.QPointF | None = None
        self._zoom_shift_held = False
        self._zoom_out_mouse_held = False
        self._cursor_controller = CanvasCursorController(self)
        self._update_cursor()
        self._position_thumbnail_loading_label()
        self._viewport = self._aspect_normalized_viewport((0.0, 1.0, 0.0, 1.0))

    def _ensure_quick_backend_loaded(self, *, force: bool = False) -> None:
        if self._quick_init_attempted:
            return
        if not force and not self.isVisible():
            return
        self._quick_init_attempted = True
        if not self._should_use_quick_backend():
            self._render_backend_name = "qtquick_disabled_for_platform"
            return
        QtQml.qmlRegisterType(_ScatterPaintItem, "DiscoveryCanvas", 1, 0, "ScatterPaintItem")
        QtQml.qmlRegisterType(_LassoPaintItem, "DiscoveryCanvas", 1, 0, "LassoPaintItem")
        if self._enable_python_scenegraph:
            QtQml.qmlRegisterType(_ScatterSceneGraphItem, "DiscoveryCanvas", 1, 0, "ScatterSceneGraphItem")
        if env_flag(FORCE_SOFTWARE_QUICK_ENV_VAR):
            QtQuick.QQuickWindow.setGraphicsApi(QtQuick.QSGRendererInterface.GraphicsApi.Software)
        runtime = detect_native_renderer_runtime() if self._requested_backend == "native" else None
        requested_host = get_env(DISCOVERY_QUICK_HOST_ENV_VAR, "window").strip().lower()
        use_window_host = bool(runtime is not None and runtime.available and requested_host != "widget")
        if use_window_host:
            view = QtQuick.QQuickView()
            view.setResizeMode(QtQuick.QQuickView.ResizeMode.SizeRootObjectToView)
            view.setPersistentGraphics(True)
            view.setPersistentSceneGraph(True)
            container = QtWidgets.QWidget.createWindowContainer(view, self)
            container.setObjectName("container_discovery_quick_window")
            container.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
            self._layout.addWidget(container)
            self._quick_view = view
            self._quick_container = container
            self._quick_host_name = "qquickview"
            self._zoom_drag_overlay.hide()
            self._find_item_overlay.hide()
            self._pin_overlay.hide()
        else:
            quick = QtQuickWidgets.QQuickWidget(self)
            quick.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Expanding)
            quick.setResizeMode(QtQuickWidgets.QQuickWidget.ResizeMode.SizeRootObjectToView)
            self._layout.addWidget(quick)
            self._quick = quick
            self._quick_host_name = "qquickwidget"
        self._quick_available = True
        if self._backend_placeholder is not None:
            self._layout.removeWidget(self._backend_placeholder)
            self._backend_placeholder.deleteLater()
            self._backend_placeholder = None
        self._load_qml()
        self._pin_overlay.raise_()
        self._zoom_drag_overlay.raise_()
        self._find_item_overlay.raise_()
        self._sync_cursor_targets()
        self._update_cursor(force=True)
        self._refresh()

    def preload_renderer(self) -> None:
        self._ensure_quick_backend_loaded(force=True)

    def activate_render_host(self) -> None:
        """Resume the hosted Quick window after its stacked page becomes current."""

        self._ensure_quick_backend_loaded(force=True)
        if self._quick_container is not None:
            self._quick_container.setVisible(True)
        if self._quick_view is not None:
            self._quick_view.requestUpdate()
        elif self._quick is not None:
            self._quick.update()
        self._request_backend_redraw_with_followup()

    def _should_use_quick_backend(self) -> bool:
        if env_flag(FORCE_WIDGET_CANVAS_ENV_VAR):
            return False
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return False
        app = QtWidgets.QApplication.instance()
        if app is None:
            return True
        platform = app.platformName().lower()
        return platform not in {"offscreen", "minimal"}

    def _load_qml(self) -> None:
        engine = self._quick_engine()
        if engine is None:
            return
        self._native_upload_signal_connected = False
        root = None
        if self._requested_backend == "native":
            runtime = detect_native_renderer_runtime()
            os.environ["EIDORA_THUMBNAIL_RENDER_MARGIN_FRACTION"] = str(
                max(0.0, float(THUMBNAIL_CONFIG.prefetch_margin_fraction))
            )
            for import_path in runtime.import_paths:
                engine.addImportPath(str(import_path))
            if runtime.available:
                root = self._load_qml_component("discovery_canvas_native.qml")
                if root is not None:
                    native_item = root.findChild(QtCore.QObject, "nativeRenderItem")
                    lib_info = str(runtime.library_path) if runtime.library_path is not None else "unknown-library"
                    if native_item is not None:
                        backend = NativeItemBackend(native_item)
                        if backend.supports_static_points():
                            self._render_backend = backend
                            thumbnail_state = "native_thumbnails" if backend.supports_thumbnail_layer() else "points_only"
                            self._render_backend_name = (
                                f"qtquick_native_v{backend.renderer_api_version()}_item_{thumbnail_state}:{lib_info}"
                            )
                            timing_signal = getattr(native_item, "renderTimingAvailable", None)
                            if timing_signal is not None:
                                timing_signal.connect(
                                    self._on_native_render_timing,
                                    QtCore.Qt.ConnectionType.QueuedConnection,
                                )
                            upload_signal = getattr(native_item, "staticUploadFinished", None)
                            if upload_signal is not None:
                                upload_signal.connect(
                                    self._on_native_upload_finished,
                                    QtCore.Qt.ConnectionType.QueuedConnection,
                                )
                                self._native_upload_signal_connected = True
                            thumbnail_status_signal = getattr(native_item, "thumbnailStatusChanged", None)
                            if thumbnail_status_signal is not None:
                                thumbnail_status_signal.connect(
                                    self._on_native_thumbnail_status,
                                    QtCore.Qt.ConnectionType.QueuedConnection,
                                )
                            thumbnail_timing_signal = getattr(native_item, "thumbnailTimingAvailable", None)
                            if thumbnail_timing_signal is not None:
                                thumbnail_timing_signal.connect(
                                    self._on_native_thumbnail_timing,
                                    QtCore.Qt.ConnectionType.QueuedConnection,
                                )
                    if self._render_backend is not None:
                        fill_item = root.findChild(QtCore.QObject, "lassoFillItem")
                        if fill_item is not None:
                            self._native_lasso_fill_item = fill_item
                        overlay_item = root.findChild(QtCore.QObject, "lassoOverlayItem")
                        if overlay_item is not None:
                            self._native_lasso_overlay_item = overlay_item
                        pin_item = root.findChild(QtCore.QObject, "pinOverlayItem")
                        if pin_item is not None:
                            self._native_pin_overlay_item = pin_item
                    else:
                        self._render_backend_name = f"native_missing_point_api:{lib_info}"
                if self._render_backend is None:
                    if (
                        not self._render_backend_name.startswith("native_missing_point_api")
                        and ":qml_error:" not in self._render_backend_name
                        and ":qml_create_failed:" not in self._render_backend_name
                    ):
                        self._render_backend_name = "native_qml_load_failed"
            else:
                self._render_backend_name = f"native_unavailable:{runtime.reason}"
        allow_painted_fallback = (
            self._requested_backend != "native"
            or self._render_backend is None
            or env_flag(
                ALLOW_PAINTED_DISCOVERY_FALLBACK_ENV_VAR,
            )

        )
        if self._render_backend is None and allow_painted_fallback:
            root = self._load_qml_component("discovery_canvas.qml")
            if root is not None:
                scene_item = (
                    root.findChild(_ScatterSceneGraphItem, "scatterItem") if self._enable_python_scenegraph else None
                )
                if scene_item is not None and self._enable_python_scenegraph:
                    self._render_backend = SceneGraphItemBackend(scene_item)
                    self._painted_lasso_fill_item = None
                    self._painted_lasso_overlay_item = None
                    self._native_lasso_fill_item = None
                    self._native_lasso_overlay_item = None
                    self._native_pin_overlay_item = None
                    if not self._render_backend_name.startswith("native_"):
                        self._render_backend_name = "qtquick_scenegraph_points"
                else:
                    scatter_item = root.findChild(_ScatterPaintItem, "scatterItem")
                    if scatter_item is not None:
                        self._render_backend = PaintedItemBackend(scatter_item)
                        self._painted_lasso_fill_item = root.findChild(_LassoPaintItem, "lassoFillItem")
                        self._painted_lasso_overlay_item = root.findChild(_LassoPaintItem, "lassoOverlayItem")
                        self._native_lasso_fill_item = None
                        self._native_lasso_overlay_item = None
                        self._native_pin_overlay_item = None
                        if not self._render_backend_name.startswith("native_"):
                            self._render_backend_name = "qtquick_painted_scaffold"
                if self._render_backend is not None:
                    if not isinstance(self._render_backend, PaintedItemBackend):
                        self._painted_lasso_fill_item = None
                        self._painted_lasso_overlay_item = None
                        self._native_lasso_fill_item = None
                        self._native_lasso_overlay_item = None
                        self._native_pin_overlay_item = None
        host_widget = self._quick if self._quick is not None else self._quick_container
        if host_widget is not None:
            host_widget.setMouseTracking(True)
            host_widget.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
            host_widget.installEventFilter(self)
        if self._quick_view is not None:
            self._quick_view.installEventFilter(self)
        self._refresh()

    def _load_qml_component(self, filename: str):
        engine = self._quick_engine()
        if engine is None:
            return None
        qml_path = Path(__file__).resolve().parent / "quick" / filename
        component = QtQml.QQmlComponent(engine)
        component.loadUrl(QtCore.QUrl.fromLocalFile(str(qml_path)))
        if component.isError():
            errors = "; ".join(error.toString() for error in component.errors())
            self._render_backend_name = f"{filename}:qml_error:{errors}"[:1000]
            return None
        created = component.create()
        if created is None:
            errors = "; ".join(error.toString() for error in component.errors())
            self._render_backend_name = f"{filename}:qml_create_failed:{errors}"[:1000]
            return None
        # Keep strong references so Python GC does not invalidate wrapped QML objects.
        self._qml_component = component
        self._qml_root_object = created
        url = QtCore.QUrl.fromLocalFile(str(qml_path))
        if self._quick is not None:
            self._quick.setContent(url, component, created)
            root_object = self._quick.rootObject()
        elif self._quick_view is not None:
            self._quick_view.setContent(url, component, created)
            root_object = self._quick_view.rootObject()
        else:
            return None
        if root_object is None:
            return None
        self._qml_root_object = root_object
        self._qml_zoom_drag_item = root_object.findChild(QtCore.QObject, "zoomDragItem")
        self._qml_find_blink_item = root_object.findChild(QtCore.QObject, "findBlinkItem")
        self._qml_selection_item = root_object.findChild(QtCore.QObject, "selectionItem")
        self._apply_background_color_to_qml()
        self._apply_viewport_frame_to_qml()
        return root_object

    def _quick_engine(self) -> QtQml.QQmlEngine | None:
        if self._quick is not None:
            return self._quick.engine()
        if self._quick_view is not None:
            return self._quick_view.engine()
        return None

    def background_color(self) -> QtGui.QColor:
        return QtGui.QColor(self._background_color)

    def set_background_color(self, color: QtGui.QColor | str) -> None:
        next_color = QtGui.QColor(color)
        if not next_color.isValid():
            return
        self._background_color = next_color
        self._apply_background_color()

    def _apply_background_color(self) -> None:
        if self._qml_root_object is not None:
            self._qml_root_object.setProperty("backgroundColor", self._background_color)
        if self._render_backend is not None:
            self._render_backend.set_background_color(self._background_color)

    def _apply_background_color_to_qml(self) -> None:
        self._apply_background_color()

    def _apply_viewport_frame_to_qml(self) -> None:
        root = self._qml_root_object
        if root is None:
            return
        frame = self.active_viewport_frame()
        root.setProperty("viewportFrameX", float(frame.x()))
        root.setProperty("viewportFrameY", float(frame.y()))
        root.setProperty("viewportFrameWidth", float(frame.width()))
        root.setProperty("viewportFrameHeight", float(frame.height()))
        root.setProperty(
            "viewportFrameActive",
            self._crop_bars_visible and self._viewport_aspect_preset != "free",
        )

    def eventFilter(self, _obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() == QtCore.QEvent.Type.WindowDeactivate:
            self._commit_camera_interaction()
            self._reset_zoom_drag()
        if event.type() == QtCore.QEvent.Type.Enter:
            self._update_keyboard_modifier_state(QtWidgets.QApplication.keyboardModifiers())
            self._zoom_out_mouse_held = bool(
                QtWidgets.QApplication.mouseButtons()
                & QtCore.Qt.MouseButton.RightButton
            )
            self._update_cursor()
        if event.type() == QtCore.QEvent.Type.MouseButtonDblClick:
            mouse_event = event
            if self._interaction_mode == "lasso" and mouse_event.button() == QtCore.Qt.MouseButton.LeftButton:
                self._finish_lasso_polygon()
                self._lasso_ignore_next_release = True
                return True
            return False
        if event.type() == QtCore.QEvent.Type.MouseButtonPress:
            mouse_event = event
            self._click_press_pos = mouse_event.position()
            self._update_cursor_from_pointer_event(mouse_event)
            if self._quick is not None:
                self._quick.setFocus()
            elif self._quick_container is not None:
                self._quick_container.setFocus()
                if self._quick_view is not None:
                    self._quick_view.requestActivate()
            else:
                self.setFocus()
            if self._interaction_mode == "lasso":
                handled = self._handle_lasso_press(mouse_event)
                self._update_cursor_from_pointer_event(mouse_event)
                return handled
            if (
                self._interaction_mode in {"selection", "pin"}
                and mouse_event.button() == QtCore.Qt.MouseButton.LeftButton
            ):
                hit = self._hit_test_pin(mouse_event.position())
                if self._option_modifier_held():
                    target = hit or self._pinned_annotation_id_at_point(mouse_event.position())
                    if target is not None and self.remove_pinned_annotation(target):
                        self._pin_ignore_next_release = True
                        self._clear_point_hover_if_pin_occluded()
                        self._update_cursor_from_pointer_event(mouse_event)
                        return True
                if hit is not None:
                    self._clear_point_hover_if_pin_occluded()
                    self._pin_drag_annotation_id = hit
                    entry = self._find_pin(hit)
                    if entry is not None:
                        point = entry.get("point")
                        if isinstance(point, (list, tuple)) and len(point) >= 2:
                            self._pin_drag_anchor_world = (float(point[0]), float(point[1]))
                        rect = self._pin_box_rect_for_entry(entry)
                        if rect is not None:
                            self._pin_drag_cursor_to_box_delta = (
                                float(mouse_event.position().x() - rect.left()),
                                float(mouse_event.position().y() - rect.top()),
                            )
                        else:
                            self._pin_drag_cursor_to_box_delta = (0.0, 0.0)
                    return True
            if mouse_event.button() == QtCore.Qt.MouseButton.LeftButton and self._interaction_mode == "pan":
                self._drag_start_pos = mouse_event.position()
                self._drag_start_viewport = self._effective_viewport()
                self._begin_camera_interaction()
                self._update_cursor()
                return True
            if self._interaction_mode == "zoom" and mouse_event.button() == QtCore.Qt.MouseButton.LeftButton:
                self._zoom_press_pos = mouse_event.position()
                self._zoom_drag_current_pos = mouse_event.position()
                self._zoom_drag_active = False
                self._update_zoom_drag_overlay()
                return True
            return False
        if event.type() == QtCore.QEvent.Type.MouseMove:
            mouse_event = event
            previous_lasso_hover = self._lasso_hover_state_key()
            self._update_cursor_from_pointer_event(mouse_event)
            next_lasso_hover = self._lasso_hover_state_key()
            if self._interaction_mode == "lasso" and previous_lasso_hover != next_lasso_hover:
                self._refresh()
            if self._interaction_mode == "pan" and self._drag_start_pos is not None and self._drag_start_viewport is not None:
                self._pan_from_drag(mouse_event.position())
                return True
            if self._interaction_mode == "lasso" and self._lasso_dragging_anchor is not None:
                world = self._screen_to_world(mouse_event.position().x(), mouse_event.position().y())
                if world is None:
                    return True
                self._move_dragged_lasso_anchor(np.array(world, dtype=np.float32))
                self._refresh()
                return True
            if (
                self._interaction_mode in {"selection", "pin"}
                and self._pin_drag_annotation_id is not None
            ):
                world = self._screen_to_world(mouse_event.position().x(), mouse_event.position().y())
                if world is not None and self._pin_drag_anchor_world is not None:
                    anchor_world = self._pin_drag_anchor_world
                    anchor_screen = self._world_to_screen(np.asarray(anchor_world, dtype=np.float32))
                    current_screen = mouse_event.position()
                    if anchor_screen is not None:
                        delta = self._pin_drag_cursor_to_box_delta or (0.0, 0.0)
                        box_left = float(current_screen.x() - delta[0])
                        box_top = float(current_screen.y() - delta[1])
                        dx = float(box_left - anchor_screen[0])
                        dy = float(box_top - anchor_screen[1])
                        self._set_pin_offset(
                            self._pin_drag_annotation_id,
                            dx,
                            dy,
                        )
                        self._refresh()
                return True
            if self._interaction_mode == "zoom" and self._zoom_press_pos is not None:
                self._zoom_drag_current_pos = mouse_event.position()
                drag_px = self._distance_px(self._zoom_press_pos, self._zoom_drag_current_pos)
                self._zoom_drag_active = drag_px > self._zoom_drag_threshold_px
                self._update_zoom_drag_overlay()
                return True
            if (
                self._interaction_mode == "selection"
                or (self._hover_enabled and self._interaction_mode != "lasso")
            ):
                pin_occludes_point = self._hit_test_pin(mouse_event.position()) is not None
                metadata_id = (
                    None
                    if pin_occludes_point
                    else self._pick_metadata_id(mouse_event.position().x(), mouse_event.position().y())
                )
                if metadata_id != self._last_hover_metadata_id:
                    self._last_hover_metadata_id = metadata_id
                    self._set_hovered_thumbnail_metadata_id(metadata_id)
                    if self._hover_enabled:
                        if metadata_id is None:
                            self._emit_point_hover(None)
                        else:
                            self._emit_point_hover(
                                {
                                    "metadata_id": int(metadata_id),
                                    "global_pos": self.mapToGlobal(mouse_event.position().toPoint()),
                                }
                            )
            return False
        if event.type() == QtCore.QEvent.Type.MouseButtonRelease:
            mouse_event = event
            release_pos = mouse_event.position()
            if (
                self._interaction_mode in {"selection", "pin"}
                and mouse_event.button() == QtCore.Qt.MouseButton.LeftButton
                and self._pin_ignore_next_release
            ):
                self._pin_ignore_next_release = False
                self._click_press_pos = None
                self._update_cursor_from_pointer_event(mouse_event)
                return True
            if self._interaction_mode == "pan":
                self._drag_start_pos = None
                self._drag_start_viewport = None
                self._commit_camera_interaction()
                self._update_cursor()
            if self._interaction_mode == "lasso":
                handled = self._handle_lasso_release(mouse_event)
                self._update_cursor_from_pointer_event(mouse_event)
                return handled
            if (
                self._interaction_mode in {"selection", "pin"}
                and mouse_event.button() == QtCore.Qt.MouseButton.LeftButton
            ):
                if self._pin_drag_annotation_id is not None:
                    self._pin_drag_annotation_id = None
                    self._pin_drag_anchor_world = None
                    self._pin_drag_cursor_to_box_delta = None
                    self._update_cursor_from_pointer_event(mouse_event)
                    return True
            if (
                self._interaction_mode in {"selection", "pin"}
                and mouse_event.button() == QtCore.Qt.MouseButton.RightButton
            ):
                target = self._hit_test_pin(release_pos) or self._pinned_annotation_id_at_point(release_pos)
                if target is not None:
                    self._click_press_pos = None
                    self.pin_context_requested.emit(target, self.mapToGlobal(release_pos.toPoint()))
                    return True
            if self._interaction_mode == "zoom":
                if mouse_event.button() == QtCore.Qt.MouseButton.RightButton:
                    self._zoom_at(release_pos.x(), release_pos.y(), zoom_in=False)
                    self._reset_zoom_drag()
                    self._zoom_out_mouse_held = False
                    self._update_cursor()
                    return True
                if mouse_event.button() == QtCore.Qt.MouseButton.LeftButton and self._zoom_press_pos is not None:
                    zoom_out = bool(
                        self._zoom_shift_held
                        or mouse_event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier
                    )
                    if zoom_out:
                        self._zoom_at(release_pos.x(), release_pos.y(), zoom_in=False)
                    elif self._zoom_drag_active and self._zoom_drag_current_pos is not None:
                        self._zoom_to_rect(self._zoom_press_pos, self._zoom_drag_current_pos)
                    else:
                        self._zoom_at(release_pos.x(), release_pos.y(), zoom_in=True)
                    self._reset_zoom_drag()
                    return True
                self._reset_zoom_drag()
            if (
                mouse_event.button() == QtCore.Qt.MouseButton.LeftButton
                and self._click_press_pos is not None
                and self._distance_px(self._click_press_pos, release_pos) > self._click_move_threshold_px
            ):
                self._click_press_pos = None
                return False
            metadata_id = self._pick_metadata_id(release_pos.x(), release_pos.y())
            self._click_press_pos = None
            if metadata_id is None:
                return False
            if mouse_event.button() == QtCore.Qt.MouseButton.LeftButton:
                if self._interaction_mode == "selection":
                    self.point_selection_requested.emit(
                        metadata_id,
                        self.mapToGlobal(release_pos.toPoint()),
                    )
                    return True
                if self._interaction_mode == "pin":
                    self._toggle_or_create_pin(metadata_id)
                    return True
                self.point_clicked.emit(metadata_id)
            elif mouse_event.button() == QtCore.Qt.MouseButton.RightButton:
                self.point_context_requested.emit(metadata_id, self.mapToGlobal(release_pos.toPoint()))
            return True
        if event.type() == QtCore.QEvent.Type.Wheel:
            wheel_event = event
            delta = wheel_event.angleDelta().y()
            if delta == 0:
                return False
            steps = float(delta) / 120.0
            factor = self._wheel_zoom_base ** steps
            camera_interaction_active = self._begin_camera_interaction()
            self._zoom_with_factor(
                wheel_event.position().x(),
                wheel_event.position().y(),
                factor=factor,
                refresh=not camera_interaction_active,
            )
            if camera_interaction_active:
                self._push_interaction_viewport()
                self._interaction_settle_timer.start()
            return True
        if event.type() == QtCore.QEvent.Type.KeyPress:
            key_event = event
            return self._handle_key_press(key_event)
        if event.type() == QtCore.QEvent.Type.KeyRelease:
            key_event = event
            return self._handle_key_release(key_event)
        if event.type() == QtCore.QEvent.Type.Leave:
            if self._last_hover_metadata_id is not None:
                self._last_hover_metadata_id = None
                self._set_hovered_thumbnail_metadata_id(None)
                if self._hover_enabled:
                    self._emit_point_hover(None)
            return False
        return False

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if self.eventFilter(self, event):
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self.eventFilter(self, event):
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if self.eventFilter(self, event):
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        if self.eventFilter(self, event):
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        if self.eventFilter(self, event):
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        if self.eventFilter(self, event):
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QtGui.QKeyEvent) -> None:
        if self.eventFilter(self, event):
            event.accept()
            return
        super().keyReleaseEvent(event)

    def _pick_metadata_id(self, x: float, y: float) -> int | None:
        if self._points.size == 0 or self._metadata_ids.size == 0:
            return None
        world = self._screen_to_world(x, y)
        if world is None:
            return None
        radius_px = max(5.0, self._point_size)
        world_radius = self._world_radius_for_pixels(radius_px)
        candidates = self._candidate_indices(world[0], world[1], world_radius)
        if candidates.size == 0:
            return None
        if self._visibility.shape[0] == self._points.shape[0]:
            candidates = candidates[self._visibility[candidates] > 0.0]
        hover_mask = self._hover_eligibility_mask
        if hover_mask is not None and hover_mask.shape[0] == self._points.shape[0]:
            candidates = candidates[hover_mask[candidates]]
        if candidates.size == 0:
            return None
        candidate_points = self._points[candidates]
        dx = candidate_points[:, 0] - world[0]
        dy = candidate_points[:, 1] - world[1]
        distances = np.sqrt(dx * dx + dy * dy)
        local_index = int(np.argmin(distances))
        index = int(candidates[local_index])
        if index >= self._metadata_ids.shape[0]:
            return None
        return int(self._metadata_ids[index])

    def _screen_to_world(self, x: float, y: float) -> tuple[float, float] | None:
        if self._points.size == 0:
            return None
        width = float(max(1, self.width()))
        height = float(max(1, self.height()))
        viewport = self._effective_viewport()
        if viewport is None:
            return None
        x_min, x_max, y_min, y_max = viewport
        if abs(x_max - x_min) < 1e-9 or abs(y_max - y_min) < 1e-9:
            return None
        world_x = x_min + (x / width) * (x_max - x_min)
        world_y = y_min + ((height - y) / height) * (y_max - y_min)
        return world_x, world_y

    def _begin_camera_interaction(self) -> bool:
        if self._camera_interaction_active:
            return True
        if self._render_backend is None or self._effective_viewport() is None:
            return False
        self._camera_interaction_active = True
        interaction_setter = getattr(
            self._render_backend,
            "set_thumbnail_interaction_active",
            None,
        )
        if callable(interaction_setter):
            interaction_setter(True)
        self._interaction_viewport_payload = self._last_viewport_payload
        return True

    def _push_interaction_viewport(self) -> None:
        if not self._camera_interaction_active or self._render_backend is None:
            return
        viewport = self._effective_viewport()
        if viewport is None or viewport == self._interaction_viewport_payload:
            return
        self._render_backend.set_viewport(viewport)
        self._camera_revision += 1
        for lasso_item in (self._painted_lasso_fill_item, self._painted_lasso_overlay_item):
            if lasso_item is not None:
                lasso_item.set_viewport(viewport)
        viewport_payload = [float(value) for value in viewport]
        for item in (
            self._native_lasso_fill_item,
            self._native_lasso_overlay_item,
            self._native_pin_overlay_item,
        ):
            setter = getattr(item, "setViewport", None)
            if callable(setter):
                setter(viewport_payload)
        self._pin_overlay.update()
        self._interaction_viewport_payload = viewport

    def _commit_camera_interaction(self) -> None:
        self._interaction_settle_timer.stop()
        if not self._camera_interaction_active:
            return
        viewport = self._effective_viewport()
        self._camera_interaction_active = False
        if self._render_backend is not None:
            interaction_setter = getattr(
                self._render_backend,
                "set_thumbnail_interaction_active",
                None,
            )
            if callable(interaction_setter):
                interaction_setter(False)
        if self._render_backend is not None and viewport != self._interaction_viewport_payload:
            self._render_backend.set_viewport(viewport)
        self._interaction_viewport_payload = None
        self._last_viewport_payload = viewport
        if viewport is not None:
            self._emit_camera_changed()
        self._refresh()

    def _refresh(self) -> None:
        self._update_selection_overlay_position()
        if self._camera_interaction_active:
            self._debug_trace("refresh_deferred_for_camera_interaction")
            return
        self._debug_trace(
            "refresh_enter",
            quick_available=self._quick_available,
            has_backend=self._render_backend is not None,
            backend=self._render_backend_name,
            static_revision=self._static_payload_revision,
            uploaded_static_revision=self._uploaded_static_payload_revision,
            viewport=self._viewport,
        )
        self._ensure_quick_backend_loaded(force=False)
        self._update_lod_decision()
        if self._render_backend is None or not self._quick_available:
            self._update_pin_overlay()
            self._debug_trace(
                "refresh_skip_no_backend",
                quick_available=self._quick_available,
                has_backend=self._render_backend is not None,
                backend=self._render_backend_name,
            )
            return
        payload = self._render_payload()
        if (
            self._uploaded_static_payload_revision == self._static_payload_revision
            and self._uploaded_position_revision < 0
        ):
            # Compatibility for tests and older adapters that only prime the
            # former combined-payload revision.
            self._uploaded_position_revision = self._position_revision
            self._uploaded_style_revision = self._style_revision
            self._uploaded_visibility_revision = self._visibility_revision
        position_pending = self._position_revision != self._uploaded_position_revision
        style_pending = self._style_revision != self._uploaded_style_revision
        visibility_pending = (
            self._visibility_revision != self._uploaded_visibility_revision
        )
        if position_pending:
            self._debug_trace(
                "refresh_set_static_data_start",
                point_shape=tuple(payload.points.shape),
                metadata_shape=tuple(payload.metadata_ids.shape),
                visibility_shape=tuple(payload.visibility.shape),
            )
            self._render_backend.set_static_data(payload)
            self._debug_trace("refresh_set_static_data_done")
            self._uploaded_static_payload_revision = self._static_payload_revision
            self._uploaded_position_revision = self._position_revision
            self._uploaded_style_revision = self._style_revision
            self._uploaded_visibility_revision = self._visibility_revision
        else:
            if style_pending:
                style_setter = getattr(self._render_backend, "set_point_style", None)
                style_updated = bool(
                    callable(style_setter) and style_setter(payload)
                )
                if style_updated:
                    self._uploaded_style_revision = self._style_revision
                else:
                    self._render_backend.set_static_data(payload)
                    self._uploaded_static_payload_revision = (
                        self._static_payload_revision
                    )
                    self._uploaded_position_revision = self._position_revision
                    self._uploaded_style_revision = self._style_revision
                    self._uploaded_visibility_revision = self._visibility_revision
                    visibility_pending = False
            if visibility_pending:
                visibility_setter = getattr(
                    self._render_backend,
                    "set_visibility",
                    None,
                )
                visibility_updated = bool(
                    callable(visibility_setter)
                    and visibility_setter(self._visibility)
                )
                if visibility_updated:
                    self._uploaded_visibility_revision = self._visibility_revision
                else:
                    self._render_backend.set_static_data(payload)
                    self._uploaded_static_payload_revision = (
                        self._static_payload_revision
                    )
                    self._uploaded_position_revision = self._position_revision
                    self._uploaded_style_revision = self._style_revision
                    self._uploaded_visibility_revision = self._visibility_revision
        if self._density_revision != self._uploaded_density_revision:
            density_setter = getattr(self._render_backend, "set_density_levels", None)
            if callable(density_setter):
                representatives = (
                    np.empty((0,), dtype=np.int64)
                    if self._active_scene is None
                    else self._active_scene.representative_indices
                )
                density_setter(self._density_levels, representatives)
            self._uploaded_density_revision = self._density_revision
        if payload.viewport != self._last_viewport_payload:
            self._debug_trace("refresh_set_viewport_start", viewport=payload.viewport)
            self._render_backend.set_viewport(payload.viewport)
            self._camera_revision += 1
            self._debug_trace("refresh_set_viewport_done")
            self._last_viewport_payload = payload.viewport
            if payload.viewport is not None:
                self._emit_camera_changed()
        lasso_state_setter = getattr(self._render_backend, "set_lasso_overlay_state", None)
        if callable(lasso_state_setter):
            lasso_state_setter(
                list(self._lasso_polygons),
                self._current_lasso_polygon_array(),
                self._lasso_selected_polygon_index,
                list(self._lasso_polygon_colors),
                self._lasso_fill_opacity,
                self._lasso_hover_anchor,
                self._lasso_hover_edge_insert,
                self._lasso_hover_polygon_index,
                option_modifier_held=self._lasso_anchor_modifier_held(),
            )
        for lasso_item, draw_fill in (
            (self._painted_lasso_fill_item, True),
            (self._painted_lasso_overlay_item, False),
        ):
            if lasso_item is None:
                continue
            lasso_item.set_draw_fill(draw_fill)
            lasso_item.set_viewport(payload.viewport)
            lasso_item.set_lasso_overlay_state(
                list(self._lasso_polygons),
                self._current_lasso_polygon_array(),
                self._lasso_selected_polygon_index,
                list(self._lasso_polygon_colors),
                self._lasso_fill_opacity,
                self._lasso_hover_anchor,
                self._lasso_hover_edge_insert,
                self._lasso_hover_polygon_index,
                option_modifier_held=self._lasso_anchor_modifier_held(),
            )
        if (
            self._thumbnail_runtime_policy is not None
            and self._thumbnail_runtime_policy != self._uploaded_thumbnail_runtime_policy
        ):
            runtime_policy_setter = getattr(
                self._render_backend,
                "set_thumbnail_runtime_policy",
                None,
            )
            if callable(runtime_policy_setter):
                runtime_policy_setter(self._thumbnail_runtime_policy)
                self._uploaded_thumbnail_runtime_policy = self._thumbnail_runtime_policy
        if self._thumbnail_layer_config != self._uploaded_thumbnail_layer_config:
            self._debug_trace(
                "refresh_set_thumbnail_layer_config_start",
                enabled=self._thumbnail_layer_config.enabled,
                variant_id=self._thumbnail_layer_config.variant_id,
            )
            self._render_backend.set_thumbnail_layer_config(self._thumbnail_layer_config)
            self._debug_trace("refresh_set_thumbnail_layer_config_done")
            self._uploaded_thumbnail_layer_config = self._thumbnail_layer_config
        if self._thumbnail_tile_policy != self._uploaded_thumbnail_tile_policy:
            self._debug_trace("refresh_set_thumbnail_tile_policy_start")
            self._render_backend.set_thumbnail_tile_policy(self._thumbnail_tile_policy)
            self._debug_trace("refresh_set_thumbnail_tile_policy_done")
            self._uploaded_thumbnail_tile_policy = self._thumbnail_tile_policy
        if self._thumbnail_tile_layer_config != self._uploaded_thumbnail_tile_layer_config:
            self._debug_trace("refresh_set_thumbnail_tile_layer_config_start")
            self._render_backend.set_thumbnail_tile_layer_config(self._thumbnail_tile_layer_config)
            self._debug_trace("refresh_set_thumbnail_tile_layer_config_done")
            self._uploaded_thumbnail_tile_layer_config = self._thumbnail_tile_layer_config
        native_lasso_items = (
            (self._native_lasso_fill_item, True),
            (self._native_lasso_overlay_item, False),
        )
        for lasso_item, draw_fill in native_lasso_items:
            setter = getattr(lasso_item, "setLassoData", None)
            if callable(setter):
                self._debug_trace("refresh_set_lasso_data_start")
                fill_setter = getattr(lasso_item, "setDrawFill", None)
                if callable(fill_setter):
                    fill_setter(draw_fill)
                viewport_payload = [] if self._viewport is None else [float(value) for value in self._viewport]
                polygons_payload = [np.asarray(poly, dtype=np.float32).tolist() for poly in self._lasso_polygons]
                current_polygon_payload = (
                    []
                    if self._current_lasso_polygon_array() is None
                    else np.asarray(self._current_lasso_polygon_array(), dtype=np.float32).tolist()
                )
                selected_polygon_index = (
                    int(self._lasso_selected_polygon_index)
                    if self._lasso_selected_polygon_index is not None
                    else -1
                )
                try:
                    setter(
                        viewport_payload,
                        polygons_payload,
                        list(self._lasso_polygon_colors),
                        self._lasso_fill_opacity,
                        current_polygon_payload,
                        selected_polygon_index,
                    )
                except TypeError as error:
                    message = str(error)
                    if "only accepts" not in message:
                        raise
                    try:
                        setter(
                            viewport_payload,
                            polygons_payload,
                            list(self._lasso_polygon_colors),
                            current_polygon_payload,
                            selected_polygon_index,
                        )
                    except TypeError as legacy_error:
                        if "only accepts" not in str(legacy_error):
                            raise
                        setter(
                            viewport_payload,
                            polygons_payload,
                            current_polygon_payload,
                            selected_polygon_index,
                        )
                self._debug_trace("refresh_set_lasso_data_done")
        self._sync_pin_overlay_state()
        self._debug_trace("refresh_done")

    def _update_lod_decision(self) -> None:
        viewport = self._effective_viewport()
        if self._points.shape[0] == 0 or viewport is None:
            visible_count = 0
        elif self._grid_index is None:
            visible_count = int(self._points.shape[0])
        else:
            visible_count = self._grid_index.visible_count(Viewport(*viewport))
        coverage_ratio = marker_coverage_ratio(
            visible_count=visible_count,
            point_size_px=self._point_size,
            edge_width_px=self._max_edge_width,
            canvas_width_px=max(1, self.width()),
            canvas_height_px=max(1, self.height()),
            device_pixel_ratio=max(0.1, float(self.devicePixelRatioF())),
        )
        decision = LodDecision(
            mode="exact",
            visible_count=visible_count,
            coverage_ratio=coverage_ratio,
            representative_limit=self._adaptive_render_budget.maximum,
        )
        previous_mode = self._render_mode
        self._render_mode = decision.mode
        self._lod_decision = decision
        self._update_representative_pick_mask(decision)
        if previous_mode != self._render_mode:
            self.render_mode_changed.emit(self._render_mode)
        lod_payload = (self._render_mode, int(decision.representative_limit))
        if lod_payload != self._last_lod_payload:
            setter = getattr(self._render_backend, "set_lod_policy", None)
            if callable(setter):
                setter(*lod_payload)
            self._last_lod_payload = lod_payload

    def _update_representative_pick_mask(self, decision: LodDecision) -> None:
        """Match hybrid CPU pointer targets to the renderer's sampled pool."""

        scene = self._active_scene
        if decision.mode != "hybrid" or scene is None:
            self._representative_pick_mask = np.empty((0,), dtype=bool)
            self._representative_pick_limit = -1
            return
        pool = scene.representative_indices
        limit = min(max(0, int(decision.representative_limit)), int(pool.size))
        if (
            limit == self._representative_pick_limit
            and self._representative_pick_mask.shape[0] == scene.point_count
        ):
            return
        if limit >= pool.size:
            mask = scene.representative_mask
        else:
            positions = np.floor(
                np.linspace(0.0, float(pool.size - 1), num=limit, dtype=np.float64) + 0.5
            ).astype(np.int64, copy=False)
            mask = np.zeros(scene.point_count, dtype=bool)
            mask[pool[positions]] = True
        self._representative_pick_mask = mask
        self._representative_pick_limit = limit

    def record_render_frame_time(self, frame_ms: float) -> int:
        """Feed renderer timing into the representative-point budget controller."""

        if self._render_mode == "exact":
            return self._adaptive_render_budget.limit
        previous = self._adaptive_render_budget.limit
        current = self._adaptive_render_budget.observe(float(frame_ms))
        if current != previous:
            self._last_lod_payload = None
            self._refresh()
        return current

    def _render_payload(self) -> RenderPayload:
        order = (
            None
            if isinstance(self._render_backend, NativeItemBackend) and self._renderer_api_version() >= 3
            else self._render_order_with_find_target_last()
        )
        if (
            isinstance(self._render_backend, NativeItemBackend)
            and self._renderer_api_version() < 3
            and self._points.shape[0] > 25_000
            and self._active_scene is not None
        ):
            limit = (
                25_000
                if self._lod_decision is None
                else min(25_000, max(2_000, self._lod_decision.representative_limit))
            )
            order = np.asarray(self._active_scene.representative_indices[:limit], dtype=np.int64)
            find_index = self._metadata_index_by_id.get(self._find_item_metadata_id) if self._find_item_metadata_id is not None else None
            if find_index is not None and int(find_index) not in order:
                order = np.append(order, int(find_index))
        elif (
            isinstance(self._render_backend, PaintedItemBackend)
            and self._points.shape[0] > 10_000
        ):
            scene = self._active_scene
            if scene is not None and scene.spatial_index is not None:
                spatial_order = scene.spatial_index.spatial_order
            else:
                spatial_order = np.arange(self._points.shape[0], dtype=np.int64)
            limit = min(10_000, int(spatial_order.size))
            positions = np.linspace(
                0,
                max(0, int(spatial_order.size) - 1),
                num=limit,
                dtype=np.int64,
            )
            order = np.asarray(spatial_order[positions], dtype=np.int64)
            find_index = (
                self._metadata_index_by_id.get(self._find_item_metadata_id)
                if self._find_item_metadata_id is not None
                else None
            )
            if find_index is not None and int(find_index) not in order:
                order = np.append(order, int(find_index))
        point_size = (
            self._reorder_for_render(self._point_sizes, order)
            if self._point_sizes is not None
            else self._point_size
        )
        return RenderPayload(
            points=self._reorder_for_render(self._points, order),
            metadata_ids=self._reorder_for_render(self._metadata_ids, order),
            visibility=self._reorder_for_render(self._visibility, order),
            colors=self._reorder_for_render(self._colors, order),
            edge_colors=self._reorder_for_render(self._edge_colors, order),
            edge_widths=self._reorder_for_render(self._edge_widths, order),
            point_size=point_size,
            viewport=self._viewport,
            lasso_polygons=self._lasso_polygons,
            lasso_current_polygon=self._current_lasso_polygon_array(),
            lasso_selected_polygon_index=self._lasso_selected_polygon_index,
        )

    def _render_order_with_find_target_last(self) -> np.ndarray | None:
        if self._find_item_metadata_id is None or self._points.shape[0] <= 1:
            return None
        index = self._metadata_index_by_id.get(int(self._find_item_metadata_id))
        count = int(self._points.shape[0])
        if index is None or index < 0 or index >= count or index == count - 1:
            return None
        order = np.arange(count, dtype=np.int64)
        return np.concatenate((order[:index], order[index + 1 :], order[index : index + 1]))

    def _invalidate_compatibility_find_order(self) -> None:
        if (
            isinstance(self._render_backend, NativeItemBackend)
            and self._renderer_api_version() >= 3
        ):
            return
        self._position_revision += 1
        self._static_payload_revision += 1

    @staticmethod
    def _reorder_for_render(value, order: np.ndarray | None):
        if value is None or order is None:
            return value
        if np.isscalar(value):
            return value
        array = np.asarray(value)
        if array.ndim == 0:
            return value
        if order.size == 0:
            return array[:0]
        if np.min(order) < 0 or np.max(order) >= array.shape[0]:
            return value
        return array[order]

    def _native_pin_overlay_payload(self) -> list[dict]:
        payload: list[dict] = []
        for item in self._pinned_annotations:
            if not bool(item.get("visible", True)) or not bool(
                item.get("available_in_projection", True)
            ):
                continue
            entry = dict(item)
            rgba = entry.get("thumbnail_rgba")
            if rgba:
                entry["thumbnail_rgba"] = QtCore.QByteArray(bytes(rgba))
            payload.append(entry)
        return payload

    def _sync_pin_overlay_state(self) -> None:
        visible = bool(self._pins_enabled and self._pins_visible)
        property_setter = getattr(self._native_pin_overlay_item, "setProperty", None)
        if callable(property_setter):
            property_setter("visible", visible)
        setter = getattr(self._native_pin_overlay_item, "setPinData", None)
        if callable(setter):
            visible_pins = self._native_pin_overlay_payload()
            self._debug_trace("refresh_set_pin_data_start", pin_count=len(visible_pins))
            setter(
                [] if self._viewport is None else [float(value) for value in self._viewport],
                visible_pins,
                dict(self._pin_style),
                visible,
            )
            self._debug_trace("refresh_set_pin_data_done")
        self._update_pin_overlay()

    def set_thumbnail_layer_config(
        self,
        *,
        enabled: bool,
        variant_id: str,
        zoom_mid_threshold: float = THUMBNAIL_CONFIG.zoom_mid_threshold,
        zoom_close_threshold: float = THUMBNAIL_CONFIG.zoom_close_threshold,
        thumbnail_size_px: float = 64.0,
        max_visible_mid: int = THUMBNAIL_CONFIG.native_draw_limits()[0],
        max_visible_close: int = THUMBNAIL_CONFIG.native_draw_limits()[1],
        coloring: str = "none",
        colors: object | None = None,
        border_colors: object | None = None,
        border_width_px: float = 2.0,
        color_opacity: float = 0.75,
        render_margin_fraction: float = THUMBNAIL_CONFIG.prefetch_margin_fraction,
    ) -> None:
        previous_config = self._thumbnail_layer_config
        self._debug_trace(
            "thumbnail_layer_config_enter",
            enabled=enabled,
            variant_id=variant_id,
            backend=self._render_backend_name,
            native_atlas_supported=self.supports_native_thumbnail_atlas(),
        )
        normalized_coloring = str(coloring or "none").strip().lower()
        if normalized_coloring not in {"none", "border", "tint", "tint_border"}:
            normalized_coloring = "none"
        color_data = self._thumbnail_color_buffer(colors)
        overlay_config = ThumbnailLayerConfigPayload(
            enabled=bool(enabled),
            variant_id=str(variant_id or "none"),
            zoom_mid_threshold=float(zoom_mid_threshold),
            zoom_close_threshold=float(zoom_close_threshold),
            thumbnail_size_px=max(1.0, float(thumbnail_size_px)),
            max_visible_mid=max(1, int(max_visible_mid)),
            max_visible_close=max(1, int(max_visible_close)),
            coloring=normalized_coloring,
            color_data=color_data,
            border_color_data=self._thumbnail_color_buffer(border_colors),
            border_width_px=max(0.0, float(border_width_px)),
            color_opacity=max(0.0, min(1.0, float(color_opacity))),
            render_margin_fraction=max(0.0, float(render_margin_fraction)),
        )
        use_backend = bool(overlay_config.enabled and self.supports_thumbnail_layer())
        use_native = bool(use_backend and self.supports_native_thumbnail_atlas())
        self._native_thumbnail_layer_active = use_native
        self._debug_trace(
            "thumbnail_layer_config_decision",
            requested_enabled=overlay_config.enabled,
            use_backend=use_backend,
            use_native=use_native,
            normalized_coloring=normalized_coloring,
        )
        if use_backend:
            next_config = overlay_config
        else:
            next_config = ThumbnailLayerConfigPayload(
                enabled=False,
                variant_id="none",
                zoom_mid_threshold=overlay_config.zoom_mid_threshold,
                zoom_close_threshold=overlay_config.zoom_close_threshold,
                thumbnail_size_px=overlay_config.thumbnail_size_px,
                max_visible_mid=overlay_config.max_visible_mid,
                max_visible_close=overlay_config.max_visible_close,
                coloring="none",
                color_data=b"",
                border_color_data=overlay_config.border_color_data,
                border_width_px=overlay_config.border_width_px,
                color_opacity=overlay_config.color_opacity,
                render_margin_fraction=overlay_config.render_margin_fraction,
            )
        self._thumbnail_layer_config = next_config
        if self._render_backend is not None and self._thumbnail_layer_config != self._uploaded_thumbnail_layer_config:
            self._debug_trace(
                "thumbnail_layer_backend_upload_start",
                enabled=self._thumbnail_layer_config.enabled,
                variant_id=self._thumbnail_layer_config.variant_id,
            )
            self._render_backend.set_thumbnail_layer_config(self._thumbnail_layer_config)
            self._debug_trace("thumbnail_layer_backend_upload_done")
            self._uploaded_thumbnail_layer_config = self._thumbnail_layer_config
        refresh_viewport_payload = (
            previous_config.enabled != next_config.enabled
            or previous_config.variant_id != next_config.variant_id
            or previous_config.zoom_mid_threshold != next_config.zoom_mid_threshold
            or previous_config.zoom_close_threshold != next_config.zoom_close_threshold
            or previous_config.thumbnail_size_px != next_config.thumbnail_size_px
            or previous_config.render_margin_fraction != next_config.render_margin_fraction
        )
        if refresh_viewport_payload:
            self._last_viewport_payload = None
            self._debug_trace("thumbnail_layer_refresh_start")
            self._refresh()
            self._debug_trace("thumbnail_layer_refresh_done")

    @staticmethod
    def _thumbnail_color_buffer(colors: object | None) -> bytes:
        if colors is None:
            return b""
        try:
            array = np.asarray(colors, dtype=np.float32)
        except (TypeError, ValueError):
            return b""
        if array.ndim != 2 or array.shape[0] <= 0 or array.shape[1] < 3:
            return b""
        rgba = np.ones((array.shape[0], 4), dtype=np.float32)
        rgba[:, : min(array.shape[1], 4)] = array[:, : min(array.shape[1], 4)]
        np.clip(rgba, 0.0, 1.0, out=rgba)
        return np.ascontiguousarray(rgba, dtype=np.float32).tobytes()

    def set_thumbnail_tile_layer_config(
        self,
        *,
        enabled: bool,
        variant_id: str,
        discovery_cache_id: str,
        tile_size_px: int = THUMBNAIL_CONFIG.tile_size_px,
    ) -> None:
        previous_config = self._thumbnail_tile_layer_config
        next_config = ThumbnailTileLayerConfigPayload(
            enabled=bool(enabled),
            variant_id=str(variant_id or "none"),
            discovery_cache_id=str(discovery_cache_id or ""),
            tile_size_px=max(64, int(tile_size_px)),
        )
        self._thumbnail_tile_layer_config = next_config
        if self._render_backend is not None and next_config != self._uploaded_thumbnail_tile_layer_config:
            self._render_backend.set_thumbnail_tile_layer_config(next_config)
            self._uploaded_thumbnail_tile_layer_config = next_config
        if previous_config.variant_id != next_config.variant_id or previous_config.discovery_cache_id != next_config.discovery_cache_id:
            self.clear_thumbnail_tile_cache_state()
        self._refresh()

    def set_thumbnail_tile_policy(
        self,
        *,
        max_cache_mb: int = THUMBNAIL_CONFIG.tile_cache_mb,
        decode_workers: int = THUMBNAIL_CONFIG.tile_decode_workers,
        max_decode_jobs: int = THUMBNAIL_CONFIG.tile_max_decode_jobs,
    ) -> None:
        self._thumbnail_tile_policy = ThumbnailTilePolicyPayload(
            max_cache_mb=max(16, int(max_cache_mb)),
            decode_workers=max(1, int(decode_workers)),
            max_decode_jobs=max(1, int(max_decode_jobs)),
        )
        if self._render_backend is not None and self._thumbnail_tile_policy != self._uploaded_thumbnail_tile_policy:
            self._render_backend.set_thumbnail_tile_policy(self._thumbnail_tile_policy)
            self._uploaded_thumbnail_tile_policy = self._thumbnail_tile_policy

    def push_thumbnail_tiles(self, entries: list[dict[str, object]]) -> None:
        if self._render_backend is None or not entries:
            return
        payload: list[ThumbnailTileEntry] = []
        for entry in entries:
            try:
                bounds = entry.get("bounds")
                if isinstance(bounds, (tuple, list)) and len(bounds) == 4:
                    x_min, x_max, y_min, y_max = [float(value) for value in bounds]
                else:
                    x_min = float(entry.get("x_min"))
                    x_max = float(entry.get("x_max"))
                    y_min = float(entry.get("y_min"))
                    y_max = float(entry.get("y_max"))
                payload.append(
                    ThumbnailTileEntry(
                        lod=int(entry.get("lod")),
                        tile_x=int(entry.get("tile_x")),
                        tile_y=int(entry.get("tile_y")),
                        path=str(entry.get("path") or ""),
                        mtime_ns=int(entry.get("mtime_ns") or 0),
                        x_min=x_min,
                        x_max=x_max,
                        y_min=y_min,
                        y_max=y_max,
                    )
                )
            except (TypeError, ValueError):
                continue
        payload = [entry for entry in payload if entry.path]
        if payload:
            self._render_backend.set_thumbnail_tiles(payload)

    def set_thumbnail_tile_display_keys(self, keys: list[dict[str, object]]) -> None:
        if self._render_backend is None:
            return
        payload: list[ThumbnailTileDisplayKeyEntry] = []
        for key in keys:
            try:
                payload.append(
                    ThumbnailTileDisplayKeyEntry(
                        lod=int(key.get("lod")),
                        tile_x=int(key.get("tile_x")),
                        tile_y=int(key.get("tile_y")),
                    )
                )
            except (TypeError, ValueError):
                continue
        self._render_backend.set_thumbnail_tile_display_keys(payload)

    def set_visible_thumbnail_metadata_ids(self, metadata_ids: list[int]) -> None:
        if self._render_backend is None:
            return
        self._render_backend.set_visible_thumbnail_metadata_ids(
            self._thumbnail_ids_without_visible_pins(metadata_ids)
        )

    def _set_hovered_thumbnail_metadata_id(self, metadata_id: int | None) -> None:
        if self._render_backend is None:
            return
        if self._find_item_metadata_id is not None:
            metadata_id = self._find_item_metadata_id
        setter = getattr(self._render_backend, "set_hovered_thumbnail_metadata_id", None)
        if callable(setter):
            setter(None if metadata_id is None else int(metadata_id))

    def set_thumbnail_candidates(self, metadata_ids: list[int], *, target_px: int) -> None:
        self._requested_thumbnail_candidate_metadata_ids = [int(value) for value in metadata_ids]
        self._requested_thumbnail_candidate_target_px = max(1, int(target_px))
        if self._render_backend is None:
            return
        self._render_backend.set_thumbnail_candidates(
            self._thumbnail_ids_without_visible_pins(metadata_ids),
            target_px=self._requested_thumbnail_candidate_target_px,
        )

    def set_thumbnail_viewport_plan(
        self,
        *,
        revision: int,
        metadata_ids: list[int],
        forced_metadata_ids: list[int],
        planning_viewport: tuple[float, float, float, float],
        display_px: int,
        decode_px: int,
    ) -> None:
        self._requested_thumbnail_candidate_metadata_ids = [int(value) for value in metadata_ids]
        self._requested_thumbnail_candidate_target_px = max(1, int(decode_px))
        if self._render_backend is None:
            return
        payload = ThumbnailPlanPayload(
            revision=max(0, int(revision)),
            metadata_ids=tuple(
                self._thumbnail_ids_without_visible_pins(metadata_ids)
            ),
            forced_metadata_ids=tuple(int(value) for value in forced_metadata_ids),
            planning_viewport=tuple(float(value) for value in planning_viewport),
            display_px=max(1, int(display_px)),
            decode_px=max(1, int(decode_px)),
        )
        setter = getattr(self._render_backend, "set_thumbnail_plan", None)
        if callable(setter):
            setter(payload)
        else:
            self._render_backend.set_thumbnail_candidates(
                list(payload.metadata_ids),
                target_px=payload.decode_px,
            )

    def set_thumbnail_runtime_policy(
        self,
        *,
        cpu_cache_mb: int,
        gpu_cache_mb: int,
        atlas_page_px: int,
        decode_workers: int,
        upload_bytes_per_frame: int,
        target_frame_ms: float,
    ) -> None:
        policy = ThumbnailRuntimePolicyPayload(
            cpu_cache_mb=max(16, int(cpu_cache_mb)),
            gpu_cache_mb=max(16, int(gpu_cache_mb)),
            atlas_page_px=max(512, int(atlas_page_px)),
            decode_workers=max(1, int(decode_workers)),
            upload_bytes_per_frame=max(256 * 1024, int(upload_bytes_per_frame)),
            target_frame_ms=max(1.0, float(target_frame_ms)),
        )
        self._thumbnail_runtime_policy = policy
        if (
            self._render_backend is not None
            and policy != self._uploaded_thumbnail_runtime_policy
        ):
            setter = getattr(self._render_backend, "set_thumbnail_runtime_policy", None)
            if callable(setter):
                setter(policy)
                self._uploaded_thumbnail_runtime_policy = policy

    def set_thumbnail_plan_diagnostics(
        self,
        *,
        desired_visible: int,
        planned_visible: int,
        planned_prefetch: int,
        budget_suppressed: int,
        estimated_screen_cost: float,
        adaptive_limit: int,
        minimum_spacing_px: float = 0.0,
        decode_px: int,
        stale_discarded: int,
        missing: int = 0,
        failed: int = 0,
    ) -> None:
        self._thumbnail_runtime_status.update(
            {
                "desired_visible": max(0, int(desired_visible)),
                "planned_visible": max(0, int(planned_visible)),
                "planned_prefetch": max(0, int(planned_prefetch)),
                "budget_suppressed": max(0, int(budget_suppressed)),
                "estimated_screen_cost": max(0.0, float(estimated_screen_cost)),
                "adaptive_limit": max(0, int(adaptive_limit)),
                "minimum_spacing_px": max(0.0, float(minimum_spacing_px)),
                "resolution_tier": max(1, int(decode_px)),
                "stale_discarded": max(0, int(stale_discarded)),
                "source_missing": max(0, int(missing)),
                "source_failed": max(0, int(failed)),
            }
        )

    def missing_thumbnail_metadata_ids(self, metadata_ids: list[int], *, target_px: int) -> list[int]:
        metadata_ids = self._thumbnail_ids_without_visible_pins(metadata_ids)
        if self._render_backend is None:
            return [int(value) for value in metadata_ids]
        missing = getattr(self._render_backend, "missing_thumbnail_metadata_ids", None)
        if not callable(missing):
            return [int(value) for value in metadata_ids]
        return missing([int(value) for value in metadata_ids], target_px=max(1, int(target_px)))

    def evict_thumbnails_outside_viewport(
        self,
        viewport: tuple[float, float, float, float] | None,
        *,
        target_px: int,
    ) -> None:
        if self._render_backend is None:
            return
        evictor = getattr(self._render_backend, "evict_thumbnails_outside_viewport", None)
        if callable(evictor):
            evictor(viewport, target_px=target_px)

    def clear_thumbnail_tile_cache_state(self) -> None:
        if self._render_backend is not None:
            self._render_backend.clear_thumbnail_tiles()

    def push_thumbnail_batch(self, entries: list[dict[str, object]]) -> None:
        if self._render_backend is None or not entries:
            return
        payload: list[ThumbnailBatchEntry] = []
        for entry in entries:
            try:
                metadata_id = int(entry.get("metadata_id"))
                width = int(entry.get("width"))
                height = int(entry.get("height"))
                target_px = int(entry.get("target_px"))
                rgba = bytes(entry.get("rgba") or b"")
            except (TypeError, ValueError):
                continue
            if width <= 0 or height <= 0:
                continue
            if len(rgba) != width * height * 4:
                continue
            payload.append(
                ThumbnailBatchEntry(
                    metadata_id=metadata_id,
                    width=width,
                    height=height,
                    rgba=rgba,
                    target_px=target_px,
                )
            )
        if payload:
            self._render_backend.set_thumbnail_batch(payload)

    def set_thumbnail_loading_indicator(self, active: bool, message: str | None = None) -> None:
        text = str(message or "Preparing thumbnails").strip() or "Preparing thumbnails"
        self._thumbnail_loading_label.setText(text)
        self._thumbnail_loading_label.adjustSize()
        self._position_thumbnail_loading_label()
        self._thumbnail_loading_label.setVisible(bool(active))
        if active:
            self._thumbnail_loading_label.raise_()

    def supports_native_thumbnail_sources(self) -> bool:
        if self._render_backend is None:
            return False
        checker = getattr(self._render_backend, "supports_thumbnail_sources", None)
        return bool(callable(checker) and checker())

    def supports_native_thumbnail_atlas(self) -> bool:
        if self._render_backend is None:
            return False
        return bool(
            self._render_backend_name.startswith("qtquick_native_v")
            and self._render_backend.supports_thumbnail_layer()
        )

    def using_native_thumbnail_layer(self) -> bool:
        return bool(self._native_thumbnail_layer_active)

    def supports_thumbnail_layer(self) -> bool:
        if self._render_backend is None:
            return False
        checker = getattr(self._render_backend, "supports_thumbnail_layer", None)
        return bool(callable(checker) and checker())

    def supports_thumbnail_tiles(self) -> bool:
        if self._render_backend is None:
            return False
        checker = getattr(self._render_backend, "supports_thumbnail_tiles", None)
        return bool(callable(checker) and checker())

    def push_thumbnail_sources(
        self,
        variant_id: str,
        entries: list[dict[str, object]],
        *,
        revision: int = 0,
    ) -> None:
        if self._render_backend is None:
            return
        setter = getattr(self._render_backend, "set_thumbnail_sources", None)
        if not callable(setter):
            return
        payload: list[ThumbnailSourceEntry] = []
        for entry in entries:
            try:
                metadata_id = int(entry.get("metadata_id"))
                path = str(entry.get("path") or "")
                target_px = int(entry.get("target_px"))
                missing = bool(entry.get("missing", False))
                raw_mtime = entry.get("mtime_ns")
                mtime_ns = None if raw_mtime is None else int(raw_mtime)
            except (TypeError, ValueError):
                continue
            if metadata_id < 0 or target_px <= 0 or (not path and not missing):
                continue
            payload.append(
                ThumbnailSourceEntry(
                    metadata_id=metadata_id,
                    path=path,
                    mtime_ns=mtime_ns,
                    target_px=target_px,
                    missing=missing,
                )
            )
        setter(str(variant_id or "none"), payload, revision=max(0, int(revision)))

    def thumbnail_scene_snapshot(self) -> CanvasScene | None:
        """Return the current immutable scene for off-thread viewport planning."""

        return self._active_scene

    def camera_interaction_active(self) -> bool:
        return bool(self._camera_interaction_active)

    def push_thumbnail_atlas(
        self,
        *,
        variant_id: str,
        target_px: int,
        pages: list[dict[str, object]],
        sprites: list[dict[str, object]],
    ) -> None:
        if self._render_backend is None:
            return
        setter = getattr(self._render_backend, "set_thumbnail_atlas", None)
        if not callable(setter):
            return
        page_payload: list[ThumbnailAtlasPageEntry] = []
        for page in pages:
            try:
                page_payload.append(
                    ThumbnailAtlasPageEntry(
                        page_index=int(page.get("page_index")),
                        path=str(page.get("path") or ""),
                        width=int(page.get("width")),
                        height=int(page.get("height")),
                    )
                )
            except (TypeError, ValueError):
                continue
        sprite_payload: list[ThumbnailAtlasSpriteEntry] = []
        for sprite in sprites:
            try:
                sprite_payload.append(
                    ThumbnailAtlasSpriteEntry(
                        metadata_id=int(sprite.get("metadata_id")),
                        page_index=int(sprite.get("page_index")),
                        u=float(sprite.get("u")),
                        v=float(sprite.get("v")),
                        width=float(sprite.get("width")),
                        height=float(sprite.get("height")),
                    )
                )
            except (TypeError, ValueError):
                continue
        if page_payload and sprite_payload:
            setter(str(variant_id or "none"), int(target_px), page_payload, sprite_payload)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        self._commit_camera_interaction()
        super().resizeEvent(event)
        self._zoom_drag_overlay.setGeometry(self.rect())
        self._find_item_overlay.setGeometry(self.rect())
        self._selection_overlay.setGeometry(self.rect())
        self._pin_overlay.setGeometry(self.rect())
        self._position_thumbnail_loading_label()
        self._apply_viewport_frame_to_qml()
        if self._viewport is None:
            return
        self._viewport = self._aspect_normalized_viewport(self._viewport)
        self._update_find_item_overlay_position()
        self._update_selection_overlay_position()
        self._refresh()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        for cancel_event in self._scene_cancel_events.values():
            cancel_event.set()
        for cancel_event in self._lasso_query_cancel_events.values():
            cancel_event.set()
        self._shutdown_quick_host()
        super().closeEvent(event)

    def _shutdown_quick_host(self) -> None:
        if self._quick_shutdown:
            return
        self._quick_shutdown = True
        self._render_backend = None
        self._native_lasso_fill_item = None
        self._native_lasso_overlay_item = None
        self._native_pin_overlay_item = None
        self._qml_zoom_drag_item = None
        self._qml_find_blink_item = None
        self._qml_selection_item = None
        self._qml_root_object = None
        self._qml_component = None
        if self._quick_view is not None:
            self._quick_view.hide()
            self._quick_view.releaseResources()
            self._quick_view.close()

    def _position_thumbnail_loading_label(self) -> None:
        margin = 12
        size = self._thumbnail_loading_label.sizeHint()
        width = min(max(size.width(), 180), max(180, self.width() - (margin * 2)))
        height = max(size.height(), 28)
        x = max(margin, self.width() - width - margin)
        y = margin
        self._thumbnail_loading_label.setGeometry(x, y, width, height)

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        QtCore.QTimer.singleShot(0, self._refresh_after_show)

    def _refresh_after_show(self) -> None:
        self._ensure_quick_backend_loaded(force=True)
        if self._pending_initial_fit_view and self._points.size > 0:
            self._pending_initial_fit_view = False
            self.fit_view()
            self._request_backend_redraw_with_followup()
            return
        self._pending_initial_fit_view = False
        # Preserve already uploaded GPU buffers. A stacked-page Show event is
        # an exposure transition, not a data revision, and forcing a second
        # async static upload here can replace a valid first frame with a blank
        # one until another setting happens to wake the scene graph.
        self._refresh()
        self._request_backend_redraw_with_followup()

    def _request_backend_redraw(self) -> None:
        if self._render_backend is None:
            return
        request_redraw = getattr(self._render_backend, "request_redraw", None)
        if callable(request_redraw):
            request_redraw()

    def _request_backend_redraw_with_followup(self) -> None:
        self._request_backend_redraw()
        self._deferred_redraw_timer.start()

    @property
    def point_count(self) -> int:
        return int(self._points.shape[0])

    @property
    def is_quick_backend_active(self) -> bool:
        return self._quick_available

    @property
    def render_backend_name(self) -> str:
        return self._render_backend_name

    def native_upload_completion_available(self) -> bool:
        return bool(self._native_upload_signal_connected)

    @property
    def current_render_mode(self) -> str:
        return self._render_mode

    def render_diagnostics(self) -> dict[str, object]:
        decision = self._lod_decision
        return {
            "backend": self._render_backend_name,
            "quick_host": self._quick_host_name,
            "renderer_api_version": self._renderer_api_version(),
            "scene_revision": self._active_scene_revision,
            "point_count": self.point_count,
            "scene_build_ms": self._last_scene_build_ms,
            "upload_ms": self._backend_upload_time_ms(),
            "native_preparation_ms": self._last_native_upload_preparation_ms,
            "native_uploaded_point_count": self._last_native_upload_point_count,
            "render_update_ms": self._last_render_frame_ms,
            "render_update_p95_ms": (
                0.0
                if not self._render_frame_samples_ms
                else float(np.percentile(np.asarray(self._render_frame_samples_ms), 95.0))
            ),
            "scene_memory_bytes": 0 if self._active_scene is None else self._active_scene.memory_bytes,
            "render_mode": self._render_mode,
            "visible_count": 0 if decision is None else decision.visible_count,
            "coverage_ratio": 0.0 if decision is None else decision.coverage_ratio,
            "representative_limit": 0 if decision is None else decision.representative_limit,
            "position_revision": self._position_revision,
            "style_revision": self._style_revision,
            "visibility_revision": self._visibility_revision,
            "density_revision": self._density_revision,
            "camera_revision": self._camera_revision,
            "last_scene_error": self._last_scene_error,
            "thumbnail_runtime": dict(self._thumbnail_runtime_status),
        }

    def _renderer_api_version(self) -> int:
        getter = getattr(self._render_backend, "renderer_api_version", None)
        if not callable(getter):
            return 0
        try:
            return int(getter())
        except (TypeError, ValueError, AttributeError):
            return 0

    def _backend_upload_time_ms(self) -> float:
        getter = getattr(self._render_backend, "upload_time_ms", None)
        if not callable(getter):
            return 0.0
        try:
            return float(getter())
        except (TypeError, ValueError, AttributeError):
            return 0.0

    @QtCore.Slot(float)
    def _on_native_render_timing(self, frame_ms: float) -> None:
        self._last_render_frame_ms = max(0.0, float(frame_ms))
        self._render_frame_samples_ms.append(self._last_render_frame_ms)
        self.record_render_frame_time(self._last_render_frame_ms)
        self.render_timing_changed.emit(self._last_render_frame_ms)

    def _on_native_upload_finished(self, _generation: int, point_count: int, preparation_ms: float) -> None:
        self._last_native_upload_point_count = max(0, int(point_count))
        self._last_native_upload_preparation_ms = max(0.0, float(preparation_ms))
        self.static_upload_finished.emit(self._last_native_upload_point_count)
        # Static native point uploads clear renderer-side thumbnail membership.
        # Replay the current plan after the new point/metadata lookup is active
        # while retaining reusable decoded images and atlas pages.
        self._refresh_thumbnail_candidates_for_pins()
        # The first async upload may finish while the hosted QQuickWindow is
        # still completing exposure. Request another frame from the adapter so
        # the initial points cannot remain hidden until an unrelated setting
        # change happens to update the scene graph.
        self._request_backend_redraw_with_followup()

    @QtCore.Slot(int, int, int, int, int, int, int, int, int, int)
    def _on_native_thumbnail_status(
        self,
        revision: int,
        desired: int,
        resident: int,
        drawn: int,
        queued: int,
        decoding: int,
        missing: int,
        failed: int,
        atlas_pages: int,
        atlas_bytes: int,
    ) -> None:
        self._thumbnail_runtime_status.update(
            {
                "revision": max(0, int(revision)),
                "desired": max(0, int(desired)),
                "resident": max(0, int(resident)),
                "drawn": max(0, int(drawn)),
                "queued": max(0, int(queued)),
                "decoding": max(0, int(decoding)),
                "missing": max(0, int(missing)),
                "failed": max(0, int(failed)),
                "atlas_pages": max(0, int(atlas_pages)),
                "atlas_bytes": max(0, int(atlas_bytes)),
            }
        )
        self.thumbnail_runtime_status_changed.emit(dict(self._thumbnail_runtime_status))

    @QtCore.Slot(float, float)
    def _on_native_thumbnail_timing(self, decode_ms: float, upload_ms: float) -> None:
        self._thumbnail_runtime_status["decode_ms"] = max(0.0, float(decode_ms))
        self._thumbnail_runtime_status["upload_ms"] = max(0.0, float(upload_ms))

    def _debug_trace(self, event: str, **payload: object) -> None:
        _ = event, payload

    def set_points(
        self,
        points: np.ndarray,
        colors: np.ndarray | None = None,
        symbols: np.ndarray | None = None,
        size: int | float | np.ndarray = 5,
        preserve_view: bool = False,
        visibility: np.ndarray | None = None,
        metadata_ids: np.ndarray | None = None,
        edge_color: np.ndarray | str | None = None,
        edge_width: int | float | np.ndarray | None = None,
        edge_alpha: float | None = None,
    ) -> None:
        # The deprecated symbols argument remains accepted only for stable
        # controller/API compatibility. The canvas always draws circles.
        del symbols, edge_alpha
        if np.isscalar(size):
            point_size = max(0.5, float(size))
        else:
            point_sizes = np.asarray(size, dtype=np.float32).reshape(-1)
            finite_sizes = point_sizes[np.isfinite(point_sizes)]
            point_size = max(0.5, float(np.max(finite_sizes))) if finite_sizes.size else 5.0
        raw_points = np.asarray(points)
        self._debug_trace(
            "set_points_enter",
            input_shape=tuple(raw_points.shape),
            metadata_shape=None if metadata_ids is None else tuple(np.asarray(metadata_ids).shape),
            preserve_view=preserve_view,
            backend=self._render_backend_name,
        )
        if raw_points.ndim != 2 or raw_points.shape[1] < 2:
            self._scene_revision += 1
            message = "Canvas points must be an N x 2 numeric array"
            self._last_scene_error = message
            self.scene_build_failed.emit(self._scene_revision, message)
            return

        self._scene_revision += 1
        revision = self._scene_revision
        for event in self._scene_cancel_events.values():
            event.set()
        self._lasso_query_revision += 1
        for event in self._lasso_query_cancel_events.values():
            event.set()
        source_token = (id(points), None if metadata_ids is None else id(metadata_ids))
        reuse_geometry = self._active_scene if source_token == self._active_position_source_token else None
        request = CanvasSceneRequest(
            revision=revision,
            points=raw_points,
            colors=colors,
            size=size,
            visibility=visibility,
            metadata_ids=metadata_ids,
            edge_colors=edge_color,
            edge_widths=edge_width,
            build_density=False,
            build_hybrid_lod=False,
            reuse_geometry=reuse_geometry,
        )
        self._latest_scene_request = request
        self._scene_preserve_view[revision] = bool(preserve_view)
        self._scene_source_tokens[revision] = source_token
        self._last_scene_error = ""
        point_count = int(raw_points.shape[0])
        self.scene_build_started.emit(revision, point_count)

        if point_count <= 10_000:
            started = time.perf_counter()
            try:
                scene = build_canvas_scene(request)
            except Exception as exc:
                self._on_scene_build_failed(revision, f"{type(exc).__name__}: {exc}")
                return
            self._on_scene_build_finished(revision, scene, (time.perf_counter() - started) * 1000.0)
            return

        cancelled = threading.Event()
        task = _SceneBuildTask(request, cancelled)
        task.signals.finished.connect(self._on_scene_build_finished)
        task.signals.failed.connect(self._on_scene_build_failed)
        task.signals.cancelled.connect(self._on_scene_build_cancelled)
        self._scene_cancel_events[revision] = cancelled
        self._scene_tasks[revision] = task
        self._scene_thread_pool.start(task)

    @QtCore.Slot(int, object, float)
    def _on_scene_build_finished(self, revision: int, scene: object, elapsed_ms: float) -> None:
        self._scene_tasks.pop(int(revision), None)
        self._scene_cancel_events.pop(int(revision), None)
        if int(revision) != self._scene_revision or not isinstance(scene, CanvasScene):
            return
        self._latest_scene_request = None
        preserve_view = self._scene_preserve_view.pop(int(revision), False)
        position_changed = self._active_scene is None or scene.spatial_index is not self._grid_index
        self._active_scene = scene
        self._active_scene_revision = int(revision)
        self._active_position_source_token = self._scene_source_tokens.pop(int(revision), None)
        self._points = scene.points
        self._metadata_ids = scene.metadata_ids
        self._metadata_index_by_id = scene.metadata_lookup
        self._visibility = scene.visibility
        self._colors = scene.colors
        self._edge_colors = scene.edge_colors
        self._edge_widths = scene.edge_widths
        self._point_size = scene.point_size
        self._point_sizes = scene.point_sizes
        self._grid_index = scene.spatial_index
        self._representative_pick_mask = scene.representative_mask
        self._representative_pick_limit = -1
        self._density_levels = scene.density_levels
        self._max_edge_width = (
            float(np.nanmax(scene.edge_widths)) if scene.edge_widths is not None and scene.edge_widths.size else 0.0
        )
        self._last_scene_build_ms = max(0.0, float(elapsed_ms))
        if position_changed:
            self._position_revision += 1
        self._style_revision += 1
        self._visibility_revision += 1
        self._density_revision += 1
        if position_changed:
            self._static_payload_revision += 1
        if not preserve_view or self._pending_scene_fit_view:
            self._viewport = self._fit_viewport()
            self._pending_initial_fit_view = not self.isVisible()
        self._pending_scene_fit_view = False
        self._refresh()
        self.scene_build_finished.emit(int(revision))

    @QtCore.Slot(int, str)
    def _on_scene_build_failed(self, revision: int, message: str) -> None:
        self._scene_tasks.pop(int(revision), None)
        self._scene_cancel_events.pop(int(revision), None)
        self._scene_preserve_view.pop(int(revision), None)
        self._scene_source_tokens.pop(int(revision), None)
        if int(revision) != self._scene_revision:
            return
        self._last_scene_error = str(message)
        self.scene_build_failed.emit(int(revision), str(message))

    @QtCore.Slot(int)
    def _on_scene_build_cancelled(self, revision: int) -> None:
        self._scene_tasks.pop(int(revision), None)
        self._scene_cancel_events.pop(int(revision), None)
        self._scene_preserve_view.pop(int(revision), None)
        self._scene_source_tokens.pop(int(revision), None)

    def update_visibility_mask(self, mask: np.ndarray | None) -> None:
        if self._active_scene_revision != self._scene_revision and self._latest_scene_request is not None:
            request = self._latest_scene_request
            self.set_points(
                request.points,
                colors=request.colors,
                size=request.size,
                preserve_view=True,
                visibility=mask,
                metadata_ids=request.metadata_ids,
                edge_color=request.edge_colors,
                edge_width=request.edge_widths,
            )
            return
        if mask is None:
            self._visibility = np.ones(self._points.shape[0], dtype=np.float32)
        else:
            visibility = np.asarray(mask, dtype=np.float32).reshape(-1)
            if visibility.shape[0] != self._points.shape[0]:
                raise ValueError(f"visibility mask must contain exactly {self._points.shape[0]} entries")
            self._visibility = visibility
        self._visibility_revision += 1
        self._refresh()

    def visibility_array(self) -> np.ndarray:
        return np.asarray(self._visibility, dtype=np.float32).copy()

    def fit_view(self) -> None:
        if self._active_scene_revision != self._scene_revision:
            self._pending_scene_fit_view = True
            return
        self._viewport = self._fit_viewport()
        self._refresh()

    def default_camera_state(self) -> dict[str, object]:
        return self._camera_state_for_viewport(self._fit_viewport())

    def is_default_camera(self, tolerance: float = 1e-6) -> bool:
        current = self._effective_viewport()
        if current is None:
            return False
        default = self._fit_viewport()
        return all(abs(float(a) - float(b)) <= tolerance for a, b in zip(current, default, strict=True))

    def _fit_viewport(self) -> tuple[float, float, float, float]:
        if self._points.size == 0:
            viewport = (0.0, 1.0, 0.0, 1.0)
        else:
            x_min = float(np.min(self._points[:, 0]))
            x_max = float(np.max(self._points[:, 0]))
            y_min = float(np.min(self._points[:, 1]))
            y_max = float(np.max(self._points[:, 1]))
            if abs(x_max - x_min) < 1e-9:
                x_max = x_min + 1.0
            if abs(y_max - y_min) < 1e-9:
                y_max = y_min + 1.0
            pad_x = (x_max - x_min) * 0.05
            pad_y = (y_max - y_min) * 0.05
            viewport = (x_min - pad_x, x_max + pad_x, y_min - pad_y, y_max + pad_y)
        return self._aspect_normalized_viewport(viewport)

    def camera_state(self) -> dict[str, object]:
        viewport = self._effective_viewport()
        if viewport is None:
            viewport = self._aspect_normalized_viewport((0.0, 1.0, 0.0, 1.0))
        return self._camera_state_for_viewport(viewport)

    def visible_item_diameter_world(self) -> float:
        """Return the current rendered item diameter in world coordinates."""
        camera = self.camera_state()
        frame = self.active_viewport_frame()
        width_px = max(1.0, float(frame.width()))
        height_px = max(1.0, float(frame.height()))
        world_per_pixel = max(
            abs(float(camera["x_max"]) - float(camera["x_min"])) / width_px,
            abs(float(camera["y_max"]) - float(camera["y_min"])) / height_px,
        )
        diameter_px = max(1.0, float(self._point_size))
        if self._thumbnail_layer_config.enabled:
            diameter_px = max(
                diameter_px,
                float(self._thumbnail_layer_config.thumbnail_size_px),
            )
        return diameter_px * world_per_pixel

    def _camera_state_for_viewport(
        self,
        viewport: tuple[float, float, float, float],
    ) -> dict[str, object]:
        frame = self.active_viewport_frame()
        cropped = cropped_viewport_for_frame(
            viewport,
            float(max(1, self.width())),
            float(max(1, self.height())),
            (frame.x(), frame.y(), frame.width(), frame.height()),
        )
        return {
            "x_min": float(cropped[0]),
            "x_max": float(cropped[1]),
            "y_min": float(cropped[2]),
            "y_max": float(cropped[3]),
            "viewport_aspect_preset": self._viewport_aspect_preset,
        }

    def viewport_aspect_preset(self) -> str:
        return self._viewport_aspect_preset

    def set_viewport_aspect_preset(self, preset: object) -> bool:
        key = normalize_viewport_aspect_preset(preset)
        if key == self._viewport_aspect_preset:
            return False
        self._viewport_aspect_preset = key
        self._apply_viewport_frame_to_qml()
        return True

    def crop_bars_visible(self) -> bool:
        return self._crop_bars_visible

    def set_crop_bars_visible(self, visible: bool) -> bool:
        next_visible = bool(visible)
        if next_visible == self._crop_bars_visible:
            return False
        self._crop_bars_visible = next_visible
        self._apply_viewport_frame_to_qml()
        return True

    def camera_preset_state(self) -> dict[str, float]:
        """Return the full canvas camera independently of the active crop."""

        viewport = self._effective_viewport()
        if viewport is None:
            viewport = self._aspect_normalized_viewport((0.0, 1.0, 0.0, 1.0))
        return {
            "x_min": float(viewport[0]),
            "x_max": float(viewport[1]),
            "y_min": float(viewport[2]),
            "y_max": float(viewport[3]),
        }

    def set_camera_preset_state(self, state: dict[str, object], preserve_aspect: bool = True) -> bool:
        """Restore camera bounds without changing the selected crop preset."""

        if not isinstance(state, dict):
            return False
        try:
            viewport = tuple(
                float(state[key]) for key in ("x_min", "x_max", "y_min", "y_max")
            )
        except (KeyError, TypeError, ValueError):
            return False
        if (
            not np.isfinite(viewport).all()
            or abs(viewport[1] - viewport[0]) < 1e-9
            or abs(viewport[3] - viewport[2]) < 1e-9
        ):
            return False
        legacy_aspect = normalize_viewport_aspect_preset(state.get("viewport_aspect_preset", "free"))
        if legacy_aspect != "free":
            frame = centered_frame_rect(
                float(max(1, self.width())),
                float(max(1, self.height())),
                legacy_aspect,
            )
            viewport = render_viewport_for_frame(
                viewport,
                float(max(1, self.width())),
                float(max(1, self.height())),
                frame,
            )
        self._viewport = (
            normalize_world_viewport(viewport, self._window_aspect_ratio(), expand=True)
            if preserve_aspect
            else viewport
        )
        self._refresh_with_camera_notification()
        return True

    def active_viewport_frame(self) -> QtCore.QRectF:
        x, y, width, height = centered_frame_rect(
            float(max(1, self.width())),
            float(max(1, self.height())),
            self._viewport_aspect_preset,
        )
        return QtCore.QRectF(x, y, width, height)

    def set_camera_state(self, state: dict[str, object], preserve_aspect: bool = True) -> bool:
        if not isinstance(state, dict):
            return False
        try:
            crop_viewport = (
                float(state.get("x_min")),
                float(state.get("x_max")),
                float(state.get("y_min")),
                float(state.get("y_max")),
            )
        except (TypeError, ValueError):
            return False
        if not np.isfinite(crop_viewport).all():
            return False
        if (
            abs(crop_viewport[1] - crop_viewport[0]) < 1e-9
            or abs(crop_viewport[3] - crop_viewport[2]) < 1e-9
        ):
            return False
        self._viewport_aspect_preset = normalize_viewport_aspect_preset(
            state.get("viewport_aspect_preset", "free")
        )
        canvas_keys = ("canvas_x_min", "canvas_x_max", "canvas_y_min", "canvas_y_max")
        try:
            viewport = (
                tuple(float(state[key]) for key in canvas_keys)
                if all(key in state for key in canvas_keys)
                else None
            )
        except (TypeError, ValueError):
            viewport = None
        if viewport is None:
            if self._viewport_aspect_preset == "free":
                viewport = crop_viewport
            else:
                frame = self.active_viewport_frame()
                viewport = render_viewport_for_frame(
                    crop_viewport,
                    float(max(1, self.width())),
                    float(max(1, self.height())),
                    (frame.x(), frame.y(), frame.width(), frame.height()),
                )
        if not np.isfinite(viewport).all():
            return False
        self._viewport = (
            normalize_world_viewport(viewport, self._window_aspect_ratio(), expand=True)
            if preserve_aspect
            else viewport
        )
        self._apply_viewport_frame_to_qml()
        self._refresh_with_camera_notification()
        return True

    def _grab_canvas_pixmap(self) -> QtGui.QPixmap:
        pixmap = QtGui.QPixmap()
        captured_quick_window = False
        # A QQuickView hosted through createWindowContainer is a native child
        # window, so QWidget.grab() does not include its contents. Capture the
        # Quick window directly before falling back to the widget host.
        if self._quick_view is not None:
            image = self._quick_view.grabWindow()
            if not image.isNull():
                pixmap = QtGui.QPixmap.fromImage(image)
                captured_quick_window = True
        elif self._quick is not None:
            quick_window = self._quick.quickWindow()
            if quick_window is not None:
                image = quick_window.grabWindow()
                if not image.isNull():
                    pixmap = QtGui.QPixmap.fromImage(image)
                    captured_quick_window = True
        if pixmap.isNull():
            pixmap = self.grab()
        elif captured_quick_window and not self._has_native_pin_overlay() and self._pin_overlay.isVisible():
            # QQuickWindow captures do not include the QWidget fallback pin
            # overlay. Composite it explicitly so exports match the canvas.
            painter = QtGui.QPainter(pixmap)
            painter.scale(
                pixmap.width() / float(max(1, self.width())),
                pixmap.height() / float(max(1, self.height())),
            )
            self._draw_pins_overlay(painter)
            painter.end()
        return pixmap

    def _active_frame_source_rect(self, pixmap: QtGui.QPixmap) -> QtCore.QRectF:
        frame = self.active_viewport_frame()
        scale_x = pixmap.width() / float(max(1, self.width()))
        scale_y = pixmap.height() / float(max(1, self.height()))
        source_rect = QtCore.QRectF(
            frame.x() * scale_x,
            frame.y() * scale_y,
            frame.width() * scale_x,
            frame.height() * scale_y,
        ).intersected(QtCore.QRectF(pixmap.rect()))
        return source_rect if not source_rect.isEmpty() else QtCore.QRectF(pixmap.rect())

    def _render_active_frame(
        self,
        pixmap: QtGui.QPixmap,
        target_size: QtCore.QSize,
    ) -> QtGui.QPixmap:
        output = QtGui.QPixmap(target_size)
        output.fill(self._background_color)
        painter = QtGui.QPainter(output)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(
            QtCore.QRectF(output.rect()),
            pixmap,
            self._active_frame_source_rect(pixmap),
        )
        painter.end()
        return output

    def capture_active_viewport_pixmap(self) -> QtGui.QPixmap:
        """Capture the active crop at the hosted canvas's native pixel size."""

        pixmap = self._grab_canvas_pixmap()
        if pixmap.isNull():
            return QtGui.QPixmap()
        source_rect = self._active_frame_source_rect(pixmap)
        target_size = QtCore.QSize(
            max(1, int(round(source_rect.width()))),
            max(1, int(round(source_rect.height()))),
        )
        return self._render_active_frame(pixmap, target_size)

    def render_snapshot_pixmap(self, size_px: int) -> QtGui.QPixmap:
        size = max(24, int(size_px))
        pixmap = self._grab_canvas_pixmap()
        if pixmap.isNull():
            frame = self.active_viewport_frame()
            frame_size = QtCore.QSize(
                max(1, int(round(frame.width()))),
                max(1, int(round(frame.height()))),
            ).scaled(size, size, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
            fallback = QtGui.QPixmap(frame_size)
            fallback.fill(QtGui.QColor("#d9dde3"))
            return fallback
        source_rect = self._active_frame_source_rect(pixmap)
        if source_rect.width() >= source_rect.height():
            target_size = QtCore.QSize(
                size,
                max(1, int(round(size * source_rect.height() / source_rect.width()))),
            )
        else:
            target_size = QtCore.QSize(
                max(1, int(round(size * source_rect.width() / source_rect.height()))),
                size,
            )
        return self._render_active_frame(pixmap, target_size)

    def clear(self) -> None:
        self._scene_revision += 1
        self._lasso_query_revision += 1
        for event in self._scene_cancel_events.values():
            event.set()
        for event in self._lasso_query_cancel_events.values():
            event.set()
        self._latest_scene_request = None
        self._active_scene = None
        self._representative_pick_mask = np.empty((0,), dtype=bool)
        self._representative_pick_limit = -1
        self._active_scene_revision = self._scene_revision
        self._active_position_source_token = None
        self._density_levels = ()
        self._pending_scene_fit_view = False
        self.clear_find_item_blink()
        self.clear_selected_metadata_id()
        self._points = np.empty((0, 2), dtype=np.float32)
        self._metadata_ids = np.empty((0,), dtype=np.int64)
        self._metadata_index_by_id = {}
        self._visibility = np.empty((0,), dtype=np.float32)
        self._colors = None
        self._edge_colors = None
        self._edge_widths = None
        self._grid_index = None
        self._viewport = self._aspect_normalized_viewport((0.0, 1.0, 0.0, 1.0))
        self._point_sizes = None
        self._position_revision += 1
        self._style_revision += 1
        self._visibility_revision += 1
        self._static_payload_revision += 1
        self._uploaded_static_payload_revision = -1
        self._uploaded_density_revision = -1
        self._last_lod_payload = None
        self._last_viewport_payload = None
        self._uploaded_thumbnail_layer_config = None
        self._uploaded_thumbnail_tile_layer_config = None
        self.clear_thumbnail_tile_cache_state()
        self._refresh()

    def blink_metadata_id(self, metadata_id: int) -> bool:
        try:
            target = int(metadata_id)
        except (TypeError, ValueError):
            return False
        index = self._metadata_index_by_id.get(target)
        if index is None or index < 0 or index >= self._points.shape[0]:
            return False
        point = self._points[index]
        if not np.isfinite(point).all():
            return False
        self._center_camera_on_point(float(point[0]), float(point[1]))
        self._find_item_metadata_id = target
        self._invalidate_compatibility_find_order()
        self._find_item_phase = 0.0
        self._set_hovered_thumbnail_metadata_id(target)
        self._refresh_thumbnail_candidates_for_pins()
        self._update_find_item_overlay_position()
        if not self._find_item_timer.isActive():
            self._find_item_timer.start()
        self._refresh()
        return True

    def active_find_metadata_id(self) -> int | None:
        return self._find_item_metadata_id

    def clear_find_item_blink(self) -> None:
        had_target = self._find_item_metadata_id is not None
        self._find_item_metadata_id = None
        if self._find_item_timer.isActive():
            self._find_item_timer.stop()
        self._set_find_blink_state(None, self._find_item_phase)
        if had_target:
            self._invalidate_compatibility_find_order()
            self._set_hovered_thumbnail_metadata_id(self._last_hover_metadata_id)
            self._refresh_thumbnail_candidates_for_pins()
            self._refresh()

    def select_metadata_id(self, metadata_id: int) -> bool:
        try:
            target = int(metadata_id)
        except (TypeError, ValueError):
            return False
        index = self._metadata_index_by_id.get(target)
        if index is None or index < 0 or index >= self._points.shape[0]:
            return False
        self._selected_metadata_id = target
        self._update_selection_overlay_position()
        return True

    def selected_metadata_id(self) -> int | None:
        return self._selected_metadata_id

    def clear_selected_metadata_id(self) -> None:
        self._selected_metadata_id = None
        self._set_selection_overlay_center(None)

    def _update_selection_overlay_position(self) -> None:
        if self._selected_metadata_id is None:
            self._set_selection_overlay_center(None)
            return
        index = self._metadata_index_by_id.get(self._selected_metadata_id)
        if index is None or index < 0 or index >= self._points.shape[0]:
            self.clear_selected_metadata_id()
            return
        screen = self._world_to_screen(self._points[index])
        center = (
            None
            if screen is None
            else QtCore.QPointF(float(screen[0]), float(screen[1]))
        )
        self._set_selection_overlay_center(center)

    def _set_selection_overlay_center(self, center: QtCore.QPointF | None) -> None:
        self._selection_overlay.set_center(center)
        item = self._qml_selection_item
        if item is None:
            return
        item.setProperty("active", center is not None)
        if center is not None:
            item.setProperty("centerX", float(center.x()))
            item.setProperty("centerY", float(center.y()))

    def _on_find_item_blink_tick(self) -> None:
        if self._find_item_metadata_id is None:
            self.clear_find_item_blink()
            return
        self._find_item_phase += 0.45
        self._update_find_item_overlay_position()

    def _update_find_item_overlay_position(self) -> None:
        if self._find_item_metadata_id is None:
            self._set_find_blink_state(None, self._find_item_phase)
            return
        index = self._metadata_index_by_id.get(self._find_item_metadata_id)
        if index is None or index < 0 or index >= self._points.shape[0]:
            self.clear_find_item_blink()
            return
        screen = self._world_to_screen(self._points[index])
        if screen is None:
            self._set_find_blink_state(None, self._find_item_phase)
            return
        self._set_find_blink_state(
            QtCore.QPointF(float(screen[0]), float(screen[1])),
            self._find_item_phase,
        )

    def _set_find_blink_state(self, center: QtCore.QPointF | None, phase: float) -> None:
        self._find_item_overlay.set_center(center)
        self._find_item_overlay.set_phase(phase)
        item = self._qml_find_blink_item
        if item is None:
            return
        item.setProperty("active", center is not None)
        item.setProperty("phase", float(phase))
        if center is not None:
            item.setProperty("centerX", float(center.x()))
            item.setProperty("centerY", float(center.y()))

    def _center_camera_on_point(self, x: float, y: float) -> None:
        viewport = self._effective_viewport()
        if viewport is None:
            self.fit_view()
            viewport = self._effective_viewport()
            if viewport is None:
                return
        x_min, x_max, y_min, y_max = viewport
        half_w = max(abs(x_max - x_min) * 0.5, 0.5)
        half_h = max(abs(y_max - y_min) * 0.5, 0.5)
        self._viewport = self._aspect_normalized_viewport((x - half_w, x + half_w, y - half_h, y + half_h))

    def set_point_click_callback(self, callback) -> None:
        try:
            self.point_clicked.disconnect()
        except Exception:
            pass
        if callback is not None:
            self.point_clicked.connect(callback)

    def set_point_context_callback(self, callback) -> None:
        try:
            self.point_context_requested.disconnect()
        except Exception:
            pass
        if callback is not None:
            self.point_context_requested.connect(callback)

    def set_point_hover_callback(self, callback) -> None:
        self._point_hover_callback = callback

    def set_hover_tooltips_enabled(self, enabled: bool) -> None:
        self._hover_enabled = bool(enabled)
        if not self._hover_enabled and self._last_hover_metadata_id is not None:
            if self._interaction_mode != "selection":
                self._last_hover_metadata_id = None
                self._set_hovered_thumbnail_metadata_id(None)
            self._emit_point_hover(None)

    def set_hover_eligibility_mask(self, mask: np.ndarray | None) -> None:
        if mask is None:
            next_mask = None
        else:
            next_mask = np.asarray(mask, dtype=bool).reshape(-1).copy()
        if (
            (self._hover_eligibility_mask is None and next_mask is None)
            or (
                self._hover_eligibility_mask is not None
                and next_mask is not None
                and np.array_equal(self._hover_eligibility_mask, next_mask)
            )
        ):
            return
        self._hover_eligibility_mask = next_mask
        if self._last_hover_metadata_id is not None:
            self._last_hover_metadata_id = None
            self._set_hovered_thumbnail_metadata_id(None)
            self._emit_point_hover(None)

    def _emit_point_hover(self, payload: object) -> None:
        self.point_hovered.emit(payload)
        if callable(self._point_hover_callback):
            self._point_hover_callback(payload)

    def set_lasso_polygons(self, polygons) -> None:
        self._lasso_polygons = [np.asarray(poly, dtype=np.float32) for poly in polygons]
        self._lasso_polygon_ids = ["" for _ in self._lasso_polygons]
        self._lasso_polygon_labels = ["" for _ in self._lasso_polygons]
        self._lasso_polygon_colors = ["#3b82f6" for _ in self._lasso_polygons]
        self._lasso_polygon_area_visible = [True for _ in self._lasso_polygons]
        self._lasso_polygon_points_visible = [True for _ in self._lasso_polygons]
        self._lasso_selected_polygon_index = None
        self._lasso_pending_draw_start = False
        self._refresh()

    def set_lasso_fill_opacity(self, opacity: float) -> None:
        self._lasso_fill_opacity = float(np.clip(float(opacity), 0.0, 1.0))
        self._refresh()

    def set_polygon_render_state(self, payloads: list[dict], *, fill_opacity: float) -> None:
        """Set persistent polygon data for the canvas background and interaction layers."""
        self._lasso_fill_opacity = float(np.clip(float(fill_opacity), 0.0, 1.0))
        self.set_lasso_area_payloads(payloads)

    def set_lasso_area_payloads(self, payloads: list[dict]) -> None:
        keep_pending_draw_start = (
            self._interaction_mode == "lasso"
            and self._lasso_pending_draw_start
            and not self._lasso_current_vertices
        )
        polygons: list[np.ndarray] = []
        ids: list[str] = []
        labels: list[str] = []
        colors: list[str] = []
        area_visible: list[bool] = []
        points_visible: list[bool] = []
        for payload in payloads:
            polygon_raw = payload.get("polygon") if isinstance(payload, dict) else None
            if not isinstance(polygon_raw, (list, tuple)):
                continue
            polygon = np.asarray(polygon_raw, dtype=np.float32)
            if polygon.ndim != 2 or polygon.shape[0] < 3:
                continue
            polygons.append(polygon)
            ids.append(str(payload.get("area_id") or ""))
            labels.append(str(payload.get("label") or ""))
            colors.append(str(payload.get("color_hex") or "#3b82f6"))
            area_visible.append(bool(payload.get("area_visible", True)))
            points_visible.append(bool(payload.get("points_visible", True)))
        self._lasso_polygons = polygons
        self._lasso_polygon_ids = ids
        self._lasso_polygon_labels = labels
        self._lasso_polygon_colors = colors
        self._lasso_polygon_area_visible = area_visible
        self._lasso_polygon_points_visible = points_visible
        self._lasso_selected_polygon_index = None
        self._lasso_pending_draw_start = keep_pending_draw_start
        self._lasso_dragging_anchor = None
        self._lasso_hover_anchor = None
        self._lasso_hover_edge_insert = None
        self._lasso_hover_polygon_index = None
        self._lasso_current_vertices.clear()
        self._update_cursor(force=keep_pending_draw_start)
        self._refresh()

    def lasso_area_payloads(self) -> list[dict]:
        payloads: list[dict] = []
        for index, polygon in enumerate(self._lasso_polygons):
            payloads.append(
                {
                    "area_id": self._lasso_polygon_ids[index] if index < len(self._lasso_polygon_ids) else "",
                    "label": self._lasso_polygon_labels[index] if index < len(self._lasso_polygon_labels) else "",
                    "color_hex": self._lasso_polygon_colors[index] if index < len(self._lasso_polygon_colors) else "#3b82f6",
                    "area_visible": self._lasso_polygon_area_visible[index] if index < len(self._lasso_polygon_area_visible) else True,
                    "points_visible": self._lasso_polygon_points_visible[index] if index < len(self._lasso_polygon_points_visible) else True,
                    "polygon": np.asarray(polygon, dtype=np.float32).tolist(),
                }
            )
        return payloads

    def set_lasso_selected_polygon_index(self, index: int | None) -> None:
        if index is None:
            self._lasso_selected_polygon_index = None
            self._refresh()
            return
        value = int(index)
        if 0 <= value < len(self._lasso_polygons):
            self._lasso_selected_polygon_index = value
            self._emit_lasso_polygon_selected(value)
            self._refresh()

    def clear_lasso_polygons(self) -> None:
        self._lasso_polygons.clear()
        self._lasso_polygon_ids.clear()
        self._lasso_polygon_labels.clear()
        self._lasso_polygon_colors.clear()
        self._lasso_polygon_area_visible.clear()
        self._lasso_polygon_points_visible.clear()
        self._lasso_current_vertices.clear()
        self._lasso_selected_polygon_index = None
        self._lasso_dragging_anchor = None
        self._lasso_pending_draw_start = False
        self._lasso_hover_anchor = None
        self._lasso_hover_edge_insert = None
        self._lasso_hover_polygon_index = None
        self._refresh()

    def load_pinned_annotations(self, entries) -> None:
        self._pinned_annotations = [dict(entry) for entry in entries if isinstance(entry, dict)]
        self.pinned_annotations_changed.emit(self.pinned_annotations_payload())
        self._refresh_thumbnail_candidates_for_pins()
        self._clear_point_hover_if_pin_occluded()
        self._refresh()

    def set_pinned_annotations(self, entries) -> None:
        self.load_pinned_annotations(entries)

    def pinned_annotations_payload(self) -> list[dict]:
        return [dict(item) for item in self._pinned_annotations]

    def set_pinned_annotations_enabled(self, enabled: bool) -> None:
        self._pins_enabled = bool(enabled)
        self._refresh_thumbnail_candidates_for_pins()
        self._clear_point_hover_if_pin_occluded()
        self._sync_pin_overlay_state()
        self._refresh()

    def set_pinned_annotations_visible(self, visible: bool) -> None:
        self._pins_visible = bool(visible)
        self._refresh_thumbnail_candidates_for_pins()
        self._clear_point_hover_if_pin_occluded()
        self._sync_pin_overlay_state()
        self._refresh()

    def set_pinned_annotation_visible(self, annotation_id: str, visible: bool) -> bool:
        target = str(annotation_id).strip()
        if not target:
            return False
        for entry in self._pinned_annotations:
            if str(entry.get("annotation_id") or "").strip() != target:
                continue
            entry["visible"] = bool(visible)
            self.pinned_annotations_changed.emit(self.pinned_annotations_payload())
            self._refresh_thumbnail_candidates_for_pins()
            self._clear_point_hover_if_pin_occluded()
            self._refresh()
            return True
        return False

    def set_pinned_annotation_scope(
        self,
        annotation_id: str,
        scope_mode: str,
        *,
        projection_scope: str | None = None,
    ) -> bool:
        target = str(annotation_id).strip()
        if not target:
            return False
        normalized_mode = (
            "projection"
            if str(scope_mode).strip().lower() == "projection"
            else "project"
        )
        normalized_projection_scope = str(projection_scope or "").strip()
        if normalized_mode == "projection" and not normalized_projection_scope:
            return False
        for entry in self._pinned_annotations:
            if str(entry.get("annotation_id") or "").strip() != target:
                continue
            current_mode = (
                "projection"
                if str(entry.get("scope_mode") or "").strip().lower() == "projection"
                else "project"
            )
            current_projection_scope = str(entry.get("projection_scope") or "").strip()
            if (
                current_mode == normalized_mode
                and (
                    normalized_mode == "project"
                    or current_projection_scope == normalized_projection_scope
                )
            ):
                return False
            entry["scope_mode"] = normalized_mode
            if normalized_mode == "projection":
                entry["projection_scope"] = normalized_projection_scope
            else:
                entry.pop("projection_scope", None)
            self.pinned_annotations_changed.emit(self.pinned_annotations_payload())
            return True
        return False

    def remove_pinned_annotation(self, annotation_id: str) -> bool:
        target = str(annotation_id).strip()
        if not target:
            return False
        before = len(self._pinned_annotations)
        self._pinned_annotations = [
            entry
            for entry in self._pinned_annotations
            if str(entry.get("annotation_id") or "").strip() != target
        ]
        changed = len(self._pinned_annotations) != before
        if changed:
            self.pinned_annotations_changed.emit(self.pinned_annotations_payload())
            self._refresh_thumbnail_candidates_for_pins()
            self._refresh()
        return changed

    def set_pin_style(self, style: dict[str, object]) -> None:
        if not isinstance(style, dict):
            return
        self._pin_style.update(style)
        self._clear_point_hover_if_pin_occluded()
        self._refresh()

    def set_interaction_mode(self, mode: str) -> None:
        if mode in {"pan", "zoom", "lasso", "selection", "pin"}:
            if mode != "selection":
                if not self._hover_enabled and self._last_hover_metadata_id is not None:
                    self._last_hover_metadata_id = None
                    self._set_hovered_thumbnail_metadata_id(None)
            self._interaction_mode = mode
            if mode != "zoom":
                self._reset_zoom_drag()
            if mode != "lasso":
                self._lasso_current_vertices.clear()
                self._lasso_dragging_anchor = None
                self._lasso_pending_draw_start = False
                self._lasso_hover_anchor = None
                self._lasso_hover_edge_insert = None
                self._lasso_hover_polygon_index = None
            elif not self._lasso_current_vertices:
                self._lasso_pending_draw_start = True
            if mode == "selection" and self._cursor_canvas_position is not None:
                position = self._cursor_canvas_position
                metadata_id = (
                    None
                    if self._hit_test_pin(position) is not None
                    else self._pick_metadata_id(position.x(), position.y())
                )
                self._last_hover_metadata_id = metadata_id
                self._set_hovered_thumbnail_metadata_id(metadata_id)
            self._update_cursor(force=True)
            self._refresh()

    def refresh_cursor(self) -> None:
        """Reapply the semantic cursor after focus or window-stack changes."""
        self._update_cursor(force=True)

    def begin_temporary_pan(self) -> bool:
        if self._space_pan_active:
            return True
        if self._interaction_mode not in {"zoom", "lasso", "selection", "pin"}:
            return False
        self._space_pan_previous_mode = self._interaction_mode
        self._interaction_mode = "pan"
        self._space_pan_active = True
        self._reset_zoom_drag()
        self._update_cursor()
        return True

    def end_temporary_pan(self) -> bool:
        if not self._space_pan_active:
            return False
        restore_mode = self._space_pan_previous_mode or "pan"
        if restore_mode in {"pan", "zoom", "lasso", "selection", "pin"}:
            self._interaction_mode = restore_mode
        self._space_pan_previous_mode = None
        self._space_pan_active = False
        self._update_cursor()
        return True

    def set_lasso_selection_callback(self, callback) -> None:
        self._lasso_selection_callback = callback

    def select_metadata_ids_in_polygon(self, polygon: np.ndarray, visible_threshold: float = 0.5) -> np.ndarray:
        polygon = np.asarray(polygon, dtype=np.float32)
        if polygon.ndim != 2 or polygon.shape[0] < 3 or self._points.shape[0] == 0:
            return np.empty((0,), dtype=np.int64)
        if self._grid_index is None:
            candidates = np.arange(self._points.shape[0], dtype=np.int64)
        else:
            candidates = self._grid_index.indices_in_polygon_bounds(polygon)
        if candidates.size == 0:
            return np.empty((0,), dtype=np.int64)
        if self._visibility.shape[0] == self._points.shape[0]:
            candidates = candidates[self._visibility[candidates] >= float(visible_threshold)]
        if candidates.size == 0:
            return np.empty((0,), dtype=np.int64)
        inside = points_in_polygon(self._points[candidates], polygon)
        selected = candidates[inside]
        return self._metadata_ids[selected]

    def _candidate_indices(self, world_x: float, world_y: float, radius: float) -> np.ndarray:
        if self._grid_index is None:
            candidates = np.arange(self._points.shape[0], dtype=np.int64)
        else:
            candidates = self._grid_index.candidate_indices_for_world_point(world_x, world_y, radius)
        if candidates.size == 0:
            return candidates
        if self._visibility.shape[0] == self._points.shape[0]:
            candidates = candidates[self._visibility[candidates] > 0.0]
        if (
            self._render_mode == "hybrid"
            and self._representative_pick_mask.shape[0] == self._points.shape[0]
            and candidates.size
        ):
            allowed = self._representative_pick_mask[candidates].copy()
            forced_metadata_ids = {
                int(entry.get("metadata_id"))
                for entry in self._pinned_annotations
                if entry.get("metadata_id") is not None
                and bool(entry.get("visible", True))
                and bool(entry.get("available_in_projection", True))
            }
            if self._find_item_metadata_id is not None:
                forced_metadata_ids.add(int(self._find_item_metadata_id))
            for metadata_id in forced_metadata_ids:
                index = self._metadata_index_by_id.get(metadata_id)
                if index is not None:
                    allowed |= candidates == int(index)
            candidates = candidates[allowed]
        return candidates

    def _world_radius_for_pixels(self, pixels: float) -> float:
        if self._points.shape[0] == 0:
            return 0.0
        width = float(max(1, self.width()))
        height = float(max(1, self.height()))
        if self._viewport is None:
            x_min = float(np.min(self._points[:, 0]))
            x_max = float(np.max(self._points[:, 0]))
            y_min = float(np.min(self._points[:, 1]))
            y_max = float(np.max(self._points[:, 1]))
        else:
            x_min, x_max, y_min, y_max = self._viewport
        dx = abs(x_max - x_min) / width
        dy = abs(y_max - y_min) / height
        return float(max(dx, dy) * pixels)

    def _pan_from_drag(self, current: QtCore.QPointF) -> None:
        viewport = self._drag_start_viewport
        start = self._drag_start_pos
        if viewport is None or start is None:
            return
        width = float(max(1, self.width()))
        height = float(max(1, self.height()))
        dx = float(current.x() - start.x())
        dy = float(current.y() - start.y())
        x_min, x_max, y_min, y_max = viewport
        span_x = x_max - x_min
        span_y = y_max - y_min
        shift_x = -(dx / width) * span_x
        shift_y = (dy / height) * span_y
        self._viewport = (x_min + shift_x, x_max + shift_x, y_min + shift_y, y_max + shift_y)
        if self._camera_interaction_active:
            self._push_interaction_viewport()
        else:
            self._refresh()

    def _zoom_at(self, x: float, y: float, *, zoom_in: bool) -> None:
        world = self._screen_to_world(x, y)
        viewport = self._effective_viewport()
        if world is None or viewport is None:
            return
        factor = self._zoom_click_in_factor if zoom_in else self._zoom_click_out_factor
        self._zoom_with_factor(x, y, factor=factor)

    def _zoom_with_factor(self, x: float, y: float, *, factor: float, refresh: bool = True) -> None:
        world = self._screen_to_world(x, y)
        viewport = self._effective_viewport()
        if world is None or viewport is None:
            return
        factor = max(0.05, float(factor))
        x_min, x_max, y_min, y_max = viewport
        wx = float(world[0])
        wy = float(world[1])
        # Anchor zoom at the cursor position: keep the world point under the cursor fixed.
        new_x_min = wx - (wx - x_min) * factor
        new_x_max = wx + (x_max - wx) * factor
        new_y_min = wy - (wy - y_min) * factor
        new_y_max = wy + (y_max - wy) * factor
        self._viewport = self._aspect_normalized_viewport((new_x_min, new_x_max, new_y_min, new_y_max))
        if refresh:
            self._refresh()

    def _effective_viewport(self) -> tuple[float, float, float, float] | None:
        if self._viewport is not None:
            return self._viewport
        if self._points.size == 0:
            return None
        x_min = float(np.min(self._points[:, 0]))
        x_max = float(np.max(self._points[:, 0]))
        y_min = float(np.min(self._points[:, 1]))
        y_max = float(np.max(self._points[:, 1]))
        if abs(x_max - x_min) < 1e-9:
            x_max = x_min + 1.0
        if abs(y_max - y_min) < 1e-9:
            y_max = y_min + 1.0
        return self._aspect_normalized_viewport((x_min, x_max, y_min, y_max))

    def _zoom_to_rect(self, start_pos: QtCore.QPointF, end_pos: QtCore.QPointF) -> None:
        world_a = self._screen_to_world(float(start_pos.x()), float(start_pos.y()))
        world_b = self._screen_to_world(float(end_pos.x()), float(end_pos.y()))
        if world_a is None or world_b is None:
            return
        x0, x1 = sorted((float(world_a[0]), float(world_b[0])))
        y0, y1 = sorted((float(world_a[1]), float(world_b[1])))
        if abs(x1 - x0) <= 1e-9 or abs(y1 - y0) <= 1e-9:
            return
        self._viewport = self._aspect_normalized_viewport((x0, x1, y0, y1))
        self._refresh()

    def _aspect_normalized_viewport(
        self,
        viewport: tuple[float, float, float, float],
    ) -> tuple[float, float, float, float]:
        return normalize_world_viewport(
            viewport,
            self._window_aspect_ratio(),
            expand=True,
        )

    def _window_aspect_ratio(self) -> float:
        return float(max(1, self.width())) / float(max(1, self.height()))

    def _emit_camera_changed(self) -> None:
        self.camera_changed.emit(self.camera_state())

    def _refresh_with_camera_notification(self) -> None:
        expected_viewport = self._effective_viewport()
        previous_payload = self._last_viewport_payload
        self._refresh()
        if previous_payload == expected_viewport or self._last_viewport_payload != expected_viewport:
            self._emit_camera_changed()

    def _reset_zoom_drag(self) -> None:
        self._zoom_press_pos = None
        self._zoom_drag_current_pos = None
        self._zoom_drag_active = False
        self._set_zoom_drag_rect(None)

    def _update_zoom_drag_overlay(self) -> None:
        if (
            self._interaction_mode != "zoom"
            or not self._zoom_drag_active
            or self._zoom_press_pos is None
            or self._zoom_drag_current_pos is None
            or self._zoom_shift_held
            or self._zoom_out_mouse_held
        ):
            self._set_zoom_drag_rect(None)
            return
        rect = QtCore.QRectF(self._zoom_press_pos, self._zoom_drag_current_pos).normalized()
        self._set_zoom_drag_rect(rect.intersected(QtCore.QRectF(self.rect())))

    def _set_zoom_drag_rect(self, rect: QtCore.QRectF | None) -> None:
        self._zoom_drag_overlay.set_selection_rect(rect)
        item = self._qml_zoom_drag_item
        if item is None:
            return
        if rect is None or rect.isEmpty():
            item.setProperty("rectWidth", 0.0)
            item.setProperty("rectHeight", 0.0)
            return
        item.setProperty("rectX", float(rect.x()))
        item.setProperty("rectY", float(rect.y()))
        item.setProperty("rectWidth", float(rect.width()))
        item.setProperty("rectHeight", float(rect.height()))

    @staticmethod
    def _distance_px(a: QtCore.QPointF, b: QtCore.QPointF) -> float:
        dx = float(a.x() - b.x())
        dy = float(a.y() - b.y())
        return float(np.sqrt(dx * dx + dy * dy))

    def _update_cursor(self, *, force: bool = False) -> None:
        self._sync_cursor_targets()
        self._cursor_controller.set_cursor(self._cursor_name(), force=force)
        self._update_lasso_status_hint()

    def _update_lasso_hover_targets(self, position: QtCore.QPointF | None) -> None:
        self._lasso_hover_anchor = None
        self._lasso_hover_edge_insert = None
        self._lasso_hover_polygon_index = None
        if self._interaction_mode != "lasso" or position is None:
            return
        self._lasso_hover_anchor = self._anchor_hit_at_canvas(position)
        if self._lasso_hover_anchor is not None:
            return
        if self._lasso_current_vertices:
            return
        if self._lasso_selected_polygon_index is not None:
            self._lasso_hover_edge_insert = self._lasso_edge_insert_hit_at_canvas(
                position,
                self._lasso_selected_polygon_index,
            )
            if self._lasso_hover_edge_insert is not None:
                return
        world = self._screen_to_world(position.x(), position.y())
        if world is not None:
            self._lasso_hover_polygon_index = self._polygon_index_at_world_point(
                np.asarray(world, dtype=np.float32)
            )

    def _update_lasso_status_hint(self) -> None:
        if self._interaction_mode != "lasso":
            self._set_lasso_status_hint("")
            return
        if self._lasso_current_vertices:
            self._set_lasso_status_hint("Click to add points. Click first point or press Enter to close. Esc cancels.")
            return
        if self._lasso_selected_polygon_index is not None:
            self._set_lasso_status_hint("Drag anchors to reshape. Click edges to add anchors. Shift/Alt/Ctrl-click anchors to remove.")
            return
        self._set_lasso_status_hint("Click empty canvas to draw. Click an area to edit. Shift-click to draw over an area.")

    def _set_lasso_status_hint(self, message: str) -> None:
        text = str(message or "")
        if text == self._last_lasso_status_hint:
            return
        self._last_lasso_status_hint = text
        if text:
            self.lasso_status_hint_changed.emit(text)

    def _lasso_hover_state_key(self) -> tuple[object, ...]:
        edge = self._lasso_hover_edge_insert
        return (
            self._lasso_hover_anchor,
            None if edge is None else (int(edge[0]), int(edge[1])),
            self._lasso_hover_polygon_index,
            self._option_modifier_held(),
        )

    def _lasso_anchor_modifier_held(self) -> bool:
        return self._option_modifier_held()

    def _option_modifier_held(self) -> bool:
        return bool(self._lasso_alt_held or self._lasso_shift_held or self._lasso_ctrl_held)

    def _cursor_name(self) -> str:
        if self._interaction_mode == "pan":
            return PAN_CLOSED if self._drag_start_pos is not None else PAN_OPEN
        if self._interaction_mode == "zoom":
            return ZOOM_OUT if self._zoom_shift_held or self._zoom_out_mouse_held else ZOOM_IN
        if self._interaction_mode == "lasso":
            position = self._cursor_canvas_position
            self._update_lasso_hover_targets(position)
            if self._lasso_shift_held and not self._lasso_current_vertices:
                return LASSO_ADD_POINT
            if self._lasso_hover_anchor is not None:
                _polygon_index, vertex_index, is_current = self._lasso_hover_anchor
                if is_current and vertex_index == 0 and len(self._lasso_current_vertices) >= 3:
                    return LASSO_CLOSE_POINT
                if not is_current:
                    return LASSO_REMOVE_POINT if self._lasso_anchor_modifier_held() else LASSO_MOVE_ANCHOR
            if self._lasso_current_vertices:
                return LASSO_ADD_POINT
            if self._lasso_hover_edge_insert is not None:
                return LASSO_ADD_ANCHOR
            if self._lasso_hover_polygon_index is not None:
                return LASSO_SELECT_AREA
            return LASSO_ADD_POINT
        if self._interaction_mode == "selection":
            return SELECTION_MODE
        if self._interaction_mode == "pin":
            position = self._cursor_canvas_position
            if position is not None:
                hit_pin = self._hit_test_pin(position)
                hit_pinned_point = self._pinned_annotation_id_at_point(position)
                if self._option_modifier_held() and (hit_pin is not None or hit_pinned_point is not None):
                    return PIN_REMOVE
                if hit_pin is not None:
                    return PIN_MOVE
                if self._pick_metadata_id(position.x(), position.y()) is not None:
                    return PIN_ADD
            return PIN_MODE
        return PAN_OPEN

    def _update_cursor_from_pointer_event(self, event: QtGui.QSinglePointEvent) -> None:
        self._cursor_canvas_position = QtCore.QPointF(event.position())
        self._update_keyboard_modifier_state(event.modifiers())
        self._zoom_out_mouse_held = bool(
            event.buttons() & QtCore.Qt.MouseButton.RightButton
        )
        self._update_cursor()

    def _update_keyboard_modifier_state(self, modifiers: QtCore.Qt.KeyboardModifier) -> None:
        self._zoom_shift_held = bool(modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier)
        self._lasso_shift_held = bool(modifiers & QtCore.Qt.KeyboardModifier.ShiftModifier)
        self._lasso_alt_held = bool(modifiers & QtCore.Qt.KeyboardModifier.AltModifier)
        self._lasso_ctrl_held = bool(modifiers & QtCore.Qt.KeyboardModifier.ControlModifier)

    def _sync_cursor_targets(self) -> None:
        targets: list[QtWidgets.QWidget | QtGui.QWindow] = []
        parent = self.parentWidget()
        if parent is not None and parent.objectName() == "panel_canvas_host":
            targets.append(parent)
        targets.append(self)
        if self._quick is not None:
            targets.append(self._quick)
            quick_window_getter = getattr(self._quick, "quickWindow", None)
            quick_window = quick_window_getter() if callable(quick_window_getter) else None
            if isinstance(quick_window, QtGui.QWindow):
                targets.append(quick_window)
        if self._quick_container is not None:
            targets.append(self._quick_container)
        if self._quick_view is not None:
            targets.append(self._quick_view)
        self._cursor_controller.set_targets(targets)

    def _find_pin(self, annotation_id: str) -> dict | None:
        target = str(annotation_id).strip()
        for entry in self._pinned_annotations:
            if str(entry.get("annotation_id") or "").strip() == target:
                return entry
        return None

    def _thumbnail_ids_without_visible_pins(self, metadata_ids) -> list[int]:
        pinned_ids: set[int] = set()
        if self._pins_enabled and self._pins_visible:
            for entry in self._pinned_annotations:
                if not bool(entry.get("visible", True)) or not bool(
                    entry.get("available_in_projection", True)
                ):
                    continue
                try:
                    pinned_ids.add(int(entry.get("metadata_id")))
                except (TypeError, ValueError):
                    continue
        ordered: list[int] = []
        found_id = self._find_item_metadata_id
        for value in metadata_ids:
            metadata_id = int(value)
            if metadata_id in pinned_ids and metadata_id != found_id:
                continue
            ordered.append(metadata_id)
        if found_id is not None and found_id in ordered:
            ordered = [value for value in ordered if value != found_id]
            ordered.append(int(found_id))
        return ordered

    def _refresh_thumbnail_candidates_for_pins(self) -> None:
        if self._render_backend is None:
            return
        self._render_backend.set_thumbnail_candidates(
            self._thumbnail_ids_without_visible_pins(
                self._requested_thumbnail_candidate_metadata_ids
            ),
            target_px=self._requested_thumbnail_candidate_target_px,
        )

    def _clear_point_hover_if_pin_occluded(self) -> None:
        position = self._cursor_canvas_position
        if (
            self._last_hover_metadata_id is None
            or position is None
            or self._hit_test_pin(position) is None
        ):
            return
        self._last_hover_metadata_id = None
        self._set_hovered_thumbnail_metadata_id(None)
        if self._hover_enabled:
            self._emit_point_hover(None)

    def _set_pin_offset(self, annotation_id: str, dx: float, dy: float) -> None:
        entry = self._find_pin(annotation_id)
        if entry is None:
            return
        entry["screen_offset"] = [float(dx), float(dy)]
        anchor = self._pin_box_anchor_for_offset(entry, float(dx), float(dy))
        if anchor is not None:
            entry["box_anchor"] = [float(anchor[0]), float(anchor[1])]
        self.pinned_annotations_changed.emit(self.pinned_annotations_payload())

    def _update_pin_overlay(self) -> None:
        use_python_overlay = not self._has_native_pin_overlay()
        visible = bool(
            use_python_overlay
            and self._pins_enabled
            and self._pins_visible
            and any(
                bool(entry.get("visible", True))
                and bool(entry.get("available_in_projection", True))
                for entry in self._pinned_annotations
            )
        )
        self._pin_overlay.setVisible(visible)
        if visible:
            self._pin_overlay.raise_()
        self._zoom_drag_overlay.raise_()
        self._find_item_overlay.raise_()
        self._pin_overlay.update()

    def _has_native_pin_overlay(self) -> bool:
        if self._native_pin_overlay_item is None:
            return False
        return callable(getattr(self._native_pin_overlay_item, "setPinData", None))

    def _draw_pins_overlay(self, painter: QtGui.QPainter) -> None:
        if not self._pins_enabled or not self._pins_visible:
            return
        for entry in self._pinned_annotations:
            if not bool(entry.get("visible", True)) or not bool(
                entry.get("available_in_projection", True)
            ):
                continue
            self._draw_pin_connector(painter, entry)
        for entry in self._pinned_annotations:
            if not bool(entry.get("visible", True)) or not bool(
                entry.get("available_in_projection", True)
            ):
                continue
            self._draw_pin_card(painter, entry)

    def _draw_pin_connector(self, painter: QtGui.QPainter, entry: dict) -> None:
        if not bool(self._pin_style.get("connector_visible", True)):
            return
        point = entry.get("point")
        if not (isinstance(point, (list, tuple)) and len(point) >= 2):
            return
        anchor = self._world_to_screen(np.asarray([float(point[0]), float(point[1])], dtype=np.float32))
        if anchor is None:
            return
        box_rect = self._pin_box_rect_for_entry(entry)
        if box_rect is None:
            return
        target = box_rect if bool(self._pin_style.get("box_visible", True)) else self._pin_thumbnail_rect_for_entry(entry, box_rect)
        mode = str(self._pin_style.get("connector_target", "center")).strip().lower()
        mode = {"box": "center", "thumbnail": "edge"}.get(mode, mode)
        color = QtGui.QColor(str(self._pin_style.get("connector_color", "#333333")))
        if not color.isValid():
            color = QtGui.QColor("#333333")
        try:
            width = max(0.5, float(self._pin_style.get("connector_width", 1.5)))
        except (TypeError, ValueError):
            width = 1.5
        painter.setPen(QtGui.QPen(color, width))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        source = QtCore.QPointF(float(anchor[0]), float(anchor[1]))
        if mode == "center":
            # Clip the line against the rendered card itself.  This is more
            # reliable than a rectangular ray intersection around rounded pins.
            painter.save()
            clip = QtGui.QPainterPath()
            clip.addRect(QtCore.QRectF(painter.viewport()))
            if bool(self._pin_style.get("box_visible", True)):
                clip.addRoundedRect(target, 4.0, 4.0)
            else:
                clip.addRect(target)
            clip.setFillRule(QtCore.Qt.FillRule.OddEvenFill)
            painter.setClipPath(clip, QtCore.Qt.ClipOperation.ReplaceClip)
            painter.drawLine(source, target.center())
            painter.restore()
            return
        elif mode == "corner":
            corners = (target.topLeft(), target.topRight(), target.bottomLeft(), target.bottomRight())
            destination = min(corners, key=lambda point: (point.x() - source.x()) ** 2 + (point.y() - source.y()) ** 2)
        else:
            edge_midpoints = (
                QtCore.QPointF(target.left(), target.center().y()),
                QtCore.QPointF(target.right(), target.center().y()),
                QtCore.QPointF(target.center().x(), target.top()),
                QtCore.QPointF(target.center().x(), target.bottom()),
            )
            destination = min(
                edge_midpoints,
                key=lambda point: (point.x() - source.x()) ** 2 + (point.y() - source.y()) ** 2,
            )
        painter.drawLine(source, destination)

    def _draw_pin_card(self, painter: QtGui.QPainter, entry: dict) -> None:
        rect = self._pin_box_rect_for_entry(entry)
        if rect is None:
            return
        if bool(self._pin_style.get("box_visible", True)):
            box = QtGui.QColor(str(self._pin_style.get("box_color", "#f0f0f0")))
            if not box.isValid():
                box = QtGui.QColor("#f0f0f0")
            try:
                box.setAlphaF(max(0.0, min(1.0, float(self._pin_style.get("box_opacity", 1.0)))))
            except (TypeError, ValueError):
                box.setAlphaF(1.0)
            border = QtGui.QColor(str(self._pin_style.get("border_color", "#333333")))
            if not border.isValid():
                border = QtGui.QColor("#333333")
            border.setAlphaF(box.alphaF())
            try:
                border_width = max(0.0, float(self._pin_style.get("border_width", 1.0)))
            except (TypeError, ValueError):
                border_width = 1.0
            painter.setPen(QtCore.Qt.PenStyle.NoPen if border_width <= 0.0 else QtGui.QPen(border, border_width))
            painter.setBrush(QtGui.QBrush(box))
            painter.drawRoundedRect(rect, 4.0, 4.0)
        thumbnail_rect = self._pin_thumbnail_rect_for_entry(entry, rect)
        text_left = rect.left() + self._pin_box_padding_px
        text_top = rect.top() + self._pin_box_padding_px
        if bool(self._pin_style.get("thumbnail_visible", False)):
            self._draw_pin_thumbnail(painter, entry, thumbnail_rect)
            position = str(self._pin_style.get("thumbnail_position", "left")).strip().lower()
            if position == "right":
                text_left = rect.left() + self._pin_box_padding_px
            elif position == "top":
                text_top = thumbnail_rect.bottom() + 6.0
            elif position == "bottom":
                text_top = rect.top() + self._pin_box_padding_px
            else:
                text_left = thumbnail_rect.right() + 6.0
        if bool(self._pin_style.get("text_visible", True)):
            self._draw_pin_text(painter, entry, text_left, text_top)

    def _pin_thumbnail_rect_for_entry(self, entry: dict, rect: QtCore.QRectF) -> QtCore.QRectF:
        if not bool(self._pin_style.get("thumbnail_visible", False)):
            return QtCore.QRectF(rect)
        try:
            max_size = max(32, int(self._pin_style.get("thumbnail_size", 96)))
        except (TypeError, ValueError):
            max_size = 96
        width, height = self._pin_thumbnail_display_size(entry, max_size)
        padding = self._pin_box_padding_px
        position = str(self._pin_style.get("thumbnail_position", "left")).strip().lower()
        if position == "right":
            return QtCore.QRectF(rect.right() - padding - width, rect.top() + padding, width, height)
        if position == "top":
            return QtCore.QRectF(rect.left() + padding + max(0.0, (rect.width() - (padding * 2.0) - width) * 0.5), rect.top() + padding, width, height)
        if position == "bottom":
            return QtCore.QRectF(rect.left() + padding + max(0.0, (rect.width() - (padding * 2.0) - width) * 0.5), rect.bottom() - padding - height, width, height)
        return QtCore.QRectF(rect.left() + padding, rect.top() + padding, width, height)

    @staticmethod
    def _pin_thumbnail_image(entry: dict) -> QtGui.QImage | None:
        try:
            width = int(entry.get("thumbnail_width", 0))
            height = int(entry.get("thumbnail_height", 0))
        except (TypeError, ValueError):
            return None
        rgba = entry.get("thumbnail_rgba")
        if width <= 0 or height <= 0 or not rgba:
            return None
        data = bytes(rgba)
        if len(data) != width * height * 4:
            return None
        return QtGui.QImage(data, width, height, width * 4, QtGui.QImage.Format.Format_RGBA8888).copy()

    def _draw_pin_thumbnail(self, painter: QtGui.QPainter, entry: dict, rect: QtCore.QRectF) -> None:
        image = self._pin_thumbnail_image(entry)
        if image is None or image.isNull():
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QBrush(QtGui.QColor("#e5e7eb")))
            painter.drawRect(rect)
            return
        painter.drawImage(rect, image, QtCore.QRectF(0.0, 0.0, float(image.width()), float(image.height())))

    def _draw_pin_text(self, painter: QtGui.QPainter, entry: dict, x: float, y: float) -> None:
        lines = entry.get("lines", [])
        text_lines = [str(line) for line in (lines if isinstance(lines, list) else [])]
        label_font, value_font = self._pin_text_fonts()
        label_metrics = QtGui.QFontMetrics(label_font)
        value_metrics = QtGui.QFontMetrics(value_font)
        line_height = max(label_metrics.height(), value_metrics.height())
        show_label = bool(self._pin_style.get("show_label", True))
        show_value = bool(self._pin_style.get("show_value", True))
        color = QtGui.QColor(str(self._pin_style.get("text_color", "#222222")))
        if not color.isValid():
            color = QtGui.QColor("#222222")
        painter.setPen(QtGui.QPen(color, 1.0))
        baseline = float(y) + float(line_height)
        for line in text_lines:
            label, separator, value = line.partition("\t")
            cursor_x = float(x)
            if separator:
                if show_label:
                    painter.setFont(label_font)
                    painter.drawText(QtCore.QPointF(cursor_x, baseline), label)
                    cursor_x += label_metrics.horizontalAdvance(label)
                    if show_value and value:
                        cursor_x += label_metrics.horizontalAdvance(" ")
                if show_value:
                    painter.setFont(value_font)
                    painter.drawText(QtCore.QPointF(cursor_x, baseline), value)
            elif show_value:
                painter.setFont(value_font)
                painter.drawText(QtCore.QPointF(cursor_x, baseline), label)
            elif show_label:
                painter.setFont(label_font)
                painter.drawText(QtCore.QPointF(cursor_x, baseline), label)
            baseline += float(line_height)

    def _pin_box_anchor_for_offset(self, entry: dict, dx: float, dy: float) -> tuple[float, float] | None:
        point = entry.get("point")
        if not (isinstance(point, (list, tuple)) and len(point) >= 2):
            return None
        anchor = self._world_to_screen(np.asarray([float(point[0]), float(point[1])], dtype=np.float32))
        if anchor is None:
            return None
        return self._screen_to_world(float(anchor[0]) + float(dx), float(anchor[1]) + float(dy))

    def _pin_box_rect_for_entry(self, entry: dict) -> QtCore.QRectF | None:
        if not bool(entry.get("visible", True)) or not bool(
            entry.get("available_in_projection", True)
        ):
            return None
        point = entry.get("point")
        box_anchor = entry.get("box_anchor")
        offset = entry.get("screen_offset", [48.0, -36.0])
        lines = entry.get("lines", [])
        if not (isinstance(point, (list, tuple)) and len(point) >= 2):
            return None
        anchor = self._world_to_screen(np.asarray([float(point[0]), float(point[1])], dtype=np.float32))
        if anchor is None:
            return None
        box_screen = None
        if isinstance(box_anchor, (list, tuple)) and len(box_anchor) >= 2:
            try:
                box_screen = self._world_to_screen(np.asarray([float(box_anchor[0]), float(box_anchor[1])], dtype=np.float32))
            except (TypeError, ValueError):
                box_screen = None
        try:
            dx = float(offset[0]) if isinstance(offset, (list, tuple)) and len(offset) >= 2 else 48.0
            dy = float(offset[1]) if isinstance(offset, (list, tuple)) and len(offset) >= 2 else -36.0
        except (TypeError, ValueError):
            dx, dy = 48.0, -36.0
        text_lines = [str(line) for line in (lines if isinstance(lines, list) else [])]
        text_width, text_height = self._pin_text_block_size(text_lines)
        draw_thumb = bool(self._pin_style.get("thumbnail_visible", False))
        thumb_size = max(32, int(self._pin_style.get("thumbnail_size", 96))) if draw_thumb else 0
        thumb_width, thumb_height = self._pin_thumbnail_display_size(entry, thumb_size) if draw_thumb else (0.0, 0.0)
        thumb_position = str(self._pin_style.get("thumbnail_position", "left")).strip().lower()
        horizontal = thumb_position in {"left", "right"}
        content_gap = 6.0
        if draw_thumb:
            if horizontal:
                content_width = float(thumb_width + (content_gap if text_width > 0 else 0.0) + text_width)
                content_height = float(max(thumb_height, text_height))
            else:
                content_width = float(max(thumb_width, text_width))
                content_height = float(thumb_height + (content_gap if text_height > 0 else 0.0) + text_height)
        else:
            content_width = float(text_width)
            content_height = float(text_height)
        width = max(1.0, float(content_width + self._pin_box_padding_px * 2.0))
        height = max(1.0, float(content_height + self._pin_box_padding_px * 2.0))
        if box_screen is not None:
            return QtCore.QRectF(box_screen[0], box_screen[1], width, height)
        return QtCore.QRectF(anchor[0] + dx, anchor[1] + dy, width, height)

    def _pin_text_block_size(self, lines: list[str]) -> tuple[int, int]:
        label_font, value_font = self._pin_text_fonts()
        label_metrics = QtGui.QFontMetrics(label_font)
        value_metrics = QtGui.QFontMetrics(value_font)
        show_label = bool(self._pin_style.get("show_label", True))
        show_value = bool(self._pin_style.get("show_value", True))
        width = 0
        for line in lines:
            label, separator, value = str(line).partition("\t")
            line_width = 0
            if separator:
                if show_label:
                    line_width += label_metrics.horizontalAdvance(label)
                    if show_value and value:
                        line_width += label_metrics.horizontalAdvance(" ")
                if show_value:
                    line_width += value_metrics.horizontalAdvance(value)
            elif show_value:
                line_width += value_metrics.horizontalAdvance(label)
            elif show_label:
                line_width += label_metrics.horizontalAdvance(label)
            width = max(width, line_width)
        line_height = max(label_metrics.height(), value_metrics.height())
        return width, int(line_height * len(lines))

    def _pin_text_fonts(self) -> tuple[QtGui.QFont, QtGui.QFont]:
        base = self.font()
        family = str(self._pin_style.get("font_family") or "").strip()
        if family:
            base.setFamily(family)
        try:
            base.setPointSize(int(self._pin_style.get("font_size", 11)))
        except Exception:
            base.setPointSize(11)
        label = QtGui.QFont(base)
        label.setBold(bool(self._pin_style.get("text_label_bold", False)))
        label.setItalic(bool(self._pin_style.get("text_label_italic", False)))
        label.setUnderline(bool(self._pin_style.get("text_label_underline", False)))
        value = QtGui.QFont(base)
        value.setBold(bool(self._pin_style.get("text_value_bold", False)))
        value.setItalic(bool(self._pin_style.get("text_value_italic", False)))
        value.setUnderline(bool(self._pin_style.get("text_value_underline", False)))
        return label, value

    @staticmethod
    def _pin_thumbnail_display_size(entry: dict, max_size: int) -> tuple[float, float]:
        fallback = float(max(1, int(max_size)))
        try:
            source_width = float(entry.get("thumbnail_width", 0))
            source_height = float(entry.get("thumbnail_height", 0))
        except (TypeError, ValueError):
            return fallback, fallback
        if source_width <= 0.0 or source_height <= 0.0:
            return fallback, fallback
        scale = min(fallback / source_width, fallback / source_height)
        return max(1.0, source_width * scale), max(1.0, source_height * scale)

    def _hit_test_pin(self, pos: QtCore.QPointF) -> str | None:
        if not self._pins_enabled or not self._pins_visible:
            return None
        for entry in reversed(self._pinned_annotations):
            rect = self._pin_box_rect_for_entry(entry)
            if rect is None:
                continue
            if rect.contains(pos):
                annotation_id = str(entry.get("annotation_id") or "").strip()
                if annotation_id:
                    return annotation_id
        return None

    def _pinned_annotation_id_at_point(self, pos: QtCore.QPointF) -> str | None:
        if not self._pins_enabled or not self._pins_visible:
            return None
        metadata_id = self._pick_metadata_id(pos.x(), pos.y())
        if metadata_id is None:
            return None
        target = int(metadata_id)
        for entry in reversed(self._pinned_annotations):
            if not bool(entry.get("visible", True)):
                continue
            try:
                entry_metadata_id = int(entry.get("metadata_id"))
            except (TypeError, ValueError):
                continue
            if entry_metadata_id != target:
                continue
            annotation_id = str(entry.get("annotation_id") or "").strip()
            if annotation_id:
                return annotation_id
        return None

    def create_pinned_annotation(
        self,
        metadata_id: int,
        *,
        annotation_id: str | None = None,
        scope_mode: str = "project",
        projection_scope: str | None = None,
        replace_existing: bool = False,
    ) -> bool:
        target = int(metadata_id)
        existing = [
            entry
            for entry in self._pinned_annotations
            if int(entry.get("metadata_id", -1)) == target
        ]
        if existing and not replace_existing:
            return False
        if self._metadata_ids.size == 0 or self._points.size == 0:
            return False
        matches = np.where(self._metadata_ids == target)[0]
        if matches.size == 0:
            return False
        index = int(matches[0])
        if index < 0 or index >= self._points.shape[0]:
            return False
        point = self._points[index]
        normalized_scope_mode = (
            "projection" if str(scope_mode).strip().lower() == "projection" else "project"
        )
        entry = {
            "annotation_id": str(annotation_id or f"Pin_{target}"),
            "metadata_id": target,
            "scope_mode": normalized_scope_mode,
            "point": [float(point[0]), float(point[1])],
            "screen_offset": [48.0, -36.0],
            "visible": True,
            "created_at": dt.datetime.utcnow().isoformat(),
            "lines": [],
        }
        if normalized_scope_mode == "projection":
            entry["projection_scope"] = str(projection_scope or "").strip()
        anchor = self._pin_box_anchor_for_offset(entry, 48.0, -36.0)
        if anchor is not None:
            entry["box_anchor"] = [float(anchor[0]), float(anchor[1])]
        if existing:
            self._pinned_annotations = [
                item
                for item in self._pinned_annotations
                if int(item.get("metadata_id", -1)) != target
            ]
        self._pinned_annotations.append(entry)
        self.pinned_annotations_changed.emit(self.pinned_annotations_payload())
        self._refresh_thumbnail_candidates_for_pins()
        self._refresh()
        return True

    def _toggle_or_create_pin(self, metadata_id: int) -> None:
        target = int(metadata_id)
        for entry in list(self._pinned_annotations):
            if int(entry.get("metadata_id", -1)) != target:
                continue
            annotation_id = str(entry.get("annotation_id") or "").strip()
            if annotation_id:
                self.remove_pinned_annotation(annotation_id)
                return
        self.create_pinned_annotation(target)

    def _handle_key_press(self, key_event: QtGui.QKeyEvent) -> bool:
        if int(key_event.key()) == int(QtCore.Qt.Key.Key_Shift):
            self._zoom_shift_held = True
            self._lasso_shift_held = True
            self._update_cursor()
            self._update_zoom_drag_overlay()
            if self._interaction_mode == "lasso":
                self._refresh()
            return False
        if int(key_event.key()) == int(QtCore.Qt.Key.Key_Alt):
            self._lasso_alt_held = True
            self._update_cursor()
            if self._interaction_mode == "lasso":
                self._refresh()
            return False
        if int(key_event.key()) in {int(QtCore.Qt.Key.Key_Control), int(QtCore.Qt.Key.Key_Meta)}:
            self._lasso_ctrl_held = True
            self._update_cursor()
            if self._interaction_mode == "lasso":
                self._refresh()
            return False
        if (
            self._interaction_mode == "lasso"
            and int(key_event.key()) in {int(QtCore.Qt.Key.Key_Return), int(QtCore.Qt.Key.Key_Enter)}
            and self._finish_lasso_polygon()
        ):
            return True
        if int(key_event.key()) == int(QtCore.Qt.Key.Key_Escape):
            if self._interaction_mode == "lasso" and self._lasso_current_vertices:
                self._lasso_current_vertices.clear()
                self._lasso_dragging_anchor = None
                self._lasso_pending_draw_start = False
                self._refresh()
                return True
            return False
        if (
            self._interaction_mode == "lasso"
            and int(key_event.key()) in {int(QtCore.Qt.Key.Key_Delete), int(QtCore.Qt.Key.Key_Backspace)}
            and self._lasso_selected_polygon_index is not None
        ):
            self.lasso_polygon_delete_requested.emit(
                self._lasso_polygon_payload(self._lasso_selected_polygon_index)
            )
            return True
        if int(key_event.key()) != int(QtCore.Qt.Key.Key_Space):
            return False
        return self.begin_temporary_pan()

    def _handle_key_release(self, key_event: QtGui.QKeyEvent) -> bool:
        if int(key_event.key()) == int(QtCore.Qt.Key.Key_Shift):
            self._zoom_shift_held = False
            self._lasso_shift_held = False
            self._update_cursor()
            self._update_zoom_drag_overlay()
            if self._interaction_mode == "lasso":
                self._refresh()
            return False
        if int(key_event.key()) == int(QtCore.Qt.Key.Key_Alt):
            self._lasso_alt_held = False
            self._update_cursor()
            if self._interaction_mode == "lasso":
                self._refresh()
            return False
        if int(key_event.key()) in {int(QtCore.Qt.Key.Key_Control), int(QtCore.Qt.Key.Key_Meta)}:
            self._lasso_ctrl_held = False
            self._update_cursor()
            if self._interaction_mode == "lasso":
                self._refresh()
            return False
        if int(key_event.key()) != int(QtCore.Qt.Key.Key_Space):
            return False
        return self.end_temporary_pan()

    def _handle_lasso_press(self, mouse_event: QtGui.QMouseEvent) -> bool:
        world = self._screen_to_world(mouse_event.position().x(), mouse_event.position().y())
        world_point = np.array(world, dtype=np.float32) if world is not None else None
        if mouse_event.button() == QtCore.Qt.MouseButton.RightButton:
            if self._lasso_current_vertices:
                self._lasso_current_vertices.clear()
                self._lasso_dragging_anchor = None
                self._refresh()
                return True
            anchor_hit = self._anchor_hit_at_canvas(mouse_event.position())
            hit_polygon = anchor_hit[0] if anchor_hit is not None and not anchor_hit[2] else None
            if hit_polygon is None:
                hit_polygon = self._polygon_index_at_world_point(world_point) if world_point is not None else None
            if hit_polygon is not None:
                self._lasso_selected_polygon_index = hit_polygon
                payload = self._lasso_polygon_payload(hit_polygon)
                if anchor_hit is not None and not anchor_hit[2]:
                    payload["anchor_index"] = int(anchor_hit[1])
                self.lasso_polygon_context.emit(payload)
                self._emit_lasso_polygon_selected(hit_polygon)
                self._emit_lasso_selection_for_polygon(hit_polygon)
                self._refresh()
                return True
            self._lasso_current_vertices.clear()
            self._refresh()
            return True
        if mouse_event.button() != QtCore.Qt.MouseButton.LeftButton:
            return False
        self._update_lasso_hover_targets(mouse_event.position())
        hit = self._anchor_hit_at_canvas(mouse_event.position())
        if hit is not None:
            poly_index, vertex_index, is_current = hit
            if is_current and vertex_index == 0 and len(self._lasso_current_vertices) >= 3:
                return True
            if not is_current and self._lasso_anchor_modifier_held():
                self.remove_lasso_anchor(poly_index, vertex_index)
                self._lasso_ignore_next_release = True
                return True
            self._lasso_dragging_anchor = hit
            return True
        if self._lasso_shift_held and not self._lasso_current_vertices:
            return True
        if (
            not self._lasso_current_vertices
            and self._lasso_selected_polygon_index is not None
        ):
            edge_hit = self._lasso_edge_insert_hit_at_canvas(
                mouse_event.position(),
                self._lasso_selected_polygon_index,
            )
            if edge_hit is not None:
                _polygon_index, insert_index, insert_point = edge_hit
                self._insert_lasso_anchor(self._lasso_selected_polygon_index, insert_index, insert_point)
                self._lasso_ignore_next_release = True
                return True
        return True

    def _handle_lasso_release(self, mouse_event: QtGui.QMouseEvent) -> bool:
        if mouse_event.button() != QtCore.Qt.MouseButton.LeftButton:
            return False
        if self._lasso_ignore_next_release:
            self._lasso_ignore_next_release = False
            return True
        if self._lasso_dragging_anchor is not None:
            poly_index, _, is_current = self._lasso_dragging_anchor
            self._lasso_dragging_anchor = None
            if not is_current:
                self.lasso_polygon_updated.emit(self._lasso_polygon_payload(poly_index))
                self._emit_lasso_selection_for_polygon(poly_index)
            self._refresh()
            return True
        if self._click_press_pos is not None:
            if self._distance_px(self._click_press_pos, mouse_event.position()) > self._click_move_threshold_px:
                return True
        world = self._screen_to_world(mouse_event.position().x(), mouse_event.position().y())
        if world is None:
            return True
        world_point = np.array([float(world[0]), float(world[1])], dtype=np.float32)
        if self._lasso_current_vertices:
            first_canvas = self._world_to_screen(self._lasso_current_vertices[0])
            if first_canvas is not None:
                dx = float(first_canvas[0] - mouse_event.position().x())
                dy = float(first_canvas[1] - mouse_event.position().y())
                distance = float(np.sqrt(dx * dx + dy * dy))
                if distance <= self._lasso_anchor_hit_radius_px and len(self._lasso_current_vertices) >= 3:
                    self._finish_lasso_polygon()
                    return True
            self._lasso_current_vertices.append(world_point)
            self._refresh()
            return True
        inside_index = self._polygon_index_at_world_point(world_point) if self._lasso_polygons else None
        if inside_index is not None and not self._lasso_shift_held:
            self._lasso_selected_polygon_index = inside_index
            self._lasso_pending_draw_start = False
            self._emit_lasso_polygon_selected(inside_index)
            self._emit_lasso_selection_for_polygon(inside_index)
            self._refresh()
            return True
        self._lasso_selected_polygon_index = None
        self._lasso_pending_draw_start = False
        self._lasso_current_vertices.append(world_point)
        self._refresh()
        return True

    def _finish_lasso_polygon(self) -> bool:
        if len(self._lasso_current_vertices) < 3:
            return False
        polygon = np.asarray(self._lasso_current_vertices, dtype=np.float32)
        self._lasso_polygons.append(polygon)
        self._lasso_polygon_ids.append("")
        self._lasso_polygon_labels.append("")
        self._lasso_polygon_colors.append("#3b82f6")
        self._lasso_polygon_area_visible.append(True)
        self._lasso_polygon_points_visible.append(True)
        created_index = len(self._lasso_polygons) - 1
        self._lasso_selected_polygon_index = created_index
        self._lasso_pending_draw_start = False
        self._lasso_dragging_anchor = None
        self._lasso_current_vertices.clear()
        created_payload = self._lasso_polygon_payload(created_index)
        self.lasso_polygon_created.emit(created_payload)
        if 0 <= created_index < len(self._lasso_polygons):
            self._emit_lasso_selection_for_polygon(created_index)
        if 0 <= created_index < len(self._lasso_polygons):
            self._emit_lasso_polygon_selected(created_index)
        self._refresh()
        self._update_cursor()
        return True

    def _current_lasso_polygon_array(self) -> np.ndarray | None:
        if not self._lasso_current_vertices:
            return None
        return np.asarray(self._lasso_current_vertices, dtype=np.float32)

    def _world_to_screen(self, world: np.ndarray) -> tuple[float, float] | None:
        viewport = self._effective_viewport()
        if viewport is None:
            return None
        width = float(max(1, self.width()))
        height = float(max(1, self.height()))
        x_min, x_max, y_min, y_max = viewport
        dx = x_max - x_min
        dy = y_max - y_min
        if abs(dx) < 1e-9 or abs(dy) < 1e-9:
            return None
        x = ((float(world[0]) - x_min) / dx) * width
        y = height - ((float(world[1]) - y_min) / dy) * height
        return x, y

    def _polygon_index_at_world_point(self, world_point: np.ndarray) -> int | None:
        if not self._lasso_polygons:
            return None
        points = np.asarray([world_point], dtype=np.float32)
        for index in range(len(self._lasso_polygons) - 1, -1, -1):
            polygon = self._lasso_polygons[index]
            if polygon.shape[0] < 3:
                continue
            mask = points_in_polygon(points, polygon)
            if mask.size and bool(mask[0]):
                return index
        return None

    def _anchor_hit_at_canvas(self, canvas_pos: QtCore.QPointF) -> tuple[int, int, bool] | None:
        if self._lasso_current_vertices:
            for vertex_index, point in enumerate(self._lasso_current_vertices):
                screen = self._world_to_screen(point)
                if screen is None:
                    continue
                if self._distance_xy(screen[0], screen[1], canvas_pos.x(), canvas_pos.y()) <= self._lasso_anchor_hit_radius_px:
                    return -1, vertex_index, True
        if self._lasso_selected_polygon_index is None:
            return None
        if not (0 <= self._lasso_selected_polygon_index < len(self._lasso_polygons)):
            return None
        polygon = self._lasso_polygons[self._lasso_selected_polygon_index]
        for vertex_index, point in enumerate(polygon):
            screen = self._world_to_screen(point)
            if screen is None:
                continue
            if self._distance_xy(screen[0], screen[1], canvas_pos.x(), canvas_pos.y()) <= self._lasso_anchor_hit_radius_px:
                return self._lasso_selected_polygon_index, vertex_index, False
        return None

    def _lasso_edge_insert_hit_at_canvas(
        self,
        canvas_pos: QtCore.QPointF,
        polygon_index: int,
    ) -> tuple[int, int, np.ndarray] | None:
        if not (0 <= polygon_index < len(self._lasso_polygons)):
            return None
        polygon = self._lasso_polygons[polygon_index]
        if polygon.shape[0] < 3:
            return None
        threshold_px = 10.0
        best_index: int | None = None
        best_distance = float("inf")
        for idx in range(polygon.shape[0]):
            a = polygon[idx]
            b = polygon[(idx + 1) % polygon.shape[0]]
            a_screen = self._world_to_screen(a)
            b_screen = self._world_to_screen(b)
            if a_screen is None or b_screen is None:
                continue
            distance = self._point_to_segment_distance(
                canvas_pos.x(),
                canvas_pos.y(),
                a_screen[0],
                a_screen[1],
                b_screen[0],
                b_screen[1],
            )
            if distance < best_distance:
                best_distance = distance
                best_index = idx + 1
        if best_index is None or best_distance > threshold_px:
            return None
        a = polygon[(best_index - 1) % polygon.shape[0]]
        b = polygon[best_index % polygon.shape[0]]
        a_screen = self._world_to_screen(a)
        b_screen = self._world_to_screen(b)
        if a_screen is None or b_screen is None:
            return None
        abx = float(b_screen[0] - a_screen[0])
        aby = float(b_screen[1] - a_screen[1])
        denom = abx * abx + aby * aby
        if denom <= 1e-12:
            insert_point = np.asarray(a, dtype=np.float32)
        else:
            t = max(
                0.0,
                min(
                    1.0,
                    (
                        (canvas_pos.x() - float(a_screen[0])) * abx
                        + (canvas_pos.y() - float(a_screen[1])) * aby
                    )
                    / denom,
                ),
            )
            insert_point = np.asarray(a + (b - a) * t, dtype=np.float32)
        return int(polygon_index), int(best_index), insert_point

    def _insert_lasso_anchor(self, polygon_index: int, insert_index: int, world_point: np.ndarray) -> bool:
        if not (0 <= polygon_index < len(self._lasso_polygons)):
            return False
        polygon = self._lasso_polygons[polygon_index]
        self._lasso_polygons[polygon_index] = np.insert(
            polygon,
            int(insert_index),
            np.asarray(world_point, dtype=np.float32),
            axis=0,
        )
        self.lasso_polygon_updated.emit(self._lasso_polygon_payload(polygon_index))
        self._emit_lasso_selection_for_polygon(polygon_index)
        self._refresh()
        return True

    def remove_lasso_anchor(self, polygon_index: int, vertex_index: int) -> bool:
        if not (0 <= polygon_index < len(self._lasso_polygons)):
            return False
        polygon = self._lasso_polygons[polygon_index]
        if polygon.shape[0] <= 3:
            self.lasso_status_hint_changed.emit("A polygon needs at least 3 anchors.")
            return False
        if not (0 <= vertex_index < polygon.shape[0]):
            return False
        self._lasso_polygons[polygon_index] = np.delete(polygon, int(vertex_index), axis=0)
        self.lasso_polygon_updated.emit(self._lasso_polygon_payload(polygon_index))
        self._emit_lasso_selection_for_polygon(polygon_index)
        self._refresh()
        return True

    def _move_dragged_lasso_anchor(self, world_point: np.ndarray) -> None:
        poly_index, vertex_index, is_current = self._lasso_dragging_anchor or (None, None, None)
        if poly_index is None or vertex_index is None:
            return
        if is_current:
            if 0 <= vertex_index < len(self._lasso_current_vertices):
                self._lasso_current_vertices[vertex_index] = np.asarray(world_point, dtype=np.float32)
            return
        if not (0 <= poly_index < len(self._lasso_polygons)):
            return
        polygon = self._lasso_polygons[poly_index]
        if 0 <= vertex_index < polygon.shape[0]:
            polygon[vertex_index] = np.asarray(world_point, dtype=np.float32)
            self._lasso_polygons[poly_index] = polygon

    @staticmethod
    def _distance_xy(x0: float, y0: float, x1: float, y1: float) -> float:
        dx = float(x0 - x1)
        dy = float(y0 - y1)
        return float(np.sqrt(dx * dx + dy * dy))

    @staticmethod
    def _point_to_segment_distance(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
        abx = bx - ax
        aby = by - ay
        apx = px - ax
        apy = py - ay
        denom = abx * abx + aby * aby
        if denom <= 1e-12:
            return DiscoveryCanvasWidget._distance_xy(px, py, ax, ay)
        t = max(0.0, min(1.0, (apx * abx + apy * aby) / denom))
        cx = ax + t * abx
        cy = ay + t * aby
        return DiscoveryCanvasWidget._distance_xy(px, py, cx, cy)

    def _emit_lasso_selection_for_polygon(self, polygon_index: int) -> None:
        if not (0 <= polygon_index < len(self._lasso_polygons)):
            return
        polygon = self._lasso_polygons[polygon_index]
        if self._points.shape[0] > 50_000:
            self._lasso_query_revision += 1
            query_revision = self._lasso_query_revision
            for event in self._lasso_query_cancel_events.values():
                event.set()
            cancelled = threading.Event()
            task = _LassoSelectionTask(
                scene_revision=self._active_scene_revision,
                query_revision=query_revision,
                polygon_index=polygon_index,
                polygon=polygon,
                points=self._points,
                metadata_ids=self._metadata_ids,
                visibility=self._visibility,
                spatial_index=self._grid_index,
                cancelled=cancelled,
            )
            task.signals.finished.connect(self._on_lasso_selection_ready)
            task.signals.cancelled.connect(self._on_lasso_selection_cancelled)
            self._lasso_query_tasks[query_revision] = task
            self._lasso_query_cancel_events[query_revision] = cancelled
            self._scene_thread_pool.start(task)
            return
        selected_ids = self.select_metadata_ids_in_polygon(polygon, visible_threshold=0.5)
        self._deliver_lasso_selection(polygon_index, polygon, selected_ids)

    @QtCore.Slot(int, int, int, object, object)
    def _on_lasso_selection_ready(
        self,
        scene_revision: int,
        query_revision: int,
        polygon_index: int,
        polygon: object,
        selected_ids: object,
    ) -> None:
        self._lasso_query_tasks.pop(int(query_revision), None)
        self._lasso_query_cancel_events.pop(int(query_revision), None)
        if int(query_revision) != self._lasso_query_revision or int(scene_revision) != self._active_scene_revision:
            return
        if not (0 <= int(polygon_index) < len(self._lasso_polygons)):
            return
        polygon_array = np.asarray(polygon, dtype=np.float32)
        if not np.array_equal(polygon_array, self._lasso_polygons[int(polygon_index)]):
            return
        self._deliver_lasso_selection(
            int(polygon_index),
            polygon_array,
            np.asarray(selected_ids, dtype=np.int64),
        )

    @QtCore.Slot(int)
    def _on_lasso_selection_cancelled(self, query_revision: int) -> None:
        self._lasso_query_tasks.pop(int(query_revision), None)
        self._lasso_query_cancel_events.pop(int(query_revision), None)

    def _deliver_lasso_selection(
        self,
        polygon_index: int,
        polygon: np.ndarray,
        selected_ids: np.ndarray,
    ) -> None:
        payload = {
            "metadata_ids": selected_ids.tolist(),
            "polygon": polygon.tolist(),
            "polygon_index": int(polygon_index),
            "area_id": self._lasso_polygon_ids[polygon_index] if polygon_index < len(self._lasso_polygon_ids) else "",
            "label": self._lasso_polygon_labels[polygon_index] if polygon_index < len(self._lasso_polygon_labels) else "",
        }
        self.lasso_selection_changed.emit(payload)
        if self._lasso_selection_callback is not None:
            self._lasso_selection_callback(payload)

    def remove_lasso_polygon(self, polygon_index: int) -> None:
        index = int(polygon_index)
        if not (0 <= index < len(self._lasso_polygons)):
            return
        payload = self._lasso_polygon_payload(index)
        del self._lasso_polygons[index]
        for values in (
            self._lasso_polygon_ids,
            self._lasso_polygon_labels,
            self._lasso_polygon_colors,
            self._lasso_polygon_area_visible,
            self._lasso_polygon_points_visible,
        ):
            if index < len(values):
                del values[index]
        if self._lasso_selected_polygon_index is not None:
            if self._lasso_selected_polygon_index == index:
                self._lasso_selected_polygon_index = None
            elif self._lasso_selected_polygon_index > index:
                self._lasso_selected_polygon_index -= 1
        self.lasso_polygon_removed.emit(payload)
        self._refresh()

    def set_lasso_polygon_metadata(self, polygon_index: int, *, area_id: str, label: str, color_hex: str) -> None:
        index = int(polygon_index)
        if not (0 <= index < len(self._lasso_polygons)):
            return
        while len(self._lasso_polygon_ids) <= index:
            self._lasso_polygon_ids.append("")
        while len(self._lasso_polygon_labels) <= index:
            self._lasso_polygon_labels.append("")
        while len(self._lasso_polygon_colors) <= index:
            self._lasso_polygon_colors.append("#3b82f6")
        self._lasso_polygon_ids[index] = str(area_id or "")
        self._lasso_polygon_labels[index] = str(label or "")
        self._lasso_polygon_colors[index] = str(color_hex or "#3b82f6")

    def _emit_lasso_polygon_selected(self, polygon_index: int) -> None:
        self.lasso_polygon_selected.emit(self._lasso_polygon_payload(polygon_index))

    def _lasso_polygon_payload(self, polygon_index: int) -> dict:
        return {
            "polygon_index": int(polygon_index),
            "area_id": self._lasso_polygon_ids[polygon_index] if polygon_index < len(self._lasso_polygon_ids) else "",
            "label": self._lasso_polygon_labels[polygon_index] if polygon_index < len(self._lasso_polygon_labels) else "",
            "color_hex": self._lasso_polygon_colors[polygon_index] if polygon_index < len(self._lasso_polygon_colors) else "#3b82f6",
            "area_visible": self._lasso_polygon_area_visible[polygon_index] if polygon_index < len(self._lasso_polygon_area_visible) else True,
            "points_visible": self._lasso_polygon_points_visible[polygon_index] if polygon_index < len(self._lasso_polygon_points_visible) else True,
            "polygon": np.asarray(self._lasso_polygons[polygon_index], dtype=np.float32).tolist(),
        }


ProjectionCanvasWidget = DiscoveryCanvasWidget
