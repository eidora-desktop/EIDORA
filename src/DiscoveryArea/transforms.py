from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from collections.abc import Callable

import numpy as np

from DiscoveryArea.canvas.viewport import normalize_viewport_aspect_preset, viewport_aspect_ratio


class TransformCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class TransformSettings:
    snap_percent: int = 0
    grid_aspect_preset: str = "free"
    minimum_spacing: float = 0.0

    def normalized(self) -> "TransformSettings":
        minimum_spacing = float(self.minimum_spacing)
        if not math.isfinite(minimum_spacing):
            minimum_spacing = 0.0
        return TransformSettings(
            snap_percent=max(0, min(100, int(self.snap_percent))),
            grid_aspect_preset=normalize_viewport_aspect_preset(self.grid_aspect_preset),
            minimum_spacing=max(0.0, min(1.0, minimum_spacing)),
        )

    def to_dict(self) -> dict[str, object]:
        value = self.normalized()
        return {
            "snap_percent": value.snap_percent,
            "grid_aspect_preset": value.grid_aspect_preset,
            "minimum_spacing": value.minimum_spacing,
        }

    def to_preset_dict(self) -> dict[str, object]:
        return self.to_dict()

    @classmethod
    def from_dict(cls, payload: object) -> "TransformSettings":
        data = payload if isinstance(payload, dict) else {}
        return cls(
            snap_percent=int(data.get("snap_percent", 0)),
            grid_aspect_preset=str(data.get("grid_aspect_preset", "free")),
            minimum_spacing=float(data.get("minimum_spacing", 0.0)),
        ).normalized()

    @classmethod
    def from_preset_dict(cls, payload: object) -> "TransformSettings":
        return cls.from_dict(payload)


@dataclass(frozen=True)
class TransformResult:
    metadata_ids: np.ndarray
    grid_coordinates: np.ndarray
    coordinates: np.ndarray
    settings: TransformSettings
    source_fingerprint: str
    grid_aspect_ratio: float = 1.0
    arrangement_ref: str | None = None


def source_fingerprint(coordinates: np.ndarray, metadata_ids: np.ndarray) -> str:
    points, ids = _validated_inputs(coordinates, metadata_ids)
    digest = hashlib.sha256()
    digest.update(np.ascontiguousarray(ids, dtype=np.int64).tobytes())
    digest.update(np.ascontiguousarray(points, dtype=np.float32).tobytes())
    return digest.hexdigest()


def transform_coordinates(
    coordinates: np.ndarray,
    metadata_ids: np.ndarray,
    settings: TransformSettings,
    *,
    cached_grid: np.ndarray | None = None,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[int], None] | None = None,
) -> TransformResult:
    points, ids = _validated_inputs(coordinates, metadata_ids)
    normalized = settings.normalized()
    check = cancelled or (lambda: False)
    if check():
        raise TransformCancelled
    fingerprint = source_fingerprint(points, ids)
    grid_aspect = resolved_grid_aspect_ratio(points, normalized.grid_aspect_preset)
    if points.shape[0] <= 1:
        grid = points.copy()
    elif cached_grid is not None and np.asarray(cached_grid).shape == points.shape:
        grid = np.ascontiguousarray(cached_grid, dtype=np.float32)
    else:
        grid = compact_grid_coordinates(points, ids, aspect_ratio=grid_aspect, cancelled=check)
    amount = normalized.snap_percent / 100.0
    arranged = points.copy() if amount == 0.0 else (
        points.astype(np.float64) * (1.0 - amount) + grid.astype(np.float64) * amount
    ).astype(np.float32)
    if normalized.minimum_spacing > 0.0 and arranged.shape[0] > 1:
        # A complete grid already has a meaningful cell assignment. Preserve
        # that assignment and enlarge its spacing uniformly when necessary.
        if normalized.snap_percent == 100:
            arranged = _expand_grid_clearance(arranged, normalized.minimum_spacing)
        else:
            arranged = enforce_minimum_spacing(
                arranged,
                ids,
                normalized.minimum_spacing,
                cancelled=check,
                progress=progress,
            )
    if check():
        raise TransformCancelled
    return TransformResult(
        metadata_ids=ids.copy(),
        grid_coordinates=np.ascontiguousarray(grid, dtype=np.float32),
        coordinates=np.ascontiguousarray(arranged, dtype=np.float32),
        settings=normalized,
        source_fingerprint=fingerprint,
        grid_aspect_ratio=grid_aspect,
    )


def compact_grid_coordinates(
    coordinates: np.ndarray,
    metadata_ids: np.ndarray,
    *,
    aspect_ratio: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> np.ndarray:
    points, ids = _validated_inputs(coordinates, metadata_ids)
    count = points.shape[0]
    if count <= 1:
        return points.copy()
    check = cancelled or (lambda: False)
    span = np.ptp(points.astype(np.float64), axis=0)
    aspect = (
        resolved_grid_aspect_ratio(points, "free")
        if aspect_ratio is None
        else float(aspect_ratio)
    )
    if not math.isfinite(aspect) or aspect <= 0.0:
        raise ValueError("Grid aspect ratio must be finite and greater than zero.")
    aspect = max(1.0 / count, min(float(count), aspect))
    columns = max(1, int(math.ceil(math.sqrt(count * aspect))))
    rows = max(1, int(math.ceil(count / columns)))
    while columns > 1 and (columns - 1) * rows >= count:
        columns -= 1
    cell_columns, cell_rows = np.meshgrid(
        np.arange(columns, dtype=np.float64),
        np.arange(rows, dtype=np.float64),
    )
    cell_xy = np.column_stack((cell_columns.reshape(-1), cell_rows.reshape(-1)))
    # Remove surplus cells symmetrically so incomplete grids do not lean to a corner.
    if cell_xy.shape[0] > count:
        center = np.asarray([(columns - 1) * 0.5, (rows - 1) * 0.5])
        order = np.lexsort((cell_xy[:, 0], cell_xy[:, 1], np.sum((cell_xy - center) ** 2, axis=1)))
        cell_xy = cell_xy[np.sort(order[:count])]
    assignment = np.empty(count, dtype=np.int64)
    _recursive_assign(points.astype(np.float64), ids, np.arange(count), cell_xy, np.arange(count), assignment, check)
    assigned = cell_xy[assignment]
    assigned -= (np.min(assigned, axis=0) + np.max(assigned, axis=0)) * 0.5
    longest = float(np.max(np.ptp(points.astype(np.float64), axis=0)))
    if longest <= 1e-12:
        longest = 1.0
    grid_longest = float(np.max(np.ptp(assigned, axis=0)))
    if grid_longest > 1e-12:
        assigned *= longest / grid_longest
    assigned += (np.min(points, axis=0) + np.max(points, axis=0)) * 0.5
    return assigned.astype(np.float32)


def resolved_grid_aspect_ratio(coordinates: np.ndarray, preset: object) -> float:
    points = np.asarray(coordinates, dtype=np.float32)
    explicit = viewport_aspect_ratio(preset)
    if explicit is not None:
        return float(explicit)
    if points.ndim != 2 or points.shape[0] <= 1 or points.shape[1] < 2:
        return 1.0
    span = np.ptp(points[:, :2].astype(np.float64), axis=0)
    if float(np.max(span)) <= 1e-12:
        return 1.0
    if span[1] <= 1e-12:
        return float(points.shape[0])
    return max(1.0 / points.shape[0], min(float(points.shape[0]), float(span[0] / span[1])))


def enforce_minimum_spacing(
    coordinates: np.ndarray,
    metadata_ids: np.ndarray,
    minimum_spacing: float,
    *,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[int], None] | None = None,
) -> np.ndarray:
    """Enforce Euclidean spacing with a sparse PRISM-style stress solver.

    Short edges in a Delaunay proximity graph receive the requested length.
    Stress majorization moves each connected collision region collectively,
    which lets displacement propagate through dense centers instead of merely
    ejecting their boundary points. Each region keeps its previous centroid;
    points outside collision regions remain bit-for-bit unchanged.
    """
    points, ids = _validated_inputs(coordinates, metadata_ids)
    desired = float(minimum_spacing)
    if not math.isfinite(desired):
        desired = 0.0
    desired = max(0.0, min(1.0, desired))
    if points.shape[0] <= 1 or desired <= 0.0:
        return points.copy()
    check = cancelled or (lambda: False)
    report = progress or (lambda _value: None)
    report(0)

    base = points.astype(np.float64)
    positions = _seed_coincident_points(base, ids, desired, check)
    tolerance = max(desired * 1e-4, 1e-12)
    maximum_passes = 36
    for pass_index in range(maximum_passes):
        if check():
            raise TransformCancelled
        first, second = _delaunay_edges(positions, ids)
        if first.size == 0:
            break
        delta = positions[first] - positions[second]
        distances = np.sqrt(np.einsum("ij,ij->i", delta, delta))
        colliding = distances < desired - tolerance
        if not np.any(colliding):
            break
        positions = _solve_proximity_stress(
            positions,
            first,
            second,
            colliding,
            desired,
            cancelled=check,
        )
        report(round((pass_index + 1) / maximum_passes * 75))

    report(75)
    positions = _finish_pair_clearance(
        positions,
        ids,
        desired * 1.00001,
        cancelled=check,
        progress=lambda value: report(75 + round(value * 0.25)),
    )
    report(100)
    return np.ascontiguousarray(positions, dtype=np.float32)


def _delaunay_edges(
    positions: np.ndarray,
    ids: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Return a deterministic sparse graph containing every closest pair."""
    count = positions.shape[0]
    if count <= 1:
        empty = np.empty(0, dtype=np.int64)
        return empty, empty
    if count == 2:
        return np.asarray([0], dtype=np.int64), np.asarray([1], dtype=np.int64)

    # Canonical ordering removes input-row order from Qhull tie resolution.
    canonical = np.argsort(ids, kind="stable")
    ordered = positions[canonical]
    centered = ordered - np.mean(ordered, axis=0)
    singular_values = np.linalg.svd(centered, compute_uv=False)
    is_collinear = (
        singular_values.size < 2
        or singular_values[0] <= 1e-14
        or singular_values[1] <= singular_values[0] * 1e-12
    )
    if is_collinear:
        return _collinear_proximity_edges(positions, ids)

    try:
        from scipy.spatial import Delaunay, QhullError

        triangulation = Delaunay(ordered, qhull_options="Qbb Qc Qz Q12")
    except (ValueError, RuntimeError, QhullError):
        return _collinear_proximity_edges(positions, ids)

    simplices = np.asarray(triangulation.simplices, dtype=np.int64)
    simplices = simplices[np.all((simplices >= 0) & (simplices < count), axis=1)]
    if simplices.size == 0:
        return _collinear_proximity_edges(positions, ids)
    ordered_first = np.concatenate((simplices[:, 0], simplices[:, 0], simplices[:, 1]))
    ordered_second = np.concatenate((simplices[:, 1], simplices[:, 2], simplices[:, 2]))
    first = canonical[ordered_first]
    second = canonical[ordered_second]
    low = np.minimum(first, second)
    high = np.maximum(first, second)
    keys = low.astype(np.int64) * count + high.astype(np.int64)
    keep = np.unique(keys, return_index=True)[1]
    return low[keep], high[keep]


def _collinear_proximity_edges(
    positions: np.ndarray,
    ids: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Use adjacent projected neighbors when a 2-D triangulation is undefined."""
    centered = positions - np.mean(positions, axis=0)
    if np.max(np.abs(centered)) <= 1e-14:
        order = np.argsort(ids, kind="stable")
    else:
        _, _, axes = np.linalg.svd(centered, full_matrices=False)
        projection = centered @ axes[0]
        # Orient the principal axis consistently before sorting.
        if axes[0, int(np.argmax(np.abs(axes[0])))] < 0.0:
            projection = -projection
        order = np.lexsort((ids, projection))
    return order[:-1].astype(np.int64), order[1:].astype(np.int64)


def _solve_proximity_stress(
    positions: np.ndarray,
    first: np.ndarray,
    second: np.ndarray,
    colliding: np.ndarray,
    target_spacing: float,
    *,
    cancelled: Callable[[], bool],
) -> np.ndarray:
    """Run one sparse stress-majorization step on active collision regions."""
    from scipy.sparse import coo_matrix, diags
    from scipy.sparse.csgraph import connected_components
    from scipy.sparse.linalg import cg

    count = positions.shape[0]
    collision_first = first[colliding]
    collision_second = second[colliding]
    adjacency = coo_matrix(
        (
            np.ones(collision_first.size * 2, dtype=np.int8),
            (
                np.concatenate((collision_first, collision_second)),
                np.concatenate((collision_second, collision_first)),
            ),
        ),
        shape=(count, count),
    ).tocsr()
    _, labels = connected_components(adjacency, directed=False, return_labels=True)
    active = np.zeros(count, dtype=bool)
    active[collision_first] = True
    active[collision_second] = True
    active_indices = np.flatnonzero(active)
    local_index = np.full(count, -1, dtype=np.int64)
    local_index[active_indices] = np.arange(active_indices.size, dtype=np.int64)

    # Retain the full Delaunay shape inside each active component. Short edges
    # expand; the other edges preserve their current length and distribute the
    # displacement through the neighborhood.
    active_edges = active[first] & active[second] & (labels[first] == labels[second])
    edge_first = local_index[first[active_edges]]
    edge_second = local_index[second[active_edges]]
    current = positions[active_indices]
    delta = current[edge_first] - current[edge_second]
    distance = np.sqrt(np.einsum("ij,ij->i", delta, delta))
    safe_distance = np.maximum(distance, target_spacing * 1e-8)
    desired_length = np.maximum(distance, target_spacing * 1.0002)
    weights = 1.0 / np.maximum(desired_length * desired_length, 1e-24)

    row = np.concatenate((edge_first, edge_second, edge_first, edge_second))
    column = np.concatenate((edge_first, edge_second, edge_second, edge_first))
    values = np.concatenate((weights, weights, -weights, -weights))
    laplacian = coo_matrix(
        (values, (row, column)),
        shape=(active_indices.size, active_indices.size),
    ).tocsr()

    rhs = np.zeros_like(current)
    contribution = delta * (weights * desired_length / safe_distance)[:, None]
    np.add.at(rhs, edge_first, contribution)
    np.add.at(rhs, edge_second, -contribution)

    positive_diagonal = laplacian.diagonal()
    regularization = max(float(np.mean(positive_diagonal)) * 1e-7, 1e-12)
    system = laplacian + diags(
        np.full(active_indices.size, regularization, dtype=np.float64),
        format="csr",
    )
    rhs += regularization * current
    solved = np.empty_like(current)

    def iteration_callback(_value: np.ndarray) -> None:
        if cancelled():
            raise TransformCancelled

    for axis in range(2):
        solution, info = cg(
            system,
            rhs[:, axis],
            x0=current[:, axis],
            rtol=1e-8,
            atol=0.0,
            maxiter=300,
            callback=iteration_callback,
        )
        if info < 0 or not np.isfinite(solution).all():
            raise ValueError("Overlap stress solver failed to converge.")
        solved[:, axis] = solution

    _, local_labels = np.unique(labels[active_indices], return_inverse=True)
    label_count = int(np.max(local_labels)) + 1
    sizes = np.bincount(local_labels, minlength=label_count).astype(np.float64)
    for axis in range(2):
        before = np.bincount(
            local_labels,
            weights=current[:, axis],
            minlength=label_count,
        ) / sizes
        after = np.bincount(
            local_labels,
            weights=solved[:, axis],
            minlength=label_count,
        ) / sizes
        solved[:, axis] += (before - after)[local_labels]

    result = positions.copy()
    result[active_indices] = solved
    return result


def _finish_pair_clearance(
    positions: np.ndarray,
    ids: np.ndarray,
    target_spacing: float,
    *,
    cancelled: Callable[[], bool],
    progress: Callable[[int], None] | None = None,
) -> np.ndarray:
    """Resolve the small residual collision set after global stress spreading.

    Sequential symmetric corrections cannot cancel at a dense center. This
    pass is deliberately last: the proximity-stress stage has already made
    the candidate set sparse, so the cleanup remains bounded in practice.
    """
    from scipy.spatial import cKDTree

    result = positions.copy()
    report = progress or (lambda _value: None)
    tolerance = max(target_spacing * 1e-7, 1e-13)
    maximum_passes = 80
    for pass_index in range(maximum_passes):
        if cancelled():
            raise TransformCancelled
        pairs = cKDTree(result).query_pairs(target_spacing, output_type="ndarray")
        if pairs.size == 0:
            break
        low_ids = np.minimum(ids[pairs[:, 0]], ids[pairs[:, 1]])
        high_ids = np.maximum(ids[pairs[:, 0]], ids[pairs[:, 1]])
        pairs = pairs[np.lexsort((high_ids, low_ids))]
        maximum_overlap = 0.0
        for pair_index, (first, second) in enumerate(pairs):
            if pair_index % 2048 == 0 and cancelled():
                raise TransformCancelled
            delta = result[first] - result[second]
            distance = float(np.linalg.norm(delta))
            overlap = target_spacing - distance
            if overlap <= tolerance:
                continue
            if distance <= 1e-14:
                direction = _deterministic_pair_directions(
                    ids[np.asarray([first])],
                    ids[np.asarray([second])],
                )[0]
            else:
                direction = delta / distance
            correction = direction * (overlap * 0.5)
            result[first] += correction
            result[second] -= correction
            maximum_overlap = max(maximum_overlap, overlap)
        if maximum_overlap <= tolerance:
            break
        report(round((pass_index + 1) / maximum_passes * 100))
    report(100)
    return result


def _seed_coincident_points(
    positions: np.ndarray,
    ids: np.ndarray,
    separation: float,
    cancelled: Callable[[], bool],
) -> np.ndarray:
    """Give exact duplicates deterministic radial directions.

    A proximity graph needs a direction when two centers coincide.
    Golden-angle offsets break that symmetry without choosing horizontal or
    vertical lanes. The group centroid remains unchanged, and subsequent
    collision iterations determine the final positions.
    """
    seeded = np.asarray(positions, dtype=np.float64).copy()
    order = np.lexsort((ids, seeded[:, 1], seeded[:, 0]))
    ordered = seeded[order]
    boundaries = np.flatnonzero(
        np.r_[True, np.any(ordered[1:] != ordered[:-1], axis=1), True]
    )
    golden_angle = math.pi * (3.0 - math.sqrt(5.0))
    for start, stop in zip(boundaries[:-1], boundaries[1:], strict=True):
        count = int(stop - start)
        if count <= 1:
            continue
        if cancelled():
            raise TransformCancelled
        members = order[start:stop]
        members = members[np.argsort(ids[members], kind="stable")]
        rank = np.arange(count, dtype=np.float64) + 0.5
        # This is close to circular close-packing density. It is only an
        # initializer for coincident centers, rather than an assigned grid.
        radius = separation * 0.66 * np.sqrt(rank)
        angle = golden_angle * np.arange(count, dtype=np.float64)
        offsets = np.column_stack((radius * np.cos(angle), radius * np.sin(angle)))
        offsets -= np.mean(offsets, axis=0)
        seeded[members] += offsets
    return seeded


def _deterministic_pair_directions(first_ids: np.ndarray, second_ids: np.ndarray) -> np.ndarray:
    first = np.asarray(first_ids, dtype=np.int64)
    second = np.asarray(second_ids, dtype=np.int64)
    low = np.minimum(first, second).astype(np.uint64)
    high = np.maximum(first, second).astype(np.uint64)
    mixed = (low * np.uint64(0x9E3779B1)) ^ (high * np.uint64(0x85EBCA77))
    angles = mixed.astype(np.float64) / float(2**64) * (2.0 * math.pi)
    directions = np.column_stack((np.cos(angles), np.sin(angles)))
    signs = np.where(first >= second, 1.0, -1.0)
    return directions * signs[:, None]


def _recursive_assign(points, ids, point_indices, cells, cell_indices, assignment, cancelled) -> None:
    if cancelled():
        raise TransformCancelled
    size = point_indices.size
    if size == 0:
        return
    if size == 1:
        assignment[point_indices[0]] = cell_indices[0]
        return
    cell_span = np.ptp(cells[cell_indices], axis=0)
    axis = 0 if cell_span[0] >= cell_span[1] else 1
    other = 1 - axis
    point_order = np.lexsort((ids[point_indices], points[point_indices, other], points[point_indices, axis]))
    cell_order = np.lexsort((cells[cell_indices, other], cells[cell_indices, axis]))
    split = size // 2
    sorted_points = point_indices[point_order]
    sorted_cells = cell_indices[cell_order]
    _recursive_assign(points, ids, sorted_points[:split], cells, sorted_cells[:split], assignment, cancelled)
    _recursive_assign(points, ids, sorted_points[split:], cells, sorted_cells[split:], assignment, cancelled)


def _expand_grid_clearance(grid: np.ndarray, margin: float) -> np.ndarray:
    points = np.asarray(grid, dtype=np.float64)
    unique_x = np.unique(points[:, 0])
    unique_y = np.unique(points[:, 1])
    gaps = np.concatenate((np.diff(unique_x), np.diff(unique_y)))
    positive = gaps[gaps > 1e-12]
    current = float(np.min(positive)) if positive.size else 0.0
    if current >= margin or current <= 0.0:
        return points.astype(np.float32)
    center = (np.min(points, axis=0) + np.max(points, axis=0)) * 0.5
    return ((points - center) * (margin / current) + center).astype(np.float32)


def _validated_inputs(coordinates: np.ndarray, metadata_ids: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    points = np.asarray(coordinates, dtype=np.float32)
    ids = np.asarray(metadata_ids, dtype=np.int64).reshape(-1)
    if points.ndim != 2 or points.shape[1] < 2:
        raise ValueError("Transform coordinates must contain x and y values.")
    points = np.ascontiguousarray(points[:, :2], dtype=np.float32)
    if points.shape[0] != ids.shape[0]:
        raise ValueError("Transform coordinates and metadata IDs must have the same length.")
    if not np.isfinite(points).all():
        raise ValueError("Transform coordinates must be finite.")
    return points, np.ascontiguousarray(ids, dtype=np.int64)
