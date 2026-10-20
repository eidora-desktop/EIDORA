from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ViewportAspectPreset:
    key: str
    label: str
    ratio: float | None


VIEWPORT_ASPECT_PRESETS = (
    ViewportAspectPreset("free", "Free", None),
    ViewportAspectPreset("1:1", "1:1", 1.0),
    ViewportAspectPreset("4:3", "4:3", 4.0 / 3.0),
    ViewportAspectPreset("3:4", "3:4", 3.0 / 4.0),
    ViewportAspectPreset("3:2", "3:2", 3.0 / 2.0),
    ViewportAspectPreset("2:3", "2:3", 2.0 / 3.0),
    ViewportAspectPreset("16:9", "16:9", 16.0 / 9.0),
)

_PRESETS_BY_KEY = {preset.key: preset for preset in VIEWPORT_ASPECT_PRESETS}


def normalize_viewport_aspect_preset(value: object) -> str:
    key = str(value or "free").strip().lower()
    return key if key in _PRESETS_BY_KEY else "free"


def viewport_aspect_ratio(preset: object) -> float | None:
    return _PRESETS_BY_KEY[normalize_viewport_aspect_preset(preset)].ratio


def centered_frame_rect(
    canvas_width: float,
    canvas_height: float,
    preset: object,
) -> tuple[float, float, float, float]:
    width = max(1.0, float(canvas_width))
    height = max(1.0, float(canvas_height))
    ratio = viewport_aspect_ratio(preset)
    if ratio is None:
        return (0.0, 0.0, width, height)
    if width / height > ratio:
        frame_width = height * ratio
        return ((width - frame_width) * 0.5, 0.0, frame_width, height)
    frame_height = width / ratio
    return (0.0, (height - frame_height) * 0.5, width, frame_height)


def normalize_world_viewport(
    viewport: tuple[float, float, float, float],
    target_aspect: float,
    *,
    expand: bool,
) -> tuple[float, float, float, float]:
    x_min, x_max, y_min, y_max = (float(value) for value in viewport)
    span_x = max(1e-12, x_max - x_min)
    span_y = max(1e-12, y_max - y_min)
    center_x = (x_min + x_max) * 0.5
    center_y = (y_min + y_max) * 0.5
    current_aspect = span_x / span_y
    target = max(1e-12, float(target_aspect))
    if abs(current_aspect - target) <= 1e-9:
        return (x_min, x_max, y_min, y_max)
    if (current_aspect < target) == expand:
        span_x = span_y * target
    else:
        span_y = span_x / target
    return (
        center_x - span_x * 0.5,
        center_x + span_x * 0.5,
        center_y - span_y * 0.5,
        center_y + span_y * 0.5,
    )


def render_viewport_for_frame(
    framed_viewport: tuple[float, float, float, float],
    canvas_width: float,
    canvas_height: float,
    frame_rect: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    canvas_w = max(1.0, float(canvas_width))
    canvas_h = max(1.0, float(canvas_height))
    _frame_x, _frame_y, frame_w, frame_h = frame_rect
    x_min, x_max, y_min, y_max = framed_viewport
    center_x = (x_min + x_max) * 0.5
    center_y = (y_min + y_max) * 0.5
    span_x = max(1e-12, float(x_max - x_min)) * canvas_w / max(1.0, frame_w)
    span_y = max(1e-12, float(y_max - y_min)) * canvas_h / max(1.0, frame_h)
    return (
        center_x - span_x * 0.5,
        center_x + span_x * 0.5,
        center_y - span_y * 0.5,
        center_y + span_y * 0.5,
    )


def cropped_viewport_for_frame(
    viewport: tuple[float, float, float, float],
    canvas_width: float,
    canvas_height: float,
    frame_rect: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    canvas_w = max(1.0, float(canvas_width))
    canvas_h = max(1.0, float(canvas_height))
    frame_x, frame_y, frame_w, frame_h = frame_rect
    x_min, x_max, y_min, y_max = viewport
    span_x = float(x_max - x_min)
    span_y = float(y_max - y_min)
    crop_x_min = x_min + (frame_x / canvas_w) * span_x
    crop_x_max = x_min + ((frame_x + frame_w) / canvas_w) * span_x
    crop_y_max = y_max - (frame_y / canvas_h) * span_y
    crop_y_min = y_max - ((frame_y + frame_h) / canvas_h) * span_y
    return (crop_x_min, crop_x_max, crop_y_min, crop_y_max)
