from EmbeddingsArea.clustering.adapter import SklearnClusteringMethod
from EmbeddingsArea.clustering.parameters import (
    integer,
    number,
    choice,
    boolean,
)


class AgglomerativeClusteringMethod(SklearnClusteringMethod):
    id = 'agglomerative'
    name = 'Agglomerative'
    description = 'Build hierarchical groups, cut by count or distance.'
    parameters = (
        choice("cut_by", "Cut hierarchy by", "count", ("count", "distance")),
        integer("n_clusters", "Number of clusters", 8, when=(("cut_by", ("count",)),)),
        number("distance_threshold", "Distance threshold", 1, advanced=False, when=(("cut_by", ("distance",)),)),
        choice("linkage", "Linkage", "ward", ("ward", "complete", "average", "single")),
        choice("metric", "Distance metric", "euclidean", ("euclidean", "manhattan", "cosine")),
        choice("compute_full_tree", "Compute full tree", "auto", ("auto", "true", "false"), advanced=True),
        boolean("compute_distances", "Record merge distances"),
    )

    def create_estimator(self, parameters):
        from sklearn.cluster import AgglomerativeClustering
        return AgglomerativeClustering(**parameters)
