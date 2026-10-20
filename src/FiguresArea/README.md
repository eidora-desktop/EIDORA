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

Each snapshot row has a **cite** button opening the shared methods report, with
copyable caption text and Markdown/BibTeX export. Plot cells retain the source
snapshot report in their saved render state. Caption editors offer **Insert
Snapshot Methods**, so reports remain usable after the original snapshot is
removed. Inserted text describes source snapshots and asks users to review later
figure-specific edits.
