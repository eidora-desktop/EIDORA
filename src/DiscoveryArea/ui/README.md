# Discovery forms

`discovery_area.ui` hosts the canvas and panels. `pins_settings.ui`,
`tooltip_settings.ui`, `areas_settings.ui`, and `previews_settings.ui` define the
separate layer-settings windows. Other forms own filters, projections and dialogs.

Preserve object names used by controllers and semantic `iconName` properties.
Settings above the native canvas use top-level transient windows; putting those
forms back into child overlays can break stacking. See [source rules](../../README.md).
