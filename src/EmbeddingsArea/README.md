# Embeddings

Start with `session.py`, which assembles project-scoped services and repositories.
`service.py` contains run, dataset, and catalog services and reexports the media
services. `media_library.py` handles source workflows; `media_linking.py` builds
compact, revision-checked plans from one selected metadata column.
`repository.py` owns SQLite stores and artifact access. `model_catalog.py`
discovers and installs models.

Finish planning reads before publication. Prepared associations are written in
2,000-row batches through one connection; creating metadata attaches the media
database to a metadata-owned transaction so allocated IDs and links commit
together. Folder discovery streams into disposable SQLite storage before the
catalog transaction starts. Never feed a live second-connection reader into a
write transaction: rollback-journal cache spilling can stall the second batch.

Repositories create the current schema directly and store explicit matching
settings separately. Construction of an up-to-date repository does not scan or
audit assets. Automatic upgrades of obsolete media and embedding schemas are
not supported.

Desktop owns the [schema-v3 package contract](../../automation/onnx_contract/ONNX_CONFIG_SCHEMA.md)
and [global preparation catalog](../../automation/onnx_contract/ONNX_PREPARATION_OPTIONS.md).
ONNX_pipelines keeps a synchronized snapshot and validates through Desktop.
Start with the human overview; use the technical specification and config schema
for model requirements, preprocessing, validation, and package configuration.

Model Zoo uses strict download/install validation. The temporary schema-repair
switch `TEMPORARILY_ALLOW_UNVALIDATED_MODEL_DOWNLOADS` in `model_catalog.py` is
`False`. Setting it to `True` downloads all repository
files without manifest, checksum, compatibility, or ONNX install validation.
Original files are preserved for local editing; `.eidora-download.json` keeps
unreadable packages visible as requiring repackaging. Normal execution validation
still applies, and refreshing the local catalog reads repaired configurations.
Set the switch to `False` to restore the original strict download/install path.
Receipt reading remains available so previously downloaded packages stay visible.

Schema-v3 multimodal packages keep aligned encoders in one installed model:
`modality: multimodal`, `runtime.components` (one runtime/preparation mapping per
modality), and `runtime.fusion: normalized_mean`. Every encoder shares the root
embedding declaration and returns normalized vectors. Users can select one or
more modalities. `input_preparation.py` pairs them by permanent item ID, includes
only items with all selected inputs, and rejects ambiguous multiple inputs.
The runtime returns one normalized equal-weight mean per item. Component snapshots
are stored beside prepared inputs, retaining the existing ordinal-to-item mapping.
This is application-level late fusion, not a joint cross-attention model.

- [runtime](runtime/README.md): chunk execution, adapters, checkpoints and artifacts.
- [exchange](exchange/README.md): validated embedding import/export.
- [clustering](clustering/README.md): algorithms and publication into Datasets groups.
- [controllers](controllers/README.md): model/run panels and job coordination.

Artifact object IDs are prepared-run ordinals. Translate them through run inputs
before using canonical metadata IDs for groups, Discovery, or thumbnails.
Keep model/backend imports lazy and heavy work off the GUI thread.
Preserve explicit accelerator choices; unavailable explicit devices must fail clearly.

Model inputs may be linked indexed assets or metadata text columns.
Listing input choices does not materialize text or allocate items. Preparation
runs in the existing background workflow: metadata text is materialized then and
uses its existing row IDs; linked assets retain their linked IDs; and unlinked
assets are excluded. No metadata rows or media links are manufactured by
embedding execution. Scanning alone still only indexes assets. Prepared-run
ordinals continue to map to permanent IDs through run inputs; the persisted
`metadata_row_id` name is retained for compatibility. An explicit later media
link affects future run preparation, never rewrites IDs inside existing saved
results. Whole assets are the current analysis unit; future segment-producing
workflows must allocate separate identities for their units.

Completed vector-data and clustering card menus expose **Citations…**.
`provenance.py` formats saved model metadata, execution/preprocessing settings,
and clustering parameters without running analysis. New embedding artifacts
retain the model package's citation metadata and version; older artifacts report
missing citations explicitly rather than substituting the current catalog.

ONNX configs require top-level `bibtex` with self-contained model references.
`model_package.py` validates the records; the runtime freezes the exact config
text in artifact provenance. Citation reports prefer this frozen BibTeX and
retain metadata-based fallbacks for older saved runs.
