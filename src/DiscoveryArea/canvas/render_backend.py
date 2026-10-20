from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Protocol

import numpy as np
from PySide6 import QtCore, QtGui


@dataclass(frozen=True)
class RenderPayload:
    points: np.ndarray
    metadata_ids: np.ndarray
    visibility: np.ndarray
    colors: np.ndarray | None
    edge_colors: np.ndarray | None
    edge_widths: np.ndarray | None
    point_size: float | np.ndarray
    viewport: tuple[float, float, float, float] | None
    lasso_polygons: list[np.ndarray]
    lasso_current_polygon: np.ndarray | None
    lasso_selected_polygon_index: int | None


@dataclass(frozen=True)
class ThumbnailLayerConfigPayload:
    enabled: bool
    variant_id: str
    zoom_mid_threshold: float
    zoom_close_threshold: float
    thumbnail_size_px: float
    max_visible_mid: int
    max_visible_close: int
    coloring: str = "none"
    color_data: bytes = b""
    border_width_px: float = 2.0
    color_opacity: float = 0.75
    render_margin_fraction: float = 0.0
    border_color_data: bytes = b""


@dataclass(frozen=True)
class ThumbnailPlanPayload:
    revision: int
    metadata_ids: tuple[int, ...]
    forced_metadata_ids: tuple[int, ...]
    planning_viewport: tuple[float, float, float, float]
    display_px: int
    decode_px: int


@dataclass(frozen=True)
class ThumbnailRuntimePolicyPayload:
    cpu_cache_mb: int
    gpu_cache_mb: int
    atlas_page_px: int
    decode_workers: int
    upload_bytes_per_frame: int
    target_frame_ms: float


@dataclass(frozen=True)
class ThumbnailTileLayerConfigPayload:
    enabled: bool
    variant_id: str
    discovery_cache_id: str
    tile_size_px: int


@dataclass(frozen=True)
class ThumbnailTilePolicyPayload:
    max_cache_mb: int
    decode_workers: int
    max_decode_jobs: int


@dataclass(frozen=True)
class ThumbnailBatchEntry:
    metadata_id: int
    width: int
    height: int
    rgba: bytes
    target_px: int


@dataclass(frozen=True)
class ThumbnailSourceEntry:
    metadata_id: int
    path: str
    mtime_ns: int | None
    target_px: int
    missing: bool = False


@dataclass(frozen=True)
class ThumbnailTileEntry:
    lod: int
    tile_x: int
    tile_y: int
    path: str
    mtime_ns: int
    x_min: float
    x_max: float
    y_min: float
    y_max: float


@dataclass(frozen=True)
class ThumbnailTileDisplayKeyEntry:
    lod: int
    tile_x: int
    tile_y: int


@dataclass(frozen=True)
class ThumbnailAtlasPageEntry:
    page_index: int
    path: str
    width: int
    height: int


@dataclass(frozen=True)
class ThumbnailAtlasSpriteEntry:
    metadata_id: int
    page_index: int
    u: float
    v: float
    width: float
    height: float


class DiscoveryRenderBackend(Protocol):
    def set_static_data(self, payload: RenderPayload) -> None: ...
    def request_redraw(self) -> None: ...
    def set_background_color(self, color: QtGui.QColor | str) -> None: ...
    def set_lasso_overlay_state(
        self,
        polygons: list[np.ndarray],
        current_polygon: np.ndarray | None,
        selected_polygon_index: int | None,
        polygon_colors: list[str],
        fill_opacity: float,
        hover_anchor: tuple[int, int, bool] | None,
        hover_edge_insert: tuple[int, int, np.ndarray] | None,
        hover_polygon_index: int | None,
        *,
        option_modifier_held: bool = False,
    ) -> None: ...
    def set_viewport(self, viewport: tuple[float, float, float, float] | None) -> None: ...
    def set_thumbnail_layer_config(self, payload: ThumbnailLayerConfigPayload) -> None: ...
    def set_thumbnail_plan(self, payload: ThumbnailPlanPayload) -> None: ...
    def set_thumbnail_runtime_policy(self, payload: ThumbnailRuntimePolicyPayload) -> None: ...
    def set_thumbnail_interaction_active(self, active: bool) -> None: ...
    def set_thumbnail_sources(
        self,
        variant_id: str,
        payload: list[ThumbnailSourceEntry],
        *,
        revision: int = 0,
    ) -> None: ...
    def set_thumbnail_atlas(
        self,
        variant_id: str,
        target_px: int,
        pages: list[ThumbnailAtlasPageEntry],
        sprites: list[ThumbnailAtlasSpriteEntry],
    ) -> None: ...
    def set_thumbnail_tile_layer_config(self, payload: ThumbnailTileLayerConfigPayload) -> None: ...
    def set_thumbnail_tile_policy(self, payload: ThumbnailTilePolicyPayload) -> None: ...
    def set_thumbnail_tiles(self, payload: list[ThumbnailTileEntry]) -> None: ...
    def set_thumbnail_tile_display_keys(self, payload: list[ThumbnailTileDisplayKeyEntry]) -> None: ...
    def set_visible_thumbnail_metadata_ids(self, payload: list[int]) -> None: ...
    def set_hovered_thumbnail_metadata_id(self, metadata_id: int | None) -> None: ...
    def set_thumbnail_candidates(self, payload: list[int], *, target_px: int) -> None: ...
    def missing_thumbnail_metadata_ids(self, payload: list[int], *, target_px: int) -> list[int]: ...
    def clear_thumbnail_tiles(self) -> None: ...
    def set_thumbnail_batch(self, payload: list[ThumbnailBatchEntry]) -> None: ...
    def supports_thumbnail_layer(self) -> bool: ...
    def supports_thumbnail_render_margin(self) -> bool: ...
    def supports_thumbnail_sources(self) -> bool: ...
    def supports_thumbnail_tiles(self) -> bool: ...
    def set_lod_policy(self, mode: str, representative_limit: int) -> None: ...
    def set_density_levels(self, levels: tuple[object, ...], representative_indices: np.ndarray) -> None: ...
    def set_visibility(self, visibility: np.ndarray) -> bool: ...
    def set_point_style(self, payload: RenderPayload) -> bool: ...


def _request_item_redraw(item: QtCore.QObject) -> None:
    updater = getattr(item, "update", None)
    if callable(updater):
        updater()
    window_getter = getattr(item, "window", None)
    if not callable(window_getter):
        return
    try:
        window = window_getter()
    except (RuntimeError, TypeError):
        return
    request_update = getattr(window, "requestUpdate", None)
    if callable(request_update):
        request_update()


class PaintedItemBackend:
    def __init__(self, item: QtCore.QObject) -> None:
        self._item = item
        self._thumbnail_variant_id = "none"
        self._tile_entries: list[ThumbnailTileEntry] = []
        self._tile_display: set[tuple[int, int, int]] = set()

    def set_static_data(self, payload: RenderPayload) -> None:
        setter = getattr(self._item, "set_data", None)
        if not callable(setter):
            return
        setter(
            payload.points,
            payload.metadata_ids,
            payload.visibility,
            payload.colors,
            payload.edge_colors,
            payload.edge_widths,
            payload.point_size,
            payload.viewport,
            payload.lasso_polygons,
            payload.lasso_current_polygon,
            payload.lasso_selected_polygon_index,
        )

    def request_redraw(self) -> None:
        _request_item_redraw(self._item)

    def set_lod_policy(self, mode: str, representative_limit: int) -> None:
        setter = getattr(self._item, "set_lod_policy", None)
        if callable(setter):
            setter(str(mode), int(representative_limit))

    def set_density_levels(self, levels: tuple[object, ...], representative_indices: np.ndarray) -> None:
        setter = getattr(self._item, "set_density_levels", None)
        if callable(setter):
            setter(levels, representative_indices)

    def set_visibility(self, visibility: np.ndarray) -> bool:
        setter = getattr(self._item, "set_visibility", None)
        if not callable(setter):
            return False
        setter(visibility)
        return True

    def set_point_style(self, payload: RenderPayload) -> bool:
        setter = getattr(self._item, "set_point_style", None)
        if not callable(setter):
            return False
        setter(
            payload.colors,
            payload.edge_colors,
            payload.edge_widths,
            payload.point_size,
        )
        return True

    def set_viewport(self, viewport: tuple[float, float, float, float] | None) -> None:
        setter = getattr(self._item, "set_viewport", None)
        if callable(setter):
            setter(viewport)

    def set_background_color(self, color: QtGui.QColor | str) -> None:
        setter = getattr(self._item, "set_background_color", None)
        if callable(setter):
            setter(color)

    def set_lasso_overlay_state(
        self,
        polygons: list[np.ndarray],
        current_polygon: np.ndarray | None,
        selected_polygon_index: int | None,
        polygon_colors: list[str],
        fill_opacity: float,
        hover_anchor: tuple[int, int, bool] | None,
        hover_edge_insert: tuple[int, int, np.ndarray] | None,
        hover_polygon_index: int | None,
        *,
        option_modifier_held: bool = False,
    ) -> None:
        setter = getattr(self._item, "set_lasso_overlay_state", None)
        if callable(setter):
            setter(
                polygons,
                current_polygon,
                selected_polygon_index,
                polygon_colors,
                fill_opacity,
                hover_anchor,
                hover_edge_insert,
                hover_polygon_index,
                option_modifier_held=option_modifier_held,
            )

    def set_thumbnail_layer_config(self, payload: ThumbnailLayerConfigPayload) -> None:
        self._thumbnail_variant_id = payload.variant_id
        setter = getattr(self._item, "set_thumbnail_layer_config", None)
        if callable(setter):
            setter(
                payload.enabled,
                payload.variant_id,
                payload.zoom_mid_threshold,
                payload.zoom_close_threshold,
                payload.thumbnail_size_px,
                payload.max_visible_mid,
                payload.max_visible_close,
                payload.coloring,
                payload.border_width_px,
                payload.color_opacity,
                payload.color_data,
                payload.border_color_data,
            )

    def set_thumbnail_tile_layer_config(self, payload: ThumbnailTileLayerConfigPayload) -> None:
        _ = payload

    def set_thumbnail_tile_policy(self, payload: ThumbnailTilePolicyPayload) -> None:
        _ = payload

    def set_thumbnail_tiles(self, payload: list[ThumbnailTileEntry]) -> None:
        self._tile_entries = list(payload)
        self._push_resolved_tile_batch()

    def set_thumbnail_tile_display_keys(self, payload: list[ThumbnailTileDisplayKeyEntry]) -> None:
        self._tile_display = {(entry.lod, entry.tile_x, entry.tile_y) for entry in payload}
        self._push_resolved_tile_batch()

    def set_visible_thumbnail_metadata_ids(self, payload: list[int]) -> None:
        setter = getattr(self._item, "set_visible_thumbnail_metadata_ids", None)
        if callable(setter):
            setter(payload)

    def set_hovered_thumbnail_metadata_id(self, metadata_id: int | None) -> None:
        setter = getattr(self._item, "set_hovered_thumbnail_metadata_id", None)
        if callable(setter):
            setter(metadata_id)

    def set_thumbnail_plan(self, payload: ThumbnailPlanPayload) -> None:
        self.set_thumbnail_candidates(list(payload.metadata_ids), target_px=payload.decode_px)

    def set_thumbnail_runtime_policy(self, payload: ThumbnailRuntimePolicyPayload) -> None:
        _ = payload

    def set_thumbnail_interaction_active(self, active: bool) -> None:
        _ = active

    def set_thumbnail_sources(
        self,
        variant_id: str,
        payload: list[ThumbnailSourceEntry],
        *,
        revision: int = 0,
    ) -> None:
        _ = variant_id, payload, revision

    def set_thumbnail_atlas(
        self,
        variant_id: str,
        target_px: int,
        pages: list[ThumbnailAtlasPageEntry],
        sprites: list[ThumbnailAtlasSpriteEntry],
    ) -> None:
        _ = variant_id, target_px, pages, sprites

    def set_thumbnail_candidates(self, payload: list[int], *, target_px: int) -> None:
        setter = getattr(self._item, "set_thumbnail_candidates", None)
        if callable(setter):
            setter(payload, target_px)

    def missing_thumbnail_metadata_ids(self, payload: list[int], *, target_px: int) -> list[int]:
        _ = target_px
        return [int(value) for value in payload]

    def evict_thumbnails_outside_viewport(
        self,
        viewport: tuple[float, float, float, float] | None,
        *,
        target_px: int,
    ) -> None:
        evictor = getattr(self._item, "evict_thumbnails_outside_viewport", None)
        if callable(evictor):
            evictor(viewport, target_px=target_px)

    def clear_thumbnail_tiles(self) -> None:
        self._tile_entries = []
        self._tile_display.clear()

    def set_thumbnail_batch(self, payload: list[ThumbnailBatchEntry]) -> None:
        setter = getattr(self._item, "set_thumbnail_batch", None)
        if not callable(setter):
            return
        entries = [
            {
                "metadata_id": entry.metadata_id,
                "width": entry.width,
                "height": entry.height,
                "target_px": entry.target_px,
                "rgba": entry.rgba,
            }
            for entry in payload
        ]
        setter(entries)

    def supports_thumbnail_layer(self) -> bool:
        return True

    def supports_thumbnail_render_margin(self) -> bool:
        return True

    def supports_thumbnail_sources(self) -> bool:
        return False

    def supports_thumbnail_tiles(self) -> bool:
        return False

    def _push_resolved_tile_batch(self) -> None:
        if not self._tile_entries:
            return
        active_keys = self._tile_display or {(e.lod, e.tile_x, e.tile_y) for e in self._tile_entries}
        entries: list[dict[str, object]] = []
        for tile in self._tile_entries:
            if (tile.lod, tile.tile_x, tile.tile_y) not in active_keys:
                continue
            image = QtCore.QFile(tile.path)
            if not image.exists():
                continue
            qimage = QtCore.QByteArray()
            # Kept intentionally minimal: painted fallback will continue to use
            # batch RGBA entries from the thumbnail service. Tile path decode is
            # deferred to a later renderer phase.
            _ = qimage
            entries.append({})
        _ = entries


class SceneGraphItemBackend(PaintedItemBackend):
    """Backend adapter for scene-graph point item.

    Thumbnail APIs are intentionally no-op in this phase; the item renders
    points only through updatePaintNode.
    """

    def supports_thumbnail_layer(self) -> bool:
        return False

    def supports_thumbnail_render_margin(self) -> bool:
        return False

    def supports_thumbnail_tiles(self) -> bool:
        return False


class NativeItemBackend:
    def __init__(self, item: QtCore.QObject) -> None:
        self._item = item
        self._last_payload: RenderPayload | None = None
        self._last_upload_ms = 0.0
        self._renderer_api_version = self._detect_renderer_api_version()
        self._thumbnail_render_margin_supported = self._detect_thumbnail_render_margin_support()

    def _invoke(self, name: str, *args) -> None:
        method = getattr(self._item, name, None)
        if callable(method):
            method(*args)

    def _detect_renderer_api_version(self) -> int:
        getter = getattr(self._item, "rendererApiVersion", None)
        if not callable(getter):
            return 1
        try:
            return max(1, int(getter()))
        except (TypeError, ValueError, AttributeError):
            return 1

    def _detect_thumbnail_render_margin_support(self) -> bool:
        checker = getattr(self._item, "supportsThumbnailRenderMargin", None)
        if callable(checker):
            try:
                return bool(checker())
            except (TypeError, AttributeError):
                pass
        meta_getter = getattr(self._item, "metaObject", None)
        if not callable(meta_getter):
            return True
        meta = meta_getter()
        if meta is None:
            return True
        try:
            count = int(meta.methodCount())
        except (TypeError, ValueError):
            return True
        for index in range(count):
            method = meta.method(index)
            try:
                name = bytes(method.name()).decode("utf-8")
            except Exception:
                try:
                    name = str(method.name(), "utf-8")
                except Exception:
                    name = str(method.name())
            if name != "setThumbnailLayerConfig":
                continue
            try:
                if int(method.parameterCount()) >= 12:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def renderer_api_version(self) -> int:
        return self._renderer_api_version

    def upload_time_ms(self) -> float:
        return float(self._last_upload_ms)

    def request_redraw(self) -> None:
        _request_item_redraw(self._item)

    def set_lod_policy(self, mode: str, representative_limit: int) -> None:
        self._invoke("setLodConfig", str(mode) == "hybrid", int(representative_limit))

    def set_density_levels(self, levels: tuple[object, ...], representative_indices: np.ndarray) -> None:
        # Native API v3 owns density GPU resources, but consumes the scene
        # worker's deterministic spatial representatives.
        _ = levels
        setter = getattr(self._item, "setRepresentativeIndexBuffer", None)
        if callable(setter):
            indices = np.ascontiguousarray(np.asarray(representative_indices, dtype=np.int32).reshape(-1))
            setter(QtCore.QByteArray(indices.tobytes()))

    def set_visibility(self, visibility: np.ndarray) -> bool:
        setter = getattr(self._item, "setVisibilityBuffer", None)
        if not callable(setter):
            return False
        values = np.ascontiguousarray(np.asarray(visibility, dtype=np.float32).reshape(-1))
        setter(QtCore.QByteArray(values.tobytes()))
        return True

    def set_point_style(self, payload: RenderPayload) -> bool:
        setter = getattr(self._item, "setPointStyleBuffers", None)
        if not callable(setter):
            return False
        point_size, point_sizes, edge_widths = self._style_scalar_buffers(payload)
        setter(
            QtCore.QByteArray(self._color_buffer(payload.colors)),
            float(point_size),
            QtCore.QByteArray(point_sizes),
            QtCore.QByteArray(self._color_buffer(payload.edge_colors)),
            QtCore.QByteArray(edge_widths),
        )
        return True

    def supports_static_points(self) -> bool:
        return (
            callable(getattr(self._item, "setStaticPointBuffers", None))
            or callable(getattr(self._item, "setStaticPointData", None))
            or callable(getattr(self._item, "setStaticData", None))
        )

    def set_static_data(self, payload: RenderPayload) -> None:
        started = time.perf_counter()
        try:
            self._set_static_data_impl(payload)
        finally:
            self._last_upload_ms = (time.perf_counter() - started) * 1000.0

    def _set_static_data_impl(self, payload: RenderPayload) -> None:
        self._last_payload = payload
        buffer_setter = getattr(self._item, "setStaticPointBuffers", None)
        if callable(buffer_setter):
            self._call_static_point_buffers(buffer_setter, payload)
            return
        setter = getattr(self._item, "setStaticPointData", None)
        if callable(setter):
            point_size = (
                float(np.nanmax(np.asarray(payload.point_size, dtype=np.float32)))
                if not np.isscalar(payload.point_size)
                else float(payload.point_size)
            )
            setter(
                np.asarray(payload.points[:, :2], dtype=np.float32).tolist(),
                np.asarray(payload.metadata_ids, dtype=np.int64).tolist(),
                np.asarray(payload.visibility, dtype=np.float32).tolist(),
                None if payload.colors is None else np.asarray(payload.colors, dtype=np.float32).tolist(),
                point_size,
                None if np.isscalar(payload.point_size) else np.asarray(payload.point_size, dtype=np.float32).tolist(),
                None,
                None if payload.edge_colors is None else np.asarray(payload.edge_colors, dtype=np.float32).tolist(),
                None if payload.edge_widths is None else np.asarray(payload.edge_widths, dtype=np.float32).tolist(),
            )
            return
        self._invoke(
            "setStaticData",
            payload.points,
            payload.metadata_ids,
            payload.visibility,
            payload.colors,
            None,
            payload.edge_colors,
            payload.edge_widths,
            payload.point_size,
            payload.viewport,
            payload.lasso_polygons,
            payload.lasso_current_polygon,
            payload.lasso_selected_polygon_index,
        )

    def set_lasso_overlay_state(
        self,
        polygons: list[np.ndarray],
        current_polygon: np.ndarray | None,
        selected_polygon_index: int | None,
        polygon_colors: list[str],
        fill_opacity: float,
        hover_anchor: tuple[int, int, bool] | None,
        hover_edge_insert: tuple[int, int, np.ndarray] | None,
        hover_polygon_index: int | None,
        *,
        option_modifier_held: bool = False,
    ) -> None:
        _ = (
            polygons,
            current_polygon,
            selected_polygon_index,
            polygon_colors,
            fill_opacity,
            hover_anchor,
            hover_edge_insert,
            hover_polygon_index,
            option_modifier_held,
        )

    def _call_static_point_buffers(self, setter, payload: RenderPayload) -> None:
        points = np.ascontiguousarray(np.asarray(payload.points[:, :2], dtype=np.float32))
        metadata_ids = np.ascontiguousarray(np.asarray(payload.metadata_ids, dtype=np.int64).reshape(-1))
        visibility = np.ascontiguousarray(np.asarray(payload.visibility, dtype=np.float32).reshape(-1))
        colors = self._color_buffer(payload.colors)
        point_size, point_sizes, edge_widths = self._style_scalar_buffers(payload)
        edge_colors = self._color_buffer(payload.edge_colors)
        setter(
            QtCore.QByteArray(points.tobytes()),
            int(points.shape[0]),
            QtCore.QByteArray(metadata_ids.tobytes()),
            QtCore.QByteArray(visibility.tobytes()),
            QtCore.QByteArray(colors),
            float(point_size),
            QtCore.QByteArray(point_sizes),
            QtCore.QByteArray(),
            QtCore.QByteArray(edge_colors),
            QtCore.QByteArray(edge_widths),
        )

    @staticmethod
    def _style_scalar_buffers(
        payload: RenderPayload,
    ) -> tuple[float, bytes, bytes]:
        point_size = (
            float(np.nanmax(np.asarray(payload.point_size, dtype=np.float32)))
            if not np.isscalar(payload.point_size)
            else float(payload.point_size)
        )
        point_sizes = (
            b""
            if np.isscalar(payload.point_size)
            else np.ascontiguousarray(
                np.asarray(payload.point_size, dtype=np.float32).reshape(-1)
            ).tobytes()
        )
        edge_widths = (
            b""
            if payload.edge_widths is None
            else np.ascontiguousarray(
                np.asarray(payload.edge_widths, dtype=np.float32).reshape(-1)
            ).tobytes()
        )
        return point_size, point_sizes, edge_widths

    def _color_buffer(self, values: np.ndarray | None) -> bytes:
        if values is None:
            return b""
        colors = np.asarray(values, dtype=np.float32)
        if self._renderer_api_version >= 3:
            return np.ascontiguousarray(np.rint(np.clip(colors, 0.0, 1.0) * 255.0), dtype=np.uint8).tobytes()
        return np.ascontiguousarray(colors, dtype=np.float32).tobytes()

    def set_viewport(self, viewport: tuple[float, float, float, float] | None) -> None:
        self._invoke("setViewport", [] if viewport is None else [float(v) for v in viewport])

    def set_background_color(self, color: QtGui.QColor | str) -> None:
        _ = color

    def set_thumbnail_layer_config(self, payload: ThumbnailLayerConfigPayload) -> None:
        args = (
            bool(payload.enabled),
            str(payload.variant_id),
            float(payload.zoom_mid_threshold),
            float(payload.zoom_close_threshold),
            float(payload.thumbnail_size_px),
            int(payload.max_visible_mid),
            int(payload.max_visible_close),
        )
        if self._renderer_api_version >= 5:
            self._invoke(
                "setThumbnailLayerConfig",
                *args,
                str(payload.coloring),
                float(payload.border_width_px),
                float(payload.color_opacity),
                bytes(payload.color_data),
                float(payload.render_margin_fraction),
                bytes(payload.border_color_data),
            )
            return
        try:
            self._invoke(
                "setThumbnailLayerConfig",
                *args,
                str(payload.coloring),
                float(payload.border_width_px),
                float(payload.color_opacity),
                bytes(payload.color_data),
                float(payload.render_margin_fraction),
            )
        except TypeError as exc:
            if "setThumbnailLayerConfig" not in str(exc):
                raise
            try:
                self._invoke(
                    "setThumbnailLayerConfig",
                    *args,
                    str(payload.coloring),
                    float(payload.border_width_px),
                    float(payload.color_opacity),
                    bytes(payload.color_data),
                )
            except TypeError as legacy_exc:
                if "setThumbnailLayerConfig" not in str(legacy_exc):
                    raise
                try:
                    self._invoke(
                        "setThumbnailLayerConfig",
                        *args,
                        str(payload.coloring),
                        bytes(payload.color_data),
                    )
                except TypeError as older_legacy_exc:
                    if "setThumbnailLayerConfig" not in str(older_legacy_exc):
                        raise
                    self._invoke("setThumbnailLayerConfig", *args)

    def set_thumbnail_tile_layer_config(self, payload: ThumbnailTileLayerConfigPayload) -> None:
        self._invoke(
            "setThumbnailTileLayerConfig",
            bool(payload.enabled),
            str(payload.variant_id),
            str(payload.discovery_cache_id),
            int(payload.tile_size_px),
        )

    def set_thumbnail_tile_policy(self, payload: ThumbnailTilePolicyPayload) -> None:
        self._invoke(
            "setThumbnailTilePolicy",
            int(payload.max_cache_mb),
            int(payload.decode_workers),
            int(payload.max_decode_jobs),
        )

    def set_thumbnail_tiles(self, payload: list[ThumbnailTileEntry]) -> None:
        self._invoke(
            "setThumbnailTiles",
            [
                {
                    "lod": int(entry.lod),
                    "tile_x": int(entry.tile_x),
                    "tile_y": int(entry.tile_y),
                    "path": str(entry.path),
                    "mtime_ns": int(entry.mtime_ns),
                    "x_min": float(entry.x_min),
                    "x_max": float(entry.x_max),
                    "y_min": float(entry.y_min),
                    "y_max": float(entry.y_max),
                }
                for entry in payload
            ],
        )

    def set_thumbnail_tile_display_keys(self, payload: list[ThumbnailTileDisplayKeyEntry]) -> None:
        self._invoke(
            "setThumbnailTileDisplayKeys",
            [
                {"lod": int(entry.lod), "tile_x": int(entry.tile_x), "tile_y": int(entry.tile_y)}
                for entry in payload
            ],
        )

    def set_visible_thumbnail_metadata_ids(self, payload: list[int]) -> None:
        self._invoke("setVisibleThumbnailMetadataIds", [int(value) for value in payload])

    def set_hovered_thumbnail_metadata_id(self, metadata_id: int | None) -> None:
        self._invoke("setHoveredThumbnailMetadataId", -1 if metadata_id is None else int(metadata_id))

    def set_thumbnail_candidates(self, payload: list[int], *, target_px: int) -> None:
        _ = target_px
        self.set_visible_thumbnail_metadata_ids(payload)

    def set_thumbnail_plan(self, payload: ThumbnailPlanPayload) -> None:
        if self._renderer_api_version >= 4 and callable(getattr(self._item, "setThumbnailPlan", None)):
            self._invoke(
                "setThumbnailPlan",
                int(payload.revision),
                [int(value) for value in payload.metadata_ids],
                [int(value) for value in payload.forced_metadata_ids],
                [float(value) for value in payload.planning_viewport],
                int(payload.display_px),
                int(payload.decode_px),
            )
            return
        self.set_thumbnail_candidates(list(payload.metadata_ids), target_px=payload.decode_px)

    def set_thumbnail_runtime_policy(self, payload: ThumbnailRuntimePolicyPayload) -> None:
        if self._renderer_api_version < 4:
            return
        self._invoke(
            "setThumbnailRuntimePolicy",
            int(payload.cpu_cache_mb),
            int(payload.gpu_cache_mb),
            int(payload.atlas_page_px),
            int(payload.decode_workers),
            int(payload.upload_bytes_per_frame),
            float(payload.target_frame_ms),
        )

    def set_thumbnail_interaction_active(self, active: bool) -> None:
        if self._renderer_api_version < 4:
            return
        self._invoke("setThumbnailInteractionActive", bool(active))

    def set_thumbnail_sources(
        self,
        variant_id: str,
        payload: list[ThumbnailSourceEntry],
        *,
        revision: int = 0,
    ) -> None:
        entries = [
            {
                "metadata_id": int(entry.metadata_id),
                "path": str(entry.path),
                "mtime_ns": None if entry.mtime_ns is None else int(entry.mtime_ns),
                "target_px": int(entry.target_px),
                "missing": bool(entry.missing),
            }
            for entry in payload
        ]
        if self._renderer_api_version >= 4 and callable(getattr(self._item, "setThumbnailSourcesV4", None)):
            self._invoke("setThumbnailSourcesV4", int(revision), str(variant_id), entries)
            return
        self._invoke("setThumbnailSources", str(variant_id), entries)

    def set_thumbnail_atlas(
        self,
        variant_id: str,
        target_px: int,
        pages: list[ThumbnailAtlasPageEntry],
        sprites: list[ThumbnailAtlasSpriteEntry],
    ) -> None:
        self._invoke(
            "setThumbnailAtlas",
            str(variant_id),
            int(target_px),
            [
                {
                    "page_index": int(page.page_index),
                    "path": str(page.path),
                    "width": int(page.width),
                    "height": int(page.height),
                }
                for page in pages
            ],
            [
                {
                    "metadata_id": int(sprite.metadata_id),
                    "page_index": int(sprite.page_index),
                    "u": float(sprite.u),
                    "v": float(sprite.v),
                    "width": float(sprite.width),
                    "height": float(sprite.height),
                }
                for sprite in sprites
            ],
        )

    def clear_thumbnail_tiles(self) -> None:
        self._invoke("clearThumbnailTiles")

    def set_thumbnail_batch(self, payload: list[ThumbnailBatchEntry]) -> None:
        entries = [
            {
                "metadata_id": entry.metadata_id,
                "width": entry.width,
                "height": entry.height,
                "target_px": entry.target_px,
                "rgba": QtCore.QByteArray(bytes(entry.rgba)),
            }
            for entry in payload
        ]
        self._invoke("setThumbnailBatch", entries)

    def missing_thumbnail_metadata_ids(self, payload: list[int], *, target_px: int) -> list[int]:
        metadata_ids = [int(value) for value in payload]
        method = getattr(self._item, "missingThumbnailMetadataIds", None)
        if not callable(method):
            return metadata_ids
        raw_result = method(metadata_ids, int(target_px))
        if raw_result is None:
            return metadata_ids
        try:
            return [int(value) for value in raw_result]
        except TypeError:
            return metadata_ids

    def supports_thumbnail_layer(self) -> bool:
        return (
            callable(getattr(self._item, "setThumbnailLayerConfig", None))
            and callable(getattr(self._item, "setThumbnailBatch", None))
        )

    def supports_thumbnail_render_margin(self) -> bool:
        return bool(self._thumbnail_render_margin_supported)

    def supports_thumbnail_sources(self) -> bool:
        return bool(
            self._renderer_api_version >= 4
            and callable(getattr(self._item, "setThumbnailSourcesV4", None))
        )

    def supports_thumbnail_tiles(self) -> bool:
        return (
            callable(getattr(self._item, "setThumbnailTileLayerConfig", None))
            and callable(getattr(self._item, "setThumbnailTiles", None))
            and callable(getattr(self._item, "setThumbnailTileDisplayKeys", None))
        )
