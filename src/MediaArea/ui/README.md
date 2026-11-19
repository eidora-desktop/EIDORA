# Media workspace forms

`media_area.ui` owns one horizontal splitter:
Media Sources on the left, Preview Assets on the right. `new_thumbnails_dialog.ui`
follows the Run Embeddings form: a source heading, secondary summary, labels
above full-width controls, and right-aligned actions in a separate footer.
Source-row creation fixes the input; the generic entry point retains a selector.
Only video sources show frame selection.
The source row owns Create Previews…; the Preview Assets header has no creation
button. `media_link_dialog.ui` exposes only the source key,
one metadata column, and a bounded preview. Artifacts forms have been removed.

Object names are controller contracts. Keep layouts Designer-owned and preserve
semantic icons in both light and dark themes.

`media_source_row.ui` and `preview_asset_row.ui` use the same two-line header as
Models and Vector Data. The first line owns the name, status and ordinary actions; the
second swaps the elided summary/path for a full-width progress bar with controls
on its right. Paused rows retain progress with Resume/Cancel controls. Shared font/button
metrics keep the header height constant through queued, running and finished
states. Media Sources is non-expandable. Preview Assets adds its wrapped details
table below the header without moving the title or controls.

Both panel views disable selection and row hover highlighting. Actions receive
the row identity directly. Preview disclosure buttons retain keyboard focus and
Space activation; expansion survives publication and refresh.

Each colored card sits in a list-colored wrapper with a six-pixel bottom gap.
Buttons retain their natural size hints, including progress controls. Shared
minimum header heights grow to fit the largest control across the four panel
types, with no fixed row or button height cap.

## Editing row layouts

Media Source and Preview Asset rows support nested layouts with or without
decorative QWidget wrappers. Preserve functional control names and the
[shared layout section names](../../shared/qt/README.md#editing-panel-row-forms)
when editing in Designer.
