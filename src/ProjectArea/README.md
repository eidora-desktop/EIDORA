# Project area

`ProjectArea` presents Media Sources on the left and Thumbnails on the right
in one equally divided horizontal splitter. Project storage stays in `project`;
thumbnail persistence stays in `DiscoveryArea.thumbnails`. There is no Artifacts
panel or creation workflow. Internal thumbnail-reuse and embedding artifact
contracts remain unchanged.

Adding or rescanning sources only indexes assets. **Link…** preserves existing
associations and adds unambiguous matches. **Relink…** previews and atomically
replaces only the selected source's links, reporting old associations that will
disappear. Neither action changes canonical item IDs or creates metadata rows.
When metadata is empty, **Create Rows and Link** is the separate explicit action
for creating one row per indexed asset. Users may instead import prepared rows.

Removing a media source also removes its EIDORA-owned generated text and archive
extraction copies, unless another registered source shares those paths. External
originals, metadata rows, and saved analysis results are retained. Generated text
storage is optional and is created when needed; an absent or removed generated
text directory must not prevent the project from opening.

New file sources infer their media type from the file. New folders and ZIP files
are inspected in the background: a source containing one supported type imports
without another question, while a mixed source asks which counted type to index.
The inspection manifest feeds publication so the filesystem is not traversed a
second time. A mixed folder or ZIP can be imported again to add another media
type as a separate source; types already registered for that path are omitted
from the next choice. Existing sources retain their saved type when rescanned.

Choose a source's relative path (relative to its registered root) or a structured
text record's Record ID, and one metadata column. Keys are stripped of surrounding
whitespace and Unicode-normalized to NFC. Relative-path separators normalize to
`/`; case and extensions stay significant. Blank, unmatched, and ambiguous keys
remain unlinked. Examples are bounded to 25 per category.

Old links remain intact, including links created with unsupported rules. On the
next Link or Relink, unsupported saved rules require a supported explicit choice.
`media_service.py` enforces this reduced Project contract; the shared legacy
`EmbeddingsArea` linking helpers remain available for unchanged Vector Data
preparation. Links across different sources may still reference the same item.

Media workers own scanning, previews, publication, cancellation, and generation
checks through the shared scheduler. The GUI only displays cached state and
bounded examples. Thumbnail options, variants, processing, persistence, and
callbacks retain their existing behavior.

Local verification on 2026-10-02 (macOS, Python 3.13, default SQLite rollback
journal/cache) measured the following synthetic selected-column workloads.
Times exclude discovery and fixture construction. Memory is peak Python
allocation during a replacement preview, measured separately from timing.

| Items | Preview + Apply | Preview memory |
| --- | --- | --- |
| 25,000 | 0.092 s | 20.3 MiB |
| 75,000 | 0.288 s | 50.8 MiB |
| 100,000 | 0.387 s | 72.7 MiB |

A disposable copy of the 74,346-item WikiArt catalog took 0.150 s for preview
and 1.403 s for Apply. The simplified workflow also passed an offscreen Qt check with 100,000 media
items and 100,000 metadata rows: both previews remained bounded at 25 rows and
the maximum measured heartbeat gaps were below 20 ms. Equal initial widths,
splitter resizing, project switching, and light/dark rendering were checked.

`state/processes.db` is a compact unfinished-job journal. It stores stable input
references, settings, lifecycle state, and reserved output names only while a
job may need recovery.
Generated files and per-file provenance remain exclusively in the thumbnail
repository. Progress stays in memory. Failed, canceled, and interrupted attempts
remain visible only for the current project session and are deleted from the
journal after cleanup.

Thumbnail creation accepts linked image/video sources or the largest available
images of another thumbnail set while preserving canonical metadata IDs.
