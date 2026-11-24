# Cursor Assets

Add custom SVG cursor icons to this directory.

Use descriptive, lowercase snake-case filenames that describe the interaction,
for example `canvas_pan.svg`, `canvas_zoom_in.svg`, or `lasso_select.svg`.

Keep the SVG artwork tightly fitted to its view box.

All canvas cursor asset mappings, hotspots, and Qt fallbacks are defined once in
`src/shared/cursors/__init__.py`. Application code should use the semantic names
and `set_cursor()` instead of loading SVG files or setting hotspots directly.

The three hand assets share a 46 × 46 canvas, 3.5 px finger widths, a 1.6 px
rounded black outline, white fill, and identical lower palm geometry. Only
the finger poses change between open, closed, and pointing hands.
`CLICKABLE` uses `hand_pointing.svg`. Its hotspot is the index fingertip at `(21, 12)`.
Discovery uses it for selectable Items and actionable Pin playback controls;
draggable Pin bodies keep `PIN_MOVE`, and removal modifiers keep `PIN_REMOVE`.
