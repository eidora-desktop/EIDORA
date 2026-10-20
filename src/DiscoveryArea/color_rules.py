"""Shared priority and overlap semantics for conditional colors."""
from __future__ import annotations

from collections.abc import Mapping
import json

import numpy as np


def rule_rank(rule: Mapping, index: int) -> int:
    """Old presets retain insertion order; zero is reserved for the fallback."""
    try:
        return max(1, int(rule.get("rank", index + 1)))
    except (TypeError, ValueError, OverflowError):
        return index + 1


def ordered_rules(rules: list) -> list[Mapping]:
    return [rule for _, rule in sorted(
        ((index, rule) for index, rule in enumerate(rules) if isinstance(rule, Mapping)),
        key=lambda entry: (rule_rank(entry[1], entry[0]), entry[0]),
    )]


def rule_mask(rule: Mapping, values: np.ndarray) -> np.ndarray:
    value = str(rule.get("value") or "")
    return values == ("" if value == "(missing)" else value)


def overlap_key(labels: list[str] | tuple[str, ...]) -> str:
    """Return a rank-independent, persistence-safe key for an exact overlap."""
    return json.dumps(sorted(labels), ensure_ascii=False, separators=(",", ":"))


def overlap_labels(key: str) -> tuple[str, ...]:
    """Read the rule labels stored in an exact-overlap key."""
    try:
        labels = json.loads(key)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
        return ()
    return tuple(labels)


def matching_overlap_keys(
    count: int,
    rules: list,
    text_values: Mapping[str, np.ndarray],
) -> np.ndarray:
    """Return the exact matching-rule signature for every conflicting item."""
    ordered = ordered_rules(rules)
    masks: list[np.ndarray] = []
    labels: list[str] = []
    for rule in ordered:
        field = str(rule.get("field") or "").strip()
        values = text_values.get(field)
        if not field or values is None or values.shape[0] != count:
            continue
        masks.append(rule_mask(rule, values))
        labels.append(str(rule.get("label") or f'{field}: {rule.get("value", "")}'))
    keys = np.full(count, "", dtype=object)
    if len(masks) < 2:
        return keys.astype(np.str_)
    if len(masks) <= 63:
        codes = np.zeros(count, dtype=np.uint64)
        for index, mask in enumerate(masks):
            codes[mask] |= np.uint64(1) << np.uint64(index)
        for code in np.unique(codes).tolist():
            indexes = [index for index in range(len(labels)) if code & (1 << index)]
            if len(indexes) > 1:
                keys[codes == code] = overlap_key(tuple(labels[index] for index in indexes))
    else:
        membership = np.column_stack(masks)
        packed = np.packbits(membership, axis=1)
        _groups, first_indexes, inverse = np.unique(
            packed, axis=0, return_index=True, return_inverse=True
        )
        for group_index, first_index in enumerate(first_indexes.tolist()):
            indexes = np.flatnonzero(membership[first_index]).tolist()
            if len(indexes) > 1:
                keys[inverse == group_index] = overlap_key(
                    tuple(labels[index] for index in indexes)
                )
    return keys.astype(np.str_)


def analyze_rule_overlaps(rules: list, metadata_access) -> tuple[dict[str, str], list[dict]]:
    """Check actual dataset overlap off the UI thread, using canonical IDs.

    Report the actual winners for overlapping items, including three-way
    overlaps. The fallback never conflicts with explicit conditions.
    """
    rules = [dict(rule, rank=rule_rank(rule, index)) for index, rule in enumerate(rules)]
    rules = ordered_rules(rules)
    if len(rules) < 2 or metadata_access is None:
        return {}, []
    ids = metadata_access.metadata_ids()
    values, _ = metadata_access.values_for_ids(
        ids, columns={rule["field"] for rule in rules}, numeric_columns=set(),
    )
    masks = [rule_mask(rule, values[rule["field"]]) for rule in rules]
    matches = np.zeros(ids.size, dtype=np.int32)
    winners = np.full(ids.size, -1, dtype=np.int32)
    for index, mask in enumerate(masks):
        winners[mask & (winners < 0)] = index
        matches += mask
    warnings: dict[str, str] = {}
    for rule, mask in zip(rules, masks):
        overlap = mask & (matches > 1)
        if not np.any(overlap):
            continue
        prioritized = [rules[index] for index in np.unique(winners[overlap])]
        names = "; ".join(f'{winner["label"]} (rank {winner["rank"]})' for winner in prioritized)
        warnings[rule["label"]] = (
            f"Conflicting rules overlap on {int(overlap.sum())} item(s). "
            f"Prioritized for these items: {names}. "
            "Smaller ranks win; equal ranks use rule creation order."
        )
    keys = matching_overlap_keys(ids.size, rules, values)
    groups: list[dict] = []
    for key in np.unique(keys).tolist():
        if not key:
            continue
        labels = overlap_labels(key)
        matching_rules = [rule for rule in rules if rule["label"] in labels]
        groups.append({
            "key": key,
            "labels": labels,
            "label": " && ".join(rule["label"] for rule in matching_rules),
            "count": int(np.count_nonzero(keys == key)),
            "winner": matching_rules[0]["label"],
        })
    groups.sort(key=lambda group: (len(group["labels"]), group["label"].casefold()))
    return warnings, groups


def conflict_warnings(rules: list, metadata_access) -> dict[str, str]:
    """Compatibility helper for callers interested only in warning text."""
    return analyze_rule_overlaps(rules, metadata_access)[0]
