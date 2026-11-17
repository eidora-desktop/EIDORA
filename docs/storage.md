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

Open **Storage…** in the starting project chooser to see locations and sizes for
the current workspace, installed models, and application caches.

- **Relocate Projects…** copies the entire current workspace into a new or empty
  folder, checks each file's SHA-256 digest, updates recent-project paths, and saves
  the new default. It preserves the original workspace for review and manual
  removal. Projects opened from other folders retain their existing locations.
- **Relocate Models…** performs the same verified copy for installed models and
  updates their storage preference. Originals remain in the previous folder.
  Storage lists the previous locations for manual review; their sizes are
  excluded from the current-folder totals.
- **Remove Selected Resources…** asks for confirmation and removes only selected
  EIDORA-downloaded model packages, unfinished model downloads, and the rebuildable
  Matplotlib font cache. Models are identified by their EIDORA download receipts.
  Manually supplied packages without receipts are preserved. The dialog shows
  folder sizes before confirmation and actual reclaimed bytes after cleanup.

Relocation and cleanup are available before a project is opened. When reached
from an open project's chooser, Storage permits inspection only. Close other
EIDORA instances before maintenance. Measurement, copying, verification, and
cleanup run outside the GUI thread. Cancellation waits for the current file
operation; cleanup already completed is not undone.

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

Projects are always preserved. Before removing EIDORA, use Storage to remove
downloads and caches if you want to reclaim that space. Removing the macOS app,
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

`state/figures_area_state.json` version 6 stores editable figure compositions.
Large frozen datasets live in `state/figure_data/<sha256>.npz`, with numeric
coordinates and permanent IDs plus dictionary-encoded metadata columns with UTF-8
JSON values (no pickled objects). Older row-JSON datasets remain readable. Encoding
reduces decoded memory; compressed file size depends on metadata cardinality.
Matching data is shared between plot cells. These artifacts belong to the project,
not a disposable global cache, and travel with project relocation and bundles.
Small datasets remain inline. Older state versions remain readable and large
inline data migrates atomically before the compact manifest is published.
Closing a tab preserves its document. Unreferenced generated figure data is
removed only after all workspace readers and writers stop, with a valid manifest.
