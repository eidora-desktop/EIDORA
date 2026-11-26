# Figures and animation

`domain.py` defines compositions, settings and animation frames. `renderer.py`
builds Matplotlib figures. `animation_service.py` owns sequencing/playback rules;
`animation_export_service.py` stages GIF output and `animation_gif_worker.py`
encodes it in an isolated process. `repository.py` automatically persists compositions and settings. Tabs are the project
figure collection, including animations. The × button asks for confirmation before
permanently deleting a figure; Cancel leaves it intact. Remaining figures reopen
with the project. Source snapshots and exported files are not deleted.

On first activation, the workspace reads the saved collection and immediately creates
lightweight tabs with loading placeholders. Large manifests load on the data-loading
thread without starting the rendering process. Only the selected tab prepares its
saved composition (including legacy layout/data conversion) and builds its static
or animation UI. Other tabs retain their saved records until selected, including
when the collection is saved, renamed, or a figure is deleted. Failed preparation
keeps the original record and offers Retry. Closing a tab or switching projects
rejects late results. Switching tabs cancels preparation that has not started;
running or completed loads remain reusable on return. Legacy animation settings
normalize in the repository, so cleanup protects their private sources even when
their tabs remain unopened. The selected preview then uses the normal rendering pipeline.

Dropping a snapshot imports its complete captured view, including its current
slice. Plot Settings identify the source snapshot separately from reusable
component presets. **From snapshot** restores the figure's imported copy;
**None** disables filters or slicing, and **None (Original)** removes a transform.
Selecting a preset copies its settings. Selecting the same preset again explicitly
reapplies its latest saved settings. Changes to source files never refresh an
imported plot automatically. Drop the updated snapshot onto that plot to replace
its captured view and clear component overrides, retaining figure layout,
authored titles/captions, and measurement settings. Older plots acquire a reset
baseline when their snapshot is dropped again.

Captured reset coordinates and metadata use the existing figure-data repository;
small datasets remain inline. New animation imports keep private immutable sources
under `state/figure_snapshots/`, so playback, thumbnails, and exports share the
same copy. Saved collections retain referenced baseline data through cleanup and
project moves. Legacy animation references are still readable; reimport them to
capture independent sources. Figure settings presets remain separate from plot
appearance/filter/slicer presets. All preset catalogs belong to the current
project. Menus expose **Apply Preset** and **Save as Preset…**, with **Manage Presets…**
at the bottom of each **Apply Preset** submenu opening Settings on the Presets tab.
Static exports
are staged beside the destination and replace it only after a successful write.
Right-click a figure tab and choose **Rename…**, or double-click its label, to
rename it. Names autosave with the project and supply export filename suggestions.

[Controllers](controllers/README.md) compose snapshots and bind Designer forms.
Keep per-frame plot overrides consistent across preview, thumbnails and export.
General figure-setting changes redraw the selected preview and enable the explicit
thumbnail refresh action; per-frame edits refresh affected thumbnails.
Snapshot, slicer and transformed timeline thumbnails all use the existing background
frame preparation/export path, one in-flight frame per tab. Full-resolution PNGs are
staged separately and published only when their frame and settings are still current.
Cancellation or failure leaves the last accepted thumbnail intact. This shares the
workspace renderer and its cache budgets with previews and exports.
Replacing snapshot data also replaces its cached dataset identity, including direct
frame exports, so each frame uses its own axis bounds rather than a previous frame's.

Export formats must also pass the packaged application self-test.

New figures use the pixel profiles in `template_settings.py`:

| Starting value (px) | Journal | Presentation | Animation |
| --- | ---: | ---: | ---: |
| Page | 1600 × 1200 | 1920 × 1080 | 800 × 600 |
| Outer margin | 40 | 48 | 24 |
| Plot spacing | 32 | 40 | 20 |
| Main title | 48 | 56 | 28 |
| Plot titles | 32 | 36 | 22 |
| Legend/grid labels | 24 | 28 | 18 |
| Captions | 22 | 24 | 16 |
| Grid line width | 1 | 1.5 | 1 |

Each starts with one cell, no visible titles, captions, legend, grid or axis box,
and no extra point scaling applied to imported snapshots. The initial plot design
fills the area inside the margins at scale 1; subsequent layout changes retain
proportional scaling. Animation retains 150 ms frames and looping playback.
`default_plot_settings` applies only to new figures. Repository loading uses
`saved_plot_defaults`, whose historical fallback values must remain unchanged so
older documents and partial presets do not inherit the new starting profile.

Width and height specify the final exported pixel dimensions, including margins and figure
elements. `page_layout.py` measures decorations before fitting each plot into
the reserved rows and columns. Empty cells retain their space. The **stretch plots** checkbox
under **Main Figure**, after width and height, applies to every plot, including a
single-plot figure. Unchecked (the default), it centers each plot using
its captured camera's width-to-height ratio, leaving excess space blank. Imported
snapshot baselines retain that ratio through camera preset changes. Older figures
without this setting also keep the snapshot ratio. Missing or
invalid camera bounds fall back to the initial plot design ratio. Checked,
it stretches each plot into its cell after reserving annotation space, without
changing the camera bounds or cropping additional content. Points, borders, grid
labels, pins, local titles, captions and legends keep uniform style scaling;
figure-level text and shared legends retain their chosen pixel sizes. Both modes
use the same layout for preview, animation frames and exports, and persist in saved
figures and figure settings presets. A page too small for its
fixed margins or shared decorations reports a layout error rather than growing.

State version 7 converts older per-plot dimensions to their measured final page
size without loading point datasets. The conversion retains the original plot
dimensions and measured decoration bands. The selected aspect mode determines
how plots use that space. Editing
those decorations replaces their inherited spacing constraints. Preview and
vector/raster export use the same page geometry; rendering never writes scaled
font or marker sizes back into the document.

Legacy animation conversion waits for the selected frame to load successfully,
so its actual legend and title determine the saved page dimensions. Pending or
failed loads retain the legacy settings. Missing cells in a legacy grid persist
as `reserved_only` gaps: they accept plot drops but draw no axes or generated
annotations until populated.

The settings panel displays actual output pixels for every dimension, margin,
font and line width, with no resolution control or unit selector. Page dimensions
are whole pixels; other measurements allow fractional pixels. Values adapt when
the page or reserved grid changes. Differing output sizes display as Mixed.
Entering a value sets that pixel size for all plots in the current layout, and
later layout edits scale it proportionally. Older presets' pending size edits
also apply to all plots; existing saved per-cell sizes remain intact until edited.
Figure-level sizes remain fixed. Display rounding never rewrites saved values.
SVG files declare the same pixel width and height as raster exports.

`measurements.py` converts between output pixels and the established internal
mm/point rendering contract. The saved resolution stays internal to preserve
existing compositions, font metrics and vector output. `page_inches_for_pixels`
protects integer pixel requests from floating-point truncation in Agg. The worker
resolves size edits using only text and style data. Per-cell design overrides
retain full precision and travel with compositions, presets and animation settings.
Results apply only to the unchanged target revision. Older presets with pending
mm/point edits convert to pixels without rounding their requested values.

Large static previews render in one persistent spawned Agg process per Figures
workspace. The GUI keeps the last image visible while a latest-only coordinator
prepares the next revision. `render_scheduler.py` keeps one render lane and at most
one queued preview per view. The visible view takes priority over hidden preview
work; queued exports and loads keep FIFO order and get a turn between interactive
previews so repeated edits cannot starve them. Superseding a queued preview releases
its captured composition. A running publication finishes, but checks visibility
and revision before entering Agg: obsolete/hidden work publishes durable dataset
references without drawing or transferring an image. Already-running Agg renders
and exports finish normally; they are not forcibly interrupted.

The GUI coalesces edits for one event-loop turn with a zero-delay single-shot timer.
Future completion emits a queued Qt signal instead of waiting for periodic 25 ms
polling. Only GUI slots adopt dataset references, validate revisions and apply
images. Future callbacks hold weak controller references, and closed or destroyed
views safely ignore completion. Matching durable data can be adopted even from a
stale image revision, allowing the GUI to release arrays without displaying stale
pixels. Accepted-frame validation remains unchanged.

Small compositions remain immediate. Snapshot and
projection data loading, document writes, and large legacy-document migration
also run outside the GUI thread. Inactive tabs defer new rendering and skip queued image work, while accepting
matching durable dataset references so their
decoded document arrays can be released. A workspace-wide 64 MiB LRU bounds their full-resolution images
and displayed pixmaps, including animation's embedded plot view. The active view
is exempt. Deactivation releases the synchronous Matplotlib canvas; eviction
releases only the view image. Selecting an evicted tab regenerates the preview
from its unchanged composition. The last displayed pixmap stays visible during
regeneration when it fits the same budget. Hidden resize events do not allocate
new pixmaps.

GUI preview completion compares each subplot's geometry and empty-state signature
before moving its controls. Preset lists are populated when their panel opens,
and refreshed on selection changes while open; hidden panels do not scan preset
folders or rebuild combo models for every preview. Opening a panel always rereads
the catalog, including renamed or newly saved presets. Unchanged element overlays
keep their geometry.

`transform_service.py` prepares transformation and Original-coordinate changes on
a dedicated worker: source loading, fingerprinting, transformation, metadata and
pin reanchoring all finish before the GUI receives a prepared cell. Coordinate
queries use bounded batches and dense arrays; pin lookup retains only requested
IDs. One thread-safe 128 MiB workspace cache retains transformed coordinate/ID
buffers across static tabs, animation tabs and snapshot overrides, without grids
or metadata. Entries are keyed by coordinate/ID content and transformation settings,
so matching results can be shared across tabs. A separate workspace-wide 64 MiB
cache bounds decoded animation previews; per-animation item limits still apply.
Eviction preserves source files and documents and only causes recomputation or
decoding on demand. Project changes and workspace shutdown clear these caches
after workers stop. These limits cover retained cache entries, not borrowed arrays,
visible timeline pixmaps or temporary computation buffers. Cooperative cancellation and revision/state checks discard obsolete
results. `transform_coordinator.py` owns a GUI polling timer; its workers own no Qt
objects. Transformed animation previews and thumbnails use background preparation,
and the interactive GIF dialog waits asynchronously for full-quality frame exports.
Synchronous frame-export helpers remain available for programmatic callers.

`point_preview_cache.py` caches each subplot's point layer within a 128 MiB budget.
Point layers are compressed losslessly with fast zlib compression in the render
worker, with raw-byte storage when compression would be larger. The budget counts
encoded pixels and style keys; a 512-entry limit also bounds small cache records.
Restoration decodes one layer at a time and copies its exact RGBA bytes into Agg,
without a second alpha blend. Existing background-digest validation preserves
pins and other content beneath overlapping plots. Decoded copies of compressed layers are released after restoration; raw fallback
regions count at their full size against the same cache budget. More tabs can reuse point layers
without retaining multiple Figures/Axes or increasing cache budgets. A miss or
an incompressible working set still rebuilds points at full resolution.
`data_repository.py` bounds decoded worker datasets to an estimated 256 MiB.
The renderer retains at most 256 small dataset summaries (bounds and membership
of requested pins). It uses these to check point-layer caches before decoding
coordinates or metadata. New data, pin identities, or a cache miss load data on
demand. Legends use saved style state. A per-render dataset map shares each
referenced dataset across cells even when it exceeds the decoded LRU budget;
exports use the same sharing but always draw ordinary full-quality artists.
The worker retains one preview Figure and Agg canvas. When measured page geometry,
DPI and occupied cells match, it reuses the Axes and unchanged plot artists; a
changed plot clears only its own Axes. Layout changes replace the Axes. Titles,
captions and legends are measured and recreated, so wrapping and legend sizes
remain authoritative. Pin geometry is reused only with unchanged plot state and
thumbnail file revisions. `partial_page.py` retains only the last successfully
drawn artist signatures and conservative pixel bounds. With unchanged layout it
clears the old/new affected regions directly in the existing Agg canvas and
redraws their plots, text and legends in the original painter order. Overlap
closure includes neighboring content such as pins crossing plot boundaries.
Unchanged regions keep their original pixels. Layout/DPI/order changes, unsupported
effects, nonopaque page backgrounds, and regions covering at least 60% of the page use a full
redraw. An unchanged page draws no artists. No additional page-sized background
buffer is retained; failures discard the page and its partial-draw state.
`preview_transfer.py` compares SHA-256 digests of 256-by-64-pixel tiles and sends
only changed regions, combining adjacent changed tiles in each row. It retains
digests for one page, never a second full image. A full image is sent when at
least 60% changes, dimensions change, or the GUI's accepted frame ID differs from
the worker's last frame. Discarded results, interleaved tabs and worker restarts
therefore cannot apply patches to the wrong image. An unchanged image sends no
pixels. Exports do not use this preview protocol.

Completed worker requests release their compositions and per-draw data callbacks.
Retained point artists release their collections and raster references; the
existing LRU is the sole owner of idle point layers, so retaining a page does not
bypass its byte budget. An evicted layer reloads its data only on the next draw.
Only one page is retained across tab switches, and failures retire that page.
After sending a reply, the isolated worker drops request references and explicitly
collects retired Matplotlib figure/canvas/artist cycles before accepting another
request. Clearing a figure alone can leave full Agg pages awaiting automatic
collection. Cleanup also follows failed renders and exports; live point and data
caches remain reusable. This collection runs only in the render process.
The cache budgets limit retained caches, not the total working memory of a render.
Cache keys include immutable data identity, appearance, filters, camera, pixel
geometry and grid settings. Cached pixels are reused only when the underlying
pixels also match, preserving pins that overlap another subplot. Layout, fonts,
legends and pin geometry still come from the authoritative Matplotlib renderer.
Previews keep the same resolution and point population as before. Exports bypass
the point cache and retain their requested DPI and vector point artists.

Figure state version 7 keeps large coordinate/ID arrays and frozen metadata in
content-addressed NPZ artifacts under `state/figure_data/`. Documents reference
these project-owned artifacts using portable paths. `compact_metadata.py` stores
metadata in lossless dictionary-encoded columns with separate codes, UTF-8 JSON
values, and offsets. Workers load only accessed fields and map repeated categories
once per distinct value. Missing fields, empty strings, nulls, Unicode and original
IDs remain distinct. Old row-JSON artifacts remain readable and are compacted in
worker memory. Lazy column allocations count toward the decoded-cache budget. Small datasets (up to 2,000
points) remain inline. A setting edit rewrites only the small document; datasets
are shared across cells and written once. Treat point buffers and metadata as
immutable and replace them when data changes: `snapshot_composition` captures
small editable state while sharing those buffers. Once a preview is delivered,
the GUI releases large data buffers and keeps the durable reference.

Snapshot metadata preparation reads bounded SQLite batches directly into compact
columns. It preserves the existing database-to-string normalization, including
NULL-to-empty-string conversion, without retaining a dictionary for every row.
High-cardinality fields use bounded distinct-value lookups and bounded byte/offset
staging. Prepared column arrays are immutable and persist without decoding and
encoding rows again. All columns are frozen so later appearance edits remain
independent of the source database.

Frame overrides choose the final coordinate/ID set before loading metadata once.
Transforms reuse already-loaded source coordinates for ordinary snapshots; frozen
arrangements still supply their established snapshot/pin state and fallback data.
Each new preparation reads current metadata; existing prepared figures retain
their frozen values. Synchronous frame exports use the same preparation path.

Preview replies send raw RGBA buffers separately from the small control message,
avoiding full-image pickling and an extra worker-side bytes allocation. Images
travel in bounded 256 KiB messages and are joined once into immutable bytes in
the receiving thread. Do not receive a whole page into Connection's growing
buffer or assemble it with a growing BytesIO/bytearray: repeated large buffer
growth can retain allocations in the GUI process even after images are released.
Qt owns a
reference to received full-image bytes without another full-page QImage copy.
The first region update detaches that image into writable Qt storage, then later
updates replace only changed pixels in place. External immutable bytes are never
mutated. Regions use bounded packets too, so small edits avoid full-page receive
assembly and replacement. The GUI accepts regions only for its displayed frame.
Mutable synchronous canvas buffers are still detached. The existing pixmap scaling
path is preserved for pixel parity; unchanged display sizes skip repeat conversion.
`tests/performance/figures_image_benchmark.py` compares this image ownership path
with the previous copied-image path in separate processes.

Old state versions remain readable. Large inline documents are migrated in the
worker, publishing all datasets before atomically replacing the state manifest.
Confirmed deletion removes the document from the next autosave. Shutdown flushes pending saves,
stops workers, and only then removes unreferenced owned data artifacts; corrupt
or unrecognized manifests do not trigger cleanup.

Maintainer performance validation is available in the private validation suite as
`tests/performance/figures_benchmark.py`. It reports initial rendering, one-cell
and title edits, save size/time, cache reuse, and GUI heartbeat gaps. With psutil
installed, it samples combined GUI and renderer RSS every 10 ms; this is a sum
of resident memory, not a unique-page count. The probe retains its input data
in the GUI throughout so before/after comparisons use the same workload. Pixel-parity
tests compare cached output with the full renderer, including overlapping pins.

`tests/performance/figures_cleanup_benchmark.py` exercises repeated edits through
the spawned worker and Qt preview adapter. Run `before` and `after` in separate
processes with `--output results.json`; `before` disables only explicit worker
collection. Automatic GC remains enabled in both modes. The probe samples combined
GUI/worker RSS and records reply latency, post-reply cleanup/collection latency,
time until the worker is ready again, heartbeat gaps and selected pixel hashes.
Use `--mixed` to include point-style edits. It retains the input data equally in
both runs and reports worker RSS separately so GUI retention cannot be mistaken
for worker growth. Cleanup time is measured before the next blocking receive.
To compare image-transfer memory, keep worker cleanup enabled with `after` in
both runs and compare `--image-transport whole` with `--image-transport bounded`.
Each edit receives a fresh frame, unlike a loop reusing one input image. Check
settled memory over extended runs as well as peak RSS and responsiveness.

To compare page reuse, keep `after --image-transport bounded` in both runs and
compare `--page-rendering rebuild` with `--page-rendering incremental`. The rebuild
baseline uses the same rendering code and caches but discards its page after every
reply. `--decorated` includes map ticks, pins and a legend; `--mixed` edits one
plot every fifth request. The report also records page-build time and the number
of rebuilt plots. Compare pixel hashes as well as timing and combined memory.

Add `--region-transfer` to compare region updates with full-image transfer.
Use `--tabs 8 --switch-every 5` to exercise tab switching and compare
`--inactive-budget-mib -1` (no eviction) with the default 64 MiB budget. Reports
include transferred bytes and retained preview memory alongside combined RSS.

Compare `--page-drawing full` and `--page-drawing partial` with the same region
transfer, page-reuse and cache settings to measure partial drawing independently.
The report records draw time and the number/area of artist groups actually drawn.
Check pixel hashes and extended mixed-edit/tab-switch runs as well as latency.

`tests/performance/figures_transform_benchmark.py` compares previous synchronous
preparation with the worker path, including coordinate loading, metadata and pins.
Run `before` and `after` in separate processes with the same `--points`; `--original`
checks restoring original coordinates. It reports wall time, GUI heartbeat gaps and
process RSS, which includes both GUI and preparation-worker threads. It measures
preparation separately from the Agg renderer; use `figures_benchmark.py` for total
GUI/renderer memory during rendering.

`tests/performance/figures_metadata_benchmark.py` compares the previous dictionary
and duplicate-load path with direct column preparation, persistence and spawned
rendering. Run `before` and `after` separately with `--output results.json`; use
`--transform`, `--points`, `--plots` and `--frames` to choose the workload. It records
load counts, preparation and completion times, combined GUI/worker RSS, heartbeat
gaps and pixel hashes. Encoding now occurs during preparation, so compare total
completion time as well as the preparation phase alone.

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

Each snapshot row has a **Methods…** button opening the shared methods report,
with selectable brief methods, caption summaries and references, expandable
details, Copy BibTeX and Markdown/BibTeX export. The project menu provides the same reports
without using Figures. Plot cells retain the source
snapshot report in their saved render state. Caption editors offer **Insert
Snapshot Methods**, so reports remain usable after the original snapshot is
removed. Inserted text describes source snapshots and asks users to review later
figure-specific edits.

`tests/performance/figures_tabs_benchmark.py` exercises real PlotTabControllers in
a QTabWidget, with a separate seeded 100,000-point dataset in every subplot.
Its default eight tabs contain nine plots each and use a catalog of 250 presets;
it prepares two actual transformations
per dataset before measuring 80 edits and switches every five edits. Compare separate
processes against before/after source using `--output results.json`. Results include
GUI overlay time, edit-to-preview latency (including coordinator polling), a 10 ms
Qt heartbeat, sampled combined GUI/render-worker RSS, cache bytes, and pixel hashes.
This benchmark uses real tab widgets but omits the surrounding project panels and
autosave callbacks. Offscreen heartbeat measures event-loop availability, not native
window/compositor latency.

A local macOS/offscreen comparison (eight tabs, 72 distinct datasets, 100,000 points
each, 250 presets, 300 DPI, 80 edits) reduced median GUI overlay work from 56.00 to 0.04 ms,
median edit-to-preview time from 137 to 82 ms, and the largest measured 10 ms
heartbeat gap from 86 to 14 ms. Combined GUI/worker RSS peaked at 1,225 versus
1,094 MiB; transformation-cache retention fell from 220 to 127 MiB. All 80 frame
hashes matched exactly. The last four 20-edit peak windows were 1,093.23, 1,093.50,
1,093.75 and 1,094.02 MiB after the change. That comparison predates the compressed point cache described below; its tab
switches still took about 1.9 seconds off the GUI thread. These measurements are
a workload comparison, not a bound on total application memory.

For tab-switch comparisons, run `figures_tabs_benchmark.py --skip-transforms
--switch-only --edits 40 --output results.json` against before/after source in
separate processes. This switches among eight actual tabs with 72 distinct original
100,000-point datasets; it skips transformation-cache warmup to isolate rendering.
Omit `--switch-only` to also edit titles and point sizes between tab switches.
The original transformation-warmup workload remains the default.

A local macOS/offscreen comparison with eight tabs, 72 distinct 100,000-point
datasets, 300 DPI and 250 presets measured 40 unchanged-tab switches at a median
1,815 ms before versus 239 ms after compressed point caching. Combined GUI/worker
RSS peaked at 996 versus 862 MiB; the largest 10 ms heartbeat gap was 13 versus
10 ms. Four consecutive 10-switch peak-memory windows after the change were
862.11, 862.19, 862.30 and 862.41 MiB. In a separate 80-edit run, switching and
editing a plot's point size fell from a median 1,846 to 446 ms, while ordinary
edits measured 77 versus 82 ms. The mixed workload's combined peak fell from
976 to 869 MiB. All 120 before/after frame hashes matched exactly. These runs
used original coordinates without transformation-cache warmup and excluded
surrounding project panels/autosave. Compression adds CPU work on cold draws
and restoration; savings depend on layer compressibility and cache fit.

`--burst-tabs 8 --edits 10 --skip-transforms` exercises eight successive real tab
edits 35 ms apart before waiting for the final visible preview. Reported ready
latency starts at that final request; per-record `burst_ms` includes the whole
burst. `render_requests` counts actual render-process requests, including initial
previews at the top level. Pixel hashes compare the final visible compositions
across before/after runs, regardless of intentionally skipped intermediate frames.

A local macOS/offscreen scheduling comparison (eight real tabs, nine distinct
100,000-point plots each, 300 DPI, 250 presets) reduced median ordinary preview
latency from 82 to 31 ms over 80 edits. Peak combined GUI/worker RSS stayed about
869 MiB, and all 80 frame hashes matched. Ten eight-tab bursts reduced median
final-view latency from 1,411 to 364 ms (95th percentile 2,452 to 680 ms), with
28 versus 80 actual draws after initialization. All ten final frame hashes matched.
Burst peak RSS was 869 versus 863 MiB; maximum GUI heartbeat gaps stayed under
14 ms. A longer 40-burst/320-edit run peaked at 896 MiB and finished at 868 MiB
combined, with a 508 ms median final-view wait and a 13 ms maximum heartbeat gap.
That wait includes an already-running request, which is deliberately not interrupted.
These runs skip transformation warmup and surrounding project panels/autosave.
