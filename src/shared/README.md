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

Reports open with **Brief methods** and references. Complete settings and
reproducibility fields remain in the collapsed **Detailed methods & technical
record** section. Methods, caption summaries, references, and technical details
are selectable text. **Copy BibTeX** is the only copy button; Export offers brief
or full Markdown with paired BibTeX, or BibTeX alone. Each reference has an
**Open citation** link for viewing, copying BibTeX, or exporting that reference
alone, without adding EIDORA or other analysis citations. Complete analysis
reports retain their full bibliography. Optional scope and caption fields preserve
compatibility with existing frozen schema-1 reports. Caption summaries omit the
parameter inventory and full bibliography. The dialog can also select saved
results through a supplied source list and report-loading callback, without
owning project storage.

`CITATION.cff` remains the EIDORA citation authority. Its `preferred-citation`
currently names Tillmann Ohm's planned arXiv paper, with an explicit placeholder
note and no invented date or identifier. Displayed/exported references use this
preferred EIDORA citation even for older reports; stored report payloads and
other frozen model/method references remain intact. Citation notes survive
plain-text, Markdown, BibTeX, and report-payload round trips.

Long settings lists show their count and first 12 entries. Individual technical
values longer than 600 characters are abbreviated in report output, including
legacy frozen reports; complete source settings remain in saved records.
