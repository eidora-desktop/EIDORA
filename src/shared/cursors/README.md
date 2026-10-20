# Cursor Assets

Add custom SVG cursor icons to this directory.

Use descriptive, lowercase snake-case filenames that describe the interaction,
for example `canvas_pan.svg`, `canvas_zoom_in.svg`, or `lasso_select.svg`.

Keep the SVG artwork tightly fitted to its view box.

All canvas cursor asset mappings, hotspots, and Qt fallbacks are defined once in
`src/shared/cursors/__init__.py`. Application code should use the semantic names
and `set_cursor()` instead of loading SVG files or setting hotspots directly.
