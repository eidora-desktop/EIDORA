from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Literal

import numpy as np


RenderMode = Literal["exact", "hybrid"]


HYBRID_REPRESENTATIVE_POOL_LIMIT = 100_000


@dataclass(frozen=True)
class LodPolicy:
    target_frame_ms: float = 12.0
    exact_visible_limit: int = 100_000
    exact_coverage_ratio: float = 0.8
    hybrid_coverage_ratio: float = 1.2
    representative_min: int = 5_000
    representative_max: int = HYBRID_REPRESENTATIVE_POOL_LIMIT
    antialias_radius_px: float = 1.0
    hybrid_fragment_budget_ratio: float = 3.1


@dataclass(frozen=True)
class LodDecision:
    mode: RenderMode
    visible_count: int
    coverage_ratio: float
    representative_limit: int


def marker_coverage_ratio(
    *,
    visible_count: int,
    point_size_px: float,
    edge_width_px: float,
    canvas_width_px: int,
    canvas_height_px: int,
    device_pixel_ratio: float = 1.0,
    antialias_radius_px: float = 1.0,
) -> float:
    """Estimate fragment work as projected marker pixels / physical canvas pixels."""

    physical_width = max(1.0, float(canvas_width_px) * max(0.1, float(device_pixel_ratio)))
    physical_height = max(1.0, float(canvas_height_px) * max(0.1, float(device_pixel_ratio)))
    radius = max(0.5, float(point_size_px) * 0.5) + max(0.0, float(edge_width_px))
    radius = (radius + max(0.0, float(antialias_radius_px))) * max(0.1, float(device_pixel_ratio))
    covered_pixels = max(0, int(visible_count)) * math.pi * radius * radius
    return float(covered_pixels / (physical_width * physical_height))


def choose_lod(
    *,
    previous_mode: RenderMode,
    visible_count: int,
    point_size_px: float,
    edge_width_px: float,
    canvas_width_px: int,
    canvas_height_px: int,
    device_pixel_ratio: float = 1.0,
    representative_budget: int | None = None,
    policy: LodPolicy = LodPolicy(),
) -> LodDecision:
    coverage = marker_coverage_ratio(
        visible_count=visible_count,
        point_size_px=point_size_px,
        edge_width_px=edge_width_px,
        canvas_width_px=canvas_width_px,
        canvas_height_px=canvas_height_px,
        device_pixel_ratio=device_pixel_ratio,
        antialias_radius_px=policy.antialias_radius_px,
    )
    must_be_hybrid = visible_count > policy.exact_visible_limit
    if previous_mode == "hybrid":
        mode: RenderMode = (
            "exact"
            if not must_be_hybrid and coverage <= policy.exact_coverage_ratio
            else "hybrid"
        )
    else:
        mode = "hybrid" if must_be_hybrid or coverage >= policy.hybrid_coverage_ratio else "exact"

    # Hybrid representatives retain the configured point diameter, but use the
    # fast borderless-circle pipeline. Budget their fill work from that actual
    # radius so unusually large points automatically reduce the sample count.
    radius = (
        max(0.5, float(point_size_px) * 0.5)
        + max(0.0, float(policy.antialias_radius_px))
    )
    marker_area = math.pi * radius * radius
    useful_markers = int(
        (
            max(1, canvas_width_px)
            * max(1, canvas_height_px)
            * max(0.1, float(policy.hybrid_fragment_budget_ratio))
        )
        / max(1.0, marker_area)
    )
    limit = int(np.clip(useful_markers, policy.representative_min, policy.representative_max))
    if representative_budget is not None:
        limit = min(limit, max(policy.representative_min, int(representative_budget)))
    limit = min(limit, max(0, int(visible_count)))
    return LodDecision(mode=mode, visible_count=max(0, int(visible_count)), coverage_ratio=coverage, representative_limit=limit)


@dataclass
class AdaptiveRenderBudget:
    """Small hysteretic controller for representative marker work."""

    limit: int = 60_000
    minimum: int = 5_000
    maximum: int = HYBRID_REPRESENTATIVE_POOL_LIMIT
    slow_frame_ms: float = 14.0
    fast_frame_ms: float = 9.0
    _slow_streak: int = 0
    _fast_streak: int = 0

    def observe(self, frame_ms: float) -> int:
        if not np.isfinite(frame_ms) or frame_ms <= 0.0:
            return self.limit
        if frame_ms > self.slow_frame_ms:
            self._slow_streak += 1
            self._fast_streak = 0
            if self._slow_streak >= 3:
                self.limit = max(self.minimum, int(self.limit * 0.8))
                self._slow_streak = 0
        elif frame_ms < self.fast_frame_ms:
            self._fast_streak += 1
            self._slow_streak = 0
            if self._fast_streak >= 30:
                self.limit = min(self.maximum, max(self.minimum, int(self.limit * 1.1)))
                self._fast_streak = 0
        else:
            self._slow_streak = 0
            self._fast_streak = 0
        return self.limit
