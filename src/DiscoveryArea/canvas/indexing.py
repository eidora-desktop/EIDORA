from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np


@dataclass(frozen=True)
class Viewport:
    x_min: float
    x_max: float
    y_min: float
    y_max: float


class UniformGridIndex:
    """Compact vectorized world-space grid index.

    The public name is retained for canvas compatibility.  Unlike the former
    dict-of-Python-lists implementation, construction is performed with NumPy
    sorting and the cells are stored in flat contiguous arrays.  Memory use is
    therefore proportional to the point count without creating one Python
    object per point.
    """

    def __init__(self, points: np.ndarray, tile_size: float = 1.0) -> None:
        raw = np.asarray(points, dtype=np.float32)
        self._points = raw[:, :2] if raw.ndim == 2 and raw.shape[1] >= 2 else np.empty((0, 2), dtype=np.float32)
        self._tile_size = max(float(tile_size), 1e-6)
        self._origin_x = 0.0
        self._origin_y = 0.0
        self._morton_bits = 1
        self._max_cell_x = -1
        self._max_cell_y = -1
        self._order = np.empty((0,), dtype=np.int64)
        self._unique_keys = np.empty((0,), dtype=np.int64)
        self._cell_starts = np.empty((0,), dtype=np.int64)
        self._cell_counts = np.empty((0,), dtype=np.int64)
        self._build()

    @property
    def point_count(self) -> int:
        return int(self._points.shape[0])

    @property
    def memory_bytes(self) -> int:
        return int(
            self._order.nbytes
            + self._unique_keys.nbytes
            + self._cell_starts.nbytes
            + self._cell_counts.nbytes
        )

    @property
    def spatial_order(self) -> np.ndarray:
        """Read-only point indices grouped in deterministic spatial-cell order."""

        view = self._order.view()
        view.setflags(write=False)
        return view

    def _build(self) -> None:
        if self._points.shape[0] == 0:
            return
        finite = np.isfinite(self._points).all(axis=1)
        finite_indices = np.flatnonzero(finite).astype(np.int64, copy=False)
        if finite_indices.size == 0:
            return
        finite_points = self._points[finite_indices]
        self._origin_x = float(np.min(finite_points[:, 0]))
        self._origin_y = float(np.min(finite_points[:, 1]))
        cell_x = np.floor((finite_points[:, 0].astype(np.float64) - self._origin_x) / self._tile_size).astype(
            np.int64
        )
        cell_y = np.floor((finite_points[:, 1].astype(np.float64) - self._origin_y) / self._tile_size).astype(
            np.int64
        )
        self._max_cell_x = int(np.max(cell_x))
        self._max_cell_y = int(np.max(cell_y))
        self._morton_bits = max(1, max(self._max_cell_x, self._max_cell_y).bit_length())
        keys = self._morton_keys(cell_x, cell_y)
        local_order = np.argsort(keys, kind="stable")
        sorted_keys = keys[local_order]
        self._order = np.ascontiguousarray(finite_indices[local_order], dtype=np.int64)
        unique_keys, starts, counts = np.unique(sorted_keys, return_index=True, return_counts=True)
        self._unique_keys = np.ascontiguousarray(unique_keys, dtype=np.int64)
        self._cell_starts = np.ascontiguousarray(starts, dtype=np.int64)
        self._cell_counts = np.ascontiguousarray(counts, dtype=np.int64)

    def _query_cell_spans(self, viewport: Viewport, margin: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        if self._unique_keys.size == 0:
            empty = np.empty((0,), dtype=np.int64)
            return empty, empty
        x0 = math.floor((float(viewport.x_min) - margin - self._origin_x) / self._tile_size)
        x1 = math.floor((float(viewport.x_max) + margin - self._origin_x) / self._tile_size)
        y0 = math.floor((float(viewport.y_min) - margin - self._origin_y) / self._tile_size)
        y1 = math.floor((float(viewport.y_max) + margin - self._origin_y) / self._tile_size)
        x0 = max(0, x0)
        y0 = max(0, y0)
        x1 = min(self._max_cell_x, x1)
        y1 = min(self._max_cell_y, y1)
        if x1 < x0 or y1 < y0:
            empty = np.empty((0,), dtype=np.int64)
            return empty, empty
        xs = np.arange(x0, x1 + 1, dtype=np.int64)
        ys = np.arange(y0, y1 + 1, dtype=np.int64)
        requested_x = np.broadcast_to(xs[:, None], (xs.size, ys.size)).reshape(-1)
        requested_y = np.broadcast_to(ys[None, :], (xs.size, ys.size)).reshape(-1)
        requested = self._morton_keys(requested_x, requested_y)
        positions = np.searchsorted(self._unique_keys, requested)
        valid = positions < self._unique_keys.size
        if not np.any(valid):
            empty = np.empty((0,), dtype=np.int64)
            return empty, empty
        positions = positions[valid]
        requested = requested[valid]
        positions = positions[self._unique_keys[positions] == requested]
        return self._cell_starts[positions], self._cell_counts[positions]

    def _morton_keys(self, cell_x: np.ndarray, cell_y: np.ndarray) -> np.ndarray:
        """Interleave cell bits into deterministic Z-order keys."""

        x = np.asarray(cell_x, dtype=np.int64)
        y = np.asarray(cell_y, dtype=np.int64)
        keys = np.zeros(x.shape, dtype=np.int64)
        for bit in range(self._morton_bits):
            keys |= ((x >> bit) & 1) << (bit * 2)
            keys |= ((y >> bit) & 1) << (bit * 2 + 1)
        return keys

    def visible_count(self, viewport: Viewport, margin: float = 0.0) -> int:
        """Return a conservative cell-level visible count without allocating indices."""

        _starts, counts = self._query_cell_spans(viewport, margin)
        return int(np.sum(counts, dtype=np.int64)) if counts.size else 0

    def visible_indices(self, viewport: Viewport, margin: float = 0.0) -> np.ndarray:
        starts, counts = self._query_cell_spans(viewport, margin)
        if starts.size == 0:
            return np.empty((0,), dtype=np.int64)
        total = int(np.sum(counts, dtype=np.int64))
        result = np.empty((total,), dtype=np.int64)
        cursor = 0
        for start, count in zip(starts.tolist(), counts.tolist(), strict=True):
            next_cursor = cursor + int(count)
            result[cursor:next_cursor] = self._order[int(start) : int(start) + int(count)]
            cursor = next_cursor
        result.sort()
        return result

    def candidate_indices_for_world_point(self, x: float, y: float, radius: float) -> np.ndarray:
        radius = max(0.0, float(radius))
        return self.visible_indices(Viewport(x - radius, x + radius, y - radius, y + radius))

    def nearest_index(self, x: float, y: float, radius: float) -> int | None:
        candidates = self.candidate_indices_for_world_point(x, y, radius)
        if candidates.size == 0:
            return None
        offsets = self._points[candidates].astype(np.float64) - np.asarray([x, y], dtype=np.float64)
        distances_squared = np.einsum("ij,ij->i", offsets, offsets)
        nearest = int(np.argmin(distances_squared))
        if float(distances_squared[nearest]) > float(radius) ** 2:
            return None
        return int(candidates[nearest])

    def indices_in_polygon_bounds(self, polygon: np.ndarray) -> np.ndarray:
        polygon = np.asarray(polygon)
        if polygon.ndim != 2 or polygon.shape[0] < 3:
            return np.empty((0,), dtype=np.int64)
        x_min = float(np.min(polygon[:, 0]))
        x_max = float(np.max(polygon[:, 0]))
        y_min = float(np.min(polygon[:, 1]))
        y_max = float(np.max(polygon[:, 1]))
        return self.visible_indices(Viewport(x_min, x_max, y_min, y_max))


def points_in_polygon(points: np.ndarray, polygon: np.ndarray) -> np.ndarray:
    """Vectorized point-in-polygon mask using ray casting."""
    pts = np.asarray(points, dtype=np.float64)
    poly = np.asarray(polygon, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[0] == 0 or poly.ndim != 2 or poly.shape[0] < 3:
        return np.zeros((pts.shape[0] if pts.ndim == 2 else 0,), dtype=bool)
    x = pts[:, 0]
    y = pts[:, 1]
    x1 = poly[:, 0]
    y1 = poly[:, 1]
    x2 = np.roll(x1, -1)
    y2 = np.roll(y1, -1)
    inside = np.zeros((pts.shape[0],), dtype=bool)
    for i in range(poly.shape[0]):
        yi = y1[i]
        yj = y2[i]
        xi = x1[i]
        xj = x2[i]
        intersects = ((yi > y) != (yj > y)) & (x < (xj - xi) * (y - yi) / ((yj - yi) + 1e-12) + xi)
        inside ^= intersects
    return inside
