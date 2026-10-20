from EmbeddingsArea.clustering.adapter import SklearnClusteringMethod
from EmbeddingsArea.clustering.parameters import (
    integer,
    number,
    METRIC,
    NEIGHBORS,
    POWER,
)


class DBSCANClusteringMethod(SklearnClusteringMethod):
    id = 'dbscan'
    name = 'DBSCAN'
    description = 'Find density-connected groups using a neighborhood radius; outliers become Noise.'
    parameters = (
        number("eps", "Neighborhood radius", .5, advanced=False, minimum=1e-12),
        integer("min_samples", "Minimum samples", 5), METRIC, NEIGHBORS,
        integer("leaf_size", "Leaf size", 30, advanced=True), POWER,
    )

    def create_estimator(self, parameters):
        from sklearn.cluster import DBSCAN
        return DBSCAN(**parameters)
