# Figure controllers

`main_controller.py` manages tabs and project restoration. `plot_tab_controller.py`
binds composition/editing; `snapshot_animation_tab_controller.py` binds the timeline.
Preview adapters, transform coordination, settings and export dialogs are separate.

Keep rendering in `renderer.py` and sequencing/export decisions in their services.
Store edits as frame overrides, and match pins by stable annotation ID.
Proxy and Matplotlib previews must reserve the same note-row height so the plot
does not jump. Keep the empty timeline card layout-managed.

GIF cancellation terminates the encoding child, not a Qt worker thread. Persist
tab state through `FigureTabsRepository` before closing or changing projects.
