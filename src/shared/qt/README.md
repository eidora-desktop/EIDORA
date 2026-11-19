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

## Lightweight accessibility

- Keep ordinary Qt keyboard behavior and a logical Tab order in Designer forms.
  `keyboard_focus.py` enables a solid neutral button outline for keyboard
  navigation, including on checked buttons, in both themes. Other controls use
  a lightweight Qt focus frame without changing geometry. Automatic startup
  focus leaves the outline hidden until keyboard navigation starts.
  Mouse presses hide the outline, and pointer movement leaves it visible.
- Workspace navigation opts into `tabAllButtons` on its exclusive button group
  so each enabled workspace has a Tab stop. Tab leaves the metadata and media
  sources tables; arrow keys continue to navigate their contents.
- Scope canvas key handlers to their visible, active workspace, and leave Space
  available to focused buttons, checkboxes, and other standard controls.
- Give icon-only controls concise `accessibleName` properties; associate text
  labels with inputs using Designer buddies. Tooltips supplement these names.
- Provide a click or keyboard route to essential hover and drag actions. Return
  focus to the initiating control when dismissing a keyboard-opened popover.
- Respect system fonts and check layouts with larger text. Avoid conveying
  important state through color alone; retain text, checkmarks, or other indicators.
- Check changed workflows with the keyboard. Use platform screen readers for
  manual validation before claiming screen-reader support for a workflow.

These are incremental UI rules, not a separate accessibility mode. Custom
visualizations need their own interaction design; do not create one widget or
accessibility object per plotted point as part of routine UI maintenance.

`panel_rows.PanelRowHeader` sizes the Designer-owned two-line headers shared by
Media Sources, Preview Assets, Models and Vector Data. An application-owned
measurement group shares the largest natural control height across live rows,
including hidden actions, and elides secondary summaries. Rows, header lines and
buttons have no fixed height cap. Shared minimum heights and item size hints
reserve both lines across states, while native button sizing stays intact.
Font, style and layout changes update the measurements; shrinking controls or
removing rows releases the extra space. The group holds only weak row references.
Header layouts reserve full widget rectangles, including native button and
progress-bar edges, so macOS layout margins cannot clip nested action groups.
Each item view coalesces size-hint changes into a deferred layout, after card
contents settle, so subsequent rows move without overlaps or stale gaps.
Progress fills the second line up to its right-aligned controls. The list-colored
outer wrapper keeps a six-pixel gap separate from the colored card. Feature controllers retain action and expansion ownership.

### Editing panel row forms

The four row forms can use nested layouts directly or place those layouts inside
decorative widgets. `panel_rows.section_widget` reuses an existing host or gives
a nested layout a runtime widget for visibility and minimum header sizing. It
preserves the layout's margins, order, stretch and alignment; it never rewrites
the `.ui` file. Decorative widget names are not controller bindings.

Keep the functional controls' `objectName` and widget types (labels, progress
bars, buttons, tables and tabs). Also keep these layout section names:

- `layout_card_root`, `layout_row_header`, `layout_header`, `layout_summary`
- `layout_progress` for the bar and its controls
- `layout_row_details` in expandable Preview Assets and Vector Data cards
- `layout_actions` for Media Source title actions

These are the explicit Python/UI contract; renaming or deleting one requires
updating its controller too. Within these sections, change arrangement, spacing
and styling in Designer normally. Shared header sizing still reserves two lines
that can grow to fit the controls. Preserve `iconName` and `surfaceRole` properties.
Validate edits with `tests/gui/test_ui_loading.py` and
`tests/unit/test_shared_panel_rows.py`; the latter exercises both wrapper-based
and layout-only forms through idle, running and expanded states in both themes.

`item_views.configure_checkable_item_view` applies a view-local Fusion style so
checkable trees/lists paint indicators at the same positions used for clicking,
including on macOS. Presets and Canvas Layers share this helper.
