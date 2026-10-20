from __future__ import annotations

import json
from importlib.resources import files

from PySide6 import QtGui


# Explicit, ordered colors are shared by menus and automatic assignments.
_CONFIG = json.loads(files("DiscoveryArea").joinpath("palettes.json").read_text(encoding="utf-8"))
MAX_CATEGORICAL_VALUES = int(_CONFIG["max_categorical_values"])
CATEGORICAL_PALETTES = tuple(
    (entry["label"], tuple(entry["colors"])) for entry in _CONFIG["palettes"]
)
_DEFAULT_COLORS = next(
    tuple(entry["colors"]) for entry in _CONFIG["palettes"]
    if entry["id"] == _CONFIG["default_palette"]
)
DISTINCT_BASE_COLORS = _DEFAULT_COLORS


def default_categorical_palette() -> tuple[str, ...]:
    return _DEFAULT_COLORS


def palette_options() -> tuple[tuple[str, tuple[str, ...]], ...]:
    return CATEGORICAL_PALETTES


def distinct_palette(count: int, *, fallback: list[str] | tuple[str, ...] | None = None) -> list[str]:
    if count <= 0:
        return []
    colors: list[str] = []
    seen: set[str] = set()

    # Saved groups, marked areas, and conditions can exceed the ordinary
    # metadata categorical limit; generate additional colors for those mappings.
    seeds = tuple(fallback or ()) + DISTINCT_BASE_COLORS
    for value in seeds:
        color = QtGui.QColor(str(value))
        if not color.isValid():
            continue
        key = color.name().lower()
        if key not in seen:
            seen.add(key)
            colors.append(key)
        if len(colors) >= count:
            return colors
    idx = 0
    while len(colors) < count:
        hue = (0.61803398875 * idx) % 1.0
        sat = 0.75 if (idx % 2 == 0) else 0.9
        val = 0.92 if (idx % 3 == 0) else 0.78
        color = QtGui.QColor.fromHsvF(hue, sat, val).name()
        idx += 1
        key = color.lower()
        if key in seen:
            continue
        seen.add(key)
        colors.append(color)
    return colors


def next_unused_color(used_colors: list[str] | tuple[str, ...]) -> str:
    """Choose by color identity, never by the row's position."""
    used = {
        QtGui.QColor(str(value).strip()).name().lower()
        for value in used_colors if QtGui.QColor(str(value).strip()).isValid()
    }
    for color in distinct_palette(len(used) + 1):
        if color not in used:
            return color
    raise AssertionError("Distinct palette must contain an unused color")


def next_unused_area_color(used_colors: list[str] | tuple[str, ...]) -> str:
    return next_unused_color(used_colors)
