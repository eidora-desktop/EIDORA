from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

from DiscoveryArea.discovery_methods.base import PCA_CITATION, DiscoveryMethodBase, ProgressCallback, ProjectionExecutionResult


@dataclass(frozen=True)
class _TriMAPSettings:
    n_inliers: int
    n_outliers: int
    n_random: int
    iterations: int
    pca_dimensions: int
    neighbor_trees: int | None
    neighbor_iterations: int | None


class TriMAPDiscoveryMethod(DiscoveryMethodBase):
    method_id = "trimap"
    name = "TriMAP"

    def fit_transform(
        self,
        vectors: np.ndarray,
        parameters: Mapping[str, object],
        progress_callback: ProgressCallback | None = None,
    ) -> np.ndarray:
        data = np.asarray(vectors, dtype=np.float32)
        if data.ndim != 2 or data.shape[0] < 4 or data.shape[1] < 2:
            raise ValueError("TriMAP requires at least four vectors with at least two dimensions.")
        if not np.isfinite(data).all():
            raise ValueError("TriMAP input vectors must contain only finite values.")
        if progress_callback is not None:
            progress_callback(45)

        import numba
        import trimap

        settings = _settings_from_parameters(parameters, data.shape[0])
        random_state = int(parameters.get("random_state", 42))
        distance = str(parameters.get("distance", "cosine"))
        projection_data = _neighbor_input(
            data,
            apply_pca=bool(parameters.get("apply_pca", True)),
            pca_dimensions=settings.pca_dimensions,
        )
        neighbor_thread_count = numba.get_num_threads()
        knn_tuple = _pynndescent_knn(
            projection_data,
            n_neighbors=max(settings.n_inliers + 1, 6),
            distance=distance,
            random_state=random_state,
            tree_count=settings.neighbor_trees,
            iteration_count=settings.neighbor_iterations,
            thread_count=neighbor_thread_count,
        )
        if progress_callback is not None:
            progress_callback(55)

        np.random.seed(random_state)
        reducer = trimap.TRIMAP(
            n_dims=2,
            n_inliers=settings.n_inliers,
            n_outliers=settings.n_outliers,
            n_random=settings.n_random,
            distance=distance,
            weight_temp=float(parameters.get("weight_temp", 0.5)),
            lr=float(parameters.get("learning_rate", 0.1)),
            n_iters=settings.iterations,
            apply_pca=False,
            opt_method=str(parameters.get("optimizer", "dbd")),
            knn_tuple=knn_tuple,
            verbose=False,
        )
        coordinates = reducer.fit_transform(projection_data)

        result = np.asarray(coordinates, dtype=np.float32)
        _validate_coordinates(result, expected_rows=data.shape[0])
        if progress_callback is not None:
            progress_callback(75)
        effective = dict(parameters)
        effective.update({
            "n_inliers": settings.n_inliers,
            "n_outliers": settings.n_outliers,
            "n_random": settings.n_random,
            "distance": distance,
            "weight_temp": float(parameters.get("weight_temp", 0.5)),
            "learning_rate": float(parameters.get("learning_rate", 0.1)),
            "iterations": settings.iterations,
            "apply_pca": bool(parameters.get("apply_pca", True)),
            "pca_dimensions": settings.pca_dimensions,
            "optimizer": str(parameters.get("optimizer", "dbd")),
            "random_state": random_state,
            "output_dimensions": 2,
            "neighbor_trees": settings.neighbor_trees,
            "neighbor_iterations": settings.neighbor_iterations or "automatic",
            "neighbor_workers": neighbor_thread_count,
            "pca_random_state": 0 if bool(parameters.get("apply_pca", True)) else None,
        })
        derived_steps = ()
        if bool(parameters.get("apply_pca", True)) and data.shape[1] > settings.pca_dimensions:
            components = min(settings.pca_dimensions, data.shape[0] - 1, data.shape[1] - 1)
            derived_steps = (f"PCA pre-reduction to {components} dimensions",)
        return ProjectionExecutionResult(
            result,
            requested_parameters=dict(parameters),
            effective_parameters=effective,
            derived_steps=derived_steps,
            repeatability="repeatable_with_recorded_seed",
            citations=((dict(PCA_CITATION),) if derived_steps else ()),
        )


def _settings_from_parameters(
    parameters: Mapping[str, object],
    row_count: int,
) -> _TriMAPSettings:
    selected = _TriMAPSettings(
        n_inliers=int(parameters.get("n_inliers", 6)),
        n_outliers=int(parameters.get("n_outliers", 1)),
        n_random=int(parameters.get("n_random", 1)),
        iterations=int(parameters.get("iterations", 200)),
        pca_dimensions=int(parameters.get("pca_dimensions", 50)),
        neighbor_trees=int(parameters.get("neighbor_trees", 10)),
        neighbor_iterations=None,
    )
    n_inliers = min(selected.n_inliers, row_count - 2)
    n_outliers = min(selected.n_outliers, row_count - n_inliers - 1)
    return _TriMAPSettings(
        n_inliers=n_inliers,
        n_outliers=n_outliers,
        n_random=selected.n_random,
        iterations=selected.iterations,
        pca_dimensions=selected.pca_dimensions,
        neighbor_trees=selected.neighbor_trees,
        neighbor_iterations=selected.neighbor_iterations,
    )


def _pynndescent_knn(
    data: np.ndarray,
    *,
    n_neighbors: int,
    distance: str,
    random_state: int,
    tree_count: int | None,
    iteration_count: int | None,
    thread_count: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Build and validate a neighbor graph without Annoy's macOS/NumPy bug."""
    from pynndescent import NNDescent

    neighbor_count = min(int(n_neighbors), data.shape[0])
    metric = "cosine" if distance in {"cosine", "angular"} else distance
    index = NNDescent(
        data,
        n_neighbors=neighbor_count,
        metric=metric,
        random_state=random_state,
        n_jobs=max(1, int(thread_count)),
        low_memory=True,
        n_trees=tree_count,
        n_iters=iteration_count,
        verbose=False,
    )
    raw_indices, raw_distances = index.neighbor_graph
    indices = np.asarray(raw_indices, dtype=np.int32)
    distances = np.asarray(raw_distances, dtype=np.float32)
    indices, distances = _canonicalize_self_neighbors(indices, distances)
    _validate_neighbor_graph(indices, distances, row_count=data.shape[0], neighbor_count=neighbor_count)
    if metric == "cosine":
        distances = np.ascontiguousarray(distances * 2.0, dtype=np.float32)
    return np.ascontiguousarray(indices), np.ascontiguousarray(distances)


def _canonicalize_self_neighbors(
    indices: np.ndarray,
    distances: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Move or insert each row's self-neighbor into the first position."""
    canonical_indices = np.array(indices, dtype=np.int32, copy=True)
    canonical_distances = np.array(distances, dtype=np.float32, copy=True)
    rows = np.arange(canonical_indices.shape[0], dtype=np.int32)
    self_matches = canonical_indices == rows[:, np.newaxis]
    has_self = np.any(self_matches, axis=1)
    self_positions = np.argmax(self_matches, axis=1)

    move_rows = np.flatnonzero(has_self & (self_positions != 0))
    if move_rows.size:
        positions = self_positions[move_rows]
        first_indices = canonical_indices[move_rows, 0].copy()
        first_distances = canonical_distances[move_rows, 0].copy()
        canonical_indices[move_rows, 0] = move_rows
        canonical_distances[move_rows, 0] = 0.0
        canonical_indices[move_rows, positions] = first_indices
        canonical_distances[move_rows, positions] = first_distances

    missing_rows = np.flatnonzero(~has_self)
    if missing_rows.size:
        canonical_indices[missing_rows, 1:] = canonical_indices[missing_rows, :-1]
        canonical_distances[missing_rows, 1:] = canonical_distances[missing_rows, :-1]
        canonical_indices[missing_rows, 0] = missing_rows
        canonical_distances[missing_rows, 0] = 0.0
    return canonical_indices, canonical_distances


def _validate_neighbor_graph(
    indices: np.ndarray,
    distances: np.ndarray,
    *,
    row_count: int,
    neighbor_count: int,
) -> None:
    expected_shape = (row_count, neighbor_count)
    if indices.shape != expected_shape or distances.shape != expected_shape:
        raise RuntimeError(
            f"TriMAP neighbor search returned {indices.shape}; expected {expected_shape}."
        )
    if np.any(indices < 0) or np.any(indices >= row_count) or not np.isfinite(distances).all():
        raise RuntimeError("TriMAP neighbor search returned invalid neighbors or distances.")
    rows = np.arange(row_count, dtype=np.int32)
    if not np.array_equal(indices[:, 0], rows):
        raise RuntimeError("TriMAP neighbor search did not return each item as its first neighbor.")
    for start in range(0, row_count, 100_000):
        chunk = np.sort(indices[start : start + 100_000], axis=1)
        if np.any(np.diff(chunk, axis=1) == 0):
            raise RuntimeError("TriMAP neighbor search returned duplicate neighbor IDs.")
    scales = np.mean(distances[:, 3:6], axis=1)
    if not np.isfinite(scales).all() or np.any(scales <= 1e-10):
        raise RuntimeError("TriMAP neighbor search produced invalid local distance scales.")


def _neighbor_input(
    data: np.ndarray,
    *,
    apply_pca: bool,
    pca_dimensions: int,
) -> np.ndarray:
    if not apply_pca or data.shape[1] <= pca_dimensions:
        return data
    from sklearn.decomposition import PCA

    components = min(pca_dimensions, data.shape[0] - 1, data.shape[1] - 1)
    return np.asarray(
        PCA(
            n_components=components,
            svd_solver="randomized",
            iterated_power=3,
            random_state=0,
        ).fit_transform(data),
        dtype=np.float32,
    )


def _validate_coordinates(coordinates: np.ndarray, *, expected_rows: int) -> None:
    if coordinates.shape != (expected_rows, 2) or not np.isfinite(coordinates).all():
        raise ValueError("TriMAP did not produce finite two-dimensional coordinates.")
    low, high = np.quantile(coordinates, [0.001, 0.999], axis=0)
    robust_span = high - low
    full_span = np.ptp(coordinates, axis=0)
    if np.any(robust_span <= 1e-8) or np.any(full_span / robust_span > 100.0):
        raise RuntimeError("TriMAP produced an unstable collapsed layout; the result was not saved.")
