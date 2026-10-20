from __future__ import annotations

from collections.abc import Mapping
import os
import tempfile

import numpy as np

from DiscoveryArea.discovery_methods.base import ProgressCallback, DiscoveryMethodBase, ProjectionExecutionResult

os.environ.setdefault("NUMBA_CACHE_DIR", os.path.join(tempfile.gettempdir(), "eidora_numba_cache"))


class UMAPDiscoveryMethod(DiscoveryMethodBase):
    method_id = "umap"
    name = "UMAP"

    def fit_transform(
        self,
        vectors: np.ndarray,
        parameters: Mapping[str, object],
        progress_callback: ProgressCallback | None = None,
    ) -> np.ndarray:
        if progress_callback is not None:
            progress_callback(45)
        import umap

        data = np.asarray(vectors, dtype=np.float32)
        if data.ndim != 2 or data.shape[0] < 2:
            raise ValueError("UMAP requires at least two embedding vectors.")
        n_neighbors = min(int(parameters.get("n_neighbors", 15)), max(2, data.shape[0] - 1))
        reproducible = bool(parameters.get("reproducible", False))
        epochs = int(parameters.get("epochs", 0))
        reducer = umap.UMAP(
            n_components=2,
            n_neighbors=n_neighbors,
            min_dist=float(parameters.get("min_dist", 0.1)),
            metric=str(parameters.get("metric", "cosine")),
            random_state=int(parameters.get("random_state", 42)) if reproducible else None,
            n_jobs=1 if reproducible else -1,
            n_epochs=epochs if epochs > 0 else None,
            low_memory=True,
        )
        coordinates = reducer.fit_transform(data)
        if progress_callback is not None:
            progress_callback(75)
        effective = dict(parameters)
        effective.update({
            "n_neighbors": n_neighbors,
            "min_dist": float(parameters.get("min_dist", 0.1)),
            "metric": str(parameters.get("metric", "cosine")),
            "random_state": int(parameters.get("random_state", 42)) if reproducible else None,
            "reproducible": reproducible,
            "epochs": epochs if epochs > 0 else "automatic",
            "output_dimensions": 2,
            "workers": 1 if reproducible else "all available",
            "low_memory": True,
        })
        return ProjectionExecutionResult(
            np.asarray(coordinates, dtype=np.float32)[:, :2],
            requested_parameters=dict(parameters),
            effective_parameters=effective,
            derived_steps=(("UMAP selected the epoch count automatically",) if epochs <= 0 else ()),
            repeatability=("repeatable_single_worker" if reproducible else "stochastic_parallel_execution"),
        )
