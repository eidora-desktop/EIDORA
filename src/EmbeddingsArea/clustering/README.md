# Embedding clustering

`service.py` coordinates runs; `pipeline.py` performs computation; `registry.py`
registers algorithms. Datasets owns published groups and memberships.

Cluster original embeddings and translate run ordinals to canonical metadata IDs.
Keep IDs and labels aligned; only `-1` denotes noise, which is excluded from the
cluster count. Publish results and exclusive group memberships in one database
transaction. Report completion only after commit.

Published group sets own their display names. Clustering reads resolve the name
through the saved group-set ID, so edits from Custom Groups appear in result
cards too. Renaming a result updates its group set and record in one transaction;
keep group IDs and memberships unchanged, and notify group-dependent panels.

Long-running computation uses a child process. Abort the process, never a Qt
thread. Deleting result history must preserve groups unless their removal is
explicitly requested.

The Advanced settings offer global CPU usage presets: Low (25%), Middle (50%),
High (75%, the default), and Maximum (100%). The selected percentage of available
logical CPUs is rounded down, with at least one thread. CPU affinity and quotas
are respected; there is no fixed eight-thread cap or environment override.
The preference is saved immediately in application preferences, independently
of the project and method defaults. Each run captures its budget when it starts
and uses it for both PCA and fitting; running jobs are unaffected by later edits.
K-Means uses OpenMP, density methods use shared-memory neighbor workers, and
PCA/mixtures use BLAS where supported. Nested numerical pools stay single-threaded
to avoid oversubscription; serial algorithm stages remain serial. Result
diagnostics record the CPU budget. No additional worker processes are started.

MiniBatch K-Means streams source vectors through filtering and normalization in
approximately 8 MiB slices into a temporary C-contiguous float32 memory map.
NPZ arrays are decompressed to temporary disk when no valid cache exists; chunked
runs are read one chunk at a time. Identifier strings are not extracted from NPZ.
The existing sklearn MiniBatch solver reads the prepared map, preserving seeds,
initialization, stopping rules and final labels rather than introducing a separate
`partial_fit` training loop. Ordinal/canonical ID checks still span all batches.
Temporary files belong to the cancellable job and mappings close before cleanup.
This trades temporary disk space for fewer full-matrix RAM copies; IDs, solver
scratch and OS-cached mapped pages still consume memory. Optional PCA retains its
existing algorithm and may allocate a full working matrix. It is not incremental PCA.

The clustering dialog reuses the Vector Data card's source count and dimensions;
guidance never loads vectors or probes available RAM. One non-blocking note is
selected in priority order: agglomerative at 10,000 vectors, PCA at 100,000 vectors
or a 512 MiB float32 matrix, other full-matrix methods at 512 MiB, K-Means at
100,000 vectors, then density methods at 10,000 vectors and over 100 dimensions
(after selected PCA). These are advisory thresholds, not resource estimates or
execution limits. Unknown sizes show no advice. Changing methods, preprocessing,
or restoring defaults refreshes the note without changing settings or Run eligibility.

## Adding an algorithm

1. Add `methods/<stable_id>/method.py` and `__init__.py`, following `base.py` and
   `SklearnClusteringMethod`.
2. Declare the parameter schema in the adapter; import the estimator locally
   inside `create_estimator()` to keep startup light.
3. Register it in `registry.py` and update the packaged dynamic-module inventory.
4. Verify parameter validation, aligned labels, expected partitions, cancellation,
   and execution in the packaged application's subprocess self-test.
