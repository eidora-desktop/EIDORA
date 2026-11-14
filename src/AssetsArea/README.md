# Assets workspace

`AssetsArea` presents Media Sources on the left and Thumbnails on the right
in one equally divided horizontal splitter. Project storage stays in `project`;
thumbnail persistence stays in `DiscoveryArea.thumbnails`. There is no Artifacts
panel or creation workflow. Internal thumbnail-reuse and embedding artifact
contracts remain unchanged.

Adding or rescanning sources only indexes assets and never opens a matching dialog.
**Match Items…** is available once items exist. It preserves existing associations;
**Relink…** previews and replaces only the selected source's associations. Both
read the resolved fields shown in Items. Several items may share one asset; blank
or conflicting inherited keys stay unmatched. Media operations never create items
or metadata rows. Only vector import and successful embedding publication create items.

**Locate Source…** moves a registration to an existing folder or the original
filename at another location. The catalog and path publish together; asset IDs,
item matches, and saved run inputs retain their identities. Reusing a location
already registered by another source is rejected. Original files are never moved.

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

Match Items starts with the best match found from actual values, preferring exact
matching when coverage ties. Saved supported rules stay selected. The preview
shows media and item values together; expand Link options to adjust or rerun
detection. Suggestions never publish links.

Choose a source's relative path (relative to its registered root) or a structured
text record's Record ID, and one metadata column. Keys are stripped of surrounding
whitespace and Unicode-normalized to NFC. Relative-path separators normalize to
`/`; case and extensions stay significant. Blank, unmatched, and ambiguous keys
remain unlinked. Examples are bounded to 25 per category.

Old links remain intact, including links created with unsupported rules. On the
next Link or Relink, unsupported saved rules require a supported explicit choice.
`media_service.py` enforces the Project contract; legacy item-creation APIs
reject requests rather than bypassing vector publication. Links across different sources may still reference the same item.

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

Thumbnail creation resolves active item IDs through approved record paths and
uses only the selected image/video source, or the largest images of an existing
thumbnail set. It never writes source-record indexes into an item thumbnail set.
