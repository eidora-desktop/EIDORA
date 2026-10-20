from __future__ import annotations

from EmbeddingsArea.clustering.base import ClusteringMethod, ClusteringMethodDescriptor, descriptor_for
from EmbeddingsArea.clustering.methods.kmeans.method import KMeansClusteringMethod
from EmbeddingsArea.clustering.methods.minibatch_kmeans.method import MiniBatchKMeansClusteringMethod
from EmbeddingsArea.clustering.methods.hdbscan.method import HDBSCANClusteringMethod
from EmbeddingsArea.clustering.methods.agglomerative.method import AgglomerativeClusteringMethod
from EmbeddingsArea.clustering.methods.dbscan.method import DBSCANClusteringMethod
from EmbeddingsArea.clustering.methods.gaussian_mixture.method import GaussianMixtureClusteringMethod


class ClusteringRegistry:
    """Discovers clustering methods available to EmbeddingsArea."""

    def __init__(self, methods: tuple[ClusteringMethod, ...] | None = None) -> None:
        if methods is None:
            methods = (
                KMeansClusteringMethod(),
                MiniBatchKMeansClusteringMethod(),
                HDBSCANClusteringMethod(),
                AgglomerativeClusteringMethod(),
                DBSCANClusteringMethod(),
                GaussianMixtureClusteringMethod(),
            )
        self._methods = methods

    def list_methods(self) -> list[ClusteringMethodDescriptor]:
        return [descriptor_for(method) for method in self._methods]

    def require_method(self, method_id: str) -> ClusteringMethod:
        normalized = method_id.strip().lower()
        for method in self._methods:
            if method.id.lower() == normalized:
                return method
        raise ValueError(f"Unknown clustering method: {method_id}")
