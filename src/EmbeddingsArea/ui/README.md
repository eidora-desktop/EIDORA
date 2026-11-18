# Embedding forms

`embeddings_area.ui` hosts Models on the left and Vector Data on the right.
`embedding_run_row.ui` is the expandable Vector Data card with Progress, Info and
Clusters tabs. Live batch size, counts and speed sit above the progress bar and
remain visible when details are collapsed. Progress owns a read-only
`QPlainTextEdit` activity log; controllers append stage events without replacing
the document for each progress update. `model_execution_dialog.ui` configures one run for the model
selected in the Models panel; it does not provide a second model selector.
It offers registered file-based Media Sources and a single Run Model action.
The focused form owns the model heading, stacked source/output/device fields,
Preview Assets creation option, and footer in Designer. The Designer-owned batch selector and
model preparation options live in the collapsed Advanced settings & parameters
panel. Its choices are Auto, High, Low, 4, 8, 16, 32 and 64. The first three tune
adaptively; numbers request fixed sizes. Auto is the default. Existing saved
sizes outside this list remain available when reopening that run.
The selector tooltip explains speed/headroom tradeoffs and model/resource limits.
Progress records measured speeds and the selection reason; completed runs retain
that explanation when reopened. Live status names the policy, actual batch,
provider, resource limits and limited monitoring; saved safety pauses show their
reason in the activity log and status tooltip.
Run Model closes after saving configuration; input preparation and its errors
appear in the embedding card. Optional Preview Assets creation runs separately in Media.
`model_zoo_dialog.ui` browses the EIDORA Hugging Face collections. Import/export
and clustering dialogs have dedicated forms.

Controllers populate data and connect actions. Preserve bounded combo sizing so
long model/source names cannot force splitter widths. Keep semantic `iconName`
properties and SVG fallbacks when saving in Designer. See [source rules](../../README.md).
