# Shared infrastructure

Cross-area helpers belong here; feature behavior stays in its owning area.

- [qt](qt/README.md): form loading, themes, and widget lifecycle helpers.
- [persistence](persistence/README.md): generic file and SQLite helpers.
- [icons](icons/README.md) and [cursors](cursors/README.md): semantic assets.
- `paths.py` and `env.py`: application storage and environment settings.
- `diagnostics/` and `branding/`: logging and application artwork.

Keep heavy imports lazy and avoid feature dependencies in general-purpose helpers.

`methods_report.py` owns the cross-area report format, reference deduplication,
caption text, and atomic Markdown/BibTeX pair export. The reusable Designer dialog
is loaded by `qt/methods_citations_dialog.py`; area services supply plain reports.
