from EmbeddingsArea.clustering.adapter import SklearnClusteringMethod
from EmbeddingsArea.clustering.parameters import (
    integer,
    number,
    SEED,
    INIT,
)


class MiniBatchKMeansClusteringMethod(SklearnClusteringMethod):
    id = 'minibatch_kmeans'
    name = 'MiniBatch K-Means'
    description = 'Approximate centroid clustering for large collections.'
    parameters = (
        integer("n_clusters", "Number of clusters", 8), integer("batch_size", "Batch size", 1024), INIT,
        integer("n_init", "Initializations", 3, advanced=True), integer("max_iter", "Maximum iterations", 100, advanced=True),
        number("tol", "Tolerance", 0), integer("max_no_improvement", "Maximum no-improvement steps", 10, advanced=True, minimum=0, nullable="Disabled"),
        integer("init_size", "Initialization size", None, advanced=True, nullable="Auto"),
        number("reassignment_ratio", "Reassignment ratio", .01, maximum=1), SEED,
    )

    def create_estimator(self, parameters):
        from sklearn.cluster import MiniBatchKMeans
        return MiniBatchKMeans(**parameters)
