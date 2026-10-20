from __future__ import annotations

from collections.abc import Mapping

import numpy as np

from DiscoveryArea.discovery_methods.base import PCA_CITATION, DiscoveryMethodBase, ProgressCallback, ProjectionExecutionResult


class PaCMAPDiscoveryMethod(DiscoveryMethodBase):
    method_id = "pacmap"
    name = "PaCMAP"

    def fit_transform(
        self,
        vectors: np.ndarray,
        parameters: Mapping[str, object],
        progress_callback: ProgressCallback | None = None,
    ) -> np.ndarray:
        data = np.asarray(vectors, dtype=np.float32)
        if data.ndim != 2 or data.shape[0] < 4 or data.shape[1] < 2:
            raise ValueError("PaCMAP requires at least four vectors with at least two dimensions.")
        if not np.isfinite(data).all():
            raise ValueError("PaCMAP input vectors must contain only finite values.")
        if float(np.ptp(data)) <= np.finfo(np.float32).eps:
            raise ValueError("PaCMAP input vectors must not all be identical.")
        if progress_callback is not None:
            progress_callback(45)

        import numba
        import pacmap

        reproducible = bool(parameters.get("reproducible", False))
        if reproducible:
            numba.set_num_threads(1)
        total_iterations = int(parameters.get("iterations", 450))
        reducer = pacmap.PaCMAP(
            n_components=2,
            n_neighbors=min(int(parameters.get("n_neighbors", 10)), data.shape[0] - 1),
            MN_ratio=float(parameters.get("mid_near_ratio", 0.5)),
            FP_ratio=float(parameters.get("further_ratio", 2.0)),
            distance=str(parameters.get("distance", "angular")),
            lr=float(parameters.get("learning_rate", 1.0)),
            num_iters=_iteration_schedule(total_iterations),
            apply_pca=bool(parameters.get("apply_pca", True)),
            random_state=int(parameters.get("random_state", 42)) if reproducible else None,
            save_tree=False,
            knn_backend="faiss",
            verbose=False,
        )
        if progress_callback is not None:
            progress_callback(55)
        coordinates = reducer.fit_transform(
            np.ascontiguousarray(data),
            init=str(parameters.get("initialization", "pca")),
            save_pairs=False,
        )

        result = np.asarray(coordinates, dtype=np.float32)
        _validate_coordinates(result, expected_rows=data.shape[0])
        if progress_callback is not None:
            progress_callback(75)
        schedule = _iteration_schedule(total_iterations)
        effective = dict(parameters)
        effective.update({
            "n_neighbors": min(int(parameters.get("n_neighbors", 10)), data.shape[0] - 1),
            "mid_near_ratio": float(parameters.get("mid_near_ratio", 0.5)),
            "further_ratio": float(parameters.get("further_ratio", 2.0)),
            "distance": str(parameters.get("distance", "angular")),
            "initialization": str(parameters.get("initialization", "pca")),
            "learning_rate": float(parameters.get("learning_rate", 1.0)),
            "iterations": total_iterations,
            "apply_pca": bool(parameters.get("apply_pca", True)),
            "reproducible": reproducible,
            "random_state": int(parameters.get("random_state", 42)) if reproducible else None,
            "output_dimensions": 2,
            "iteration_schedule": list(schedule),
            "neighbor_backend": "faiss",
        })
        return ProjectionExecutionResult(
            result,
            requested_parameters=dict(parameters),
            effective_parameters=effective,
            derived_steps=(f"Optimization phases used {schedule[0]}, {schedule[1]}, and {schedule[2]} iterations",),
            repeatability=("repeatable_single_worker" if reproducible else "stochastic_parallel_execution"),
            citations=((dict(PCA_CITATION),) if bool(parameters.get("apply_pca", True)) else ()),
        )


def _iteration_schedule(total: int) -> tuple[int, int, int]:
    """Preserve PaCMAP's 100/100/250 phase ratio for any requested total."""
    if total < 3:
        raise ValueError("PaCMAP requires at least three optimization iterations.")
    phase_one = max(1, round(total * 2 / 9))
    phase_two = max(1, round(total * 2 / 9))
    phase_three = total - phase_one - phase_two
    if phase_three < 1:
        phase_three = 1
        phase_two = total - phase_one - phase_three
    return phase_one, phase_two, phase_three


def _validate_coordinates(coordinates: np.ndarray, *, expected_rows: int) -> None:
    if coordinates.shape != (expected_rows, 2) or not np.isfinite(coordinates).all():
        raise ValueError("PaCMAP did not produce finite two-dimensional coordinates.")
    if np.any(np.ptp(coordinates, axis=0) <= 1e-8):
        raise RuntimeError("PaCMAP produced a collapsed layout; the result was not saved.")
