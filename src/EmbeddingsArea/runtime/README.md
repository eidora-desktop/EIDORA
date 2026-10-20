# Embedding runtime

`pipeline.py` executes chunks; `contracts.py` defines inputs/progress/cancellation.
`artifacts.py` owns chunk manifests and output payloads, `checkpoints.py` records
resume state, and `readers.py` loads stored embeddings. `process_runner.py`
provides isolated execution.

Keep IDs, vectors, and identifiers aligned. Recovery counts only complete,
committed triples and checks their identities. Summaries must not load full vector
payloads. Preserve input/model fingerprints when resuming a run.

Add backends through [adapters](adapters/README.md). Keep runtime work Qt-free and
report completion only after artifacts are finalized.
