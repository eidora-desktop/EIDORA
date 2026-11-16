# Embedding interchange

`formats.py` defines supported layouts and writers. `importer.py` probes and
streams external data. `service.py` validates, stages, and publishes imports and
exports; `domain.py` carries inspection and mapping data.

Preview at most 25 vectors by 50 dimensions. Translate imported identifiers
through run ordinals to canonical metadata IDs. Stage the full data/sidecar set
before publication and restore the previous set if publication fails.

Advertise only formats the packaged application can write independently. New
dependency-backed formats also need release self-test and bundle coverage.

Import accepts a selection of files as shards of one set or as separate sets,
including one-dimensional NumPy files (one vector per file). Structural detection
recognizes arbitrary vector/identifier field names, JSON arrays, numeric arrays,
comma/space/semicolon vector strings and numbered feature columns. Ambiguous
fields stay selectable under Import options. A batch must have one dimension
and unique record identifiers; use generated identities for independent observations.
Different named vector fields in different shards are detected per member.

Probes inspect headers/samples, not every vector. Validation streams all values
before publication; that complete validation is necessary to reject malformed or
nonfinite vectors. Related descriptive fields are retained in Items. The dialog always imports every
vector, automatically linking exact identifiers to existing Items and creating missing
Items. A same-named identifier field takes precedence; otherwise matching fields
must agree on the Item. Ambiguous identities block publication. Bare arrays create
new Items without guessing from row order. Conflicting descriptive values require a keep/incoming choice.
Multiple observations may share a media source while retaining separate identities.

The dialog probes, validates and publishes off the GUI thread. It captures widget
values before starting work, coalesces progress updates, reserves completion until
publication, and handles the completed run before any metadata refresh. Vector Data
refreshes and selects the published run immediately. No restart is required.

New-item imports retain source label fields once under their supplied names. The
importer does not add an embedding_identifier column. Bare arrays without labels
create permanent item IDs without a visible generated label; generated run keys
remain internal. Named label arrays are staged as source descriptions, preserving
their values independently of normalized matching keys. Companion labels are kept
separately only when they differ from an embedded field of the same name. Existing
project columns are not removed by this import change.

The import dialog follows Metadata Import: filename and file details, a read-only
preview with Item links, collapsed Import options, inline errors, and Cancel/Import
actions. Destination tables and manual linking strategies are not user choices.
