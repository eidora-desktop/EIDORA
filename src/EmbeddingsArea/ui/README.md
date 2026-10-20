# Embedding forms

`embeddings_area.ui` hosts Models on the left and Vector Data on the right.
`embedding_run_row.ui` is the expandable Vector Data card with Info and
Clusters tabs. `model_execution_dialog.ui` configures one run for the model
selected in the Models panel; it does not provide a second model selector.
`model_zoo_dialog.ui` browses the EIDORA Hugging Face collections. Import/export
and clustering dialogs have dedicated forms.

Controllers populate data and connect actions. Preserve bounded combo sizing so
long model/source names cannot force splitter widths. Keep semantic `iconName`
properties and SVG fallbacks when saving in Designer. See [source rules](../../README.md).
