# Discovery

Group fields use stable IDs with current display names. Refresh group-dependent
controls after renames, and migrate conditional rule labels with their assigned
colors and conflict resolutions so existing appearance settings stay intact.

`controllers/main_controller.py` composes the exploration UI. `service.py` owns
projection workflows; `metadata_access.py` supplies cached column-oriented data.
`repository.py`, `snapshot_repository.py`, `presets_repository.py`, and
`arrangement_repository.py` own their distinct persisted concepts.

- [canvas](canvas/README.md): scene contracts, interaction and native rendering.
- [discovery_methods](discovery_methods/README.md): projection algorithms.
- [thumbnails](thumbnails/README.md): variant storage, resolution and decoding.
- [controllers](controllers/README.md): panels and asynchronous coordination.

Keep metadata IDs canonical and arrays aligned. A snapshot restores its complete
saved state, including disabled controls; absent state must reset previous values.
Camera bounds and snapshot crop framing are separate concepts.

Metadata fields are listed once per table. Resolve their values from each plotted
item through parent links (for example, vector item → face → photo), preserving
the vector's item identity and row count. Agreeing paths coalesce; conflicting
parents leave an empty value. Old relationship field IDs remain aliases to stable
table fields in saved tooltip, pin, appearance, filter, and slicer settings.

Canvas toolbar controls have accessible names and explicit Tab order. In the
Layers bar, the checkbox toggles visibility and the adjacent arrow toggles
settings, even for a hidden layer. Enter on the checkbox and its right-click
menu also open settings. Clicking outside the popover closes it.
Escape in the settings window closes it and returns focus to the arrow.

Mapped appearance always exposes saved group fields, including clustering results
with more categories than the ordinary metadata limit. Point and border mappings
include every present group value and generate extra palette colors as needed.

The automatic Random view stays selectable alongside saved projections and
running jobs. The canvas always loads the highlighted projection, and refreshing
the list preserves the current selection. Random contains one point per current
Items row, independent of media links, source records, and saved coordinates.
Adding or removing Items updates its points while retaining the positions of
existing items. Loading it writes no projection or preset; legacy automatic
coordinates are ignored and do not block metadata replacement. Saved
snapshots, pins, marked areas, and explicit projections remain real dependencies.
Discovery resolves asset-only analysis IDs through the project registry and can
load saved coordinates without metadata rows. Missing metadata has empty values.

Snapshots expose **Citations…** from their context and panel menus.
New snapshot captures freeze a methods report alongside their inline filter,
slicer, and transform settings. Reports include projection/model provenance and
clustering records referenced by group-set fields. Legacy snapshots show the
available settings and explicit provenance gaps. `snapshot_provenance.py` owns
assembly; `settings_provenance.py` reports applied transforms and filter settings
without evaluating data or loading coordinates.

Hover tooltips show up to 300 characters per metadata value, with an ellipsis
for longer values. Text wraps within a maximum 360 px section, including URLs
and identifiers without spaces. Short tooltips remain compact; thumbnail size
and placement are independent. Full metadata stays unchanged.
