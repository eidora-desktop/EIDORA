from __future__ import annotations

from collections.abc import Mapping

import numpy as np

from DiscoveryArea.discovery_methods.base import ProgressCallback, DiscoveryMethodBase, ProjectionExecutionResult


class PCADiscoveryMethod(DiscoveryMethodBase):
    method_id = "pca"
    name = "PCA"

    def fit_transform(
        self,
        vectors: np.ndarray,
        parameters: Mapping[str, object],
        progress_callback: ProgressCallback | None = None,
    ) -> np.ndarray:
        data = np.asarray(vectors, dtype=np.float32)
        if data.ndim != 2 or data.shape[0] < 2 or data.shape[1] < 2:
            raise ValueError("PCA requires at least two vectors with at least two dimensions.")
        if progress_callback is not None:
            progress_callback(45)
        from sklearn.decomposition import PCA

        reducer = PCA(
            n_components=2,
            svd_solver="randomized",
            iterated_power=int(parameters.get("power_iterations", 3)),
            random_state=int(parameters.get("random_state", 42)),
        )
        coordinates = reducer.fit_transform(data)
        if progress_callback is not None:
            progress_callback(75)
        effective = dict(parameters)
        effective.setdefault("power_iterations", 3)
        effective.setdefault("random_state", 42)
        effective.update({"output_dimensions": 2, "svd_solver": "randomized"})
        return ProjectionExecutionResult(
            np.asarray(coordinates, dtype=np.float32),
            requested_parameters=dict(parameters),
            effective_parameters=effective,
            repeatability="repeatable_with_recorded_seed",
        )
