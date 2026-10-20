# Project area forms

`project_area.ui` owns the active-project header and one horizontal splitter:
Media Sources on the left, Thumbnails on the right. `new_thumbnails_dialog.ui`
retains all thumbnail options. `media_link_dialog.ui` exposes only the source key,
one metadata column, and a bounded preview. Artifacts forms have been removed.

Object names are controller contracts. Keep layouts Designer-owned and preserve
semantic icons in both light and dark themes.
