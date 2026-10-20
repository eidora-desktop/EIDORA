from EmbeddingsArea.clustering.adapter import SklearnClusteringMethod
from EmbeddingsArea.clustering.parameters import (
    integer,
    number,
    choice,
    boolean,
    METRIC,
    NEIGHBORS,
    POWER,
)


class HDBSCANClusteringMethod(SklearnClusteringMethod):
    id = 'hdbscan'
    name = 'HDBSCAN'
    description = 'Find density-based groups of varying density and leave outliers as Noise.'
    parameters = (
        integer("min_cluster_size", "Minimum cluster size", 10, minimum=2),
        integer("min_samples", "Minimum samples", None, nullable="Auto"), METRIC,
        choice("cluster_selection_method", "Cluster selection", "eom", ("eom", "leaf"), advanced=True),
        number("cluster_selection_epsilon", "Selection epsilon", 0), integer("max_cluster_size", "Maximum cluster size", None, advanced=True, minimum=2, nullable="Unlimited"),
        number("alpha", "Alpha", 1, minimum=1e-12), boolean("allow_single_cluster", "Allow single cluster"), NEIGHBORS,
        integer("leaf_size", "Leaf size", 40, advanced=True), POWER,
    )

    def create_estimator(self, parameters):
        from sklearn.cluster import HDBSCAN
        return HDBSCAN(**parameters)
