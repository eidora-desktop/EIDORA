from __future__ import annotations

from collections.abc import Mapping
import os
import tempfile

import numpy as np

from DiscoveryArea.discovery_methods.base import ProgressCallback, DiscoveryMethodBase, ProjectionExecutionResult
from DiscoveryArea.discovery_methods.methods.umap.parameters import resolve_legacy_method

os.environ.setdefault("NUMBA_CACHE_DIR", os.path.join(tempfile.gettempdir(), "eidora_numba_cache"))


class UMAPDiscoveryMethod(DiscoveryMethodBase):
    method_id = "umap"
    name = "UMAP 0.6"

    def fit_transform(
        self,
        vectors: np.ndarray,
        parameters: Mapping[str, object],
        progress_callback: ProgressCallback | None = None,
    ) -> ProjectionExecutionResult:
        if progress_callback is not None:
            progress_callback(45)
        import numba
        import umap
        from DiscoveryArea.method_catalog import DiscoveryMethodCatalog

        data = np.asarray(vectors, dtype=np.float32)
        if data.ndim != 2 or data.shape[0] < 2:
            raise ValueError("UMAP requires at least two embedding vectors.")
        method_id = resolve_legacy_method(self.method_id, parameters)
        compatibility = method_id == "umap_05"
        requested = DiscoveryMethodCatalog().normalize_parameters(method_id, dict(parameters))
        kwargs = dict(requested)
        kwargs["compatibility_layout"] = compatibility
        seeded = kwargs["random_state"] is not None
        jobs = int(kwargs["n_jobs"])
        if jobs == 0 or jobs < -1:
            raise ValueError("n_jobs must be -1 or a positive integer.")
        budget = numba.get_num_threads()
        workers = budget if jobs == -1 else min(jobs, budget)
        if compatibility and seeded:
            workers = 1
        kwargs["n_jobs"] = workers
        kwargs["precomputed_knn"] = tuple(kwargs["precomputed_knn"])
        for name in ("metric_kwds", "output_metric_kwds"):
            if kwargs[name] is not None:
                kwargs[name] = {key: np.asarray(value) if isinstance(value, list) else value
                                for key, value in kwargs[name].items()}
        reducer = umap.UMAP(**kwargs)
        coordinates = reducer.fit_transform(data)
        if progress_callback is not None:
            progress_callback(75)
        effective = dict(requested)
        effective.update({
            "compatibility_layout": compatibility,
            "n_neighbors": int(getattr(reducer, "_n_neighbors", requested["n_neighbors"])),
            "n_jobs": workers,
            "workers": workers,
            "optimizer": getattr(reducer, "_optimizer", None) or ("compatibility" if compatibility else requested.get("optimizer") or "adam"),
            "init": getattr(reducer, "_init", None) or ("recursive" if requested["init"] == "auto" else requested["init"]),
            "output_dimensions": 2,
        })
        steps = []
        if requested["n_epochs"] is None:
            steps.append("UMAP selected the epoch count automatically")
        if jobs != workers:
            steps.append(f"UMAP used {workers} worker(s) within the projection CPU budget and seeded-layout constraints")
        return ProjectionExecutionResult(
            np.asarray(coordinates, dtype=np.float32),
            requested_parameters=requested,
            effective_parameters=effective,
            derived_steps=tuple(steps),
            repeatability=(
                ("repeatable_single_worker" if workers == 1 else "repeatable_parallel_execution")
                if seeded else "stochastic_parallel_execution"
            ),
        )
