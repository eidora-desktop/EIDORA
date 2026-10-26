# Figures and animation

`domain.py` defines compositions, settings and animation frames. `renderer.py`
builds Matplotlib figures. `animation_service.py` owns sequencing/playback rules;
`animation_export_service.py` stages GIF output and `animation_gif_worker.py`
encodes it in an isolated process. `repository.py` persists compositions and settings. Closing a figure hides its
view; saved figures reopen when the project is reopened. Static exports
are staged beside the destination and replace it only after a successful write.

[Controllers](controllers/README.md) compose snapshots and bind Designer forms.
Keep per-frame plot overrides consistent across preview, thumbnails and export.
General figure-setting changes redraw the selected preview and enable the explicit
thumbnail refresh action; per-frame edits refresh affected thumbnails.

Export formats must also pass the packaged application self-test.

Plot fonts initialize on the first plot, without a system-wide font scan.
`shared/plot_fonts.py` owns the standard-font wishlist and registers only existing
font files: known macOS locations, matching Windows font registry entries, or a
bounded, family-filtered fontconfig query on Linux. Matplotlib 3.11 or newer is
required. DejaVu Sans, DejaVu Serif, DejaVu Sans Mono, and STIXGeneral remain
available from Matplotlib; installed wishlist fonts are added to the title and
caption selectors with their real family names and available styles. The catalog
is cached for the session; restart after installing fonts.
Matplotlib uses automatic text hinting so installed faces with incompatible
native hinting instructions still render at figure sizes.

Unavailable fonts are omitted from new choices. A saved unavailable family stays
selected as “unavailable”, with a tooltip explaining the DejaVu Sans fallback;
applying unrelated settings preserves the original family name.

Each snapshot row has a **cite** button opening the shared methods report, with
copyable caption text and Markdown/BibTeX export. Plot cells retain the source
snapshot report in their saved render state. Caption editors offer **Insert
Snapshot Methods**, so reports remain usable after the original snapshot is
removed. Inserted text describes source snapshots and asks users to review later
figure-specific edits.
