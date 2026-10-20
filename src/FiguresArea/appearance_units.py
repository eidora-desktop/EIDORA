from __future__ import annotations


def pixels_to_points(value_px: float, dpi: float) -> float:
    return max(float(value_px), 0.0) * 72.0 / max(float(dpi), 1.0)


def diameter_pixels_to_matplotlib_area(value_px: float, dpi: float) -> float:
    diameter_points = pixels_to_points(value_px, dpi)
    return max(diameter_points * diameter_points, 0.01)


def signed_percent_to_scale(value_percent: float) -> float:
    return max(1.0 + (float(value_percent) / 100.0), 0.0)
