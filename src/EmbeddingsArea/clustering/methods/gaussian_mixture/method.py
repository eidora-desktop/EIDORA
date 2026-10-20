from EmbeddingsArea.clustering.adapter import SklearnClusteringMethod
from EmbeddingsArea.clustering.parameters import (
    integer,
    number,
    choice,
    SEED,
)


class GaussianMixtureClusteringMethod(SklearnClusteringMethod):
    id = 'gaussian_mixture'
    name = 'Gaussian mixture'
    description = 'Fit Gaussian components and assign each item to its highest-probability group.'
    parameters = (
        integer("n_components", "Number of components", 8),
        choice("covariance_type", "Covariance", "diag", ("diag", "full", "tied", "spherical")),
        number("tol", "Tolerance", 1e-3), number("reg_covar", "Covariance regularization", 1e-6),
        integer("max_iter", "Maximum iterations", 100, advanced=True), integer("n_init", "Initializations", 3, advanced=True),
        choice("init_params", "Initialization", "kmeans", ("kmeans", "k-means++", "random", "random_from_data"), advanced=True), SEED,
    )

    def create_estimator(self, parameters):
        from sklearn.mixture import GaussianMixture
        return GaussianMixture(**parameters)
