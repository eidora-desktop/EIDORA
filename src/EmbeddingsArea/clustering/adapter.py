"""Small sklearn adapters: no storage or UI dependencies."""
from __future__ import annotations
from EmbeddingsArea.clustering.base import ClusteringOutput, descriptor_for, normalize_parameters


class SklearnClusteringMethod:
    def descriptor(self):
        return descriptor_for(self)

    def validate(self, parameters, row_count):
        p = normalize_parameters(self.parameters, parameters)
        count = p.get("n_components", p.get("n_clusters"))
        if p.get("cut_by") == "distance":
            count = None
        if count is not None and count > row_count:
            raise ValueError("Cluster count cannot exceed the number of eligible embeddings.")
        if p.get("linkage") == "ward" and p.get("metric") != "euclidean":
            raise ValueError("Ward linkage requires Euclidean distance.")
        if p.get("cut_by") == "distance" and p["compute_full_tree"] == "false":
            raise ValueError("Distance cuts require full-tree computation.")
        if p.get("metric") == "cosine" and p.get("algorithm") not in (None, "auto", "brute"):
            raise ValueError("Cosine distance requires auto or brute neighbor search.")
        if p.get("min_samples") is not None and self.id == "hdbscan" and p["min_samples"] > row_count:
            raise ValueError("Minimum samples cannot exceed eligible embeddings.")
        if p.get("min_cluster_size", 0) > row_count:
            raise ValueError("Minimum cluster size cannot exceed eligible embeddings.")
        if p.get("max_cluster_size") is not None and p["max_cluster_size"] < p["min_cluster_size"]:
            raise ValueError("Maximum cluster size must be at least the minimum cluster size.")
        if p.get("init_size") is not None and p["init_size"] > row_count:
            raise ValueError("Initialization size cannot exceed eligible embeddings.")
        if p.get("init_size") is not None and p["init_size"] < p["n_clusters"]:
            raise ValueError("Initialization size must be at least the cluster count.")
        return p

    def available_choices(self, values):
        allowed = {}
        if values.get("linkage") == "ward":
            allowed["metric"] = ("euclidean",)
        elif "linkage" in values:
            allowed["metric"] = ("euclidean", "manhattan", "cosine")
        if "compute_full_tree" in values:
            allowed["compute_full_tree"] = ("auto", "true") if values.get("cut_by") == "distance" else ("auto", "true", "false")
        if "metric" in values and "algorithm" in values:
            allowed["algorithm"] = ("auto", "brute") if values["metric"] == "cosine" else ("auto", "brute", "kd_tree", "ball_tree")
        return allowed

    def estimator_parameters(self, p):
        p = dict(p)
        if p.get("metric") == "cosine" and p.get("algorithm") == "auto":
            p["algorithm"] = "brute"
        if self.id == "agglomerative":
            cut = p.pop("cut_by")
            if cut == "count":
                p["distance_threshold"] = None
            else:
                p["n_clusters"] = None
                p["compute_full_tree"] = "true"
            if p["compute_full_tree"] != "auto":
                p["compute_full_tree"] = p["compute_full_tree"] == "true"
        if self.id == "hdbscan":
            p["copy"] = True
            power = p.pop("p")
            if p["metric"] == "minkowski":
                p["metric_params"] = {"p": power}
            p["n_jobs"] = 1
        if self.id == "dbscan":
            if p["metric"] != "minkowski":
                p.pop("p")
            p["n_jobs"] = 1
        if self.id == "minibatch_kmeans":
            p["compute_labels"] = True
        return p

    def fit(self, embeddings, parameters):
        import numpy as np
        from threadpoolctl import threadpool_limits
        values = np.asarray(embeddings, dtype=np.float32)
        if values.ndim != 2:
            raise ValueError("Embeddings must be a two-dimensional matrix.")
        if len(values) < 2 or not values.shape[1]:
            raise ValueError("At least two embeddings are required for clustering.")
        if not np.isfinite(values).all():
            raise ValueError("Embeddings contain non-finite values.")
        p = self.estimator_parameters(self.validate(parameters, len(values)))
        estimator = self.create_estimator(p)
        with threadpool_limits(limits=1):
            labels = estimator.fit_predict(values)
            strengths = None
            if self.id == "gaussian_mixture":
                # Bounded probability batches; never retain the full n-by-k matrix.
                strengths = np.concatenate([estimator.predict_proba(values[i:i+1024]).max(axis=1)
                                            for i in range(0, len(values), 1024)])
            elif self.id == "hdbscan":
                strengths = estimator.probabilities_
        diagnostics = {"resolved_parameters": p}
        for key in ("n_iter_", "converged_", "inertia_", "lower_bound_"):
            if hasattr(estimator, key):
                value = getattr(estimator, key)
                diagnostics[key.rstrip("_")] = value.item() if hasattr(value, "item") else value
        return ClusteringOutput(np.asarray(labels, dtype=np.int32), strengths, diagnostics)

    def fit_predict(self, embeddings, parameters):
        return self.fit(embeddings, parameters).labels
