# Assets workspace forms

`assets_area.ui` owns one horizontal splitter:
Media Sources on the left, Thumbnails on the right. `new_thumbnails_dialog.ui`
retains all thumbnail options. `media_link_dialog.ui` exposes only the source key,
one metadata column, and a bounded preview. Artifacts forms have been removed.

Object names are controller contracts. Keep layouts Designer-owned and preserve
semantic icons in both light and dark themes.

`media_source_row.ui` owns each Media Sources row, including its separate progress
area. The panel uses one stretched column without a header; the row controller
elides long names and paths and adjusts row height when progress is shown.
