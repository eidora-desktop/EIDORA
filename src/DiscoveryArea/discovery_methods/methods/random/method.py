from __future__ import annotations

from collections.abc import Mapping

import numpy as np

from DiscoveryArea.discovery_methods.base import ProgressCallback, DiscoveryMethodBase, ProjectionExecutionResult


class RandomDiscoveryMethod(DiscoveryMethodBase):
    method_id = "random"
    name = "Random"

    def fit_transform(
        self,
        vectors: np.ndarray,
        parameters: Mapping[str, object],
        progress_callback: ProgressCallback | None = None,
    ) -> np.ndarray:
        row_count = int(np.asarray(vectors).shape[0])
        seed = int(parameters.get("random_state", 42))
        coordinates = np.random.default_rng(seed).random((row_count, 2), dtype=np.float32)
        if progress_callback is not None:
            progress_callback(75)
        return ProjectionExecutionResult(
            coordinates,
            requested_parameters=dict(parameters),
            effective_parameters={"random_state": seed},
            repeatability="deterministic",
        )
