# Clustering algorithms

Each subfolder supplies one sklearn adapter. `method.py` owns its parameter
schema and estimator construction; the parent `registry.py` registers it.

Import sklearn locally when constructing an estimator. Return labels aligned
with input rows; only `-1` means noise. New algorithms need validation,
determinism, cancellation and packaged-process coverage. Follow the
[clustering contract](../README.md#adding-an-algorithm); do not duplicate settings in YAML.
