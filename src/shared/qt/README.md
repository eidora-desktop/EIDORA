# Shared Qt helpers

`ui_loader.py` loads Designer forms, semantic icons and surface roles.
`theme.py` follows light/dark palettes; `icons.py` refreshes tinted pixmaps on
palette changes. Row hover/selection and enabled button hover/pressed/checked
states use paired neutral background/text colors from one application stylesheet.
Named `surfaceRole` backgrounds use that same stylesheet, including detached
settings windows and embedded cards, so native palette repolishing cannot leave
their painted backgrounds in the previous mode. Palettes retain the same surface
colors for custom painting; forms need no cached background styles.
Theme tokens have explicit names; headers, selection, and combo boxes share one
primary text color. Active controls use a stronger border than decorative grids.
Designer labels can declare `textRole="primary"`, `textRole="secondary"`, or
`textRole="error"`. Primary text can also be applied to item views.
Semantic button icons keep their tint in all enabled states. `activity.py` owns
busy indicators; `deferred.py` defers actions.
`panel_controller.py`, `widgets.py`, and `item_views.py` provide small UI helpers.

- Defer dialogs/tree rebuilds triggered by combo/item signals until the event returns.
- Use one deletion strategy. Do not combine `WA_DeleteOnClose` with `deleteLater()`.
- Load transient modal dialogs unparented, set modality plus the main window's
  native transient owner, and disconnect captured handlers after `exec()` so
  hidden dialogs do not survive or float above other applications.
- Worker completion owns deletion; controller cleanup only releases references.
- Apply icon color at the form root and preserve semantic `iconName` properties.
- Keep item-view spacing and intentional row-background overrides in Designer.
  Shared selection rules live at application level and do not replace these styles. Nonselectable control lists can suppress native row
  hover/selection backgrounds in Designer without disabling their child controls.

Keep feature computation and persistence out of these helpers.
