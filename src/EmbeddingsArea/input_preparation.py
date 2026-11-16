"""Pair model inputs by existing item or source-record identity."""
from dataclasses import replace

from EmbeddingsArea.domain import EmbeddingRunInput


def pair_model_inputs(
    inputs: list[EmbeddingRunInput], source_modalities: dict[str, str],
) -> list[EmbeddingRunInput]:
    selected = set(source_modalities.values())
    if not selected or len(selected) != len(source_modalities):
        raise ValueError("Select exactly one source for each selected modality.")
    for item in inputs:
        if item.source_id not in source_modalities or item.content_type.value != source_modalities[item.source_id]:
            raise ValueError("Selected source contains incompatible input data.")
    if len(selected) == 1:
        return [replace(item, ordinal=index, components=(item,)) for index, item in enumerate(inputs)]
    by_item: dict[int, dict[str, EmbeddingRunInput]] = {}
    for item in inputs:
        modality = source_modalities[item.source_id]
        if item.content_type.value != modality:
            raise ValueError(f"Selected {modality} source contains incompatible input data.")
        if item.metadata_row_id is None:
            raise ValueError("Link the selected Media Sources to the same Items before combining inputs.")
        components = by_item.setdefault(item.metadata_row_id, {})
        if modality in components:
            raise ValueError(
                f"Item {item.metadata_row_id} has multiple {modality} inputs. "
                "Select a source with one input per item."
            )
        components[modality] = item
    paired = []
    for item_id in sorted(by_item):
        components = by_item[item_id]
        if set(components) != selected:
            continue
        ordered = tuple(components[modality] for modality in source_modalities.values())
        paired.append(replace(ordered[0], ordinal=len(paired), components=ordered))
    if not paired:
        raise ValueError("No items have all selected modalities. Link inputs to the same items.")
    return paired


def match_input_records(inputs, values_by_record, record_source_ids):
    """Match exact relative paths for this run without publishing media links."""
    import unicodedata

    def key(value):
        if value is None or isinstance(value, (list, dict)):
            return ""
        return unicodedata.normalize("NFC", str(value).strip()).replace("\\", "/")

    records = {item.metadata_row_id for item in inputs if item.source_id in record_source_ids}
    by_key = {}
    for record, value in values_by_record.items():
        normalized = key(value)
        if record in records and normalized:
            by_key.setdefault(normalized, []).append(record)
    result = []
    seen = set()
    for item in inputs:
        if item.source_id in record_source_ids or item.metadata_row_id is not None:
            result.append(item)
            continue
        normalized = key(item.source_key)
        matches = by_key.get(normalized, ())
        if not matches:
            continue
        identity = (item.source_id, normalized)
        if identity in seen:
            raise ValueError("Several media inputs have the same matching path. Choose an unambiguous source.")
        seen.add(identity)
        result.extend(replace(item, metadata_row_id=record) for record in matches)
    return result
