# Discovery forms

`discovery_area.ui` hosts the canvas and panels. `pins_settings.ui`,
`tooltip_settings.ui` and `previews_settings.ui` define the
separate layer-settings windows. Other forms own filters, projections and dialogs.

`new_projection_dialog.ui` owns projection setup and its compact memory assessment.
`save_area_dialog.ui` owns the lasso name, membership counts and Include filtered
checkbox. The controller hides the checkbox only when no excluded members exist;
it remains visible and editable in Designer.

Preserve object names used by controllers and semantic `iconName` properties.
Settings above the native canvas use top-level transient windows; putting those
forms back into child overlays can break stacking. See [source rules](../../README.md).
