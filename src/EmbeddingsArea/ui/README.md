# Embedding forms

`embeddings_area.ui` hosts Models on the left and Vector Data on the right.
`vector_data_panel.ui` uses the same single-column, hidden-header tree structure
as Preview Assets. Full-width cards use the shared two-line header: name, status
and ordinary actions above a model/dimension summary. Active jobs replace that
summary with a full-width progress bar and right-aligned Pause/Cancel controls.
Idle rows show their summary; paused jobs retain progress with Resume/Cancel. Header
minimum height is independent of job state and grows with the largest natural
control height across the four panel types. Rows and buttons have no fixed height cap.
A six-pixel gap outside the colored card shows the list background. Expansion
adds tabs below the header; sorting preserves live card widgets.
`model_row.ui` gives Models the same non-expandable design as Media Sources,
including input types, size, install progress, Run and options controls.
All four outer panel lists disable row selection and hover highlighting. Row
buttons act on explicit identities, and disclosure buttons support keyboard use.
`embedding_run_row.ui` owns the expandable Vector Data card with Progress, Info,
PCA and Clusters tabs. Progress stays in the header when details are collapsed.
Progress owns a read-only
`QPlainTextEdit` activity log containing the model, status and processed-item summary;
controllers append changed summaries and stage events without replacing
the document for each progress update. Live batch counts, speeds, resource status
and PCA activity are coalesced into the textbox at most once per second between
stage messages, preserving selection and scroll position. Hidden reports defer
rendering until reopened. PCA settings stay in PCA, and
the Progress report opens when reduction starts. Reduction reuses the card's
existing progress bar and Cancel button, including when details
are collapsed. Pause is disabled for PCA because it cannot resume a partial fit.
Info displays the compact dataset facts and reduction summary, with Technical
details collapsed by default. Its first visit loads details on a plain-data
worker while the tab opens immediately; later visits reuse the loaded tree.
Panel refreshes invalidate the details, but hidden tabs defer reads until opened.
Info uses the card's captured run/dataset/model and reads only its selected source
names, never media-library counts or the full dataset catalog. Results belong to
one card revision/session; obsolete results are discarded and failures can retry
on the next visit. Unchanged values preserve the tree and expanded sections.
`model_execution_dialog.ui` configures one run for the model
selected in the Models panel; it does not provide a second model selector.
It offers registered file-based Media Sources and a single Run Model action.
The focused form owns the model heading, horizontal source/output/device rows
with colon-suffixed labels beside their controls,
Preview Assets creation option, and footer in Designer. The Designer-owned parameter table uses alternating rows, hidden headers,
and label/editor columns for Batch performance and model preparation options in
the collapsed Advanced settings & parameters panel. Controllers populate the
editors from model descriptors; the preparation summary wraps above the table. Batch choices are Auto, High, Low, 4, 8, 16, 32 and 64. The first three tune
adaptively; numbers request fixed sizes. Auto is the default. Existing saved
sizes outside this list remain available when reopening that run.
The selector tooltip explains speed/headroom tradeoffs and model/resource limits.
Progress records measured speeds and the selection reason; completed runs retain
that explanation when reopened. Live status names the policy, actual batch,
provider, resource limits and limited monitoring; saved safety pauses show their
reason in the activity log and status tooltip.
Run Model closes after saving configuration; input preparation and its errors
appear in the embedding card. Optional Preview Assets creation runs separately in Media.
`new_clusters_dialog.ui` follows the Preview Assets dialog: a heading and secondary
description, aligned Method and Output name rows, basic parameters, and an
Advanced parameters disclosure beside Reset defaults. Advanced parameters and
the remembered CPU usage selector start collapsed. The scrollable content uses
22-pixel margins and 18-pixel section spacing; errors and right-aligned Cancel/Run
actions sit in a separate footer. Controllers initialize conditional visibility
so all controls remain editable in Designer.
`model_zoo_dialog.ui` browses the EIDORA Hugging Face collections. Import/export
dialogs have dedicated forms.

Controllers populate data and connect actions. Preserve bounded combo sizing so
long model/source names cannot force splitter widths. Keep semantic `iconName`
properties and SVG fallbacks when saving in Designer. See [source rules](../../README.md).
Row forms support both nested layouts and decorative widget wrappers; see the
[editable row binding contract](../../shared/qt/README.md#editing-panel-row-forms)
for the control and layout names that must remain stable.
