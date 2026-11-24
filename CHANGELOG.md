# Changelog

Release-relevant changes are tracked here. Version selection follows the [versioning policy](packaging/README.md#versioning).

## Unreleased

- Create plain-text excerpt Preview Assets from linked text sources in Media. Choose beginning, middle, end, or reproducible random excerpts, with a character limit and optional word-boundary trimming. Inspect saved excerpts without the original source and reuse the existing pause, cancel, retry, and set-management workflow.

- Prepare text files consistently as whole documents, including JSONL, while preserving explicit indexed-record inputs. Support BOM-marked Unicode, identify failing files/records, bound text reads, and reuse tokenizers and record offsets without adding model dependencies.

- Simplify the Project Chooser’s More Options menu: Settings opens directly on Storage, followed by a separator; remove the separate Storage action.

- Add confirmed bulk deletion for projects in the Settings Project Location, closing the active project and waiting for saves first. Move preset selection and deletion controls into the Presets tab.

- Use basic and expandable advanced parameter tables for Preview Asset creation, with aligned Asset Type and Output name form rows, Reset defaults, and saved settings. Expose image/video quality, video/GIF frame-rate and size limits, audio bitrate/channels, GIF palette/dithering, and reproducible random seeds.

- Connect the Settings Storage tab to background size measurement and confirmed model cleanup through its trash icon. Nonempty folder changes offer relocation or keeping the current folder; relocation closes the project, verifies and moves its contents, and returns to the Project Chooser after Settings closes.

- Create compact audio clips, video clips, and animated GIFs from linked Media Sources. Add excerpt settings, optional video muting, GIF palette/dithering and Play once/Forever/Ping-Pong controls, stored output sizes, and a Media inspector. Keep clip sets separate from Canvas previews until later integration.

- Rename the project-menu action to “Switch Projects…”.

- Hide the Project Chooser immediately when opening a project and use only the square loading indicator until the main window is ready. Remove the chooser's redundant progress bar and loading text; restore the chooser on errors for retry or cancellation.

- Show a square light/dark loading panel with a rotating square and the EIDORA wordmark when opening a project. Tint the wordmark for light/dark appearance and render it at the display scale. Load project files in a worker and prepare hidden workspaces in queued steps, keeping Discovery's graphics host attached before the main window appears. Keep the loader visible through workspace refresh and the window handoff when switching projects from the chooser.

- Remove the duplicate menu arrow from the camera preset button in Discovery Canvas Layers.

- Allow Grid and No Overlap transforms for every projection, including PCA, MDS, and imported coordinates, in Discovery and Figures. Preserve and restore their saved arrangements.

- Remove oversized gaps between Preview Assets cards on opening by refreshing the tree layout after hidden cards become visible.

- Close the Main Window before showing the Project Chooser and restore it after selection or cancellation.

- Restyle Settings to match EIDORA’s form dialogs: clean sections, compact choices, grouped storage fields, taller preset rows, and a shared action footer. Keep both tabs the same size and synchronize native controls and lazy preset lists with Light/Dark/System appearance.

- Make preset and category selection checkboxes visible on macOS, keeping their drawing and click targets aligned after scrolling in both themes.

- Manage reusable presets in Settings with a project selector, collapsible type groups, group checkboxes, and confirmed bulk deletion. Preserve snapshots, projections, and presets still required by saved work; refresh preset menus without changing applied settings.

- Rename the main-window Help button to Settings and use the simplified EIDORA icon with light/dark palette tinting; preserve its menu and size.

- Paint the selected Settings appearance radio before applying the theme, and combine pending theme changes so the latest choice wins.

- Keep only preset actions in the Filters options menu and remove its citations dialog entry and report handler.

- Add Settings to Help and the startup chooser, with saved Light/Dark/System appearance, startup project choice, storage destinations, optional verified project relocation, and a scoped General reset.

- Separate Discovery snapshots from reusable presets. Snapshot saves and updates capture their own settings without generating presets; Figures preserve imported views until another snapshot is dropped onto the plot. Add per-component “From snapshot” resets and explicit preset application, restore saved slicer views, and resolve mapped and conditional appearance fields for Figures. Keep presets project-only, with “Apply preset…” and “Save as preset…” actions; remove Global Template options. “Manage Presets...” at the bottom of Apply preset menus opens Settings directly on the Presets tab.

- Prevent model-list refreshes from resizing deleted tree items while old row widgets await Qt cleanup.

- Open Vector Data Info tabs and cards without blocking the interface: remove whole-library scans, load details in the background and reuse them across tab switches.

- Let panel rows grow to fit native and styled buttons, sharing minimum header heights across Media Sources, Preview Assets, Models and Vector Data without fixed height caps.

- Keep panel row bindings working when Designer replaces decorative widget wrappers with nested layouts, including the Vector Data progress controls.

- Hide Preview Assets options while jobs are active, matching Vector Data; keep the menu available when paused or finished.

- Let active panel jobs use the full second line for progress and right-aligned controls, restoring info labels when idle. Use standard button heights and list-colored gaps between cards.

- Unify Media Sources, Preview Assets, Models and Vector Data with stable two-line headers, compact progress and direct row actions. Keep Models and Media Sources non-expandable, anchor expanded details below the header, and remove outer-row selection and hover highlights.

- Match Vector Data cards to Preview Assets using a full-width tree, consistent collapsed row height, keyboard expansion and elided names. Preserve live cards when their order changes.

- Move live embedding and PCA activity text into the Progress textbox, coalescing rapid updates. Collapsed Vector Data cards now show only the header and progress controls.

- Bind Preview Assets to the revised Model Run card design, with an inline expandable details table, visible job controls while collapsed, and height fitted to the Designer layout.

- Reuse the existing Vector Data card progress bar and Cancel button for PCA reduction, removing the duplicate reduction activity controls. Disable Pause while PCA is running.

- Compact projection memory to “Estimated memory: ~estimate / budget” beside the refresh icon. Use numeric controls, checkboxes and dropdowns for simple parameters, with free-text JSON for compound settings.

- Start PCA copies in their own destination card immediately, with source-import history, progress and cancellation. Preserve the card through publication and keep replacement jobs in the original card.

- Explain potential projection runtime with one relevant reason, such as all-pairs comparisons, iteration settings or selection size, while keeping memory notices separate.
- Simplify Preview Assets into compact expandable rows matching Media Sources height, with source context in details, live progress and adjacent Pause/Resume and Cancel controls, an options menu, and one settings/results section. Preserve expansion through publication and refresh; show actual generated counts and skip totals.

- Refresh projection estimates from an icon beside Memory budget and keep assessment messages brief, tentative and free of routine implementation details.
- Keep Advanced parameters expand/collapse working when the projection dialog uses a layout directly around its parameter table.

- Move Vector Data’s model, status and processed-item summary into the Progress activity log, removing the separate label above it.
- Move PCA progress and cancellation into Progress, retaining method, dimension changes, variance and duration in saved results. Simplify Vector Data Info to dataset facts and reduction summaries, with technical details collapsed by default.

- Redesign New Preview Assets to match Run Embeddings with a source heading, compact stacked controls and a separate action footer. Use media-specific default names for manual and automatic previews, including `(thumbs)` for images and `(frames)` for video stills.

- Create previews directly from Media Source rows with source-specific image/video settings and informational Text/Audio placeholders. Show Link on unlinked sources and replace it with Create Previews once linked; show jobs and outputs in Preview Assets and remove its Add New button.

- Present projection assessments with consistent body text, aligned estimated-memory and memory-budget rows and a fixed Create Projection button label.

- Assess projection inputs and parameters inline before running. Show memory estimates and runtime warnings, keep every method selectable, and block invalid settings or necessary allocations exceeding the current memory budget. Recheck in the worker before loading vectors.

- Add PCA and Incremental PCA to Vector Data’s PCA tab, with dimensions, vector-storage estimates, a copy-by-default output choice, editable PCA/PCAin result names for copies and replacements, safe original replacement, and saved variance-retention results.

- Add MDS with metric and non-metric modes and a separate t-SNE (openTSNE) projection with Barnes–Hut/FFT optimization, advanced controls and saved execution details. Check MDS memory requirements before allocating pairwise matrices.

- Add a right-aligned Reset button beside Advanced parameters in New Projection to restore the selected method's basic and advanced defaults.

- Match New Projection to the import dialogs, with each method's basic parameters visible and editable advanced parameters collapsed. Initialize both sections from library defaults and preserve edits when collapsing. Align PCA, t-SNE, PaCMAP and TriMAP execution defaults with their libraries, including automatic solver/learning-rate choices, unseeded execution and TriMAP's full default triplet/iteration settings.

- Offer UMAP 0.6 first and UMAP 0.5 directly below it in New Projection. Expose their complete library parameter sets and defaults, with advanced controls and explicit restrictions for unsupported inputs/outputs. UMAP 0.5 uses the 0.6 library's compatibility pipeline; saved projection coordinates remain unchanged.

- Resolve animation Slicer frames through Discovery’s field and relationship catalog so linked-table fields produce distinct frame selections.

- Add Create animation… to the Slicer menu to capture the current Discovery view and generate a new Figures animation from the current Slicer settings.

- Let snapshot preview backgrounds follow light/dark mode when the snapshot uses the default canvas background, including existing MET_Example previews.

- Align Simple and Advanced filter footer controls and expose the shared hide-tooltip setting beside Apply Query.

- Match Transform play-button spacing to Filters.

- Enlarge Appearance palette icons within their existing buttons.

- Quiet routine footer messages for workspace navigation, selections, and canvas controls; retain progress, warnings, errors, and operation results.

- Resolve Advanced filter metadata by stable field identity and remove the metadata-loading retry button.

- Activate the Discovery Slicer when users operate its slider, navigation buttons, or range controls, while preserving inactive saved settings.

- Clicking empty space in the Discovery snapshot list deselects the snapshot and disables update/overwrite actions; list refreshes preserve deselection.

- Size Metadata tabs with standard Qt tab icons and wider padding so long labels and link icons stay inside the tab.

- Keep the current Discovery canvas view when a projection finishes; select the new projection from the list to display it.

- Compact the Advanced filter operators into one combobox with an insert operator button, aligned with field and value insertion.

- Correct the failed execution message to reference Error details without assuming their position in the report.

- Show focus outlines only after keyboard navigation and hide them on mouse click.

- Use Preview Assets terminology across the app, including Discovery settings, tooltips, and job progress.

- Rename the Assets workspace to Media and update the tour to cover Media Sources and Preview Assets.

- Move the project chooser's example project, storage, and diagnostics actions into More Options.

- Keep clustering status and result lists responsive while groups are being saved. Source validation now reads only the item identities needed to verify the embeddings.

- Metadata table deletion now tolerates unrelated Discovery appearance autosaves. Confirmation still checks for new dependencies and edits to data scheduled for removal.

- Closing a metadata import or update releases its dialog and selection popups, preventing widgets from accumulating during repeated imports.

- Discovery returns to points when the active thumbnail set is deleted from Media.

- Open the bundled starter and older version 5 media databases with their existing asset links and saved matching choices intact.

- Mac builds use upstream Qt fixes for accessibility elements retained after list and table changes. Discovery reuses its palette menus, and deleting Figures releases their tab close buttons.

- Release deleted Figures controllers and the Python wrappers for removed pages, media rows, filter rules and vector cards during long editing sessions.

- Release the embedding import dialog and its four selection popups after each import or cancellation, reducing memory use during repeated imports.

- Fix a native crash when refreshing Discovery metadata fields, snapshots, and other lists during project switches and repeated imports.

- Keep GUI-thread garbage collection progressing after short-lived allocation bursts and during idle periods, including in packaged GUI self-tests. Explicitly delete disposed project-chooser widget trees so row callbacks cannot retain closed dialogs and their controllers.

- Schedule cyclic garbage collection on the GUI thread so background allocations cannot finalize abandoned Qt widget cycles during event dispatch. Retire Vector Data card controllers with their widgets on project switches instead of retaining them in the persistent list.

- Make failed metadata choices explicitly retryable without losing saved settings; keep pending numeric filters active, missing fields inactive, and advanced-query values tied to the selected field. Track Model Zoo card and size completion separately. Drain final Figure saves after renderer-stop errors, reject new preparations and saves after retirement, and keep long exports independent of the interactive preview deadline. Start render processes without holding the GUI-facing lifetime lock and close pipe endpoints when startup fails.

- Load Discovery metadata choices and numeric ranges in bounded background queues; preserve saved colors, filters and slicer settings while loading, and discard obsolete control/project results.

- Cancel obsolete or stalled Figure renderer exchanges, recover with a fresh worker, and retire Figures services without blocking the GUI. Preserve final-save ordering when returning to a project and discard cancelled animation-export staging.

- Bound Model Zoo catalog, card and size requests independently, keeping the latest selection responsive without accumulating network threads or delaying cards behind size lookups.

- Preserve saved Figures after unreadable or invalid collection files: report load failures, keep autosave disabled until restoration succeeds, and allow retry without restarting.

- Capture snapshot state before background writes and reject stale UI activation; cancel Figure callbacks with their widgets and reuse the snapshot-save dialog. Retry unfinished thumbnail sources across viewport changes, bound source lookup queues and caches, and account for metadata summaries in the shared cache budget without retaining failed reads or unbounded panel copies.

- Give projection jobs unique output identities so cancellation cannot delete another writer's result. Preserve committed imports through cleanup errors, keep coordinate read failures visible without replacing the current view, and finish jobs whose saved projection disappears before activation.

- Handle re-entrant background progress delivery and release superseded Discovery task inputs immediately; end item sessions when reloading a different projection and keep temporarily locked recent projects retryable.

- Keep Discovery computation tasks free of Qt objects, collect results on the GUI thread, and retain only active and latest queued work. Give projection and clustering-initialization runners GUI-owned lifetimes and reject initialization callbacks from previous projects.

- Keep projection children tied to the application lifetime, report actual computation stages, reserve default CPU capacity for interaction, and prepare newly saved coordinates in the background before display. Preserve the pinned UMAP version for this reliability change.

- Cancel deferred Appearance and Vector Data layout callbacks when their widgets are destroyed.

- Finish Discovery pan gestures independently of tool and Space-key release order, restore pending appearance updates, and discard abandoned gestures on focus loss, mouse-grab loss, hiding, and closure without re-uploading point positions.

- Dispose item/nearest-neighbor dialogs and their callbacks when a session ends, restore selection and blinking on workspace/project changes, and preserve the open window after saving a group. Limit neighbor searches to one running task and the latest pending request; cancelled requests skip obsolete loading and cannot publish into another session.

- Protect saved vectors from accidental model execution, reconfiguration, reset, cancellation, and startup cleanup, including legacy runs whose saved status is inconsistent. Dismissing clustering menus no longer cancels finished jobs.

- Report committed embedding imports as saved even if derived metadata or display refresh fails. Close import iterators and mapped files before cleanup, preserve cards after read failures, and keep locked projects in Recent Projects instead of treating storage errors as missing data.

- Simplify footer activity feedback to a single-line process description and adjacent progress bar, removing the redundant activity-summary box.

- Rename static and animation tabs through their right-click menu or by double-clicking the tab label. Save names automatically and use them for export filename suggestions.

- Treat static and animation tabs as the saved figure collection. The × button now confirms permanent deletion with Cancel selected by default; remaining figures continue to autosave and reopen with the project.

- Preserve legacy animation dimensions after loading the selected frame and keep gaps in saved plot grids blank. Refresh local legend changes in partial previews and update grid controls after adjacent plot insertion without discarding pending edits.

- Start new Journal figures at 1600 × 1200 px, Presentation figures at 1920 × 1080 px, and Animation figures at 800 × 600 px, with coordinated margins, spacing and text sizes. Preserve snapshot point sizing and all existing saved settings, including legacy fallback values.

- Use pixels for all figure dimensions, spacing, fonts and line widths, with no DPI or unit selector. Show actual output measurements as the layout changes. Apply exact output sizes to one plot or all plots, show mixed values explicitly, and preserve full precision through resizing, saved figures, presets and animation exports.

- Make figure width and height the exact final export dimensions, reserve empty grid cells, and scale each plot proportionally with its annotations. Preserve saved figures through measured layout conversion, keep shared text at its chosen size, and retain plots when expanding the grid above or to the left.

- Prepare and render all animation timeline thumbnails in the shared background worker, keeping the GUI responsive for large snapshot and slicer frames. Publish only current completed images, preserve previous thumbnails on cancellation or failure, and retain full-resolution appearance and export quality.

- Replace cached dataset identities when loading snapshot data so direct animation-frame exports cannot reuse another frame's axis bounds.

- Simplify the thumbnail footer to “Creating Preview Assets” with the same progress state as the Media workspace.

- Prioritize visible figure previews across the workspace, supersede queued stale draws, and deliver completion directly through the GUI event loop. Hidden previews still publish durable datasets, and exports retain their full-quality rendering and reliable queue order.

- Fix stalled thumbnail input resolution on large collections with indexed, cancellable lookups; show live thumbnail preparation/progress in the footer, clear stale activity labels, finish footer cancellation for queued jobs, and omit video frame settings from image jobs.

- Speed up figure tab switching with losslessly compressed point-layer caches inside the existing memory budget. Reuse exact rendered pixels across more tabs while preserving full-resolution previews, overlapping annotations and vector exports.

- Update figure controls only when their geometry or visible preset selection changes, and share transformation and decoded-animation cache budgets across tabs. Preserve full-resolution previews, document data and export quality.

- Close Run Model immediately and prepare inputs in the embedding card with pause/cancel support. After successful embedding completion, run optional default thumbnails as a separate Media job with aspect-preserving previews, tracked in the footer without changing workspaces.

- Simplify the batch selector by removing its explanatory paragraph; Auto now prefers faster batches when the throughput gap exceeds 2%, matching High's tolerance.

- Redraw only affected figure-preview regions on the retained canvas, including overlapping plot annotations, with full redraws for changed layouts and unsupported cases. Preserve exact preview appearance and full-quality exports without another page-sized image cache.

- Offer Auto, High, Low and fixed batches of 4, 8, 16, 32 or 64 for embedding runs. Tune High through larger CoreML batches, retain mild thermal throughput, remove fixed per-batch waits, and reduce soft memory pressure gradually while keeping critical safety pauses and recovery.

- Transfer only changed figure-preview pixel regions with explicit frame validation, and cap inactive-tab preview images at a shared 64 MiB while regenerating evicted views on activation. Preserve full-resolution appearance and vector export quality.

- Recover embedding batch sizes gradually after temporary resource pressure clears, preserving measured preferences and resuming interrupted Auto comparisons without weakening memory, thermal, disk or allocation-failure limits. Report temporary reductions and recovery with resource readings.

- Reuse the preview Figure, canvas and unchanged plot Axes between figure edits, rebuilding changed plots and invalidating changed layouts while preserving raster-cache budgets, preview pixels and full-quality vector exports.

- Prepare figure metadata directly as immutable encoded columns, reuse those arrays for persistence, and load metadata once after frame transformations select their final points; preserve frozen snapshot appearance and full-quality exports.

- Bound figure preview-image transfers and assemble immutable pixels once, preventing repeated full-page receive-buffer growth in the GUI process while preserving preview pixels and export quality.

- Guard embedding runs with memory, available thermal and disk checks; compare Auto batches of 8, 16 and 32 over longer measurement windows, retaining measured speeds and selection reasons in the run report. Support fixed CoreML batches up to 32 and checkpoint at 4,096 items or 10 seconds. Preserve readable reasons for resumable safety pauses.

- Reclaim retired Matplotlib figures and canvas buffers after each render-worker reply, including failures and exports, while preserving reusable plot caches and full-quality output.

- Move figure transformation preparation, Original-coordinate restoration and transformed animation frames off the GUI thread; use bounded coordinate batches and pin lookups, cancel obsolete work, and preserve preview/export quality.

- Default embedding runs to automatic batch tuning with a saved manual override in Advanced settings & parameters; show live batch size, counts and speed directly above the progress bar.

- Keep embedding inference batches independent of checkpoint saves, throttle live reporting, and use compact status labels with an incremental bounded Vector Data activity log.

- Match Discovery item previews to the selected Items row, using readable column names and the same linked values; keep values visible beside long field names.

- Present Run Model as a focused form with a model heading, stacked Media Source/output/device fields, source counts and paths, and collapsed “Advanced settings & parameters”.

- Show linked/total media counts beside source status icons combined with the media type; tighten Metadata tabs while retaining complete scrolling labels.

- Place media-source and Metadata-tab status icons after their labels; show a red question mark for unlinked media and retain linked-column tooltips.

- Show a fixed-width disabled “linked” label for linked media sources, show their link icon beside the name, and offer Relink... in source options.

- Simplify local embedding runs to registered file-based Media Sources and one Run Model action, with automatic background validation and collapsed advanced settings.

- Standardize Media Sources, Models, and Vector Data options on Snapshot-style 22-pixel toolbuttons, with matching Link... and Run... actions; use standard pushbutton heights and 6-pixel gaps.

- Match Media Sources action buttons to Models with standard pushbuttons and a 4-pixel gap; label model options “...”.

- Reduce figure worker memory with lazy encoded metadata columns, shared category mapping, and raw preview-image transfer without redundant full-page copies. Preserve existing preview pixels and export quality.

- Compact Media Sources to two-line rows, with source details above the path and Link/status beside the top-right options button.

- Show vector counts and saved embedding file sizes on Embeddings cards, with exact byte counts in tooltips.

- Reuse cached figure plots before loading their datasets, and share one decoded dataset across repeated subplots even when it exceeds the cache budget.

- Put source actions in a row options button, move Link to a natural-size left control with a smaller linked icon and Items-column tooltip, and show linked counts beside the media type.

- Limit item previews to a shared common-format contract, including MPEG, and require packaged playback audits on every release target. Check WAV variants, default/software video decoding, source switching and per-case diagnostics without adding runtime libraries; distinguish decoding success from audible-device QA.

- Consolidate media matching into Link… with an explicit replacement checkbox and added, kept, and removed link counts.

- Speed up metadata inspection with bounded batches, compact temporary tables and deferred indexes; show provisional samples, reuse completed type checks, and label database progress “Writing rows”. Full validation and transactional safeguards remain required.

- Load audio metadata when selecting an item preview so duration and seeking are available before playback starts.

- Match media folders against multiple metadata columns and filename lists, allowing several assets per Item with saved column selections and complete Source previews.

- Label the Media Sources linking pushbutton and context-menu action “Link...” to match the Metadata workflow.

- Index ordinary media folders with one inspection stage, reuse captured file statistics, and avoid repeated per-file path resolution while retaining transactional publication.

- Give Media Sources full-width rows with separate progress, show cancellable folder/archive inspection immediately, and prevent duplicate rows when switching projects.

- Resolve linked metadata in Items and Discovery regardless of matching direction; show readable mapped-size fields, load complete previews in bounded background pages, and reuse compact metadata columns and saved field summaries for faster project opening.

- Identify sessions interrupted by force quit even without a crash trace, and show their saved diagnostics before the new session after restarting.

- Enforce a 10 MiB budget for automatic diagnostics, with oldest-inactive-session cleanup, byte-exact log rotation, and bounded crash traces that cannot grow after a freeze or crash.

- Give Discovery item previews dedicated image, video, audio, and read-only text views; limit the source selector to supported file formats and resolve JSONL text to the linked record.

- Simplify the Help pop-up: place About EIDORA first, separate the remaining help options, and keep update checks in About instead of a duplicate pop-up action.

- Add a media source selector to Discovery’s individual item dialog, using Media Sources names and allowing image, video, and audio previews across directly linked and inherited media.

- Keep large figure editing responsive with cached subplot point layers, background previews/loading/export, lazy tab restoration, and compact shared figure datasets. Preserve full-resolution previews and vector export quality; migrate large saved figures without blocking the GUI.

- Add Help → Diagnostics... with editable reports for manual submission to mail@eidora.org, bounded session logs, crash/freeze stack capture, and recovery guidance for common storage and access failures. Keep sharing information above the report and put the user's problem description before technical details.

- Enable Vector Data’s Run Again action only for successful model runs; imported vectors cannot be rerun, retried, or resumed as model jobs.

- Require unique embedding names when adding or importing outputs, ignoring case and surrounding whitespace. Show duplicate or empty import-name errors inline before publication.

- Refresh Tooltip and Pin metadata fields immediately after clustering groups are published, alongside Appearance, Filters, and Slicer, while preserving selected fields.

- Build Discovery's Random projection from every Items row, without requiring media links. Added Items receive new dots while existing dots retain their positions.

- Import embeddings directly into Items, automatically reusing matching identities and creating missing rows. Align the dialog with Metadata Import using a preview-first layout, collapsed Import options, inline errors, and Cancel/Import actions.

- Simplify Metadata controls to Add Files and source-only Link. Items offers Unlink all, Delete all, and Export; remove explicit source-replacement controls.

- Offer metadata linking only when Items exist, restrict automatic and manual links to Items–source pairs, and add Unlink for a source’s saved relationships without deleting records.

- Move the active project name and project actions into the main window, with Switching Projects first. Project storage and bundle formats remain unchanged.

- Align Media Source matching with the Metadata Link dialog: suggest settings from actual matches, preview source and item values together, and keep manual adjustments in collapsed Link options.

- Show metadata import column, file-reading, and applicable matching settings directly inside the collapsed Import options panel without tabs or an extra enclosing box.

- Keep registered media available without creating items or requiring matching. Match existing Items through linked metadata, validate embedding inputs and item reuse automatically before execution, and create items only when vectors publish successfully.
- Resolve thumbnails to canonical items and relocate registered media without changing asset identities or saved embedding provenance.

- Match the metadata import header to Link tables with a prominent filename, file details, and left-aligned preview headers; remove redundant preview and result headings while retaining the sample row count.

- Simplify Link tables to a compact match headline, source-to-target columns, and labeled preview filter; retain the displayed-row count, use left-aligned column (table) headers, and show existing-link status on the disabled action button.

- Show source and target matching values side by side in Link tables, emphasize source coverage, and filter unmatched or multiple-match records across the complete collection.

- Simplify Link tables around the automatically suggested match and record preview. Keep manual columns and multiple-match approval in collapsed Link options, with a retry action and clear guidance when detection finds no match.

- Show source-tab link status with a chain icon for linked tables and a red broken chain for unlinked tables; retain source paths in status tooltips.

- Flatten nested metadata properties into fields and retain value lists and empty entries. Reject nested arrays of objects with a short, field-specific error inside the import dialog and restructuring guidance; existing saved lists remain readable. Discovery filters match list entries; coloring uses unambiguous values and leaves conflicting values unmapped. JSON exports preserve arrays.

- Fix metadata import Include checkboxes painting at the top of the table on macOS; give each column its own checkbox that scrolls with its row.

- Reserve consecutive numbering for items: source metadata uses a separate internal index counter and qualified record references, so imports and replacements cannot create gaps in future item numbers.

- Center metadata imports on a data preview, with column settings, file reading, and matching in collapsed Import options. Preview all selected columns and keep matching and replacement outcomes visible.

- Separate active item membership from source records. Source tabs hide internal indexes and inherited fields; vector imports and successful model runs publish explicit item bindings.
- Resolve all approved linked values with provenance for Items and Discovery. Add explicit multiple-match approval and a manual record picker; use any/no-value filtering and unambiguous scalar mappings.

- Track imported metadata by source path and content instead of tab name. Unchanged reimports focus the existing tab; changed sources require replacement preview, and tab tooltips show source paths.

- Remove retired automatic-linking and metadata-preview helpers, simplify embedding item creation, and share column-label rules. Fix overview exports to use visible fields and labels, and avoid duplicate physical-field reads.

- Distinguish the fixed Items tab with bold text and a small star beside italic imported metadata tabs; underline and subtly shade the selected tab. Tighten icon-to-label spacing and use a connected chain or question mark for link status.

- Replace manual Metadata row/column creation with a source-replacement preview that retains matched IDs and links, blocks broken dependencies, and disables Link when no additional source tabs exist.

- Embedding imports retain the supplied label field once instead of adding embedding_identifier; unlabelled vectors create items without a generated label column.

- Hide equivalent numbered metadata copies in Items and Discovery, preserve columns whose aligned values differ, and expose only the canonical item ID in Discovery.

- Consolidate fully matching approved link keys under the Items label; automatically number remaining duplicate field labels and allow unique overview label edits without changing source headers.

- Linked metadata uses plain field labels in Items and Discovery; spreadsheet headers show a link icon with the source tab in a tooltip instead of extra relationship ID columns.

- Metadata imports now open in separate filename tabs, leaving the initial Items tab empty with guidance to import or generate embeddings.

### Changed

- Keep the first Metadata tab named Items and open a compact linking dialog after import, preselecting the strongest exact match with a live background preview.

- Metadata imports suggest relationships without applying them; a visible Link… action opens review for the selected table.

- Link metadata tables automatically through unique filename or named-key matches in either import order, and provide **Link tables…** with an exact-match preview for explicit column matching.
- Show readable related-field names in Discovery tooltip and pin selectors and rendered text while retaining stable saved field identities.

- Import multiple embedding files with detected vector/identifier fields, flexible numeric encodings, retained descriptions, and exact matching that keeps unmatched vectors.
- Organize metadata in named table tabs with shared-source and one-hop record relationships, dependency-aware table removal, and three main actions while retaining all search controls.

- Use the available height for Mapped and Custom Fill Color tables in Discovery Appearance.

### Fixed

- Resolve Discovery metadata through vector-item → face → photo links, expose each table field once, and preserve saved selections without duplicate empty relationship fields. Reuse matching relationships across table, key, and relationship-name changes.

- Complete embedding imports before reporting success and immediately refresh Vector Data; probe files in background workers and coalesce progress updates.

- Limit thumbnail progress updates so large collections do not flood the interface during automatic or manual thumbnail creation.

## 0.2.7 — Prepared

### Changed

- Size Figures map and guideline grids in millimetres, with square cells and a 25% minimum visible fraction for the last cell.
- Open Canvas Layers settings with a dedicated arrow beside each visibility checkbox, and close them when clicking outside the popover.

### Fixed

- Avoid repainting the Layers bar on the first mouse-opened settings popover.
- Give Figures legends separate side and alignment controls, and size the layout to fit rendered labels.
- Keep the startup project chooser visible with an opening indicator until the main window is ready.
- Draw checkboxes on every row in the Canvas Layers settings for Pins, Tooltips, and Marked Areas, including after scrolling.

## 0.2.6 — 2026-11-03

### Changed

- Make ONNX Runtime's CUDA provider and NVIDIA libraries an optional, verified download on Windows and Linux
  instead of including them in every installer. Offer GPU setup only after detecting
  a compatible NVIDIA device, with download progress, cancellation, retry, and an
  isolated inference check. CPU execution works without downloading GPU support;
  macOS CoreML behavior is unchanged.

### Fixed

- Use patched ONNX 1.22.0 for macOS model compatibility processing.

- Prevent CoreML dynamic execution-plan failures and native compiler crashes
  with validated single-item batch specialization and static-shape CoreML partitions,
  retaining strict output validation and CPU execution for unsupported graph sections.
- Show Model Zoo download sizes from Hugging Face file metadata, counting the
  installed package files. Keep model cards responsive while sizes load and
  allow unavailable sizes to retry.
- Use CoreML's MLProgram format so normalized embedding outputs retain sufficient
  precision to pass validation instead of unnecessarily restarting on CPU.
  Handle its missing-padding compiler error for models such as VGG16 using a
  temporary equivalent graph, preserving downloaded packages and checksums.
- Exercise actual CoreML inference in Apple Silicon release self-tests and
  retain CPU execution on Intel Macs.

## 0.2.5 — 2026-11-02

Published for macOS Apple Silicon and Intel, Linux x64, and Windows x64
through Microsoft Store (package 1.2.5.0).
This patch preserves the existing project format.

Prototype software. Use at your own risk. Stability and backward compatibility are not guaranteed.

### Fixed

- Re-enable the welcome tour after app updates or replacing an installation,
  including reinstalls of the same version.
- Use Qt 6.11.2 on macOS to fix native cursor crashes when refreshing models and
  starting runs on macOS 27.2 (QTBUG-147602).
- Coalesce model download progress and update progress bars without repeatedly
  rescanning the Model Zoo's installed catalog.
- Open Microsoft Store's updates page from Windows Help/About update actions;
  keep website update checks on macOS and Linux.
- Show the release version in website update results instead of the date and channel label.

## 0.2.4 — 2026-11-01

Experimental release for macOS Apple Silicon and Intel, and Linux x64.
Windows Store x64 is built separately for validation and Store submission.
This patch preserves the existing project format and dependency pins.

Prototype software. Use at your own risk. Stability and backward compatibility are not guaranteed.

### Fixed

- Refine the alignment and spacing of Discovery filter and snapshot footer controls.
- Correct macOS signing permissions for UMAP's Numba/LLVM runtime, and exercise a real UMAP projection in packaged release checks.
- Wait for Discovery cancellation to finish before releasing its Qt thread during shutdown.

- Default to creating thumbnails after embedding whenever none exist, including after earlier runs, and support video sources in automatic thumbnail creation.
- Keep automatic thumbnails at the source aspect ratio independently of model preprocessing, so model resize settings cannot prevent thumbnail creation.
- Correct upside-down portrait video thumbnails caused by reversed display-matrix rotation.

- Start the workspace tour correctly on Windows when the hidden overlay's native position has not caught up with its assigned geometry.

## 0.2.3 — 2026-10-30

Experimental release for macOS Apple Silicon and Intel, and Linux x64.
Windows Store x64 is built separately for validation and Store submission.
This patch improves first launch without changing the project format.

Prototype software. Use at your own risk. Stability and backward compatibility are not guaranteed.

### Fixed

- Install the already-audited MET_Example after archive checksum and safe extraction checks, without repeating the detailed content audit on first launch.
- Render semantic widget icons directly from bundled SVGs so their pixmaps do not depend on the SVG icon-engine plugin.

## 0.2.2 — 2026-10-29

Experimental release for macOS Apple Silicon and Intel, and Linux x64.
Windows is excluded from this publication while validation continues.
This patch improves existing workflows and bundles the updated MET example;
it does not introduce project-format changes.

Prototype software. Use at your own risk. Stability and backward compatibility are not guaranteed.

### Added

- Bundle MET_Example v2 with reduced, citation-bearing ResNet50 and DINO vectors,
  saved SVD transforms, projections, snapshots, filters, and figure settings.
  Preserve historical cluster groups with an explicit provenance note.

- Add short Designer tooltips for non-obvious controls across the application,
  including appearance, filters, media linking, vector exchange, and figures.
  Omit redundant hints where visible labels already explain the control.
  Workspace navigation only shows hints when a workspace is unavailable.

- Add keyboard and context-menu access to Discovery layer settings, accessible
  names and tab order for canvas controls, and solid button focus borders shown
  only during keyboard navigation in both themes. Mouse clicks hide the extra
  focus cue without changing hover or selection styling.

- Navigate the workspace tour with Tab or arrow keys on all platforms; Shift+Tab goes back.

### Fixed

- Read bundled starter settings as UTF-8 on Windows and report starter-install
  failures directly in packaged self-tests instead of waiting on an error dialog.

- Resolve saved static-figure snapshot and preset references relative to their
  project so filters and other controls remain available after relocation.

- Apply Discovery’s shared filter rules in Figures so saved numeric ranges fade
  only excluded points; preserve disabled filters, match-any rules, and advanced queries.

- Keep keyboard focus frames owned by their original windows so closing thumbnail
  or clustering dialogs cannot invalidate workspace controls or Discovery settings.
- Finish clustering with GUI-owned thread cleanup, preventing worker teardown
  hangs and crashes after computation. Limit Discovery's global key filter to
  live-workspace keyboard events.

- Let Tab reach each enabled workspace button and leave the metadata and media
  sources tables. Show a keyboard-only focus outline on fields, lists, checkboxes,
  and the canvas as well as buttons. Keep Discovery's Space-to-pan handler from
  consuming keyboard activation on other controls or hidden workspaces.

- Refresh Discovery's Random view after linking media to existing metadata, so linked items appear without reopening the project or restarting the app.

- Default the Discovery canvas to gray (#7D7D7E) in dark mode and white in light mode, preserving custom background colors across theme changes and restoring the background choice with projects and snapshots.

- Open projects and validate starter collections on Windows UNC paths, including Parallels shared folders, without SQLite URI authority errors.

- Accept macOS arrow-key events in workspace-tour navigation, including Qt's keypad flag.

- Restore readable Linux dark-mode text, icons, inputs, and item views when the desktop reports a dark preference with a light Qt palette; retain Linux platform-theme plugins in packaged builds without changing macOS theme handling.
- Align Discovery filter and slicer bottom insets with the existing panel spacing across native widget styles.
- Keep the Linux X11 workspace-tour overlay aligned with the application beside desktop docks, while preserving macOS transient-window behavior.

## 0.2.1 — 2026-10-27

This Linux-only patch adds developer attribution and package metadata.
It does not introduce project-format changes. Published as [v0.2.1](https://github.com/eidora-desktop/EIDORA/releases/tag/v0.2.1)
from `443fde7bdc009e507f0e738565103c96d6f34726`. Mac downloads remain at 0.2.0;
this release contains no Windows build.

Prototype software. Use at your own risk. Stability and backward compatibility are not guaranteed.

### Fixed

- Identify Tillmann Ohm as the Linux package maintainer and developer, include
  the EIDORA homepage and contact address, and ship AppStream metadata in both
  DEB and AppImage packages.

## 0.2.0 — 2026-10-27

This minor prototype release adds Methods and Citations reporting, a guided
workspace tour, storage management, and platform-specific update checks.

Prototype software. Use at your own risk. Stability and backward compatibility are not guaranteed.

ONNX model packages require BibTeX; packages without it must be repackaged.
Preparation contract implementation 2 requires unfinished ONNX runs to be
prepared again; completed embedding datasets and existing saved runs remain
readable. Existing workspaces stay in place unless explicitly relocated.
Published as [v0.2.0](https://github.com/eidora-desktop/EIDORA/releases/tag/v0.2.0)
for macOS and Linux. Windows Store packaging was prepared separately.

### Added

- Add Help → Take a Tour… with a spotlight walkthrough of the five workspaces
  and their panels. Explanations sit beside red highlights, with Back, Next,
  and Finish controls. Empty workspaces are temporarily available during the
  tour, and the MET example opens it once for new users. Titles and descriptions
  can be edited in `src/MainWindow/tour_text.toml` and reload when the tour opens.
  Discovery starts with Canvas, then Projections; Figures starts with its canvas,
  followed by Snapshots and Figure Settings.
- Support optional ONNX `preparation.resize_rounding: truncate` for image and
  video models that discard fractional resize dimensions. Omitted settings keep
  the existing `round` behavior; preparation contract implementation 2 documents
  and validates both choices. Existing unfinished ONNX runs require re-preparation;
  completed embedding datasets remain unchanged.
- Add Storage controls for inspecting disk usage, copying and verifying project
  and model folders, and removing downloaded models and disposable caches.
- Show project destinations during creation and allow choosing another folder.
  New installed workspaces default to Documents/EIDORA/Projects; existing
  workspaces remain in place until explicitly relocated.
- Make MET example creation optional in the project chooser and preserve edited
  examples when creating another copy.
- Connect Check for Updates to eidora.org's platform-specific release catalog,
  including recommended prototype releases, release notes, and website downloads.
  Windows installation and updates remain managed by Microsoft Store.

### Changed

- Separate unsigned contributor builds from private official signing and publication; use MSIX for official Windows candidates, document contributor rules, and keep maintainer tests and agent instructions private.

- Show “Copied ✓” for two seconds on methods-report copy buttons after copying to the clipboard.

- Use concise, consistent menu labels for data import/export, renaming, deletion, preset and snapshot saving, and clustering removal choices.

- Shorten methods-report menu actions to “Citations…” and improve dialog readability with spaced section headings and padded tables.

- Require model BibTeX in ONNX package configs, validate it before execution, and preserve it in embedding artifacts for citations and figure captions. Older packages without config BibTeX require repackaging; saved runs remain readable.

- Unify methods, parameter settings, citations, caption text, and Markdown/BibTeX export across Discovery snapshots, Filters, Transforms, embedding runs, clustering results, and Figures snapshots; freeze new snapshot reports and offer snapshot methods in figure caption editors.

- Add paper-ready Methods and Citations reports for newly created projections, with immutable model, algorithm, parameter, version, and repeatability provenance plus Markdown and BibTeX export.
- Expand active embedding run cards onto a copyable progress report, preserve diagnostics when execution fails, and return successful runs to their Info tab.
- Present structured embedding details as expandable Info-tab child rows, including grouped execution and preprocessing settings.

### Fixed

- Correct Linux installed-size metadata by counting symbolic links themselves
  instead of counting their library targets again, and include the full payload.
- Derive Microsoft Store package versions from the EIDORA release, adding one
  only to the major component (`0.2.0` → `1.2.0.0`). Keep installer filenames
  and Windows executable version properties on the actual application version.
- Restore automatic MET example creation and opening on first packaged launch,
  followed by the welcome tour. Keep release self-tests from consuming the
  user's onboarding preference.
- Update Pillow, certifi, and filelock to patched releases for image-processing,
  certificate-trust, and file-locking vulnerabilities. Require patched minimum
  versions for source installations as well as pinned application bundles.
- Avoid the first Figures plot freezing the UI during system font discovery.
  Load Matplotlib's bundled fonts and directly locate a standard-font wishlist;
  show only available families while preserving missing saved font choices.
- Prevent the main window from blinking during project startup when Discovery
  initializes its Qt Quick widget host.
- Keep Discovery hover tooltips compact with a 360 px text-width limit, word
  wrapping for long values and identifiers, and a 300-character preview per value.
- Give the About dialog update area more room and keep its links compact.
- Retain the Qt X11 platform library and its OpenGL integration plugins in Linux
  bundles so the packaged application can create and render its window; remove
  aliases for excluded Qt libraries.
- Install and validate the complete Linux audio and X11 runtime before bundling,
  and exercise the packaged Linux application under an X11 display.
- Preserve checksum-pinned icon source bytes on Windows checkout and align the
  Linux bundle limit with its verified scientific runtime dependencies.
- Restore saved custom tooltip fields after reopening a project.
- Keep the Model Zoo install button tied to the selected model when another model's card finishes loading.
- Restore deterministic starter-settings archive preparation without modifying an existing collection release.
- Keep the Local Models Actions column wide enough to show the full Run and More button labels.
- Restrict CoreML embedding execution to native Apple Silicon on macOS; Intel Macs use CPU only, including Auto and previously saved accelerator choices.
- Match Figures snapshot row heights and label rendering to Discovery for aspect-preserving previews.
- Package a native layered macOS icon so Tahoe displays EIDORA as one teal tile instead of placing the existing icon inside a silver surround; preserve the bundle icon when the installed app starts, using the original teal gradient and highlights in both default and dark appearances.
- Exclude top-level aliases of the unused PyArrow Flight library too, avoiding dangling bundle links that fail macOS signature verification.
- Prevent failed build-diagnostics uploads from blocking installer delivery in GitHub Actions; installer and Store artifact uploads remain required.

### Documentation

- Establish application versioning rules and release tracking from the `0.1.0` prototype baseline.
- Continue releases without a fixed final version; review the next release's
  contents before choosing `0.1.1` for compatible fixes or `0.2.0` for new
  features or breaking changes.

## 0.1.0 — Prototype baseline

- Current prototype baseline confirmed by the maintainer. This entry records the starting point for release tracking, not a verified publication date or a reconstructed history of earlier changes.
