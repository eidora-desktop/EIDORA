# Architecture

EIDORA Desktop is a PySide desktop application organized around product areas.
Project, metadata, embeddings, Discovery, and Figures workflows are implemented.
See the public [source guide](../src/README.md) for concise contributor rules.

The goal of this structure is to keep the project readable as it grows. UI wiring, application behavior, persistence, and computation should stay separate so each part can be tested and changed without pulling the whole application into the change.

## Current State

Implemented now:

- Application startup through `app`.
- Main window navigation between area pages.
- Feature-sliced area packages for project resources, metadata, embeddings, discovery, and figures.
- Designer-owned `.ui` files loaded at runtime.
- Area-level and panel-level controllers for UI composition.
- Shared Qt helpers for loading UI files, wiring panels, icons, deferred calls, worker signals, and cross-area activity feedback.
- Project manifest/layout validation, recent-project management, and ZIP import/export.
- Matplotlib figure composition, snapshot/slicer animation, GIF export, and saved tab settings.
- Metadata persistence and workflows: descriptions keyed by permanent item IDs, edits, exact-key imports, and search.
- Embeddings persistence/runtime workflows: media scanning/linking, run preparation, checkpointed pipeline execution, and artifact summaries.
- Discovery persistence, method execution, filtering, lasso, pins, snapshots, and camera workflows.
- A revisioned Discovery scene pipeline with spatial indexing and asynchronous preparation for large collections.
- A stable Python canvas adapter backed by Qt Quick and a native renderer, with bounded compatibility fallbacks.
- Revision-cancelled thumbnail planning, batched source resolution, and persistent native atlas rendering.
- ONNX embedding packages follow the [config schema](../automation/onnx_contract/ONNX_CONFIG_SCHEMA.md) and [preparation contract](../automation/onnx_contract/ONNX_PREPARATION_OPTIONS.md).
- Tests for startup imports, UI loading, area controller construction, icon resources, domain boundaries, and implemented project/metadata/embeddings/Discovery behavior.

Use the active workflow modules described in each area README. Unused service
scaffolds and legacy project import adapters have been removed.

## Package Layout

Top-level package:

```text
src/
  app.py                  Application entry point
  MainWindow/             Main shell and navigation
  ProjectArea/            Active-project media sources and thumbnail preparation
  MetadataArea/           Metadata import, table, and metadata workflows
  EmbeddingsArea/         Embedding runs, models, and clustering
  DiscoveryArea/         Discovery generation, canvas, filters, snapshots
  FiguresArea/            Figure setup, rendering, animation, export
  project/                Project domain, storage, item identity
  application_storage/    Storage preferences, verified relocation, resource cleanup
  shared/                 Cross-area helpers with no product ownership
```

Each feature area should remain internally consistent:

```text
FeatureArea/
  controllers/            Qt/PySide UI binders
  ui/                     Qt Designer files
  domain.py               Dataclasses and value objects
  service.py              Application behavior
  repository.py           Persistence owner, when needed
```

Areas may also have focused subpackages when the concept is large enough, such as `EmbeddingsArea/runtime`, `EmbeddingsArea/clustering`, `DiscoveryArea/canvas`, and `DiscoveryArea/discovery_methods`.

## Discovery Runtime

`DiscoveryCanvasWidget` is the stable controller-facing boundary. Controllers
provide aligned coordinate, metadata-ID, style, and visibility arrays without
depending on the active renderer implementation.

The Discovery runtime is split into focused responsibilities:

- `canvas/scene.py` builds immutable, revisioned scenes and shared indexes.
- `canvas/style_preparation.py` prepares only active visual mappings.
- `metadata_access.py` supplies cached, column-oriented metadata without full-row scans.
- `canvas/thumbnail_planner.py` produces deterministic viewport plans from the scene index.
- `controllers/thumbnail_plan_coordinator.py` owns latest-only worker cancellation and stale-result rejection.
- `canvas/widget.py` adapts scene, camera, interaction, and thumbnail contracts to the renderer.
- `canvas/native/` owns GPU buffers, shaders, atlas textures, and draw execution.

Coordinates, styles, visibility, thumbnails, and camera state have separate
revisions. Camera-only interaction updates transforms; it must not rebuild
scene data, redo metadata lookup, or recreate thumbnail membership while a
gesture is active. Painted and older native paths are compatibility fallbacks,
not independent homes for new Discovery behavior.

## Dependency Direction

Keep dependencies moving inward from UI to behavior to data:

```text
controllers -> services -> repositories/adapters
controllers -> domain
services -> domain
repositories/adapters -> domain
```

Avoid dependencies in the opposite direction:

- Domain modules must not import Qt, controllers, services, repositories, or persistence helpers.
- Services must not import Qt widgets or controllers.
- Repositories/adapters must not import Qt widgets or controllers.
- Shared modules must not import feature areas unless there is a deliberate, documented reason.
- Computation modules must not depend on UI objects.

Use callbacks, small protocols, or plain data objects when a lower layer needs to report status or progress upward.

## UI Placement

Durable screens, dialogs, cards, and panels belong in Qt Designer `.ui` files under each area package's `ui/` directory.

Qt wiring belongs in `controllers/`:

- Use `controllers/main_controller.py` for an area-level controller that composes the area page.
- Use `controllers/*_panel_controller.py` for one-panel UI binders.
- Keep signal-slot wiring, widget lookup, page switching, and UI state updates in controllers.
- Do not put domain computation or persistence in controllers.

Shared Qt helpers belong under `src/shared/qt`. Add helpers there only when they are reusable across multiple areas and have no feature-specific behavior.

## Domain, Services, And Persistence

Domain modules should contain stable language for the product: dataclasses, enums, IDs, and value objects. They should be easy to import in unit tests and should not require a QApplication.

Services hold application behavior. A service may coordinate domain objects, repositories, method implementations, and long-running work contracts. Services should expose plain Python inputs and outputs where possible.

Repositories and adapters own persistence. Each persisted concept should have one authoritative persistence owner. Avoid writing the same state from several modules.

Project-wide storage concerns belong under `project/` or `shared/persistence`; feature-owned data belongs in the feature area's repository or adapter layer.

Project identity, manifest, recent-entry, and path value objects live in
`project/domain.py`; filesystem validation and manifest I/O live in
`project/storage.py`. The storage module re-exports the moved types for compatibility.

Project storage compatibility is identified by a project-root `project.json` manifest. The current v1 manifest contains `schema_version` and `name`; project opening should validate this manifest and the required storage layout before returning a `Project`.

## Computation And Long-Running Work

Thumbnail creation uses `ProjectArea/thumbnail_service.py` to resolve linked
image or video media to canonical metadata IDs. `thumbnail_generator.py` resizes
images with a bounded worker pool and launches at most two isolated video workers
to decode first, middle, last, or deterministic-random stills through PyAV.
Decoder dependencies load only in those workers. Workers receive plain inputs,
never UI rows. `DiscoveryArea/thumbnails/repository.py` remains the storage owner
shared with Discovery: it stages generated files, publishes the variant,
provenance, and index in one transaction, and cleans unpublished files after
workers stop. Bad sources are counted separately from missing files; output or
persistence errors abort the job. Counts and a bounded error sample survive
project reopening.

Cancellation is cooperative until publication begins. Once publication commits,
the result is complete. A forced process exit may leave unlisted staging/orphan
files, but must not expose a partially generated variant. New variants contain
individual WebP thumbnails only; Discovery builds its own runtime atlases. Older
atlas and tile storage remains readable for project compatibility.

Embedding, clustering, discovery, rendering, and import/export computation should live outside Qt UI modules. Prefer focused modules with explicit inputs and outputs.

Qt workers may be used to run long operations, but the worker should call computation/services rather than implement the computation itself. This keeps the same behavior testable without a GUI.

## Shared Code

Use `shared/` for infrastructure that is genuinely cross-area:

- `shared/qt`: Qt safety, UI loading, widget helpers, icons, workers, and activity feedback.
- `shared/persistence`: generic persistence helpers.
- `shared/diagnostics`: logging and error helpers.
- `shared/icons`: icon resources and manifest.

Do not put feature behavior in `shared/` just because two places might eventually use it. Start inside the owning area, then extract only when duplication is real.

## Adding New Code

Use this placement checklist:

- Is it signal-slot wiring, widget lookup, or visual state? Put it in a controller.
- Is it a Designer-maintained screen, dialog, panel, or card? Put it in `ui/`.
- Is it a product concept with no behavior? Put it in `domain.py`.
- Is it user-visible application behavior? Put it in `service.py` or a focused service submodule.
- Does it read or write durable state? Put it in a repository or adapter.
- Is it reusable Qt safety code? Put it in `shared/qt`.
- Is it numerical, embedding, discovery, clustering, or rendering computation? Put it outside controllers and pass plain data in and out.

Keep changes small. Prefer clear module names and direct functions/classes over early abstractions. Add an abstraction only when it removes real duplication or defines a stable boundary.

## Tests

Add regression tests at the layer where the behavior lives:

- Domain and service tests should be plain unit tests with no Qt application.
- Repository tests should verify current-format persistence and reopening.
- Controller tests should verify UI wiring, widget presence, and signal behavior.
- GUI smoke tests should stay small and focus on whether core screens load and compose correctly.
- Boundary tests should continue to prevent domain and service modules from importing Qt.

Capture recurring architectural issues in this document so contributors can follow the current guidance.

## UI surfaces and system themes

`shared.qt.theme` owns the light/dark tokens and follows the system color scheme.
Install it before constructing windows; the shared UI loader prepares new forms.
The theme owns named surface, text, selection, secondary/error text, and control
border tokens. A single application stylesheet paints named surface backgrounds, row hover/selection,
button hover/pressed/checked states, headers, subtle grids, and semantic label
roles. Designer styles retain precedence; Filters and Transform control lists
keep transparent rows. Do not copy global rules into each form or cache copies
of those rules on individual views. Use `textRole="secondary"` for explanatory
labels and `textRole="error"` for error labels.

Palette refresh is coalesced until native stylesheet repolishing finishes.
Unchanged styles are not reapplied. Semantic icons retain their tint in every
enabled state; Qt still dims disabled icons. Contrast tests cover theme-owned
text on surfaces (4.5:1) and active-control outlines (3:1).

Designer surfaces declare a dynamic string property (`stdset="0"`):
`surfaceRole` is `window`, `area`, `panel`, `group`, or `view`. The main shell
uses `window`, area pages use `area`, visible panels/dialogs/cards use `panel`,
and control groups use `group`. The derived `view` role uses the native
`QPalette.Base` color so a surface can match list, tree, and table backgrounds.
Layout wrappers remain unmarked so they inherit the surrounding surface.
Object names do not select colors.
Surface palettes also expose the current background to custom painters. Native
palette changes alone must not own surface painting: stylesheet-backed panels
and detached settings windows need the shared rules to repaint live.

## Simplified Project and metadata workflows

Project composes Media Sources and Thumbnails in one horizontal splitter. The
Artifacts UI is removed; internal thumbnail/embedding artifacts remain. Project
linking exposes only exact relative paths or record IDs to one metadata column,
with separate Link and source-scoped Relink actions. Scanning never creates rows.

Metadata importing owns four fixed operations: initial import, append unmatched
items, update matching rows (nonblank values), and authoritative table replacement.
Replacement retains canonical IDs and rejects referenced removals, including
saved snapshots and field settings. Staging tokens, source and metadata
fingerprints, transaction-time dependency checks, and scheduler reservation guard
publication. No project migration or canonical ID rewrite is involved. Legacy
embedding preparation helpers remain behind their existing compatibility API.

## Independent analysis items

`project/item_repository.py` owns `project_items` in the metadata database.
Migration retains existing IDs; a metadata insert trigger and a shared allocation
high-water mark prevent collisions with asset-only items. Metadata replacement
never resets this registry. Removed, unused metadata IDs remain reserved without
appearing in the active collection. Active IDs combine current metadata rows and
asset identities explicitly prepared for analysis. Existing `metadata_id` API and
array names remain compatibility names for permanent item IDs.

Model configuration offers metadata text columns and linked indexed assets.
Preparation materializes selected text columns in the worker and uses existing
row identities. Linked assets use their linked identities; unlinked assets are
excluded from embedding runs. Discovery, groups, and thumbnail resolution accept
the resulting IDs. The automatic random layout is a disposable view, while
user-saved analyses and annotations remain dependencies.
