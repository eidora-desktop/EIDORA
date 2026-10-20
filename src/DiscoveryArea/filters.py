from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping

import numpy as np

from DiscoveryArea.advanced_query import (
    AdvancedQueryError,
    evaluate_advanced_query,
    inspect_advanced_query,
)


class FilterOperator(str, Enum):
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    LESS_OR_EQUAL = "less_or_equal"
    GREATER_OR_EQUAL = "greater_or_equal"


class FilterMatching(str, Enum):
    ALL = "all"
    ANY = "any"


@dataclass(frozen=True)
class FilterDefinition:
    column: str
    operator: FilterOperator


@dataclass(frozen=True)
class SelectionFilter(FilterDefinition):
    value: str


@dataclass(frozen=True)
class QueryFilter(FilterDefinition):
    query: str | None


@dataclass(frozen=True)
class SliderFilter(FilterDefinition):
    threshold: float


@dataclass(frozen=True)
class RangeFilter:
    column: str
    lower: float
    upper: float


@dataclass(frozen=True)
class AdvancedQueryFilter:
    expression: str


FilterRule = (
    SelectionFilter
    | QueryFilter
    | SliderFilter
    | RangeFilter
    | AdvancedQueryFilter
)


def filters_from_state_payload(
    payload: Mapping[str, object],
) -> list[FilterRule]:
    """Restore the active filter rules stored in a project preset."""
    if payload.get("enabled", True) is False:
        return []
    mode = str(payload.get("mode") or "simple").strip().casefold()
    if mode == "advanced":
        expression = str(payload.get("advanced_query") or "").strip()
        return [AdvancedQueryFilter(expression)] if expression else []
    rules = payload.get("rules")
    if not isinstance(rules, list):
        return []

    filters: list[SelectionFilter | QueryFilter | SliderFilter | RangeFilter] = []
    for raw_rule in rules:
        if not isinstance(raw_rule, Mapping) or not bool(raw_rule.get("active", False)):
            continue
        column = str(raw_rule.get("column") or "").strip()
        if not column or column.casefold() == "none":
            continue
        kind = str(raw_rule.get("kind") or "").strip().casefold()
        if kind == "selection":
            filters.append(
                SelectionFilter(
                    column=column,
                    operator=_operator_from_preset(raw_rule.get("operator")),
                    value=str(raw_rule.get("value") or ""),
                )
            )
        elif kind == "query":
            filters.append(
                QueryFilter(
                    column=column,
                    operator=_operator_from_preset(raw_rule.get("operator")),
                    query=str(raw_rule.get("query") or ""),
                )
            )
        elif kind == "slider":
            try:
                threshold = float(raw_rule.get("threshold", 0.0))
            except (TypeError, ValueError):
                continue
            mode = str(raw_rule.get("mode") or "max").strip().casefold()
            operator = {
                "min": FilterOperator.GREATER_OR_EQUAL,
                "exact": FilterOperator.EQUALS,
            }.get(mode, FilterOperator.LESS_OR_EQUAL)
            filters.append(
                SliderFilter(
                    column=column,
                    operator=operator,
                    threshold=threshold,
                )
            )
        elif kind == "range":
            try:
                lower = float(raw_rule.get("lower", 0.0))
                upper = float(raw_rule.get("upper", 0.0))
            except (TypeError, ValueError):
                continue
            filters.append(
                RangeFilter(
                    column=column,
                    lower=min(lower, upper),
                    upper=max(lower, upper),
                )
            )
    return filters


def build_mask(
    filters: Iterable[FilterRule],
    metadata_values: dict[str, np.ndarray],
    metadata_numeric_values: dict[str, np.ndarray],
    metadata_ids: np.ndarray,
    matching: FilterMatching = FilterMatching.ALL,
) -> np.ndarray | None:
    if metadata_ids.size == 0:
        return None
    active_filters = [
        flt
        for flt in filters
        if (
            isinstance(flt, AdvancedQueryFilter)
            and bool(flt.expression.strip())
        )
        or (
            isinstance(flt, (FilterDefinition, RangeFilter))
            and bool(flt.column)
        )
    ]
    if not active_filters:
        return None

    match_any = matching == FilterMatching.ANY
    mask = (
        np.zeros(metadata_ids.shape[0], dtype=bool)
        if match_any
        else np.ones(metadata_ids.shape[0], dtype=bool)
    )
    applied_filter_count = 0
    for flt in active_filters:
        if isinstance(flt, AdvancedQueryFilter):
            try:
                comparison = evaluate_advanced_query(
                    flt.expression,
                    metadata_values,
                    metadata_numeric_values,
                    metadata_ids.shape[0],
                )
            except AdvancedQueryError:
                continue
        elif isinstance(flt, RangeFilter):
            numeric_values = metadata_numeric_values.get(flt.column)
            if numeric_values is None or numeric_values.shape[0] != metadata_ids.shape[0]:
                continue
            valid = np.isfinite(numeric_values)
            comparison = np.zeros_like(numeric_values, dtype=bool)
            comparison[valid] = (
                (numeric_values[valid] >= flt.lower)
                & (numeric_values[valid] <= flt.upper)
            )
        elif isinstance(flt, SliderFilter):
            numeric_values = metadata_numeric_values.get(flt.column)
            if numeric_values is None or numeric_values.shape[0] != metadata_ids.shape[0]:
                continue
            valid = np.isfinite(numeric_values)
            comparison = np.zeros_like(numeric_values, dtype=bool)
            if flt.operator == FilterOperator.GREATER_OR_EQUAL:
                comparison[valid] = numeric_values[valid] >= flt.threshold
            elif flt.operator == FilterOperator.LESS_OR_EQUAL:
                comparison[valid] = numeric_values[valid] <= flt.threshold
            elif flt.operator == FilterOperator.EQUALS:
                comparison[valid] = numeric_values[valid] == flt.threshold
            else:
                comparison[valid] = numeric_values[valid] <= flt.threshold
        else:
            values = metadata_values.get(flt.column)
            if values is None or values.shape[0] != metadata_ids.shape[0]:
                continue
            expected = _normalize(getattr(flt, "value", getattr(flt, "query", None)))
            if flt.operator == FilterOperator.EQUALS:
                comparison = values == expected
            elif flt.operator == FilterOperator.NOT_EQUALS:
                comparison = values != expected
            elif flt.operator == FilterOperator.CONTAINS:
                comparison = np.char.find(values, expected) >= 0
            elif flt.operator == FilterOperator.NOT_CONTAINS:
                comparison = np.char.find(values, expected) < 0
            else:
                comparison = values == expected
        applied_filter_count += 1
        if match_any:
            mask |= comparison
        else:
            mask &= comparison
        if not match_any and not mask.any():
            break

    if applied_filter_count == 0 or np.all(mask):
        return None
    return mask


def filter_matching_from_state_payload(payload: Mapping[str, object]) -> FilterMatching:
    value = str(payload.get("matching") or "").strip().casefold()
    return FilterMatching.ANY if value in {"any", "match any"} else FilterMatching.ALL


def required_filter_columns(filters: Iterable[FilterRule]) -> tuple[set[str], set[str]]:
    columns: set[str] = set()
    numeric_columns: set[str] = set()
    for flt in filters:
        if isinstance(flt, AdvancedQueryFilter):
            try:
                info = inspect_advanced_query(flt.expression)
            except AdvancedQueryError:
                continue
            columns.update(info.columns)
            numeric_columns.update(info.numeric_columns)
            continue
        column = str(flt.column or "").strip()
        if not column:
            continue
        columns.add(column)
        if isinstance(flt, (SliderFilter, RangeFilter)):
            numeric_columns.add(column)
    return columns, numeric_columns


def _normalize(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def _operator_from_preset(value: object) -> FilterOperator:
    text = str(value or "").strip().casefold()
    return {
        "is not": FilterOperator.NOT_EQUALS,
        "not_equals": FilterOperator.NOT_EQUALS,
        "contains": FilterOperator.CONTAINS,
        "does not contain": FilterOperator.NOT_CONTAINS,
        "not_contains": FilterOperator.NOT_CONTAINS,
    }.get(text, FilterOperator.EQUALS)
