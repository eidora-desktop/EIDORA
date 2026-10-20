"""Shared scalar parameter constructors; descriptors are the authoritative schema."""
from EmbeddingsArea.clustering.base import ClusteringParameter as P


HELP = {
    "n_clusters": "Requested number of groups. Every eligible item is assigned to one group.",
    "n_components": "Number of Gaussian components; each item joins its highest-probability component.",
    "init": "Choose k-means++ for spread-out initial centers or random sampled centers.",
    "n_init": "Number of independent initializations used to choose a fitted solution.",
    "max_iter": "Maximum fitting iterations; convergence warnings are retained with the result.",
    "tol": "Convergence tolerance. Smaller values can require more fitting work.",
    "random_state": "Random seed used for reproducible initialization.",
    "batch_size": "Number of vectors processed per mini-batch.",
    "max_no_improvement": "Stop after this many mini-batches without improvement, or disable this stopping rule.",
    "init_size": "Number of samples used to initialize MiniBatch K-Means; Auto lets the backend choose.",
    "reassignment_ratio": "Controls reassignment of low-count MiniBatch centers; values closer to zero reassign less.",
    "min_cluster_size": "Smallest group HDBSCAN should retain; smaller components become Noise.",
    "min_samples": "Density neighborhood requirement. Larger values generally mark more items as Noise.",
    "metric": "Distance measured in the prepared embedding space, after optional normalization and PCA.",
    "p": "Minkowski exponent: 1 corresponds to Manhattan distance and 2 to Euclidean distance.",
    "cluster_selection_method": "EOM selects stable groups; leaf selects finer groups from the hierarchy.",
    "cluster_selection_epsilon": "Merge selected HDBSCAN groups separated by less than this distance.",
    "max_cluster_size": "Maximum selected group size for EOM selection; Unlimited removes this constraint.",
    "alpha": "Distance scaling used in HDBSCAN's robust single-linkage construction.",
    "allow_single_cluster": "Allow HDBSCAN to return one overall cluster.",
    "algorithm": "Computational strategy. Available neighbor searches depend on the distance metric.",
    "leaf_size": "Leaf size for tree-based neighbor searches; changes speed and memory use.",
    "cut_by": "Choose a fixed cluster count or cut the hierarchy at a distance threshold.",
    "distance_threshold": "Merge-distance threshold at which the hierarchy is cut.",
    "linkage": "How distance between groups is measured. Ward minimizes variance and requires Euclidean distance.",
    "compute_full_tree": "Whether to construct the complete hierarchy; required for a distance cut.",
    "compute_distances": "Compute hierarchy merge distances in the estimator.",
    "eps": "Maximum neighborhood distance for DBSCAN. Its scale depends on preprocessing and metric.",
    "covariance_type": "Diagonal models independent dimensions; full, tied, and spherical use other covariance constraints.",
    "reg_covar": "Nonnegative covariance regularization, useful for avoiding singular Gaussian components.",
    "init_params": "Strategy for initializing Gaussian components.",
    "normalize": "Scale nonzero embedding vectors to unit L2 length before optional PCA.",
    "pca": "Reduce embedding dimensions using PCA before clustering; does not use Discovery coordinates.",
    "components": "PCA output dimensions; cannot exceed either eligible row count or original dimensions.",
    "whiten": "Scale PCA components to unit variance, changing the distance geometry used for clustering.",
    "seed": "Random seed for PCA solvers that use randomized operations.",
}


def integer(key, label, default, *, advanced=False, minimum=1, nullable="", when=()):
    return P(key, label, "int", default, minimum, 2147483647, description=HELP.get(key, label),
             advanced=advanced, nullable=nullable, enabled_when=when)


def number(key, label, default, *, advanced=True, minimum=0, maximum=None, when=()):
    return P(key, label, "float", default, minimum, maximum, description=HELP.get(key, label),
             advanced=advanced, enabled_when=when)


def choice(key, label, default, choices, *, advanced=False, when=()):
    return P(key, label, "choice", default, choices=tuple(choices), description=HELP.get(key, label),
             advanced=advanced, enabled_when=when)


def boolean(key, label, default=False, *, advanced=True, when=()):
    return P(key, label, "bool", default, description=HELP.get(key, label), advanced=advanced, enabled_when=when)


SEED = integer("random_state", "Random seed", 42, advanced=True, minimum=0)
INIT = choice("init", "Initialization", "k-means++", ("k-means++", "random"), advanced=True)
METRIC = choice("metric", "Distance metric", "euclidean", ("euclidean", "manhattan", "cosine", "minkowski"))
NEIGHBORS = choice("algorithm", "Neighbor search", "auto", ("auto", "brute", "kd_tree", "ball_tree"), advanced=True)
POWER = number("p", "Minkowski power", 2, minimum=1, when=(("metric", ("minkowski",)),))
PREPROCESSING = (
    boolean("normalize", "L2 normalization", advanced=False),
    boolean("pca", "PCA", advanced=False),
    integer("components", "PCA components", 50, when=(("pca", (True,)),)),
    boolean("whiten", "PCA whitening", when=(("pca", (True,)),)),
    integer("seed", "PCA random seed", 42, advanced=True, minimum=0, when=(("pca", (True,)),)),
)

NORMALIZED_EMBEDDING_METHODS = frozenset({
    "kmeans",
    "minibatch_kmeans",
    "hdbscan",
    "agglomerative",
    "dbscan",
})


def preprocessing_defaults(method_id):
    """Recommended embedding preprocessing for a new algorithm configuration."""
    values = {parameter.id: parameter.default for parameter in PREPROCESSING}
    values["normalize"] = method_id in NORMALIZED_EMBEDDING_METHODS
    return values
