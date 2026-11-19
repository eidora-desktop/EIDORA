from __future__ import annotations

from collections.abc import Mapping

import numpy as np

from DiscoveryArea.discovery_methods.base import DiscoveryMethodBase, ProgressCallback, ProjectionExecutionResult


class MDSDiscoveryMethod(DiscoveryMethodBase):
    method_id = "mds"
    name = "MDS"

    def fit_transform(
        self,
        vectors: np.ndarray,
        parameters: Mapping[str, object],
        progress_callback: ProgressCallback | None = None,
    ) -> ProjectionExecutionResult:
        from DiscoveryArea.method_catalog import DiscoveryMethodCatalog
        from DiscoveryArea.domain import ProjectionInputInfo
        from DiscoveryArea.projection_assessment import assess_projection

        data = np.asarray(vectors)
        if data.ndim != 2 or data.shape[0] < 3 or data.shape[1] < 1:
            raise ValueError("MDS requires at least three embedding vectors.")
        requested = DiscoveryMethodCatalog().normalize_parameters(self.method_id, dict(parameters))
        assessment = assess_projection(self.method_id, requested,
                                       ProjectionInputInfo(len(data), len(data), data.shape[1]))
        if assessment.state == "blocked":
            raise ValueError(" ".join(assessment.messages))
        data = np.asarray(data, dtype=np.float64)
        if not np.isfinite(data).all():
            raise ValueError("MDS requires finite embedding values.")

        from sklearn.manifold import MDS

        kwargs = dict(requested)
        kwargs["metric_mds"] = kwargs.pop("mode") == "metric"
        if kwargs["normalized_stress"] != "auto":
            kwargs["normalized_stress"] = kwargs["normalized_stress"] == "true"
        if progress_callback is not None:
            progress_callback(45)
        # Run restarts sequentially so the memory estimate remains bounded.
        reducer = MDS(n_components=2, n_jobs=1, **kwargs)
        coordinates = reducer.fit_transform(data)
        if not np.isfinite(coordinates).all() or not np.isfinite(reducer.stress_):
            raise ValueError("MDS produced non-finite coordinates or stress; check the input distances.")
        if progress_callback is not None:
            progress_callback(75)
        effective = dict(requested)
        effective.update({
            "output_dimensions": 2,
            "n_jobs": 1,
            "n_init": 1 if requested["init"] == "classical_mds" else requested["n_init"],
            "normalized_stress": (not kwargs["metric_mds"] if kwargs["normalized_stress"] == "auto"
                                  else kwargs["normalized_stress"]),
            "stress": float(reducer.stress_),
            "iterations_completed": int(reducer.n_iter_),
        })
        return ProjectionExecutionResult(
            np.asarray(coordinates, dtype=np.float32),
            requested_parameters=requested,
            effective_parameters=effective,
            repeatability=("deterministic_execution" if requested["init"] == "classical_mds"
                           else "repeatable_with_recorded_seed" if requested["random_state"] is not None
                           else "stochastic_execution"),
        )
