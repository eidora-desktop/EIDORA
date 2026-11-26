# Embeddings

Start with `session.py`, which assembles project-scoped services and repositories.
`service.py` contains run, dataset, and catalog services and reexports the media
services. `media_library.py` handles source workflows; `media_linking.py` builds
compact, revision-checked plans from one selected metadata column.
`repository.py` owns SQLite stores and artifact access. `model_catalog.py`
discovers and installs models.

Finish planning reads before publication. Prepared associations are written in
2,000-row batches through one connection; matching attaches the media database to a metadata-owned transaction so
approved associations commit together. Folder discovery streams into disposable SQLite storage before the
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

Download sizes use Hub file metadata (`blobs=true`) and the config at the same
immutable repository revision. Count declared artifacts and present optional
installer files once, excluding unrelated repository contents. Share the optional
file list between the HTTP and Hub-client installers and size lookup. Cache only
successful totals; missing sizes or required files must not produce partial totals.
The dialog displays model cards before size requests finish, retries unavailable
sizes on selection/reopening, and invalidates its size cache on forced refresh.

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

The local-model dialog offers only registered file-based Media Sources, filtered
by the model's input modalities. It processes available assets without a scope
selector or metadata-key pairing. Metadata columns are not offered, even if an
older run registered one as a text source. Existing saved runs retain their input
snapshots and service-level compatibility.

Text files are whole documents regardless of extension, including CSV and JSONL.
Importing a file does not select columns or expand records. Existing indexed text
records and materialized metadata text retain their explicit record keys in run
snapshots; previews and inference use the same distinction. Rescanning an old
file source now catalogs the file as one document. Previously prepared runs must
be prepared again before resuming with the updated preparation contract.
Text preparation uses UTF-8 or BOM-marked UTF-16/32, rejects empty/invalid input
with its path, and limits a document/record to 2,000,000 characters. Tokenization
uses the package's existing length, truncation and padding settings; full-document
chunking and format-specific extraction are not performed.

**Run Model** saves the configuration and closes the dialog immediately. The
embedding card opens with input preparation running in its background job, followed
by inference. Preparation supports pause/cancel and reports failures on the card
for retry. Optional Preview Assets creation starts as a separate Media job only after
the embedding completes and releases its worker. The current workspace stays open;
the footer tracks the Preview Assets job. The assets use the same visual source,
original aspect ratio, and the middle frame for video; model tensor resizing does
not affect previews. The Designer-owned
focused form presents the model heading, Media Source, output name, and execution
device in that order. Selected-source counts and paths appear below the inputs.
Batch size and model preparation settings are under collapsed
**Advanced settings & parameters**. A single modality needs no existing item links.
Multiple selected modalities require established associations to the same items;
only complete combinations are processed. Ambiguous item assignments are rejected.
Preparation creates no items. Only successful vectors
publish item bindings and media associations in the same transaction as the run.
Repeated observations reuse unambiguous identities; failed publication rolls back.
Source, metadata, and file revisions guard against stale prepared input.

Artifact ordinals remain the mapping into run inputs. Relocating media updates
future preparation and preview lookup without rewriting saved run provenance.
Deleting a source or vector set does not implicitly delete its items.

Completed vector-data and clustering card menus expose **Methods & Citations…**,
also available through the project menu. Reports start with brief methods and
references; detailed parameters and technical records remain expandable.
`provenance.py` formats saved model metadata, execution/preprocessing settings,
and clustering parameters without running analysis. New embedding artifacts
retain the model package's citation metadata and version. When references are
missing, saved-run reports consult the installed package with the same model ID
and recorded version (when available). Recovered references are labeled with
their source; saved artifact metadata is unchanged. Unmatched models retain an
explicit missing-citation warning.

ONNX configs require top-level `bibtex` with self-contained model references.
`model_package.py` validates the records; the runtime freezes the exact config
text in artifact provenance. Citation reports prefer this frozen BibTeX and
retain metadata-based fallbacks for older saved runs.

Vector Data’s **PCA** tab saves PCA or Incremental PCA results as normal
vector artifacts. The Incremental checkbox selects batch fitting. Copy names use
`Model name (PCA_48)` or `Model name (PCAin_48)`, with the source name
as fallback for imported vectors. Replacement starts with the existing name and
the same suffix; both output modes allow editing the name. It defaults to a copy, accepts a target dimension count and
estimates vector payload storage (excluding metadata and the fitted transform).
64-bit inputs use float64; other widths use float32. Whitening and automatic
normalization are disabled. Explained variance is saved with the method, source
artifact identity, dimensions, solver/batch settings and scikit-learn version;
constant inputs have undefined retained variance. The fitted mean and components
are stored in the result artifact for reproducibility.

Creating a reduced copy immediately creates its destination card and opens its
**Progress** tab. This card owns the source-import history, stage status, progress
bar and cancellation; the original card retains its existing status and history.
The destination identity stays the same when its saved artifact is published.
Failed or cancelled unpublished copies remain visible until dismissed or the
project is closed. Replacement runs stay in the existing card.
Reduction uses the same progress bar and Cancel button as
embedding generation. These controls remain available with details collapsed;
Pause is disabled because PCA does not support resumable fitting.
All activity text appears in the Progress textbox, with rapid live updates
coalesced once per second between stage messages. Collapsed cards contain only
their header and progress controls.
Batch counts update without appending repeated log entries;
ordinary PCA fitting shows indeterminate progress. The report includes method,
source, vector count, original/reduced dimensions and output choice. Completed
artifacts retain the report summary, processing duration and explained variance
relative to the immediate source, including after reopening the project.

**Info** shows origin, model, source, vector count, dimensions, actual artifact
storage, precision, normalization and creation date, plus a reduction summary
when applicable. **Technical details** starts collapsed and contains saved
settings, identifiers, preprocessing and provenance. Missing historical metadata
is shown as not recorded; import filenames are recorded for new imports.

Reduction runs in a cancellable spawned process through the area coordinator.
Incremental PCA reads compressed inputs through disk-backed arrays, fits bounded
batches and transforms in a second pass. Its final fitting batch includes any
short tail. PCA can require the full input matrix in memory. Both methods require
temporary disk space. Item ordinals and all run input bindings remain intact.

Replacement publishes a new artifact path and commits the run pointer before
removing owned original files and runtime caches. A failed computation or database
publication leaves the original intact. Cleanup failures are reported separately
from successful publication. Saved projections, clusters and figures remain as
previously computed; future analyses use the reduced vectors.

Embedding-run methods reports describe the model, input data, preprocessing and
resulting vectors. Their bibliography includes recorded model and processing
references, without automatically adding EIDORA.
