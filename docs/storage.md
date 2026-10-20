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
