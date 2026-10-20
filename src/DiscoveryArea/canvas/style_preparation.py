from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import numpy as np

from DiscoveryArea.color_rules import matching_overlap_keys, ordered_rules, rule_mask
from DiscoveryArea.filters import (
    FilterMatching,
    FilterRule,
    build_mask,
    required_filter_columns,
)


class StyleBuildCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class AreaColorOverlay:
    metadata_ids: np.ndarray
    color_hex: str


@dataclass(frozen=True)
class CanvasStyleRequest:
    revision: int
    metadata_ids: np.ndarray
    metadata_rows: Mapping[int, Mapping[str, str]]
    point_style: Mapping[str, object]
    color_style: Mapping[str, object]
    border_style: Mapping[str, object]
    filters: tuple[FilterRule, ...]
    slicer_column: str
    slicer_values: tuple[str, ...]
    filtered_opacity: float
    area_overlays: tuple[AreaColorOverlay, ...]
    filter_matching: FilterMatching = FilterMatching.ALL
    metadata_values: Mapping[str, np.ndarray] = field(default_factory=dict)
    metadata_numeric_values: Mapping[str, np.ndarray] = field(default_factory=dict)


@dataclass(frozen=True)
class CanvasStyleResult:
    revision: int
    colors: np.ndarray
    edge_colors: np.ndarray | None
    edge_widths: np.ndarray | None
    visibility: np.ndarray
    preview_visibility: np.ndarray
    point_size: float | np.ndarray


def build_canvas_style(
    request: CanvasStyleRequest,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> CanvasStyleResult:
    is_cancelled = cancelled or (lambda: False)

    def checkpoint() -> None:
        if is_cancelled():
            raise StyleBuildCancelled()

    metadata_ids = np.asarray(request.metadata_ids, dtype=np.int64).reshape(-1)
    count = int(metadata_ids.size)
    columns, numeric_columns = required_metadata_columns(request)
    text_values = {
        column: np.asarray(values, dtype=np.str_).reshape(-1)
        for column, values in request.metadata_values.items()
        if column in columns
    }
    numeric_values = {
        column: np.asarray(values, dtype=np.float32).reshape(-1)
        for column, values in request.metadata_numeric_values.items()
        if column in numeric_columns
    }
    missing_columns = columns - set(text_values)
    missing_numeric = numeric_columns - set(numeric_values)
    if missing_columns or missing_numeric:
        fallback_text, fallback_numeric = _materialize_metadata(
            metadata_ids,
            request.metadata_rows,
            columns=missing_columns | missing_numeric,
            numeric_columns=missing_numeric,
        )
        text_values.update(fallback_text)
        numeric_values.update(fallback_numeric)
    checkpoint()

    opacity = float(np.clip(float(request.point_style.get("opacity", 1.0)), 0.0, 1.0))
    colors = _mapped_colors(count, request.color_style, text_values, numeric_values, opacity, "#1f1f1f")
    for overlay in request.area_overlays:
        member_ids = np.asarray(overlay.metadata_ids, dtype=np.int64).reshape(-1)
        if member_ids.size == 0:
            continue
        mask = np.isin(metadata_ids, member_ids, assume_unique=False)
        colors[mask, :3] = _hex_to_rgb01(overlay.color_hex)
    checkpoint()

    border_width = max(0.0, float(request.border_style.get("width", 0.0)))
    edge_widths = _mapped_numeric_style(
        count,
        request.border_style,
        numeric_values,
        mode_key="width_mode",
        column_key="width_column",
        minimum_key="width_min",
        maximum_key="width_max",
        fallback=border_width,
    )
    if np.isscalar(edge_widths):
        edge_widths = np.full(count, float(edge_widths), dtype=np.float32)
    border_visible = bool(np.any(edge_widths > 0.0)) if isinstance(edge_widths, np.ndarray) else edge_widths > 0.0
    if border_visible:
        border_opacity = float(np.clip(float(request.border_style.get("opacity", 1.0)), 0.0, 1.0))
        edge_colors = _mapped_colors(
            count,
            request.border_style,
            text_values,
            numeric_values,
            border_opacity,
            "#111111",
        )
    else:
        edge_colors = None
        edge_widths = None

    filter_mask = build_mask(
        request.filters,
        text_values,
        numeric_values,
        metadata_ids,
        request.filter_matching,
    )
    slicer_mask = None
    if request.slicer_column:
        values = text_values.get(request.slicer_column)
        if values is not None:
            slicer_mask = np.isin(values, np.asarray(request.slicer_values, dtype=np.str_))
            if bool(np.all(slicer_mask)):
                slicer_mask = None
    if filter_mask is None:
        mask = slicer_mask
    elif slicer_mask is None:
        mask = filter_mask
    else:
        mask = np.asarray(filter_mask, dtype=bool) & np.asarray(slicer_mask, dtype=bool)
    point_size = _mapped_numeric_style(
        count,
        request.point_style,
        numeric_values,
        mode_key="size_mode",
        column_key="size_column",
        minimum_key="size_min",
        maximum_key="size_max",
        fallback=max(0.5, float(request.point_style.get("size", 5.0))),
    )
    if mask is None:
        visibility = np.ones(count, dtype=np.float32)
        preview_visibility = np.ones(count, dtype=np.float32)
    else:
        visibility = np.where(mask, 1.0, np.clip(request.filtered_opacity, 0.0, 1.0)).astype(np.float32)
        preview_visibility = np.asarray(mask, dtype=np.float32)
    checkpoint()
    return CanvasStyleResult(
        revision=int(request.revision),
        colors=np.ascontiguousarray(colors, dtype=np.float32),
        edge_colors=None if edge_colors is None else np.ascontiguousarray(edge_colors, dtype=np.float32),
        edge_widths=None if edge_widths is None else np.ascontiguousarray(edge_widths, dtype=np.float32),
        visibility=np.ascontiguousarray(visibility, dtype=np.float32),
        preview_visibility=np.ascontiguousarray(preview_visibility, dtype=np.float32),
        point_size=point_size,
    )


def required_metadata_columns(
    request: CanvasStyleRequest,
) -> tuple[set[str], set[str]]:
    """Return text and numeric metadata columns required by one style build."""

    columns: set[str] = set()
    numeric: set[str] = set()
    column = str(request.color_style.get("column") or "").strip()
    if bool(request.color_style.get("custom")):
        columns.update(_custom_rule_columns(request.color_style))
    elif column and column.lower() not in {"global", "all items", "none"}:
        columns.add(column)
        if str(request.color_style.get("mode") or "categorical").lower() == "continuous":
            numeric.add(column)
    size_column = str(request.point_style.get("size_column") or "").strip()
    if str(request.point_style.get("size_mode") or "uniform").lower() == "mapped" and size_column:
        columns.add(size_column)
        numeric.add(size_column)
    if max(0.0, float(request.border_style.get("width", 0.0))) > 0.0 or str(request.border_style.get("width_mode") or "uniform").lower() == "mapped":
        border_column = str(request.border_style.get("column") or "").strip()
        if bool(request.border_style.get("custom")):
            columns.update(_custom_rule_columns(request.border_style))
        elif border_column and border_column.lower() not in {"global", "all items", "none"}:
            columns.add(border_column)
            if str(request.border_style.get("mode") or "categorical").lower() == "continuous":
                numeric.add(border_column)
        width_column = str(request.border_style.get("width_column") or "").strip()
        if str(request.border_style.get("width_mode") or "uniform").lower() == "mapped" and width_column:
            columns.add(width_column)
            numeric.add(width_column)
    filter_columns, filter_numeric_columns = required_filter_columns(request.filters)
    columns.update(filter_columns)
    numeric.update(filter_numeric_columns)
    if request.slicer_column:
        columns.add(request.slicer_column)
    return columns, numeric


def _materialize_metadata(
    metadata_ids: np.ndarray,
    rows: Mapping[int, Mapping[str, str]],
    *,
    columns: set[str],
    numeric_columns: set[str],
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    text: dict[str, np.ndarray] = {}
    numeric: dict[str, np.ndarray] = {}
    for column in sorted(columns):
        values = [str(rows.get(int(metadata_id), {}).get(column, "")) for metadata_id in metadata_ids]
        as_text = np.asarray(values, dtype=np.str_)
        text[column] = as_text
        if column not in numeric_columns:
            continue
        try:
            numeric[column] = np.asarray(values, dtype=np.float32)
        except (TypeError, ValueError):
            parsed = np.full(len(values), np.nan, dtype=np.float32)
            for index, value in enumerate(values):
                try:
                    parsed[index] = float(value)
                except (TypeError, ValueError):
                    pass
            numeric[column] = parsed
    return text, numeric


def _mapped_colors(
    count: int,
    style: Mapping[str, object],
    text_values: Mapping[str, np.ndarray],
    numeric_values: Mapping[str, np.ndarray],
    alpha: float,
    default_hex: str,
) -> np.ndarray:
    palette = [_hex_to_rgb01(str(color)) for color in style.get("palette", [])]
    base = palette[0] if palette else _hex_to_rgb01(default_hex)
    rgba = np.empty((count, 4), dtype=np.float32)
    rgba[:, :3] = base
    rgba[:, 3] = alpha
    if bool(style.get("custom")):
        labels = custom_color_labels(count, style, text_values)
        raw_mapping = style.get("mapping", {})
        mapping = dict(raw_mapping) if isinstance(raw_mapping, Mapping) else {}
        raw_resolutions = style.get("conflict_resolutions", {})
        if isinstance(raw_resolutions, Mapping):
            for key, resolution in raw_resolutions.items():
                if isinstance(resolution, Mapping) and resolution.get("type") == "color":
                    mapping[f"overlap:{key}"] = resolution.get("color", default_hex)
        for label in np.unique(labels).tolist():
            color = mapping.get(str(label)) or mapping.get("all rest") or default_hex
            rgba[labels == label, :3] = _hex_to_rgb01(str(color))
        return rgba
    column = str(style.get("column") or "").strip()
    if not column or column.lower() in {"global", "all items", "none"}:
        return rgba
    mode = str(style.get("mode") or "categorical").lower()
    if mode == "continuous":
        values = numeric_values.get(column)
        if values is None:
            return rgba
        finite = np.isfinite(values)
        if not np.any(finite):
            return rgba
        minimum = float(np.nanmin(values[finite]))
        span = max(1e-9, float(np.nanmax(values[finite])) - minimum)
        normalized = np.zeros(count, dtype=np.float32)
        normalized[finite] = ((values[finite] - minimum) / span).astype(np.float32)
        start = _hex_to_rgb01(str(style.get("gradient_start", "#3b82f6")))
        end = _hex_to_rgb01(str(style.get("gradient_end", "#ef4444")))
        rgba[:, :3] = start + normalized[:, None] * (end - start)
        return rgba
    values = text_values.get(column)
    if values is None:
        return rgba
    unique, inverse = np.unique(values, return_inverse=True)
    palette = palette or [_hex_to_rgb01(default_hex)]
    raw_mapping = style.get("mapping", {})
    mapping = raw_mapping if isinstance(raw_mapping, Mapping) else {}
    mapped = np.empty((unique.size, 3), dtype=np.float32)
    for index, value in enumerate(unique.tolist()):
        key = str(value).strip() or "(missing)"
        mapped_color = mapping.get(key) or mapping.get(str(value))
        mapped[index] = _hex_to_rgb01(str(mapped_color)) if mapped_color else palette[index % len(palette)]
    rgba[:, :3] = mapped[inverse]
    return rgba


def color_style_metadata_columns(style: Mapping[str, object]) -> tuple[set[str], set[str]]:
    if bool(style.get("custom")):
        return _custom_rule_columns(style), set()
    column = str(style.get("column") or "").strip()
    if not column or column.lower() in {"global", "all items", "none"}:
        return set(), set()
    numeric = {column} if str(style.get("mode") or "categorical").lower() == "continuous" else set()
    return {column}, numeric


def mapped_colors(
    count: int,
    style: Mapping[str, object],
    text_values: Mapping[str, np.ndarray],
    numeric_values: Mapping[str, np.ndarray],
    alpha: float,
    default_hex: str,
) -> np.ndarray:
    return _mapped_colors(count, style, text_values, numeric_values, alpha, default_hex)


def custom_color_labels(
    count: int,
    style: Mapping[str, object],
    text_values: Mapping[str, np.ndarray],
) -> np.ndarray:
    labels = np.full(count, "all rest", dtype=object)
    unmatched = np.ones(count, dtype=bool)
    raw_rules = style.get("rules")
    if not isinstance(raw_rules, list):
        return labels.astype(np.str_)
    for raw_rule in ordered_rules(raw_rules):
        if not isinstance(raw_rule, Mapping):
            continue
        field = str(raw_rule.get("field") or "").strip()
        value = str(raw_rule.get("value") or "")
        label = str(raw_rule.get("label") or f"{field}: {value}").strip()
        values = text_values.get(field)
        if not field or values is None or values.shape[0] != count:
            continue
        comparison = rule_mask(raw_rule, values)
        matched = unmatched & comparison
        labels[matched] = label
        unmatched[matched] = False
    raw_resolutions = style.get("conflict_resolutions")
    resolutions = raw_resolutions if isinstance(raw_resolutions, Mapping) else {}
    if resolutions:
        overlap_keys = matching_overlap_keys(count, raw_rules, text_values)
        for key in np.unique(overlap_keys).tolist():
            resolution = resolutions.get(key)
            if not key or not isinstance(resolution, Mapping):
                continue
            if resolution.get("type") == "color" and resolution.get("color"):
                labels[overlap_keys == key] = f"overlap:{key}"
            elif resolution.get("type") == "rule":
                winner = str(resolution.get("winner") or "")
                if winner:
                    labels[overlap_keys == key] = winner
    return labels.astype(np.str_)


def _custom_rule_columns(style: Mapping[str, object]) -> set[str]:
    raw_rules = style.get("rules")
    if not isinstance(raw_rules, list):
        return set()
    return {
        str(rule.get("field") or "").strip()
        for rule in raw_rules
        if isinstance(rule, Mapping) and str(rule.get("field") or "").strip()
    }


def _mapped_numeric_style(
    count: int,
    style: Mapping[str, object],
    numeric_values: Mapping[str, np.ndarray],
    *,
    mode_key: str,
    column_key: str,
    minimum_key: str,
    maximum_key: str,
    fallback: float,
) -> float | np.ndarray:
    if str(style.get(mode_key) or "uniform").lower() != "mapped":
        return float(fallback)
    column = str(style.get(column_key) or "").strip()
    values = numeric_values.get(column)
    if values is None or values.shape[0] != count:
        return float(fallback)
    low = float(style.get(minimum_key, fallback))
    high = float(style.get(maximum_key, fallback))
    if high < low:
        low, high = high, low
    finite = np.isfinite(values)
    result = np.full(count, low, dtype=np.float32)
    if not np.any(finite):
        return result
    source_low = float(np.nanmin(values[finite]))
    source_span = max(1e-9, float(np.nanmax(values[finite])) - source_low)
    normalized = np.zeros(count, dtype=np.float32)
    normalized[finite] = ((values[finite] - source_low) / source_span).astype(np.float32)
    result[finite] = low + normalized[finite] * (high - low)
    return np.ascontiguousarray(result, dtype=np.float32)


def _hex_to_rgb01(value: str) -> np.ndarray:
    text = str(value or "").strip().lstrip("#")
    if len(text) != 6:
        text = "1f1f1f"
    try:
        return np.asarray([int(text[index : index + 2], 16) / 255.0 for index in (0, 2, 4)], dtype=np.float32)
    except ValueError:
        return np.asarray([0.12, 0.12, 0.12], dtype=np.float32)
