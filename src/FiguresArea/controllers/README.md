# Figure controllers

`main_controller.py` manages tabs and project restoration.
`saved_tabs_controller.py` owns lightweight placeholders, selected-document preparation,
retry, and cancellation; unopened records remain available to collection autosave. `plot_tab_controller.py`
binds composition/editing; `snapshot_animation_tab_controller.py` binds the timeline.
Preview adapters, latest-only preview coordination, transform coordination, settings and export dialogs are separate.
Large plots publish plain image/layout results from `work_service.py`; controllers
never pass Qt canvases or widgets into workers. Stop panel-owned timers and reject obsolete results when a tab closes or its data
changes. Preview completion uses a weak-reference future callback that only emits
a queued Qt signal; GUI slots own adoption and image updates. A zero-delay
single-shot timer coalesces edits within one event-loop turn. Forward tab visibility
to the workspace scheduler and adopt matching dataset publications even when their
image revision is stale.
Transformation preparation runs in a plain worker with cooperative cancellation;
only the GUI-owned coordinator polls results and applies the still-current cell.
Interactive animation exports and all timeline thumbnails also poll prepared-frame
futures, cancelling pending preparation when stopped or superseded. Thumbnail jobs
write to separate staging directories. Only the GUI accepts a completed image whose
frame and figure settings still match; cancelled jobs clean their staging files after
the worker finishes without retaining a Qt controller. Changed figure settings keep
the explicit thumbnail-refresh action available.

Keep rendering in `renderer.py` and sequencing/export decisions in their services.
Store edits as frame overrides, and match pins by stable annotation ID.
Proxy and Matplotlib previews must reserve the same note-row height so the plot
does not jump. Keep the empty timeline card layout-managed.

GIF cancellation terminates the encoding child, not a Qt worker thread. Persist
tab state through `FigureTabsRepository` before closing or changing projects.

The main controller owns workspace-wide transformation, animation-image and inactive-preview budgets. Pass them into every static/animation tab; snapshot override preparation shares the transformation cache. Overlay updates skip unchanged geometry, and hidden preset panels never rebuild their catalog on preview completion.
