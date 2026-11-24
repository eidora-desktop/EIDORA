# Projects and application storage

New installed copies of EIDORA create projects in the platform's Documents folder,
under `EIDORA/Projects`. New Project shows the full destination and lets you choose
another parent folder, including an external drive. Each project is a directory
containing its metadata, analyses, saved views, and other project-owned files.
The project list's **Open Project Folder** action opens that directory.

Source checkouts retain their `workspace_projects` default. An
`EIDORA_WORKSPACE_ROOT` environment override takes precedence over saved choices.
Existing installations continue using a nonempty legacy workspace until the user
explicitly relocates it. Existing projects are never moved on startup.

## Storage controls

**Settings... → Storage** in the application menu or project chooser shows the
project, model, and cache locations and measures their sizes in the background.
Use the trash icon beside the model size to remove downloaded models, then
confirm the action. This removes EIDORA-managed model downloads; projects,
manually installed models, and application caches are preserved. Cache location
and size remain visible for inspection. A progress bar
appears only when relocation or deletion lasts longer than half a second; size
measurement uses the size labels without a progress bar.
Finish background processes before changing folders or removing resources.

The trash icon beside the project size removes all projects directly inside the
displayed Project Location. Confirmation shows the count and folder, with project
names available in the details. This permanently deletes those projects and all
their stored work, including the MET example. Projects elsewhere, external media,
and unrelated files are kept. The open project closes and pending saves finish
before deletion runs in the background. Closing Settings afterward returns to the
Project Chooser. Partial failures report the removed count and remaining errors.

Changing a nonempty project or model folder offers **Relocate** or **Keep Current
Folder**. Relocate closes the open project and Main Window, waits for pending
saves, copies and verifies the contents into a new or empty destination, updates
the location, and removes the originals. Settings stays open during the move;
closing it returns to the Project Chooser. If the old folder is empty, the new
location is saved directly. A failed copy or preference write preserves the old
location and files. If removing originals fails after the new location is saved,
the new location stays active and Settings identifies the previous folder for
review. External media keeps its existing location.

Reset on the General tab restores appearance and startup preferences, preserving
storage paths and saved work.

In the Project Chooser, **More Options → Settings...** opens directly on the
Storage tab. Measurement, copying, verification, and deletion run outside the
GUI thread. Close other EIDORA instances before maintenance.

Projects, model storage, and caches must occupy separate locations. Maintenance
rejects symbolic links/junctions and cleanup refuses folders containing project
manifests. Source files remain intact on relocation failure or cancellation.
If saving the new preference fails after copying, the verified destination copy
is retained and the original location remains active.

Internal project paths remain relative to the project. External media links stay
at their original paths; relocating a project does not copy external media.
Retain access to those sources. Saved analyses and project thumbnails belong to
the project and are not included in application-cache cleanup.

## MET example

The release includes the MET archive, but startup no longer extracts it
automatically. Choose **Create MET Example…** to see its extracted size and
destination before creating a project. Repeating this action creates a separate
copy without replacing an edited example. Once extracted, MET is treated as an
ordinary project and survives application removal.

## Problem reports

Choose **Diagnostics...** in Help or the project chooser to review recent local
diagnostics. Add a description, remove any personal information, and choose
**Save Report…** or **Copy Report**.

EIDORA never shares your data or sends diagnostic reports. Reports are stored locally,
and sharing them is entirely your choice. If you encounter a problem, we would
appreciate your help improving EIDORA by forwarding the diagnostics report with a
short description to [**mail@eidora.org**](mailto:mail@eidora.org).

The `diagnostics` folder in EIDORA's local application-data directory has a strict
**10 MiB budget for automatic diagnostic files**, independently of project and
source storage. At startup, EIDORA removes the oldest inactive sessions to keep
five recent sessions where possible and make room within that budget. Space is
reserved before recording starts, including when several EIDORA instances are
running. Active captures are protected; if they occupy the available budget,
EIDORA continues without starting another disk capture.

Event logs rotate at 256 KiB with one backup per session. Crash/freeze traces stop
at 256 KiB, even if EIDORA stays frozen or exits unexpectedly. A small helper
receives crash output and sleeps when there is none; it does not scan project or
source folders. Old oversized reports are included in cleanup. The limit covers
file contents, not filesystem bookkeeping, manually exported reports, or crash
reports created separately by the operating system. Your exported reports are
never automatically deleted.

Diagnostics contain technical logs and best-effort crash/freeze stack traces,
not project or media files. These files may contain original paths;
the editable report masks paths where possible. A freeze lasting about 30 seconds
can produce a stack snapshot; force quit before capture, power loss, full storage,
or a native failure can leave incomplete diagnostics. A snapshot is evidence of
where work stopped, not proof of the cause. After restarting, the chooser flags
unreviewed crash/freeze diagnostics and sessions that ended without a normal
shutdown. Reports show these sessions before fresh startup output and label
running and normally closed sessions separately. A missing shutdown marker
does not establish the cause; older sessions without lifecycle markers have an
unknown shutdown status. Restarting creates a separate session, with previous
sessions retained until the oldest-session cleanup is necessary.

## Uninstalling

Removing the application preserves projects. Before uninstalling EIDORA, use
Settings → Storage to remove downloaded models if you want to reclaim that space. Removing the macOS app,
Windows EXE installation, Linux DEB, or AppImage does not invoke this in-app
cleanup automatically. There is no new installer hook that deletes user folders.

Windows Store/MSIX removal can remove package-managed application data, including
default model storage and preferences. Projects are outside that storage.
Model folders explicitly relocated outside package-managed storage can survive
removal and should be cleaned up explicitly if no longer wanted.

The bundled MET archive is removed with the application. Extracted projects,
external media, and original folders retained after relocation are preserved.
Reinstalling after preferences have been removed may require opening a project
from its chosen folder again.

## Implementation

`shared/paths.py` resolves defaults, environment overrides, and saved locations.
`application_storage/repository.py` atomically saves `storage.json` in application
data. `application_storage/service.py` owns inspection, verified copying, and
cleanup. MainWindow's Designer forms and controllers own interaction and
background progress. Model downloads always record ownership receipts.


## Figure documents

`state/figures_area_state.json` version 7 stores editable figure compositions.
Figure settings distinguish fixed final-page dimensions from the plot design
dimensions used for proportional scaling. Rows and columns reserve empty cells.
Older per-plot layouts are converted using measured decoration bounds, retaining
their original appearance and final size; converted spacing constraints remain
until the corresponding decorations are edited. This measurement does not load
external point datasets. Each plot cell may store `size_overrides` in original
design units. Missing legacy grid cells carry `reserved_only: true`, preserving
blank space until a plot is loaded there. Legacy animations retain their old
settings until the selected frame loads and supplies its decoration measurements.
The editor derives its displayed pixel values from the rendered
scale without rounding saved values. Presets and animation settings include the
same per-cell overrides as `plot_sizes`; presets may also carry pending pixel
`measurements` with `units: "px"` to resolve when applied. Earlier pending edits
without this marker are converted from mm/points. The established internal mm,
point and resolution fields remain readable and retain their precision; the UI
provides only pixels and no DPI control. Missing saved fields use the historical
profile defaults, independently of the starting profiles for new figures.
Large frozen datasets live in `state/figure_data/<sha256>.npz`, with numeric
coordinates and permanent IDs plus dictionary-encoded metadata columns with UTF-8
JSON values (no pickled objects). Older row-JSON datasets remain readable. Encoding
reduces decoded memory; compressed file size depends on metadata cardinality.
Matching data is shared between plot cells. These artifacts belong to the project,
not a disposable global cache, and travel with project relocation and bundles.
Small datasets remain inline. Older state versions remain readable and large
inline data migrates atomically before the compact manifest is published.
Tabs are the saved figure collection, including animations. The × button deletes
the document after confirmation; remaining tabs autosave and reopen with the project.
Existing saved records load directly as tabs. Source snapshots and exports remain
untouched. Unreferenced generated figure data is
removed only after all workspace readers and writers stop, with a valid manifest.
