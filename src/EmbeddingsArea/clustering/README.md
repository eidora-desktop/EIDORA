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

## Adding an algorithm

1. Add `methods/<stable_id>/method.py` and `__init__.py`, following `base.py` and
   `SklearnClusteringMethod`.
2. Declare the parameter schema in the adapter; import the estimator locally
   inside `create_estimator()` to keep startup light.
3. Register it in `registry.py` and update the packaged dynamic-module inventory.
4. Verify parameter validation, aligned labels, expected partitions, cancellation,
   and execution in the packaged application's subprocess self-test.
