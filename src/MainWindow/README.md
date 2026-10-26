# Main window

`controllers/main_controller.py` composes the application areas, routes navigation,
and connects project changes between them. Project operations use `ProjectService`.
`project_chooser_controller.py` owns the standalone startup project list; `help_panel_controller.py`
owns About, and `update_panel_controller.py` binds explicit website update checks
inside it. `navigation.py` is Qt-facing navigation data.

Normal startup constructs all workspaces before showing the main window.
This lets Qt select the graphics surface before the window becomes visible;
adding Discovery's first Quick widget afterward can recreate the window and blink.

Keep feature behavior in its area. When switching to Discovery, explicitly
activate its Quick host; a hidden native window may otherwise miss its first frame.
Call area `shutdown()` methods before releasing the application window.

Discovery navigation requires at least one canonical metadata item. Indexed
media alone does not enable it. Recheck eligibility after metadata changes and
project switches; defer the check while media publication is active.
Figures navigation requires at least one saved snapshot. Refresh availability
after snapshot creation/deletion and project switches, returning to Project if
the last snapshot is removed while Figures is open.

The `ui/` forms own the shell, project chooser, and About dialog. Match widget
contracts when editing them.

The Help menu offers a workspace tour once all area controllers are loaded,
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
Save and reopen Help → Take a Tour… to see changes without restarting the app.
Keep descriptions short so the card fits alongside its highlight. Invalid text
files show a status message without opening the tour. Both the explanation card
and spotlight areas have thick red outlines in either theme. Discovery starts
with Canvas, then Projections, Transform, Filters, Slicer, Snapshots, and Appearance.

During the tour, the shell temporarily permits navigation into empty workspaces.
Closing it restores the normal availability checks and returns to Project if
the displayed workspace is unavailable. Opening the MET example automatically
starts the tour after the window and all controllers are ready. `tour_repository.py`
stores whether a tour has been shown in the user's application-data folder,
independently of projects. Manual tours and cancellation count as having seen
onboarding; Help always allows replay. Pending launches stop on project changes
and shutdown.

The chooser also offers explicit MET example creation, a New Project dialog with
a visible destination, and Storage inspection/maintenance. Storage controllers
submit filesystem work through a background manager and poll results on the GUI
thread. Maintenance is disabled while a project is open. See
[storage behavior](../../docs/storage.md).
