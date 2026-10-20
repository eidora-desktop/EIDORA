"""Pair selected encoder inputs by permanent item identity before execution."""
from dataclasses import replace

from EmbeddingsArea.domain import EmbeddingRunInput


def pair_model_inputs(
    inputs: list[EmbeddingRunInput], source_modalities: dict[str, str],
) -> list[EmbeddingRunInput]:
    selected = set(source_modalities.values())
    if not selected or len(selected) != len(source_modalities):
        raise ValueError("Select exactly one source for each selected modality.")
    by_item: dict[int, dict[str, EmbeddingRunInput]] = {}
    for item in inputs:
        modality = source_modalities[item.source_id]
        if item.content_type.value != modality:
            raise ValueError(f"Selected {modality} source contains incompatible input data.")
        if item.metadata_row_id is None:
            raise ValueError("Multimodal inputs require permanent item IDs.")
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
