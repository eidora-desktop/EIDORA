# Main window

`controllers/main_controller.py` composes the application areas, routes navigation,
and connects project changes between them. Project operations use `ProjectService`.
`project_chooser_controller.py` owns the standalone startup project list; `help_panel_controller.py`
owns About and release-check UI. `navigation.py` is Qt-facing navigation data.

Normal startup constructs Project Area first and shows the main window immediately.
Metadata, Embeddings, Discovery, and Figures are initialized incrementally after
the window is visible. Keep Project Area imports and construction lightweight.

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
