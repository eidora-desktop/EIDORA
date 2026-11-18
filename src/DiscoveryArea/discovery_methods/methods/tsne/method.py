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
    ) -> ProjectionExecutionResult:
        if progress_callback is not None:
            progress_callback(45)
        data = np.asarray(vectors, dtype=np.float32)
        if data.ndim != 2 or data.shape[0] < 3:
            raise ValueError("t-SNE requires at least three embedding vectors.")
        from DiscoveryArea.method_catalog import DiscoveryMethodCatalog
        from sklearn.manifold import TSNE

        requested = DiscoveryMethodCatalog().normalize_parameters(self.method_id, dict(parameters))
        kwargs = dict(requested)
        pca_dimensions = kwargs.pop("pca_dimensions")
        kwargs["max_iter"] = kwargs.pop("iterations")
        # Preserve the small-dataset guard, recording any effective adjustment.
        perplexity = min(requested["perplexity"], max(1.0, (data.shape[0] - 1) / 3.0))
        kwargs["perplexity"] = perplexity
        derived_steps: list[str] = []
        if pca_dimensions > 0 and data.shape[1] > pca_dimensions:
            from sklearn.decomposition import PCA

            components = min(pca_dimensions, data.shape[0] - 1, data.shape[1] - 1)
            derived_steps.append(f"PCA pre-reduction to {components} dimensions")
            data = np.asarray(
                PCA(n_components=components, random_state=requested["random_state"]).fit_transform(data),
                dtype=np.float32,
            )
            if progress_callback is not None:
                progress_callback(55)
        reducer = TSNE(n_components=2, **kwargs)
        coordinates = reducer.fit_transform(data)
        if progress_callback is not None:
            progress_callback(75)
        effective = dict(requested)
        effective.update({
            "perplexity": perplexity,
            "learning_rate": getattr(reducer, "learning_rate_", requested["learning_rate"]),
            "output_dimensions": 2,
        })
        if perplexity != requested["perplexity"]:
            derived_steps.append(f"Perplexity clamped from {requested['perplexity']:g} to {perplexity:g}")
        return ProjectionExecutionResult(
            np.asarray(coordinates, dtype=np.float32)[:, :2],
            requested_parameters=requested,
            effective_parameters=effective,
            derived_steps=tuple(derived_steps),
            repeatability=("repeatable_with_recorded_seed" if requested["random_state"] is not None
                           else "stochastic_execution"),
            citations=((dict(PCA_CITATION),) if any("PCA pre-reduction" in step for step in derived_steps) else ()),
        )
