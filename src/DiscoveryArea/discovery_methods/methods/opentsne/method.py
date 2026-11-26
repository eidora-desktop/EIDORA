from __future__ import annotations

from collections.abc import Mapping
import os

import numpy as np

from DiscoveryArea.discovery_methods.base import PCA_CITATION, DiscoveryMethodBase, ProgressCallback, ProjectionExecutionResult


class OpenTSNEDiscoveryMethod(DiscoveryMethodBase):
    method_id = "opentsne"
    name = "t-SNE (openTSNE)"

    def fit_transform(
        self,
        vectors: np.ndarray,
        parameters: Mapping[str, object],
        progress_callback: ProgressCallback | None = None,
    ) -> ProjectionExecutionResult:
        from DiscoveryArea.method_catalog import DiscoveryMethodCatalog

        data = np.asarray(vectors, dtype=np.float32)
        if data.ndim != 2 or data.shape[0] < 3 or data.shape[1] < 1:
            raise ValueError("t-SNE (openTSNE) requires at least three embedding vectors.")
        if not np.isfinite(data).all():
            raise ValueError("t-SNE (openTSNE) requires finite embedding values.")
        requested = DiscoveryMethodCatalog().normalize_parameters(self.method_id, dict(parameters))
        kwargs = dict(requested)
        pca_dimensions = kwargs.pop("pca_dimensions")
        jobs = kwargs["n_jobs"]
        if jobs == 0 or jobs < -1:
            raise ValueError("Workers must be -1 or a positive integer.")
        # The projection subprocess sets OMP_NUM_THREADS to its CPU budget.
        available = max(1, os.cpu_count() or 1)
        budget = max(1, min(available, int(os.environ.get("OMP_NUM_THREADS", min(8, max(1, available - 1))))))
        kwargs["n_jobs"] = budget if jobs == -1 else min(jobs, budget)
        kwargs["perplexity"] = min(requested["perplexity"], (len(data) - 1) / 3)
        # Keep auto neighbor search independent of optional Annoy installations.
        if kwargs["neighbors"] == "auto":
            kwargs["neighbors"] = "exact" if len(data) < 1000 else "pynndescent"
        # Match openTSNE 1.0.4's auto rule and record the executed backend.
        if kwargs["negative_gradient_method"] == "auto":
            kwargs["negative_gradient_method"] = "bh" if len(data) < 10000 else "fft"
        steps: list[str] = []
        citations = ()
        if progress_callback is not None:
            progress_callback(45)
        if pca_dimensions > 0 and data.shape[1] > pca_dimensions:
            from sklearn.decomposition import PCA

            components = min(pca_dimensions, data.shape[0] - 1, data.shape[1] - 1)
            data = np.asarray(PCA(n_components=components, random_state=requested["random_state"]).fit_transform(data), dtype=np.float32)
            steps.append(f"PCA pre-reduction to {components} dimensions")
            citations = (dict(PCA_CITATION),)
        if kwargs["initialization"] == "pca" and data.shape[1] < 2:
            kwargs["initialization"] = "random"
            steps.append("Random initialization used because the input has fewer than two dimensions")
        if kwargs["perplexity"] != requested["perplexity"]:
            steps.append(f"Perplexity clamped from {requested['perplexity']:g} to {kwargs['perplexity']:g}")
        if progress_callback is not None:
            progress_callback(55)

        from openTSNE import TSNE

        reducer = TSNE(
            n_components=2, **kwargs,
            callbacks=_OptimizationProgress(progress_callback, kwargs["early_exaggeration_iter"], kwargs["n_iter"]),
            callbacks_every_iters=25,
        )
        embedding = reducer.fit(data)
        coordinates = np.asarray(embedding, dtype=np.float32)
        if coordinates.shape != (len(data), 2) or not np.isfinite(coordinates).all():
            raise ValueError("t-SNE (openTSNE) produced invalid coordinates.")
        if progress_callback is not None:
            progress_callback(75)
        effective = dict(requested)
        effective.update(kwargs)
        effective.update({
            "output_dimensions": 2,
            "early_exaggeration": reducer.early_exaggeration,
            "early_learning_rate": (len(data) / reducer.early_exaggeration
                                    if requested["learning_rate"] == "auto" else requested["learning_rate"]),
            "learning_rate": (len(data) / (requested["exaggeration"] or 1)
                              if requested["learning_rate"] == "auto" else requested["learning_rate"]),
            "kl_divergence": float(embedding.kl_divergence),
        })
        return ProjectionExecutionResult(
            coordinates, requested_parameters=requested, effective_parameters=effective,
            derived_steps=tuple(steps), citations=citations,
            repeatability=("repeatable_with_recorded_seed" if requested["random_state"] is not None
                           else "stochastic_execution"),
        )


class _OptimizationProgress:
    """Map openTSNE's two optimization phases to bounded application progress."""

    def __init__(self, callback: ProgressCallback | None, early: int, normal: int):
        self.callback = callback
        self.early = early
        self.total = early + normal
        self.phase = -1
        self.last = 55

    def optimization_about_to_start(self):
        self.phase += 1

    def __call__(self, iteration, error, embedding):
        completed = iteration + (self.early if self.phase > 0 else 0)
        value = min(74, 55 + int(20 * completed / max(1, self.total)))
        if self.callback is not None and value > self.last:
            self.callback(value)
            self.last = value
        return False
