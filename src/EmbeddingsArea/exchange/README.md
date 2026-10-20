# Embedding interchange

`formats.py` defines supported layouts and writers. `importer.py` probes and
streams external data. `service.py` validates, stages, and publishes imports and
exports; `domain.py` carries inspection and mapping data.

Preview at most 25 vectors by 50 dimensions. Translate imported identifiers
through run ordinals to canonical metadata IDs. Stage the full data/sidecar set
before publication and restore the previous set if publication fails.

Advertise only formats the packaged application can write independently. New
dependency-backed formats also need release self-test and bundle coverage.
