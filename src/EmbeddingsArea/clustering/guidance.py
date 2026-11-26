"""Cheap, advisory guidance from source dimensions and selected settings."""


def clustering_guidance(method_id, item_count, dimensions, preprocessing):
    """Return only the highest-priority note; thresholds are not resource limits."""
    if item_count <= 0 or dimensions <= 0:
        return ""
    large_matrix = item_count * dimensions * 4 >= 512 * 1024 * 1024
    large_dataset = item_count >= 100_000
    if method_id == "agglomerative" and item_count >= 10_000:
        return ("Agglomerative clustering may require substantial time and memory "
                "to build a hierarchy at this size.")
    if preprocessing.get("pca") and (large_dataset or large_matrix):
        return ("PCA may require substantial working memory at this size, "
                "including when using MiniBatch K-Means.")
    if large_matrix and method_id != "minibatch_kmeans":
        return ("This method prepares the full vector matrix in memory; "
                "additional working memory is needed during clustering.")
    if method_id == "kmeans" and large_dataset:
        return ("K-Means may take longer on this dataset. If speed is more important, "
                "consider MiniBatch K-Means; results may differ.")
    effective_dimensions = min(dimensions, preprocessing.get("components", dimensions)) if preprocessing.get("pca") else dimensions
    if method_id in {"dbscan", "hdbscan"} and item_count >= 10_000 and effective_dimensions > 100:
        return ("Density-based clustering can be slow with this many dimensions. "
                "PCA may help, but changes distances and can change the groups found.")
    return ""
