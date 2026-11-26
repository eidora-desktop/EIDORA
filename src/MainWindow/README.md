# Main window

`controllers/main_controller.py` composes the application areas, routes navigation,
and connects project changes between them. Project operations use `ProjectService`.
`project_chooser_controller.py` owns the standalone startup project list; `help_panel_controller.py`
owns About, and `update_panel_controller.py` binds explicit website update checks
on macOS/Linux and opens Microsoft Store's updates page on Windows.
`project_panel_controller.py` binds the shell project name and actions menu, with
Switch Projects first, followed by **Methods & Citations…** for saved analyses.
The report selector works independently of Figures and includes source analysis
steps where available. Project changes update both the label and action availability.
The Media workspace opens with Media Sources and Preview Assets side by side.
`navigation.py` is Qt-facing navigation data.

`controllers/window_layout_controller.py` restores main-window geometry, maximized
state and workspace splitter positions from installation-local `session.json`.
Qt adjusts geometry for the available screens. Register splitters after workspace
setup, including deferred workspaces, and save on window close or shutdown.
`application_settings/session_repository.py` owns this storage and separate last
import/export folders used by `shared.qt.file_dialogs`. Cancelled dialogs retain
the old folder; missing folders fall back to each dialog's normal suggestion.

Normal startup constructs all workspaces before showing the main window.
`loading_panel_controller.py` shows the Designer-owned `ui/loading_panel.ui`
with a palette-colored rotating square and the EIDORA wordmark beneath it during
project opening. Both follow light/dark appearance; the SVG wordmark is rendered
at the display scale. Loading remains named for assistive technology.
The card stays 180 × 180 px and does not display the project name. Project I/O runs
in a single worker; `MainWindowController.initialize_workspaces()` yields between
GUI imports, construction, and project binding while the shell remains hidden.
The loading controller schedules each next step only after the previous one
returns, so painting and existing activity event processing cannot reenter setup.
The main window is shown before the panel closes. Failures stop the animation
and return to the chooser's existing error handling; partially built workspaces
are shut down. Automatic reopening and first-launch example setup use the same
panel when preparing the main window.

Open, New Project, and Import hide the chooser immediately and use only the
square loading panel for progress. The chooser has no loading text or progress
bar. Its modal loop waits for acceptance or rejection, allowing it to stay hidden
during loading and return for retry/cancel on failure. The loader is independent
of hidden windows, while remaining centered over the chooser's former position.

Switching projects closes the main window before opening the chooser. Its
preparation callback binds the selected project to each workspace in queued
steps and shows the ready window before the loader closes. Do not defer that
binding until after the chooser returns: that dismisses the loader too early.
Cancellation restores the existing window; failed preparation restores its
previous project and workspace before returning to the chooser for retry/cancel.
This lets Qt select the graphics surface before the window becomes visible;
adding Discovery's first Quick widget afterward can recreate the window and blink.

Keep feature behavior in its area. When switching to Discovery, explicitly
activate its Quick host; a hidden native window may otherwise miss its first frame.
Call area `shutdown()` methods before releasing the application window.

Discovery navigation requires at least one canonical metadata item. Indexed
media alone does not enable it. Recheck eligibility after metadata changes and
project switches; defer the check while media publication is active.
Figures navigation requires at least one saved snapshot. Refresh availability
after snapshot creation/deletion and project switches, returning to Media if
the last snapshot is removed while Figures is open.

The `ui/` forms own the shell, project chooser, and About dialog. Match widget
contracts when editing them.

The Project Chooser lists valid projects directly inside the configured Project
path together with recent projects stored elsewhere, without duplicate paths.
Most recently opened projects come first; projects without opening timestamps
follow alphabetically. Refreshing the list does not change recent-project history.
Open Project offers Folder and ZIP options: Folder opens and saves in place;
ZIP extracts a separate project into the configured project storage. Write-access
failures when opening a folder or listed project offer Copy to My Projects and
Open. Copying and ZIP extraction use the existing background loading panel.
Open Project and More Options open at the mouse click position; keyboard
activation retains native menu placement. Their indicator sizes live in the form.

The Settings menu offers a workspace tour once all area controllers are loaded,
including for empty collections. `tour_steps.py` defines its
ordered panel targets; target names resolve inside the
chosen workspace. `workspace_tour_controller.py` uses normal navigation and owns
the tour's lifetime. `tour_spotlight.py` paints a translucent, native transient
window above the application, including Discovery's native canvas. The Designer
card sits beside the highlighted panel while its workspace button stays clear.
Placement follows layout events without polling or changing the application's
layout. Keep new steps tied to visible panels and avoid opening or creating data.

To edit tour copy, open [`tour_text.toml`](tour_text.toml) and change the `title`
and `description` under the relevant named section. Keep the section names and
quote marks intact; descriptions can span lines between the triple quotes.
Save and reopen Settings → Take a Tour… to see changes without restarting the app.
Keep descriptions short so the card fits alongside its highlight. Invalid text
files show a status message without opening the tour. Both the explanation card
and spotlight areas have thick red outlines in either theme. Discovery starts
with Canvas, then Projections, Transform, Filters, Slicer, Snapshots, and Appearance.

During the tour, the shell temporarily permits navigation into empty workspaces.
Closing it restores the normal availability checks and returns to Media if
the displayed workspace is unavailable. Opening the MET example automatically
starts the tour after the window and all controllers are ready. On first packaged
startup, the cancellable starter setup creates and opens this example directly;
subsequent launches keep the project chooser. `tour_repository.py`
stores whether a tour has been shown in the user's application-data folder,
independently of projects. The flag applies to the current app version and
installed executable, so updates and replacing an installation re-enable the
tour on the next example opening. AppImage uses the outer file rather than its
temporary mount; source runs have a separate identity. Manual tours and
cancellation count as having seen onboarding; the Settings menu always allows replay.
Pending launches stop on project changes and shutdown.

The chooser also offers explicit MET example creation and a New Project dialog
with a visible destination. More Options starts with Settings, which opens the
Storage tab, followed by a separator, MET example creation, and Diagnostics.
See [storage behavior](../../docs/storage.md).

About EIDORA is first in the EIDORA-icon Settings menu, followed by Settings.
Settings is also available in the startup chooser.
`settings_dialog_controller.py` binds the Designer General tab to application
preferences and existing storage services. Appearance applies immediately;
startup chooses the project chooser or attempts to reopen the last project.
Missing/unreadable projects fall back to the chooser. Initial packaged example
setup is preserved. Reset restores appearance/startup only.

`settings_storage_panel_controller.py` binds the Storage tab to background size
measurement, confirmed project/downloaded-model removal, and folder changes. Cache
controls display its location and size only. Nonempty folders
can only be relocated or kept. Relocation closes the Main Window and detaches its
project, waits for retired Figure writers, verifies the copied contents, saves
the new location, then removes the originals. Settings stays open until the user
closes it; the shell then opens the Project Chooser, or refreshes the chooser
already open. Empty folders can change destination directly. The installed model
list refreshes after cleanup or folder changes. Background processes must finish
before storage changes. General reset is hidden on the Storage and Presets tabs.

The Presets tab lists reusable settings grouped by type, with collapsible headers
and group/individual checkboxes. It defaults to the open project; the selector
also includes known workspace/recent projects and a folder browser. Browsing
another project does not open it or change startup history. Project changes
clear selection; collapsing a group retains its selection. Reset is hidden on
this tab.

`presets_panel_controller.py` binds the Designer controls and runs filesystem
work in background tasks. `application_settings/presets_service.py` owns project
inspection and checked deletion, with persistence in `presets_repository.py`.
Bulk deletion confirms the project, count, and names, reports partial failures,
and refreshes active preset menus without changing applied settings. Snapshot
and projection records are excluded. Legacy snapshot and saved/open animation
dependencies block deletion of required presets. Settings cannot close during
deletion; late browsing results are ignored after project changes or disposal.

Settings has separate General, Storage, and Presets tabs in a resizable Designer
form. The shared footer shows Restore Defaults on General, with Close on the right.
Preset selection counts and deletion actions live below the tree inside the
Presets tab. Routine preference saves show no
status banner; storage progress, resets, deletion results, and errors remain
visible on their relevant tab.
