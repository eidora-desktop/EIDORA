# Media workspace

`MediaArea` presents Media Sources on the left and Preview Assets on the right
in one equally divided horizontal splitter. In Media Sources, link images, video,
audio, and text to Items, then use **Create Previews…** on a source row.
Preview Assets shows queued and running jobs and saved previews from linked
images, video frames, audio/video clips, animated GIFs, and text excerpts. Project storage stays in `project`;
thumbnail persistence stays in `DiscoveryArea.thumbnails`. There is no Artifacts
panel or creation workflow. Internal thumbnail-reuse and embedding artifact
contracts remain unchanged.

Media Sources uses two-line rows: the source name, status icon and linked/total file count on the first
line, combined as “45/567 audio files linked” beside the icon, and an elided path
on the second. Each idle row shows one primary action beside the options button:
**Link...** until it has links, then **Create Previews…**. Linked sources show the
linked icon directly after their name; its tooltip names the linked Items columns.
Unlinked sources show a red question mark without a circle. The options menu
retains **Relink...** for linked sources.
While loading, the status replaces the count and a full-width progress bar and Cancel replace
the path on the second line. Folder and ZIP inspection creates a cancellable row
immediately and reuses it for indexing. Project changes reset rows and their lookup.

**Create Previews…** opens a dialog fixed to that source. Images offer the preview
name and resize method. Video sources offer Still frames, Video clips, and
Animated GIFs; audio sources offer Audio clips, and text sources offer Text excerpts.
Sources without links show Link instead. Confirming a
supported creation dialog creates a progress row in Preview Assets, which becomes a saved set on
success. Cancelling the dialog creates nothing. Preview Assets has no Add New
button; its existing set-management and reuse actions remain available.

Preview Assets uses expandable cards without column headers, following Model Run
cards. Each card shows its name, item count or current status, and an options menu.
The second header line shows the source and preview sizes when idle.
Active jobs replace that line with a full-width progress bar and right-aligned
Pause/Cancel controls. Paused jobs retain their progress with Resume/Cancel controls.
Pause becomes available when running and is disabled during final publication. Progress stays visible when details are collapsed. Stage messages
are available in details and progress tooltips. The shared two-line header keeps its height through state changes; expanded
details grow below it.
Completed rows show actual generated item counts, including zero;
partially successful rows also show the skipped count. One expandable details
section contains the source, settings, creation date and results, with errors only
when present. Expansion survives publication and list refreshes. Rows have no selection or hover
highlight, and menus act directly on their own source or asset.
Rename, Run Again, Create Previews From, and Remove live in the row menu; failed
jobs offer Retry and Dismiss. Retry/Run Again are disabled when their original
input is unavailable, and removal is disabled while an output is in use.

Default names append `(thumbs)` for images and `(frames)` for video stills.
Automatic previews after embeddings use the same names. Video clips use `(video clips)`, audio clips `(audio clips)`, and GIFs `(GIFs)`.
Text excerpts use `(excerpts)`. Existing names receive a numeric suffix, and the dialog explains
empty or duplicate names before creation.

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
uses only the selected image/video source, or the largest images of an existing group
of Preview Assets. Each generated Preview Asset is indexed by Item ID.
Input resolution uses indexed lookups in bounded batches with SQLite cancellation
checks and preparation progress. Image jobs omit video frame settings. The footer
shows the live thumbnail stage and progress; queued and running cancellations
follow the same cleanup path from the footer and Media.
Pausing drains in-flight files before suspending the coordinator. Staged previews
and prepared inputs remain in memory for resume; cancellation and project changes
wake the coordinator and clean up staging. Paused jobs retain their scheduler
admission and executor slot. Decoder workers and database reads are never suspended.

A local 50,000-file synthetic folder benchmark on 2026-11-16 measured inspection
and publication at 3.54 s before and 1.23 s after removing repeated file checks
and the second staging index (2.9× faster). Fixture creation was excluded; these
are local filesystem measurements, not network-storage guarantees.

Media matching saves the selected columns together. Link adds missing asset/Item
pairs without duplicating existing links; checking replacement replaces only the
selected source’s complete set. Exact filenames retain extensions, and duplicate
media keys remain ambiguous. Source preview lists every linked supported asset.

## Audio/video clips and animated GIFs

Clip creation uses the selected linked source and the existing deterministic
one-asset-per-Item resolver. It never creates Items. Choose an excerpt duration
(1–30 seconds, default 5) and Beginning, Middle, End, or Random (default Middle).
Short inputs use their available duration; random selection is stable for the
same registered source/asset and random seed. Each set retains its type, settings, source asset
IDs, actual start times/durations, dimensions, and byte sizes.

Video clips use H.264 MP4, with Compact (CRF 32), Balanced (26), or High (20)
quality; Compact is the default. The maximum edge is 128/192/256 px (default
256), and the frame-rate cap is 1–24 fps (default 24). Sources are never upscaled
or increased in frame rate. Audio is included by default when present;
**Mute video clip** omits it. Audio clips use AAC M4A. Both support
32/48/64/96/128 kbit/s audio (default 48) and Mono/Stereo (default Mono).
Stereo preserves stereo sources; mono sources remain mono.
Video and audio clips contain one forward excerpt and have no loop setting.

Animated GIFs are silent, at most 256 pixels on the longest edge, with a
configurable 1–24 fps cap (default 12), 64/128/256-color palette (default 128),
and optional dithering (default off). Loop offers **Play once**, **Forever**,
and **Ping-Pong**. Ping-Pong encodes the forward and reversed frames without
repeating the endpoint frames, then loops forever. Its playback cycle is roughly
twice the selected excerpt duration. GIFs can be larger than equivalent videos.

`clip_generator.py` runs one isolated `preview-clip` worker at a time for each
job. `clip_worker.py` keeps scaled video frames in temporary disk-backed storage,
uses a bounded mono/stereo audio buffer, and writes a poster or waveform alongside each
clip. Multiple Items sharing an input within a set reuse the encoded files.
Pause drains the active file; cancellation terminates its worker before cleanup.
Bad sources are reported separately from missing files. Output/storage failures
abort publication. Successful sets publish through the existing preview repository
in one transaction; old image-only projects remain compatible.

Completed sets provide **Inspect…** with bounded Item navigation, manual playback,
seeking for audio/video, and playback muting. Switching Items or closing stops
and unloads playback. The inspector honors the loop instructions embedded in GIFs.
Cards show settings and actual total storage, counting each Item once regardless
of its supporting files. Rename, Run Again, Retry, and Remove retain their existing
semantics. Generated clips remain inspectable without the original media.

Clip/GIF sets cannot be used as image-derived preview inputs. Discovery playback
is owned by its preview controllers.

## Preview parameters

Creation follows the projection dialog's parameter/value table layout. Basic
parameters remain visible; **Advanced parameters** starts collapsed, and
**Reset defaults** resets both sections without changing the output name.
Collapsing preserves edits. Changing processing type loads its defaults;
**Run Again** restores the saved basic and advanced settings. The summary shows
effective settings even while Advanced is collapsed.

- Image thumbnails: image sizing is basic; WebP quality (1–100, default 85) is
  advanced. Output sizes remain 32/64/128/256 px.
- Video still frames: frame selection and image sizing are basic; WebP quality
  and the conditional random seed are advanced.
- Audio clips: duration and position are basic; bitrate, channels, and the
  conditional random seed are advanced.
- Video clips: duration, position, and muting are basic; quality, FPS cap, maximum
  edge, audio settings, and the conditional random seed are advanced. Muting hides
  audio controls without discarding their values.
- GIFs: duration, position, and loop are basic; FPS cap, maximum edge, palette,
  dithering, and the conditional random seed are advanced.

Random seed appears only for Random selection, defaults to zero, and accepts
0–2147483647. The same seed and source identity reproduce selections across new
sets; changing it requests different selections. Saved generation settings are
validated again outside the UI and recorded with each completed set. Older sets
without the new settings use their previous encoding defaults.


## Text excerpts

Linked text sources offer **Text excerpts**, named `Source (excerpts)` by default.
Basic parameters are maximum characters (1–10,000, default 400) and Beginning,
Middle, End, or Random (default Beginning). Advanced parameters offer **Prefer
complete words** (default) or **Character cut-off**, plus the shared random seed
when Random is selected. Word boundaries use whitespace; unbroken text falls
back to the character limit. Short sources use their available text, and
paragraph breaks remain intact. Leading/trailing excerpt whitespace is trimmed.

`text_source_repository.py` reads whole files unless the linked asset explicitly
identifies a JSONL record. BOM-marked UTF-8/16/32 inputs are supported. Reads are
cancellable, with a 2,000,000-character limit per document or record; larger inputs
are reported as failures rather than silently treating a truncated prefix as the
whole document. Shared record files are grouped and their seek offsets reused.
Missing, empty, malformed, and ambiguous record inputs produce bounded error
reports. Pause, resume, cancellation, retry, and publication use the existing job
lifecycle. Output/storage failures abort publication.

`text_generator.py` selects literal excerpts and saves them in `preview_texts`
inside the existing preview repository, indexed by set and Item. Each excerpt
retains its source asset ID, optional record key, character offsets, source length,
and UTF-8 content size. Set storage reports text bytes, excluding database overhead.
Text sets have no pixel sizes, image files, font settings, or colors.

**Inspect…** opens a Designer-owned plain-text viewer with Previous/Next Item
navigation, omission markers, and source positions. It reads saved excerpts in a
background worker and works without the original media. **Run Again…** restores
saved selection settings; rename and removal use normal set management. Text
sets are excluded from image-derived generation and canvas image previews.
Discovery tooltips and Pins display saved text excerpts with independent font and
size settings owned by their respective settings panels.
