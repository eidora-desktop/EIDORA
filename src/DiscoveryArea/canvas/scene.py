from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math

import numpy as np

from DiscoveryArea.canvas.indexing import UniformGridIndex
from DiscoveryArea.canvas.lod import HYBRID_REPRESENTATIVE_POOL_LIMIT
from DiscoveryArea.canvas.thumbnail_lod import build_thumbnail_lod_levels


class SceneBuildCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class DensityLevel:
    width: int
    height: int
    counts: np.ndarray
    visibility_weights: np.ndarray
    weighted_premultiplied_color: np.ndarray
    rgba: np.ndarray
    world_bounds: tuple[float, float, float, float]

    @property
    def memory_bytes(self) -> int:
        return int(
            self.counts.nbytes
            + self.visibility_weights.nbytes
            + self.weighted_premultiplied_color.nbytes
            + self.rgba.nbytes
        )


class MetadataIdLookup:
    """Memory-bounded metadata ID to point-index lookup with dict-like get()."""

    def __init__(self, metadata_ids: np.ndarray) -> None:
        ids = np.asarray(metadata_ids, dtype=np.int64).reshape(-1)
        order = np.argsort(ids, kind="stable")
        self._ids = np.ascontiguousarray(ids[order], dtype=np.int64)
        self._indices = np.ascontiguousarray(order, dtype=np.int64)
        self._ids.setflags(write=False)
        self._indices.setflags(write=False)

    def get(self, metadata_id: int, default=None):
        if self._ids.size == 0:
            return default
        target = int(metadata_id)
        position = int(np.searchsorted(self._ids, target))
        if position >= self._ids.size or int(self._ids[position]) != target:
            return default
        return int(self._indices[position])

    @property
    def memory_bytes(self) -> int:
        return int(self._ids.nbytes + self._indices.nbytes)


@dataclass(frozen=True)
class CanvasSceneRequest:
    revision: int
    points: np.ndarray
    colors: np.ndarray | None
    size: float | np.ndarray
    visibility: np.ndarray | None
    metadata_ids: np.ndarray | None
    edge_colors: np.ndarray | str | None
    edge_widths: float | np.ndarray | None
    build_density: bool = True
    build_hybrid_lod: bool = True
    reuse_geometry: CanvasScene | None = None


@dataclass(frozen=True)
class CanvasScene:
    revision: int
    points: np.ndarray
    metadata_ids: np.ndarray
    metadata_lookup: MetadataIdLookup | dict[int, int]
    visibility: np.ndarray
    colors: np.ndarray | None
    edge_colors: np.ndarray | None
    edge_widths: np.ndarray | None
    point_size: float
    point_sizes: np.ndarray | None
    world_bounds: tuple[float, float, float, float]
    spatial_index: UniformGridIndex | None
    thumbnail_lod_levels: np.ndarray
    representative_indices: np.ndarray
    representative_mask: np.ndarray
    density_levels: tuple[DensityLevel, ...]

    @property
    def point_count(self) -> int:
        return int(self.points.shape[0])

    @property
    def memory_bytes(self) -> int:
        arrays = (
            self.points,
            self.metadata_ids,
            self.visibility,
            self.colors,
            self.edge_colors,
            self.edge_widths,
            self.point_sizes,
            self.thumbnail_lod_levels,
            self.representative_indices,
            self.representative_mask,
        )
        total = sum(int(array.nbytes) for array in arrays if isinstance(array, np.ndarray))
        total += sum(level.memory_bytes for level in self.density_levels)
        total += self.spatial_index.memory_bytes if self.spatial_index is not None else 0
        total += int(getattr(self.metadata_lookup, "memory_bytes", 0))
        return int(total)


def build_canvas_scene(
    request: CanvasSceneRequest,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> CanvasScene:
    is_cancelled = cancelled or (lambda: False)

    def checkpoint() -> None:
        if is_cancelled():
            raise SceneBuildCancelled()

    raw_points = np.asarray(request.points)
    if raw_points.ndim != 2 or raw_points.shape[1] < 2:
        raise ValueError("Canvas points must be an N x 2 numeric array")
    count = int(raw_points.shape[0])
    reuse = request.reuse_geometry if request.reuse_geometry is not None and request.reuse_geometry.point_count == count else None
    points = reuse.points if reuse is not None else _readonly_contiguous(raw_points[:, :2], np.float32)
    checkpoint()

    if reuse is not None:
        metadata_ids = reuse.metadata_ids
    else:
        metadata_ids = (
            np.arange(count, dtype=np.int64)
            if request.metadata_ids is None
            else _aligned_array(request.metadata_ids, count, np.int64, "metadata_ids")
        )
        metadata_ids = _readonly_contiguous(metadata_ids, np.int64)
    visibility = (
        np.ones(count, dtype=np.float32)
        if request.visibility is None
        else _aligned_array(request.visibility, count, np.float32, "visibility")
    )
    visibility = _readonly_contiguous(visibility, np.float32)
    colors = _optional_rows(request.colors, count, np.float32, "colors")
    edge_colors = _normalize_edge_colors(request.edge_colors, count)
    edge_widths = _optional_vector(request.edge_widths, count, np.float32, "edge_widths")
    if np.isscalar(request.size):
        point_size = max(0.5, float(request.size))
        point_sizes = None
    else:
        point_sizes = _aligned_array(request.size, count, np.float32, "point_sizes")
        point_sizes = _readonly_contiguous(point_sizes, np.float32)
        point_size = float(np.nanmax(point_sizes)) if point_sizes.size else 1.0
    checkpoint()

    if reuse is not None:
        bounds = reuse.world_bounds
        spatial_index = reuse.spatial_index
        metadata_lookup = reuse.metadata_lookup
        thumbnail_lod_levels = reuse.thumbnail_lod_levels
        if request.build_hybrid_lod:
            representative_indices = reuse.representative_indices
            representative_mask = reuse.representative_mask
        else:
            representative_indices = _readonly_contiguous(np.empty((0,), dtype=np.int64), np.int64)
            representative_mask = _readonly_contiguous(np.empty((0,), dtype=bool), bool)
    else:
        bounds = _world_bounds(points)
        span = max(bounds[1] - bounds[0], bounds[3] - bounds[2], 1e-6)
        spatial_index = UniformGridIndex(points, tile_size=span / 128.0) if count else None
        checkpoint()
        thumbnail_lod_levels = build_thumbnail_lod_levels(
            points,
            world_bounds=bounds,
            checkpoint=checkpoint,
        )
        if count <= 10_000:
            metadata_lookup = {
                int(metadata_id): int(index) for index, metadata_id in enumerate(metadata_ids.tolist())
            }
        else:
            metadata_lookup = MetadataIdLookup(metadata_ids)
        if request.build_hybrid_lod:
            representative_indices = _representative_indices(
                spatial_index,
                count,
                limit=HYBRID_REPRESENTATIVE_POOL_LIMIT,
            )
            representative_mask = np.zeros(count, dtype=bool)
            representative_mask[representative_indices] = True
            representative_mask = _readonly_contiguous(representative_mask, bool)
        else:
            representative_indices = _readonly_contiguous(np.empty((0,), dtype=np.int64), np.int64)
            representative_mask = _readonly_contiguous(np.empty((0,), dtype=bool), bool)
    checkpoint()
    density_levels: tuple[DensityLevel, ...] = ()
    unusually_expensive_small_scene = count >= 2_000 and point_size >= 20.0
    if request.build_hybrid_lod and request.build_density and (
        count >= 25_000 or unusually_expensive_small_scene
    ):
        resolutions = (128, 256, 512) if count < 250_000 else (128, 256, 512, 1024)
        density_levels = tuple(
            _build_density_level(points, visibility, colors, bounds, resolution)
            for resolution in resolutions
        )
        checkpoint()

    return CanvasScene(
        revision=int(request.revision),
        points=points,
        metadata_ids=metadata_ids,
        metadata_lookup=metadata_lookup,
        visibility=visibility,
        colors=colors,
        edge_colors=edge_colors,
        edge_widths=edge_widths,
        point_size=point_size,
        point_sizes=point_sizes,
        world_bounds=bounds,
        spatial_index=spatial_index,
        thumbnail_lod_levels=thumbnail_lod_levels,
        representative_indices=representative_indices,
        representative_mask=representative_mask,
        density_levels=density_levels,
    )


def _aligned_array(value, count: int, dtype, name: str) -> np.ndarray:
    array = np.asarray(value, dtype=dtype).reshape(-1)
    if array.shape[0] != count:
        raise ValueError(f"{name} must contain exactly {count} entries")
    return array


def _optional_vector(value, count: int, dtype, name: str) -> np.ndarray | None:
    if value is None:
        return None
    if np.isscalar(value):
        return _readonly_contiguous(np.full(count, value, dtype=dtype), dtype)
    return _readonly_contiguous(_aligned_array(value, count, dtype, name), dtype)


def _optional_rows(value, count: int, dtype, name: str) -> np.ndarray | None:
    if value is None:
        return None
    array = np.asarray(value, dtype=dtype)
    if array.ndim == 1 and array.shape[0] in {3, 4}:
        array = np.broadcast_to(array, (count, array.shape[0]))
    if array.ndim != 2 or array.shape[0] != count:
        raise ValueError(f"{name} must contain exactly {count} rows")
    return _readonly_contiguous(array, dtype)


def _normalize_edge_colors(value, count: int) -> np.ndarray | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) not in {6, 8}:
            raise ValueError("edge color strings must use #RRGGBB or #RRGGBBAA")
        channels = [int(text[index : index + 2], 16) / 255.0 for index in range(0, len(text), 2)]
        if len(channels) == 3:
            channels.append(1.0)
        return _readonly_contiguous(np.broadcast_to(np.asarray(channels), (count, 4)), np.float32)
    return _optional_rows(value, count, np.float32, "edge_colors")


def _readonly_contiguous(value, dtype) -> np.ndarray:
    array = np.ascontiguousarray(np.asarray(value, dtype=dtype)).view()
    array.setflags(write=False)
    return array


def _world_bounds(points: np.ndarray) -> tuple[float, float, float, float]:
    if points.shape[0] == 0:
        return (0.0, 1.0, 0.0, 1.0)
    finite = np.isfinite(points).all(axis=1)
    if not np.any(finite):
        return (0.0, 1.0, 0.0, 1.0)
    valid = points[finite]
    x_min = float(np.min(valid[:, 0]))
    x_max = float(np.max(valid[:, 0]))
    y_min = float(np.min(valid[:, 1]))
    y_max = float(np.max(valid[:, 1]))
    if math.isclose(x_min, x_max, abs_tol=1e-9):
        x_max = x_min + 1.0
    if math.isclose(y_min, y_max, abs_tol=1e-9):
        y_max = y_min + 1.0
    return (x_min, x_max, y_min, y_max)


def _representative_indices(index: UniformGridIndex | None, count: int, limit: int) -> np.ndarray:
    if count <= 0:
        result = np.empty((0,), dtype=np.int64)
    elif count <= limit or index is None:
        result = np.arange(count, dtype=np.int64)
    else:
        order = index.spatial_order
        positions = np.linspace(0, order.size - 1, num=limit, dtype=np.int64)
        result = np.ascontiguousarray(order[positions], dtype=np.int64)
    result.setflags(write=False)
    return result


def _build_density_level(
    points: np.ndarray,
    visibility: np.ndarray,
    colors: np.ndarray | None,
    bounds: tuple[float, float, float, float],
    resolution: int,
) -> DensityLevel:
    x_min, x_max, y_min, y_max = bounds
    width = height = max(8, int(resolution))
    finite = np.isfinite(points).all(axis=1)
    weights = np.clip(visibility.astype(np.float64, copy=False), 0.0, 1.0)
    valid = finite & (weights > 0.0)
    counts = np.zeros((height, width), dtype=np.uint32)
    visibility_weights = np.zeros((height, width), dtype=np.float32)
    weighted_color = np.zeros((height, width, 3), dtype=np.float32)
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    if np.any(finite):
        finite_points = points[finite].astype(np.float64, copy=False)
        finite_ix = np.clip(
            ((finite_points[:, 0] - x_min) / max(1e-12, x_max - x_min) * (width - 1)).astype(np.int64),
            0,
            width - 1,
        )
        finite_iy = np.clip(
            ((y_max - finite_points[:, 1]) / max(1e-12, y_max - y_min) * (height - 1)).astype(np.int64),
            0,
            height - 1,
        )
        finite_flat = finite_iy * width + finite_ix
        counts.reshape(-1)[:] = np.bincount(finite_flat, minlength=width * height).astype(np.uint32)
    if np.any(valid):
        valid_points = points[valid].astype(np.float64, copy=False)
        valid_weights = weights[valid]
        ix = np.clip(((valid_points[:, 0] - x_min) / max(1e-12, x_max - x_min) * (width - 1)).astype(np.int64), 0, width - 1)
        iy = np.clip(((y_max - valid_points[:, 1]) / max(1e-12, y_max - y_min) * (height - 1)).astype(np.int64), 0, height - 1)
        flat = iy * width + ix
        cell_weights = np.bincount(flat, weights=valid_weights, minlength=width * height)
        visibility_weights.reshape(-1)[:] = cell_weights.astype(np.float32)
        mean_occupancy = max(1.0, float(np.sum(valid_weights)) / float(width * height))
        alpha = 1.0 - np.exp(-cell_weights / mean_occupancy)
        if colors is None or colors.shape[0] != points.shape[0]:
            rgb = np.full((width * height, 3), 0.12, dtype=np.float64)
            weighted_color.reshape(-1, 3)[:] = (cell_weights[:, None] * 0.12).astype(np.float32)
        else:
            source_rgb = np.clip(colors[valid, :3].astype(np.float64, copy=False), 0.0, 1.0)
            source_alpha = (
                np.clip(colors[valid, 3].astype(np.float64, copy=False), 0.0, 1.0)
                if colors.shape[1] >= 4
                else np.ones(valid_weights.shape, dtype=np.float64)
            )
            rgb = np.zeros((width * height, 3), dtype=np.float64)
            nonzero = cell_weights > 0.0
            for channel in range(3):
                totals = np.bincount(
                    flat,
                    weights=source_rgb[:, channel] * source_alpha * valid_weights,
                    minlength=width * height,
                )
                weighted_color.reshape(-1, 3)[:, channel] = totals.astype(np.float32)
                rgb[nonzero, channel] = totals[nonzero] / cell_weights[nonzero]
        premultiplied = rgb * alpha[:, None]
        rgba.reshape(-1, 4)[:, :3] = np.rint(np.clip(premultiplied, 0.0, 1.0) * 255.0).astype(np.uint8)
        rgba.reshape(-1, 4)[:, 3] = np.rint(np.clip(alpha, 0.0, 1.0) * 255.0).astype(np.uint8)
    for array in (counts, visibility_weights, weighted_color, rgba):
        array.setflags(write=False)
    return DensityLevel(
        width=width,
        height=height,
        counts=counts,
        visibility_weights=visibility_weights,
        weighted_premultiplied_color=weighted_color,
        rgba=rgba,
        world_bounds=bounds,
    )
