from EmbeddingsArea.clustering.adapter import SklearnClusteringMethod
from EmbeddingsArea.clustering.parameters import (
    integer,
    number,
    choice,
    SEED,
    INIT,
)


class KMeansClusteringMethod(SklearnClusteringMethod):
    id = 'kmeans'
    name = 'K-Means'
    description = 'Partition all embeddings into a specified number of centroid-based groups.'
    parameters = (
        integer("n_clusters", "Number of clusters", 8), INIT,
        integer("n_init", "Initializations", 10, advanced=True), integer("max_iter", "Maximum iterations", 300, advanced=True),
        number("tol", "Tolerance", 1e-4), SEED,
        choice("algorithm", "Solver", "lloyd", ("lloyd", "elkan"), advanced=True),
    )

    def create_estimator(self, parameters):
        from sklearn.cluster import KMeans
        return KMeans(**parameters)
