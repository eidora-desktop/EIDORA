from __future__ import annotations

from collections.abc import Mapping

import numpy as np

from DiscoveryArea.discovery_methods.base import ProgressCallback, DiscoveryMethodBase, ProjectionExecutionResult


class CustomCoordinatesDiscoveryMethod(DiscoveryMethodBase):
    method_id = "custom_coordinates"
    name = "Custom Coordinates"

    def fit_transform(
        self,
        vectors: np.ndarray,
        parameters: Mapping[str, object],
        progress_callback: ProgressCallback | None = None,
    ) -> np.ndarray:
        _ = parameters
        coordinates = np.asarray(vectors, dtype=np.float32)
        if coordinates.ndim != 2 or coordinates.shape[1] < 2:
            raise ValueError("Custom coordinates require x and y columns.")
        if progress_callback is not None:
            progress_callback(75)
        return ProjectionExecutionResult(
            coordinates[:, :2],
            requested_parameters=dict(parameters),
            effective_parameters=dict(parameters),
            repeatability="deterministic",
        )
