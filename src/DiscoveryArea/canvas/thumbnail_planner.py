from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

import numpy as np

from DiscoveryArea.canvas.indexing import UniformGridIndex, Viewport


SPATIAL_ORDER_SAMPLE_CAP = 100_000
MINIMUM_THUMBNAIL_LIMIT = 1
MAXIMUM_THUMBNAIL_LIMIT = 5_000
PREFETCH_LIMIT_FRACTION = 0.5
MAXIMUM_PREFETCH_LIMIT = 1_000


class ThumbnailPlanCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class ThumbnailPlanRequest:
    """Immutable inputs for an off-thread viewport thumbnail plan."""

    scene_revision: int
    request_revision: int
    points: np.ndarray
    metadata_ids: np.ndarray
    visibility: np.ndarray
    spatial_index: UniformGridIndex | None
    viewport: tuple[float, float, float, float]
    planning_viewport: tuple[float, float, float, float]
    canvas_size: tuple[float, float]
    device_pixel_ratio: float
    display_px: int
    decode_px: int
    screen_coverage_budget: float
    thumbnail_limit: int = 100
    minimum_thumbnail_limit: int = MINIMUM_THUMBNAIL_LIMIT
    maximum_thumbnail_limit: int = MAXIMUM_THUMBNAIL_LIMIT
    # Kept for API-v4/source compatibility. Packing membership is no longer
    # reduced by a hidden runtime count limit.
    adaptive_limit: int | None = None
    forced_metadata_ids: tuple[int, ...] = ()
    thumbnail_lod_levels: np.ndarray | None = None
    world_bounds: tuple[float, float, float, float] | None = None
    zoom_px_per_world: float = 0.0
    close_zoom_threshold: float = float("inf")


@dataclass(frozen=True)
class ThumbnailViewportPlan:
    """Revisioned renderer plan whose arrays are safe to share without copies."""

    scene_revision: int
    request_revision: int
    viewport: tuple[float, float, float, float]
    planning_viewport: tuple[float, float, float, float]
    renderer_indices: np.ndarray
    visible_renderer_indices: np.ndarray
    forced_renderer_indices: np.ndarray
    metadata_ids: np.ndarray
    visible_metadata_ids: np.ndarray
    forced_metadata_ids: np.ndarray
    display_px: int
    decode_px: int
    desired_visible_count: int
    planned_visible_count: int
    planned_prefetch_count: int
    budget_suppressed_count: int
    estimated_screen_cost: float
    screen_budget_limit: int
    thumbnail_limit: int
    adaptive_limit: int
    minimum_spacing_px: float
    lod_level: int
    complete_visible: bool


def estimate_thumbnail_screen_cost(
    visible_count: int,
    *,
    display_px: float,
    device_pixel_ratio: float,
    canvas_size: tuple[float, float],
) -> float:
    """Return thumbnail pixel work as multiples of the physical canvas area."""

    width, height = canvas_size
    canvas_pixels = max(1.0, float(width) * float(height))
    dpr = max(0.5, float(device_pixel_ratio))
    marker_pixels = max(1.0, float(display_px)) ** 2 * dpr**2
    return max(0.0, float(visible_count)) * marker_pixels / canvas_pixels


def initial_thumbnail_limit(
    *,
    display_px: float,
    device_pixel_ratio: float,
    canvas_size: tuple[float, float],
    screen_coverage_budget: float,
) -> int:
    width, height = canvas_size
    canvas_pixels = max(1.0, float(width) * float(height))
    dpr = max(0.5, float(device_pixel_ratio))
    item_pixels = max(1.0, float(display_px)) ** 2 * dpr**2
    return max(1, int(np.floor(canvas_pixels * max(0.25, float(screen_coverage_budget)) / item_pixels)))


def build_thumbnail_viewport_plan(
    request: ThumbnailPlanRequest,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> ThumbnailViewportPlan:
    """Build a stable, spatially distributed thumbnail plan off the GUI thread."""

    is_cancelled = cancelled or (lambda: False)

    def checkpoint() -> None:
        if is_cancelled():
            raise ThumbnailPlanCancelled()

    checkpoint()
    points = np.asarray(request.points, dtype=np.float32)
    metadata_ids = np.asarray(request.metadata_ids, dtype=np.int64).reshape(-1)
    visibility = np.asarray(request.visibility, dtype=np.float32).reshape(-1)
    count = min(points.shape[0], metadata_ids.shape[0], visibility.shape[0])
    if count <= 0:
        empty = _readonly_int64(np.empty((0,), dtype=np.int64))
        return ThumbnailViewportPlan(
            scene_revision=int(request.scene_revision),
            request_revision=int(request.request_revision),
            viewport=request.viewport,
            planning_viewport=request.planning_viewport,
            renderer_indices=empty,
            visible_renderer_indices=empty,
            forced_renderer_indices=empty,
            metadata_ids=empty,
            visible_metadata_ids=empty,
            forced_metadata_ids=empty,
            display_px=max(1, int(request.display_px)),
            decode_px=max(1, int(request.decode_px)),
            desired_visible_count=0,
            planned_visible_count=0,
            planned_prefetch_count=0,
            budget_suppressed_count=0,
            estimated_screen_cost=0.0,
            screen_budget_limit=0,
            thumbnail_limit=_normalized_thumbnail_limit(request),
            adaptive_limit=0,
            minimum_spacing_px=0.0,
            lod_level=0,
            complete_visible=True,
        )

    if request.spatial_index is None:
        planning_candidates = np.arange(count, dtype=np.int64)
    else:
        x0, x1, y0, y1 = request.planning_viewport
        planning_candidates = request.spatial_index.visible_indices(Viewport(x0, x1, y0, y1))
        planning_candidates = planning_candidates[planning_candidates < count]
    checkpoint()
    if planning_candidates.size:
        candidate_points = points[planning_candidates]
        px0, px1, py0, py1 = request.planning_viewport
        planning_mask = (
            (candidate_points[:, 0] >= px0)
            & (candidate_points[:, 0] <= px1)
            & (candidate_points[:, 1] >= py0)
            & (candidate_points[:, 1] <= py1)
            & (visibility[planning_candidates] > 0.0)
        )
        planning_candidates = planning_candidates[planning_mask]
    checkpoint()

    vx0, vx1, vy0, vy1 = request.viewport
    if planning_candidates.size:
        candidate_points = points[planning_candidates]
        inside = (
            (candidate_points[:, 0] >= vx0)
            & (candidate_points[:, 0] <= vx1)
            & (candidate_points[:, 1] >= vy0)
            & (candidate_points[:, 1] <= vy1)
        )
        visible_indices = planning_candidates[inside]
    else:
        visible_indices = np.empty((0,), dtype=np.int64)

    desired_visible = int(visible_indices.size)
    estimated_cost = estimate_thumbnail_screen_cost(
        desired_visible,
        display_px=request.display_px,
        device_pixel_ratio=request.device_pixel_ratio,
        canvas_size=request.canvas_size,
    )
    thumbnail_limit = _normalized_thumbnail_limit(request)
    minimum_spacing_px = 0.0
    forced_indices = _forced_renderer_indices(
        metadata_ids,
        request.forced_metadata_ids,
        visibility=visibility,
    )
    forced_visible = (
        forced_indices[np.isin(forced_indices, visible_indices, assume_unique=False)]
        if forced_indices.size
        else forced_indices
    )
    lod_levels = (
        None
        if request.thumbnail_lod_levels is None
        else np.asarray(request.thumbnail_lod_levels, dtype=np.uint8).reshape(-1)
    )
    use_nested_lod = lod_levels is not None and lod_levels.shape[0] >= count
    selected_visible_regular, selected_margin = _select_fixed_budget_representatives(
        points,
        planning_candidates,
        visible_indices=visible_indices,
        lod_levels=lod_levels if use_nested_lod else None,
        viewport=request.viewport,
        world_bounds=request.world_bounds,
        thumbnail_limit=thumbnail_limit,
        forced_visible_indices=forced_visible,
    )
    checkpoint()
    selected_visible = _merge_unique_indices(
        forced_visible,
        selected_visible_regular,
    )
    selected_all = _merge_unique_indices(forced_indices, selected_visible, selected_margin)
    selected_metadata_ids = metadata_ids[selected_all] if selected_all.size else np.empty((0,), dtype=np.int64)
    visible_metadata_ids = (
        metadata_ids[selected_visible] if selected_visible.size else np.empty((0,), dtype=np.int64)
    )
    forced_selected = (
        forced_indices[np.isin(forced_indices, selected_all, assume_unique=False)]
        if forced_indices.size
        else forced_indices
    )
    forced_metadata = (
        metadata_ids[forced_selected] if forced_selected.size else np.empty((0,), dtype=np.int64)
    )
    complete_visible = desired_visible <= thumbnail_limit
    screen_capacity = thumbnail_limit
    planned_visible_count = min(desired_visible, int(selected_visible.size))
    return ThumbnailViewportPlan(
        scene_revision=int(request.scene_revision),
        request_revision=int(request.request_revision),
        viewport=request.viewport,
        planning_viewport=request.planning_viewport,
        renderer_indices=_readonly_int64(selected_all),
        visible_renderer_indices=_readonly_int64(selected_visible),
        forced_renderer_indices=_readonly_int64(forced_selected),
        metadata_ids=_readonly_int64(selected_metadata_ids),
        visible_metadata_ids=_readonly_int64(visible_metadata_ids),
        forced_metadata_ids=_readonly_int64(forced_metadata),
        display_px=max(1, int(request.display_px)),
        decode_px=max(1, int(request.decode_px)),
        desired_visible_count=desired_visible,
        planned_visible_count=planned_visible_count,
        planned_prefetch_count=int(selected_margin.size),
        budget_suppressed_count=max(0, desired_visible - planned_visible_count),
        estimated_screen_cost=estimated_cost,
        screen_budget_limit=screen_capacity,
        thumbnail_limit=thumbnail_limit,
        adaptive_limit=thumbnail_limit,
        minimum_spacing_px=minimum_spacing_px,
        lod_level=0,
        complete_visible=complete_visible,
    )


def _normalized_thumbnail_limit(request: ThumbnailPlanRequest) -> int:
    minimum = max(1, int(request.minimum_thumbnail_limit))
    maximum = max(minimum, int(request.maximum_thumbnail_limit))
    return max(
        minimum,
        min(maximum, int(request.thumbnail_limit)),
    )


def _select_fixed_budget_representatives(
    points: np.ndarray,
    planning_candidates: np.ndarray,
    *,
    visible_indices: np.ndarray,
    lod_levels: np.ndarray | None,
    viewport: tuple[float, float, float, float],
    world_bounds: tuple[float, float, float, float] | None,
    thumbnail_limit: int,
    forced_visible_indices: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Select a fixed number of stable, spatially distributed previews."""

    candidates = np.asarray(planning_candidates, dtype=np.int64).reshape(-1)
    visible = np.asarray(visible_indices, dtype=np.int64).reshape(-1)
    if candidates.size == 0:
        empty = np.empty((0,), dtype=np.int64)
        return empty, empty
    forced_visible = np.asarray(forced_visible_indices, dtype=np.int64).reshape(-1)
    regular_visible = visible[
        ~np.isin(visible, forced_visible, assume_unique=False)
    ]
    visible_budget = max(0, int(thumbnail_limit) - int(forced_visible.size))
    selected_visible = _ranked_thumbnail_prefix(
        points,
        regular_visible,
        lod_levels=lod_levels,
        viewport=viewport,
        world_bounds=world_bounds,
        limit=visible_budget,
    )
    margin = candidates[~np.isin(candidates, visible, assume_unique=False)]
    prefetch_limit = max(
        0,
        min(
            MAXIMUM_PREFETCH_LIMIT,
            int(np.ceil(float(thumbnail_limit) * PREFETCH_LIMIT_FRACTION)),
        ),
    )
    selected_margin = _ranked_thumbnail_prefix(
        points,
        margin,
        lod_levels=lod_levels,
        viewport=viewport,
        world_bounds=world_bounds,
        limit=prefetch_limit,
    )
    return selected_visible, selected_margin


def _ranked_thumbnail_prefix(
    points: np.ndarray,
    indices: np.ndarray,
    *,
    lod_levels: np.ndarray | None,
    viewport: tuple[float, float, float, float],
    world_bounds: tuple[float, float, float, float] | None,
    limit: int,
) -> np.ndarray:
    indices = np.asarray(indices, dtype=np.int64).reshape(-1)
    maximum = max(0, int(limit))
    if maximum <= 0 or indices.size == 0:
        return np.empty((0,), dtype=np.int64)
    if indices.size <= maximum:
        return indices.copy()
    if lod_levels is None:
        return _stable_spatial_order(
            points,
            indices,
            viewport,
            maximum_output=maximum,
        )[:maximum]
    bounds = world_bounds if world_bounds is not None else _finite_point_bounds(points)
    priorities = _stable_index_priority_uint64(indices)
    candidate_levels = lod_levels[indices]
    selected: list[np.ndarray] = []
    remaining = maximum
    for level in np.unique(candidate_levels):
        in_level = candidate_levels == level
        level_indices = indices[in_level]
        level_priorities = priorities[in_level]
        level_cell_priorities = _thumbnail_level_cell_priority(
            points,
            level_indices,
            level=int(level),
            world_bounds=bounds,
            stable_priorities=level_priorities,
            previous_indices=(
                np.concatenate(selected)
                if selected
                else np.empty((0,), dtype=np.int64)
            ),
        )
        order = np.lexsort(
            (
                level_indices,
                level_priorities,
                level_cell_priorities,
            )
        )
        if level_indices.size > remaining:
            selected.append(level_indices[order[:remaining]])
            break
        selected.append(level_indices[order])
        remaining -= int(level_indices.size)
        if remaining <= 0:
            break
    if not selected:
        return np.empty((0,), dtype=np.int64)
    return np.concatenate(selected).astype(np.int64, copy=False)


def _thumbnail_level_cell_priority(
    points: np.ndarray,
    indices: np.ndarray,
    *,
    level: int,
    world_bounds: tuple[float, float, float, float],
    stable_priorities: np.ndarray,
    previous_indices: np.ndarray,
) -> np.ndarray:
    indices = np.asarray(indices, dtype=np.int64).reshape(-1)
    if indices.size == 0:
        return np.empty((0,), dtype=np.int64)
    x0, x1, y0, y1 = [float(value) for value in world_bounds]
    span_x = max(1e-9, x1 - x0)
    span_y = max(1e-9, y1 - y0)
    divisions = 1 << max(0, min(15, int(level)))
    selected = points[indices]
    cell_x = np.clip(
        np.floor(((selected[:, 0] - x0) / span_x) * divisions).astype(np.int64),
        0,
        divisions - 1,
    )
    cell_y = np.clip(
        np.floor(((selected[:, 1] - y0) / span_y) * divisions).astype(np.int64),
        0,
        divisions - 1,
    )
    if divisions <= 1:
        return np.zeros(indices.size, dtype=np.int64)
    parent_divisions = max(1, divisions >> 1)
    parent_x = cell_x >> 1
    parent_y = cell_y >> 1
    parent_key = parent_y * parent_divisions + parent_x
    child_priority = _balanced_cell_priority(cell_x, cell_y, divisions)
    anchor_x = x0 + (parent_x.astype(np.float64) + 0.5) * (
        span_x / float(parent_divisions)
    )
    anchor_y = y0 + (parent_y.astype(np.float64) + 0.5) * (
        span_y / float(parent_divisions)
    )
    previous = np.asarray(previous_indices, dtype=np.int64).reshape(-1)
    if previous.size:
        previous_points = points[previous]
        previous_parent_x = np.clip(
            np.floor(
                ((previous_points[:, 0] - x0) / span_x) * parent_divisions
            ).astype(np.int64),
            0,
            parent_divisions - 1,
        )
        previous_parent_y = np.clip(
            np.floor(
                ((previous_points[:, 1] - y0) / span_y) * parent_divisions
            ).astype(np.int64),
            0,
            parent_divisions - 1,
        )
        previous_parent_key = (
            previous_parent_y * parent_divisions + previous_parent_x
        )
        previous_order = np.argsort(previous_parent_key, kind="stable")
        sorted_previous_keys = previous_parent_key[previous_order]
        first_for_parent = np.r_[
            True,
            sorted_previous_keys[1:] != sorted_previous_keys[:-1],
        ]
        anchor_keys = sorted_previous_keys[first_for_parent]
        anchor_points = previous_points[previous_order[first_for_parent]]
        anchor_positions = np.searchsorted(anchor_keys, parent_key)
        valid_anchor = anchor_positions < anchor_keys.size
        valid_anchor &= anchor_keys[
            np.minimum(anchor_positions, max(0, anchor_keys.size - 1))
        ] == parent_key
        anchor_x[valid_anchor] = anchor_points[anchor_positions[valid_anchor], 0]
        anchor_y[valid_anchor] = anchor_points[anchor_positions[valid_anchor], 1]
    distance_from_parent_representative = (
        np.square(selected[:, 0] - anchor_x)
        + np.square(selected[:, 1] - anchor_y)
    )
    grouped = np.lexsort(
        (
            np.asarray(stable_priorities, dtype=np.uint64),
            child_priority,
            -distance_from_parent_representative,
            parent_key,
        )
    )
    grouped_parent_keys = parent_key[grouped]
    starts = np.r_[
        0,
        np.flatnonzero(grouped_parent_keys[1:] != grouped_parent_keys[:-1]) + 1,
    ]
    repeated_starts = np.repeat(starts, np.diff(np.r_[starts, grouped.size]))
    round_in_parent = np.empty(grouped.size, dtype=np.int64)
    round_in_parent[grouped] = np.arange(grouped.size, dtype=np.int64) - repeated_starts
    parent_priority = _balanced_cell_priority(
        parent_x,
        parent_y,
        parent_divisions,
    )
    return round_in_parent * (parent_divisions**2) + parent_priority


def _finite_point_bounds(points: np.ndarray) -> tuple[float, float, float, float]:
    coordinates = np.asarray(points, dtype=np.float32)
    finite = coordinates[np.isfinite(coordinates[:, :2]).all(axis=1), :2]
    if finite.size == 0:
        return (0.0, 1.0, 0.0, 1.0)
    x_min = float(np.min(finite[:, 0]))
    x_max = float(np.max(finite[:, 0]))
    y_min = float(np.min(finite[:, 1]))
    y_max = float(np.max(finite[:, 1]))
    return (x_min, x_max, y_min, y_max)


def _forced_renderer_indices(
    metadata_ids: np.ndarray,
    forced_metadata_ids: Iterable[int],
    *,
    visibility: np.ndarray,
) -> np.ndarray:
    forced = np.asarray(tuple(dict.fromkeys(int(value) for value in forced_metadata_ids)), dtype=np.int64)
    if forced.size == 0 or metadata_ids.size == 0:
        return np.empty((0,), dtype=np.int64)
    order = np.argsort(metadata_ids, kind="stable")
    sorted_ids = metadata_ids[order]
    positions = np.searchsorted(sorted_ids, forced)
    valid = positions < sorted_ids.size
    positions = positions[valid]
    forced = forced[valid]
    matches = sorted_ids[positions] == forced
    indices = order[positions[matches]].astype(np.int64, copy=False)
    if indices.size:
        indices = indices[visibility[indices] > 0.0]
    return indices


def _stable_spatial_order(
    points: np.ndarray,
    indices: np.ndarray,
    viewport: tuple[float, float, float, float],
    *,
    maximum_output: int | None = None,
) -> np.ndarray:
    indices = np.asarray(indices, dtype=np.int64).reshape(-1)
    if indices.size <= 1:
        return indices.copy()
    if (
        maximum_output is not None
        and maximum_output <= SPATIAL_ORDER_SAMPLE_CAP
        and indices.size > SPATIAL_ORDER_SAMPLE_CAP
    ):
        priorities = _stable_index_priority(indices)
        selected_positions = np.argpartition(
            priorities,
            SPATIAL_ORDER_SAMPLE_CAP - 1,
        )[:SPATIAL_ORDER_SAMPLE_CAP]
        indices = indices[selected_positions]
    x0, x1, y0, y1 = [float(value) for value in viewport]
    span_x = max(1e-9, x1 - x0)
    span_y = max(1e-9, y1 - y0)
    divisions = 64
    selected = points[indices]
    cell_x = np.clip(
        np.floor(((selected[:, 0] - x0) / span_x) * divisions).astype(np.int64),
        0,
        divisions - 1,
    )
    cell_y = np.clip(
        np.floor(((selected[:, 1] - y0) / span_y) * divisions).astype(np.int64),
        0,
        divisions - 1,
    )
    cell_key = cell_y * divisions + cell_x
    stable_priority = _stable_index_priority(indices)
    grouped = np.lexsort((stable_priority, cell_key))
    grouped_keys = cell_key[grouped]
    starts = np.r_[0, np.flatnonzero(grouped_keys[1:] != grouped_keys[:-1]) + 1]
    repeated_starts = np.repeat(starts, np.diff(np.r_[starts, grouped.size]))
    round_in_cell = np.empty(grouped.size, dtype=np.int64)
    round_in_cell[grouped] = np.arange(grouped.size, dtype=np.int64) - repeated_starts
    cell_priority = _balanced_cell_priority(cell_x, cell_y, divisions)
    order = np.lexsort((stable_priority, cell_priority, round_in_cell))
    return indices[order]


def _merge_unique_indices(*arrays: np.ndarray) -> np.ndarray:
    output: list[int] = []
    seen: set[int] = set()
    for array in arrays:
        for raw in np.asarray(array, dtype=np.int64).reshape(-1).tolist():
            value = int(raw)
            if value in seen:
                continue
            seen.add(value)
            output.append(value)
    return np.asarray(output, dtype=np.int64)


def _readonly_int64(values: np.ndarray) -> np.ndarray:
    output = np.ascontiguousarray(np.asarray(values, dtype=np.int64).reshape(-1))
    output.setflags(write=False)
    return output


def _balanced_cell_priority(cell_x: np.ndarray, cell_y: np.ndarray, divisions: int) -> np.ndarray:
    """Order grid cells so early prefixes are distributed across the grid."""

    bits = max(1, int(np.ceil(np.log2(max(1, int(divisions))))))
    size = 1 << bits
    shift = size >> 2
    x = (np.asarray(cell_x, dtype=np.int64) ^ shift) & (size - 1)
    y = (np.asarray(cell_y, dtype=np.int64) ^ shift) & (size - 1)
    reversed_x = _bit_reverse(x, bits)
    reversed_y = _bit_reverse(y, bits)
    priority = np.zeros_like(reversed_x, dtype=np.int64)
    for bit in range(bits):
        priority |= ((reversed_x >> bit) & 1) << (2 * bit)
        priority |= ((reversed_y >> bit) & 1) << ((2 * bit) + 1)
    return priority


def _bit_reverse(values: np.ndarray, bits: int) -> np.ndarray:
    output = np.zeros_like(values, dtype=np.int64)
    working = np.asarray(values, dtype=np.int64).copy()
    for _ in range(bits):
        output = (output << 1) | (working & 1)
        working >>= 1
    return output


def _stable_index_priority(indices: np.ndarray) -> np.ndarray:
    return _stable_index_priority_uint64(indices).astype(np.int64)


def _stable_index_priority_uint64(indices: np.ndarray) -> np.ndarray:
    values = np.asarray(indices, dtype=np.uint64)
    values = (values ^ np.uint64(0x9E3779B97F4A7C15)) * np.uint64(0xBF58476D1CE4E5B9)
    values = (values ^ (values >> np.uint64(30))) * np.uint64(0x94D049BB133111EB)
    values = values ^ (values >> np.uint64(31))
    return values


def expand_thumbnail_viewport(
    viewport: tuple[float, float, float, float],
    margin_fraction: float,
) -> tuple[float, float, float, float]:
    x_min, x_max, y_min, y_max = [float(value) for value in viewport]
    margin = max(0.0, float(margin_fraction))
    if margin <= 0.0:
        return (x_min, x_max, y_min, y_max)
    span_x = max(1e-9, x_max - x_min)
    span_y = max(1e-9, y_max - y_min)
    return (
        x_min - (span_x * margin),
        x_max + (span_x * margin),
        y_min - (span_y * margin),
        y_max + (span_y * margin),
    )
