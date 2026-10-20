from __future__ import annotations

from collections.abc import Mapping

import numpy as np

from DiscoveryArea.discovery_methods.base import PCA_CITATION, ProgressCallback, DiscoveryMethodBase, ProjectionExecutionResult


class TSNEDiscoveryMethod(DiscoveryMethodBase):
    method_id = "tsne"
    name = "t-SNE"

    def fit_transform(
        self,
        vectors: np.ndarray,
        parameters: Mapping[str, object],
        progress_callback: ProgressCallback | None = None,
    ) -> np.ndarray:
        if progress_callback is not None:
            progress_callback(45)
        data = np.asarray(vectors, dtype=np.float32)
        if data.ndim != 2 or data.shape[0] < 3:
            raise ValueError("t-SNE requires at least three embedding vectors.")
        from sklearn.manifold import TSNE

        perplexity = min(float(parameters.get("perplexity", 30.0)), max(1.0, (data.shape[0] - 1) / 3.0))
        requested_perplexity = float(parameters.get("perplexity", 30.0))
        pca_dimensions = int(parameters.get("pca_dimensions", 50))
        derived_steps: list[str] = []
        if pca_dimensions > 0 and data.shape[1] > pca_dimensions:
            from sklearn.decomposition import PCA

            components = min(pca_dimensions, data.shape[0] - 1, data.shape[1] - 1)
            derived_steps.append(f"PCA pre-reduction to {components} dimensions")
            data = np.asarray(
                PCA(
                    n_components=components,
                    svd_solver="randomized",
                    random_state=int(parameters.get("random_state", 42)),
                ).fit_transform(data),
                dtype=np.float32,
            )
            if progress_callback is not None:
                progress_callback(55)
        reducer = TSNE(
            n_components=2,
            perplexity=perplexity,
            learning_rate=str(parameters.get("learning_rate", "auto")),
            init=str(parameters.get("init", "pca")),
            random_state=int(parameters.get("random_state", 42)),
            max_iter=int(parameters.get("iterations", 1000)),
            method="barnes_hut",
            angle=float(parameters.get("angle", 0.5)),
            n_jobs=-1,
        )
        coordinates = reducer.fit_transform(data)
        if progress_callback is not None:
            progress_callback(75)
        effective = dict(parameters)
        effective.update({
            "perplexity": perplexity,
            "learning_rate": str(parameters.get("learning_rate", "auto")),
            "random_state": int(parameters.get("random_state", 42)),
            "init": str(parameters.get("init", "pca")),
            "pca_dimensions": pca_dimensions,
            "iterations": int(parameters.get("iterations", 1000)),
            "angle": float(parameters.get("angle", 0.5)),
            "output_dimensions": 2,
            "method": "barnes_hut",
            "workers": "all available",
        })
        if perplexity != requested_perplexity:
            derived_steps.append(f"Perplexity clamped from {requested_perplexity:g} to {perplexity:g}")
        return ProjectionExecutionResult(
            np.asarray(coordinates, dtype=np.float32)[:, :2],
            requested_parameters=dict(parameters),
            effective_parameters=effective,
            derived_steps=tuple(derived_steps),
            repeatability="repeatable_with_recorded_seed",
            citations=((dict(PCA_CITATION),) if any("PCA pre-reduction" in step for step in derived_steps) else ()),
        )
