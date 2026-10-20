from __future__ import annotations

from collections.abc import Callable
import math

import numpy as np


THUMBNAIL_LOD_MAX_DEPTH = 12
THUMBNAIL_LOD_COMPLETE_LEVEL = THUMBNAIL_LOD_MAX_DEPTH + 1
THUMBNAIL_LOD_CENTERED_MAX_DEPTH = 4


def build_thumbnail_lod_levels(
    points: np.ndarray,
    *,
    world_bounds: tuple[float, float, float, float],
    max_depth: int = THUMBNAIL_LOD_MAX_DEPTH,
    checkpoint: Callable[[], None] | None = None,
) -> np.ndarray:
    """Assign each point to the first nested world-grid level that reveals it.

    The hierarchy is immutable for a scene. A deeper level is always a strict
    superset of every shallower level, so zooming can add previews without
    replacing previews that were already visible.
    """

    coordinates = np.asarray(points, dtype=np.float32)
    count = int(coordinates.shape[0])
    depth_limit = max(0, min(15, int(max_depth)))
    complete_level = depth_limit + 1
    levels = np.full(count, complete_level, dtype=np.uint8)
    if count <= 0:
        return _readonly_uint8(levels)

    finite_indices = np.flatnonzero(np.isfinite(coordinates[:, :2]).all(axis=1)).astype(
        np.int64,
        copy=False,
    )
    if finite_indices.size == 0:
        return _readonly_uint8(levels)

    x_min, x_max, y_min, y_max = [float(value) for value in world_bounds]
    span_x = max(1e-12, x_max - x_min)
    span_y = max(1e-12, y_max - y_min)
    divisions = 1 << depth_limit
    finite_points = coordinates[finite_indices, :2].astype(np.float64, copy=False)
    cell_x = np.clip(
        np.floor(((finite_points[:, 0] - x_min) / span_x) * divisions),
        0,
        divisions - 1,
    ).astype(np.uint64)
    cell_y = np.clip(
        np.floor(((finite_points[:, 1] - y_min) / span_y) * divisions),
        0,
        divisions - 1,
    ).astype(np.uint64)
    morton = _morton_keys(cell_x, cell_y, depth_limit)
    order = np.argsort(morton, kind="stable")
    sorted_morton = morton[order]
    sorted_indices = finite_indices[order]
    sorted_points = finite_points[order]
    sorted_cell_x = cell_x[order]
    sorted_cell_y = cell_y[order]
    if checkpoint is not None:
        checkpoint()

    for depth in range(depth_limit + 1):
        shift = 2 * (depth_limit - depth)
        coarse_keys = sorted_morton if shift == 0 else sorted_morton >> np.uint64(shift)
        starts = np.r_[0, np.flatnonzero(coarse_keys[1:] != coarse_keys[:-1]) + 1]
        ends = np.r_[starts[1:], coarse_keys.size]
        assigned = levels[sorted_indices] != complete_level
        cell_already_represented = np.logical_or.reduceat(assigned, starts)
        available_cells = ~cell_already_represented
        if depth <= THUMBNAIL_LOD_CENTERED_MAX_DEPTH:
            coordinate_shift = depth_limit - depth
            divisions_at_depth = 1 << depth
            group_x = sorted_cell_x[starts] >> np.uint64(coordinate_shift)
            group_y = sorted_cell_y[starts] >> np.uint64(coordinate_shift)
            cell_width = span_x / float(divisions_at_depth)
            cell_height = span_y / float(divisions_at_depth)
            representative_positions = []
            for start, end, group_cell_x, group_cell_y, available in zip(
                starts,
                ends,
                group_x,
                group_y,
                available_cells,
                strict=True,
            ):
                if not available:
                    continue
                center_x = x_min + (float(group_cell_x) + 0.5) * cell_width
                center_y = y_min + (float(group_cell_y) + 0.5) * cell_height
                group_points = sorted_points[start:end]
                distances = (
                    np.square(group_points[:, 0] - center_x)
                    + np.square(group_points[:, 1] - center_y)
                )
                representative_positions.append(int(start) + int(np.argmin(distances)))
            representatives = sorted_indices[
                np.asarray(representative_positions, dtype=np.int64)
            ]
        else:
            representatives = sorted_indices[starts[available_cells]]
        levels[representatives] = depth
        if checkpoint is not None:
            checkpoint()

    return _readonly_uint8(levels)


def thumbnail_lod_level_for_view(
    *,
    world_bounds: tuple[float, float, float, float],
    viewport: tuple[float, float, float, float],
    canvas_size: tuple[float, float],
    minimum_spacing_px: float,
    max_depth: int = THUMBNAIL_LOD_MAX_DEPTH,
) -> int:
    """Return the nested detail level needed for the current screen scale."""

    x_min, x_max, y_min, y_max = [float(value) for value in world_bounds]
    vx_min, vx_max, vy_min, vy_max = [float(value) for value in viewport]
    world_span_x = max(1e-12, x_max - x_min)
    world_span_y = max(1e-12, y_max - y_min)
    view_span_x = max(1e-12, vx_max - vx_min)
    view_span_y = max(1e-12, vy_max - vy_min)
    width = max(1.0, float(canvas_size[0]))
    height = max(1.0, float(canvas_size[1]))
    spacing = max(1.0, float(minimum_spacing_px))
    desired_divisions = max(
        world_span_x * (width / view_span_x) / spacing,
        world_span_y * (height / view_span_y) / spacing,
        1.0,
    )
    level = int(math.ceil(math.log2(desired_divisions)))
    return max(0, min(max(0, int(max_depth)), level))


def _morton_keys(cell_x: np.ndarray, cell_y: np.ndarray, bits: int) -> np.ndarray:
    keys = np.zeros(cell_x.shape[0], dtype=np.uint64)
    for bit in range(max(0, int(bits))):
        keys |= ((cell_x >> np.uint64(bit)) & np.uint64(1)) << np.uint64(2 * bit)
        keys |= ((cell_y >> np.uint64(bit)) & np.uint64(1)) << np.uint64((2 * bit) + 1)
    return keys


def _readonly_uint8(values: np.ndarray) -> np.ndarray:
    output = np.ascontiguousarray(np.asarray(values, dtype=np.uint8).reshape(-1))
    output.setflags(write=False)
    return output
