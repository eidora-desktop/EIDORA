from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np


@dataclass(frozen=True)
class Discovery:
    name: str
    method: str
    item_count: int


@dataclass(frozen=True)
class DiscoveryOption:
    key: str
    label: str
    discovery_type: str
    discovery_file: Path | None
    parameters: dict[str, Any] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    embeddings_label: str | None = None
    is_random: bool = False


@dataclass(frozen=True)
class DiscoveryDataset:
    coordinates: np.ndarray
    metadata_ids: np.ndarray


@dataclass(frozen=True)
class DiscoveryViewport:
    x_min: float
    x_max: float
    y_min: float
    y_max: float


@dataclass(frozen=True)
class DiscoveryStyleArrays:
    face_colors: np.ndarray | None = None
    border_colors: np.ndarray | None = None
    sizes: np.ndarray | float | int | None = None
    border_widths: np.ndarray | float | int | None = None
    visibility: np.ndarray | None = None


@dataclass(frozen=True)
class ThumbnailRef:
    metadata_id: int
    path: Path
    fingerprint: str
    source: str
    mtime_ns: int | None = None


@dataclass(frozen=True)
class LassoState:
    polygons: tuple[np.ndarray, ...] = ()
    selected_polygon_index: int | None = None


@dataclass(frozen=True)
class PolygonArea:
    area_id: str
    label: str
    color_hex: str
    polygon: tuple[tuple[float, float], ...]
    metadata_ids: tuple[int, ...]
    discovery_scope: str
    arrangement_ref: str = "original"
    area_visible: bool = True
    points_visible: bool = True


@dataclass(frozen=True)
class PolygonAreaMetadataWriteResult:
    affected_row_count: int
    unmatched_row_count: int
    overlap_count: int
    column_names: tuple[str, ...]
    mode: str


@dataclass(frozen=True)
class PolygonAreaMetadataWritePreview:
    overlap_count: int
    binary_columns: tuple[tuple[str, str], ...]
    overwrite_columns: tuple[str, ...]


@dataclass
class DiscoverySceneState:
    dataset: DiscoveryDataset | None = None
    viewport: DiscoveryViewport | None = None
    style: DiscoveryStyleArrays = field(default_factory=DiscoveryStyleArrays)
    lasso: LassoState = field(default_factory=LassoState)
    pinned_annotations: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class PinnedAnnotation:
    annotation_id: str
    metadata_id: int
    point: tuple[float, float]
    screen_offset: tuple[float, float]
    visible: bool
    created_at: str
    lines: tuple[str, ...] = ()


PIN_DISPLAY_STYLE_DEFAULTS: dict[str, Any] = {
    "thumbnail_visible": False,
    "box_visible": True,
    "connector_visible": True,
    "text_visible": True,
    "box_color": "#f0f0f0",
    "border_color": "#666666",
    "connector_color": "#333333",
    "text_color": "#222222",
    "box_opacity": 0.96,
    "border_width": 1.0,
    "connector_width": 1.5,
    "font_family": "Arial",
    "font_size": 11,
    "show_label": True,
    "show_value": True,
    "text_label_bold": False,
    "text_label_italic": False,
    "text_label_underline": False,
    "text_value_bold": False,
    "text_value_italic": False,
    "text_value_underline": False,
    "thumbnail_size": 96,
    "thumbnail_position": "left",
    "connector_target": "center",
}


@dataclass(frozen=True)
class DiscoveryPinDisplaySettings:
    pin_fields: tuple[str, ...] = ("id",)
    tooltip_fields: tuple[str, ...] = ("id",)
    pin_thumbnail_variant_id: str = "none"
    tooltip_thumbnail_variant_id: str = "none"
    tooltip_thumbnail_size: int = 250
    tooltip_thumbnail_alignment: str = "bottom"
    tooltip_thumbnail_visible: bool = True
    tooltip_info_field_visible: bool = True
    tooltip_show_label: bool = True
    style: dict[str, Any] = field(default_factory=lambda: dict(PIN_DISPLAY_STYLE_DEFAULTS))
    layer_state: dict[str, Any] = field(default_factory=lambda: {"pins_visible": True})
    last_discovery_key: str = "random"

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "DiscoveryPinDisplaySettings":
        if not isinstance(payload, dict):
            return cls()
        pin_fields = _string_tuple(payload.get("pin_fields"))
        if not pin_fields and "pin_fields" not in payload:
            pin_fields = _string_tuple(payload.get("fields"))
        tooltip_fields = _string_tuple(payload.get("tooltip_fields")) if "tooltip_fields" in payload else ("id",)
        style = _normalized_pin_style(payload.get("style"))
        pin_thumbnail = _thumbnail_id(payload.get("thumbnail_variant_id"))
        if pin_thumbnail == "none":
            pin_thumbnail = _thumbnail_id(payload.get("thumbnail_field"))
        tooltip_thumbnail = _thumbnail_id(payload.get("tooltip_thumbnail_variant_id"))
        return cls(
            pin_fields=pin_fields if "pin_fields" in payload or "fields" in payload else ("id",),
            tooltip_fields=tooltip_fields,
            pin_thumbnail_variant_id=pin_thumbnail,
            tooltip_thumbnail_variant_id=tooltip_thumbnail,
            tooltip_thumbnail_size=_clamp_int(payload.get("tooltip_thumbnail_size"), 25, 500, 250),
            tooltip_thumbnail_alignment=_thumbnail_alignment(payload.get("tooltip_thumbnail_alignment")),
            tooltip_thumbnail_visible=bool(
                payload.get("tooltip_thumbnail_visible", tooltip_thumbnail != "none")
            ),
            tooltip_info_field_visible=bool(payload.get("tooltip_info_field_visible", True)),
            tooltip_show_label=bool(payload.get("tooltip_show_label", True)),
            style=style,
            layer_state=_normalized_layer_state(payload.get("layer_state")),
            last_discovery_key=str(payload.get("last_discovery_key") or "random").strip() or "random",
        )

    def to_payload(self) -> dict[str, Any]:
        pin_fields = list(self.pin_fields)
        tooltip_fields = list(self.tooltip_fields)
        return {
            "schema_version": 1,
            "pin_fields": pin_fields,
            "tooltip_fields": tooltip_fields,
            "fields": pin_fields,
            "thumbnail_variant_id": _thumbnail_id(self.pin_thumbnail_variant_id),
            "tooltip_thumbnail_variant_id": _thumbnail_id(self.tooltip_thumbnail_variant_id),
            "tooltip_thumbnail_size": _clamp_int(self.tooltip_thumbnail_size, 25, 500, 250),
            "tooltip_thumbnail_alignment": _thumbnail_alignment(self.tooltip_thumbnail_alignment),
            "tooltip_thumbnail_visible": bool(self.tooltip_thumbnail_visible),
            "tooltip_info_field_visible": bool(self.tooltip_info_field_visible),
            "tooltip_show_label": bool(self.tooltip_show_label),
            "style": _normalized_pin_style(self.style),
            "layer_state": _normalized_layer_state(self.layer_state),
            "last_discovery_key": str(self.last_discovery_key or "random").strip() or "random",
        }


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return tuple(output)


def _thumbnail_alignment(value: Any) -> str:
    alignment = str(value or "bottom").strip().lower()
    return alignment if alignment in {"left", "right", "top", "bottom"} else "bottom"


def _thumbnail_id(value: Any) -> str:
    text = str(value or "none").strip() or "none"
    return "none" if text == "create" else text


def _clamp_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        number = fallback
    return max(minimum, min(maximum, number))


def _normalized_pin_style(value: Any) -> dict[str, Any]:
    style = dict(PIN_DISPLAY_STYLE_DEFAULTS)
    if isinstance(value, dict):
        style.update(value)
    for key in (
        "thumbnail_visible",
        "box_visible",
        "connector_visible",
        "text_visible",
        "show_label",
        "show_value",
        "text_label_bold",
        "text_label_italic",
        "text_label_underline",
        "text_value_bold",
        "text_value_italic",
        "text_value_underline",
    ):
        style[key] = bool(style.get(key))
    for key, fallback in (
        ("box_color", "#f0f0f0"),
        ("border_color", "#666666"),
        ("connector_color", "#333333"),
        ("text_color", "#222222"),
    ):
        text = str(style.get(key) or fallback).strip()
        style[key] = text if text.startswith("#") else fallback
    style["box_opacity"] = _clamp_float(style.get("box_opacity"), 0.0, 1.0, 0.96)
    style["border_width"] = _clamp_float(style.get("border_width"), 0.0, 100.0, 1.0)
    style["connector_width"] = _clamp_float(style.get("connector_width"), 0.0, 100.0, 1.5)
    style["font_size"] = max(8, int(round(_clamp_float(style.get("font_size"), 8.0, 96.0, 11.0))))
    style["font_family"] = str(style.get("font_family") or "Arial").strip() or "Arial"
    style["thumbnail_size"] = max(25, int(round(_clamp_float(style.get("thumbnail_size"), 25.0, 500.0, 96.0))))
    position = str(style.get("thumbnail_position") or "left").strip().lower()
    style["thumbnail_position"] = position if position in {"left", "right", "top", "bottom"} else "left"
    target = str(style.get("connector_target") or "center").strip().lower()
    style["connector_target"] = {"box": "center", "thumbnail": "edge"}.get(target, target)
    if style["connector_target"] not in {"center", "edge", "corner"}:
        style["connector_target"] = "center"
    return style


def _normalized_layer_state(value: Any) -> dict[str, Any]:
    state = {"pins_visible": True}
    if isinstance(value, dict) and "pins_visible" in value:
        state["pins_visible"] = bool(value.get("pins_visible"))
    return state


def _clamp_float(value: Any, minimum: float, maximum: float, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(minimum, min(maximum, parsed))
