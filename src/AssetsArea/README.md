# Assets workspace

`AssetsArea` presents Media Sources on the left and Thumbnails on the right
in one equally divided horizontal splitter. Project storage stays in `project`;
thumbnail persistence stays in `DiscoveryArea.thumbnails`. There is no Artifacts
panel or creation workflow. Internal thumbnail-reuse and embedding artifact
contracts remain unchanged.

Media Sources uses two-line rows: the source name, status icon and linked/total file count on the first
line, combined as “45/567 audio files linked” beside the icon, and an elided path on the second. Link sits next to the top-right options button. Linked sources show a disabled “linked” label at the same width and show the linked icon directly after their name; its
tooltip names the linked Items columns. Unlinked sources show a red question mark without a circle. Their options menu offers Relink...
While loading, the status replaces the count and a small progress bar sits beside
the path, retaining two lines. Folder and ZIP inspection creates a cancellable row
immediately and reuses it for indexing. Project changes reset rows and their lookup.

Adding or rescanning sources only indexes assets and never opens a matching dialog.
**Link...** is available once items exist and reads the resolved fields shown in
Items. It adds links by default. The dialog’s **Replace existing links for this
source** checkbox also removes links that no longer match. It starts unchecked
on every visit. The preview counts added, kept, and removed asset/Item pairs.
Several items may share one asset; blank or conflicting inherited keys stay
unmatched. An Item can link to multiple assets from the same source. Media
operations never create items or metadata rows. Only vector import and successful embedding publication create items.

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
second time. Ordinary folders publish directly from this manifest in one transaction,
reusing recorded size/time and already resolved paths. ZIP extraction retains its
separate staging path. A mixed folder or ZIP can be imported again to add another media
type as a separate source; types already registered for that path are omitted
from the next choice. Existing sources retain their saved type when rescanned.

Link starts with the best match found from actual values, preferring exact
matching when coverage ties. Saved supported rules stay selected. The preview
shows media and item values together; expand Link options to adjust or rerun
detection. Suggestions never publish links.

Choose a source's relative path (relative to its registered root) or a structured
text record's Record ID, and one or more metadata columns. Explicit list cells
contribute every filename; conflicting inherited scalar values remain unresolved.
Keys are stripped of surrounding
whitespace and Unicode-normalized to NFC. Relative-path separators normalize to
`/`; case and extensions stay significant. Blank, unmatched, and ambiguous keys
remain unlinked. Examples are bounded to 25 per category.

Old links remain intact, including links created with unsupported rules. On the
next Link, unsupported saved rules require a supported explicit choice.
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
Input resolution uses indexed lookups in bounded batches with SQLite cancellation
checks and preparation progress. Image jobs omit video frame settings. The footer
shows the live thumbnail stage and progress; queued and running cancellations
follow the same cleanup path from the footer and Assets.

A local 50,000-file synthetic folder benchmark on 2026-11-16 measured inspection
and publication at 3.54 s before and 1.23 s after removing repeated file checks
and the second staging index (2.9× faster). Fixture creation was excluded; these
are local filesystem measurements, not network-storage guarantees.

Media matching saves the selected columns together. Link adds missing asset/Item
pairs without duplicating existing links; checking replacement replaces only the
selected source’s complete set. Exact filenames retain extensions, and duplicate
media keys remain ambiguous. Source preview lists every linked supported asset.
