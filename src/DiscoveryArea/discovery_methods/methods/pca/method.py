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
    ) -> ProjectionExecutionResult:
        data = np.asarray(vectors, dtype=np.float32)
        if data.ndim != 2 or data.shape[0] < 2 or data.shape[1] < 2:
            raise ValueError("PCA requires at least two vectors with at least two dimensions.")
        from DiscoveryArea.method_catalog import DiscoveryMethodCatalog
        from sklearn.decomposition import PCA

        requested = DiscoveryMethodCatalog().normalize_parameters(self.method_id, dict(parameters))
        kwargs = dict(requested)
        kwargs["iterated_power"] = kwargs.pop("power_iterations")
        if kwargs["svd_solver"] == "arpack" and min(data.shape) <= 2:
            raise ValueError("The ARPACK solver needs more than two items and input dimensions.")
        if progress_callback is not None:
            progress_callback(45)
        reducer = PCA(n_components=2, **kwargs)
        coordinates = reducer.fit_transform(data)
        if progress_callback is not None:
            progress_callback(75)
        effective = dict(requested)
        effective.update({
            "output_dimensions": 2,
            "svd_solver": getattr(reducer, "_fit_svd_solver", requested["svd_solver"]),
        })
        return ProjectionExecutionResult(
            np.asarray(coordinates, dtype=np.float32),
            requested_parameters=requested,
            effective_parameters=effective,
            repeatability=("repeatable_with_recorded_seed" if requested["random_state"] is not None
                           else "solver_dependent_unseeded"),
        )
