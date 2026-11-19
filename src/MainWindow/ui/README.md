# Shell forms

`main_window.ui` owns navigation and area containers. `project_chooser.ui` defines
the standalone startup launcher; `about_dialog.ui` defines Help.
`loading_panel.ui` owns the compact startup card; its controller adds the painted
rotating square to `panel_animation`.

Keep durable layout edits in Designer and update the matching controller widget
contract. Use semantic `iconName` properties with SVG fallbacks. See [source rules](../../README.md).
