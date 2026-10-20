"""Global thumbnail rendering configuration.

This module is the single tuning point for discovery thumbnail loading and
native rendering budgets. Keep these values here until a UI for renderer
performance presets exists.
"""

from dataclasses import dataclass
import os


MAX_NATIVE_THUMBNAIL_BUDGET = 2_147_483_647

# Developer-facing prefetch tuning. Prefetch warms thumbnails outside the
# visible viewport; it does not change the visible thumbnail density.
THUMBNAIL_PREFETCH_MARGIN_FRACTION = 0.25
THUMBNAIL_PREFETCH_BUDGET_MULTIPLIER = 2.0
THUMBNAIL_DENSITY = 1.0

# Debug/performance switch. When enabled, every visible thumbnail is requested
# and rendered. This can be very expensive for dense discovery.
RENDER_EVERY_THUMBNAIL = False

AUTO_DENSITY_MIN_THUMBNAIL_SIZE_PX = 16
AUTO_DENSITY_MAX_THUMBNAIL_SIZE_PX = 128
AUTO_DENSITY_LIMIT_AT_MIN_SIZE = 1_250
AUTO_DENSITY_LIMIT_AT_MAX_SIZE = 50


@dataclass(frozen=True)
class ThumbnailRequestPolicy:
    """Computed thumbnail request budget for one viewport."""

    tier: str
    limit: int | None
    candidate_scan_limit: int | None
    grid_cell_px: float
    prefetch_margin_fraction: float
    prefetch_limit: int | None
    visible_items_factor: float
    visible_items: int | None
    density: float


@dataclass(frozen=True)
class ThumbnailRuntimePolicy:
    """Automatically selected thumbnail runtime and frame budgets."""

    profile: str
    cpu_cache_mb: int
    gpu_cache_mb: int
    atlas_page_px: int
    decode_workers: int
    upload_bytes_per_frame: int
    screen_coverage_budget: float
    target_frame_ms: float = 12.0
    reduce_frame_ms: float = 14.0
    increase_frame_ms: float = 9.0
    reduce_after_frames: int = 3
    increase_after_frames: int = 30
    settle_ms: int = 150
    progressive_interval_ms: int = 180


def automatic_thumbnail_runtime_policy(
    *,
    logical_cores: int | None = None,
    total_memory_bytes: int | None = None,
) -> ThumbnailRuntimePolicy:
    """Choose conservative defaults without exposing a user-facing setting."""

    cores = max(1, int(logical_cores or os.cpu_count() or 1))
    memory = _total_memory_bytes() if total_memory_bytes is None else max(0, int(total_memory_bytes))
    gib = float(memory) / float(1024**3) if memory > 0 else 0.0
    if cores <= 4 or (gib > 0.0 and gib < 8.0):
        return ThumbnailRuntimePolicy(
            profile="low",
            cpu_cache_mb=64,
            gpu_cache_mb=48,
            atlas_page_px=1024,
            decode_workers=1,
            upload_bytes_per_frame=1 * 1024 * 1024,
            screen_coverage_budget=0.35,
        )
    if cores >= 8 and gib >= 16.0:
        return ThumbnailRuntimePolicy(
            profile="high",
            cpu_cache_mb=256,
            gpu_cache_mb=192,
            atlas_page_px=2048,
            decode_workers=4,
            upload_bytes_per_frame=8 * 1024 * 1024,
            screen_coverage_budget=0.75,
        )
    return ThumbnailRuntimePolicy(
        profile="standard",
        cpu_cache_mb=128,
        gpu_cache_mb=96,
        atlas_page_px=2048,
        decode_workers=2,
        upload_bytes_per_frame=4 * 1024 * 1024,
        screen_coverage_budget=0.55,
    )


def recommended_thumbnail_limit(
    *,
    profile: str,
    point_count: int,
    thumbnail_size_px: float = 64.0,
    reference_thumbnail_px: float = 64.0,
) -> int:
    """Recommend a viewport budget for a device, collection, and preview size."""

    count = max(0, int(point_count))
    normalized_profile = str(profile or "standard").strip().lower()
    if normalized_profile == "low":
        if count <= 1_000:
            base_limit = 250
        elif count <= 10_000:
            base_limit = 200
        elif count <= 60_000:
            base_limit = 150
        elif count <= 250_000:
            base_limit = 100
        else:
            base_limit = 75
    elif normalized_profile == "high":
        if count <= 1_000:
            base_limit = 1_000
        elif count <= 10_000:
            base_limit = 800
        elif count <= 60_000:
            base_limit = 600
        elif count <= 250_000:
            base_limit = 400
        else:
            base_limit = 300
    elif count <= 1_000:
        base_limit = 500
    elif count <= 10_000:
        base_limit = 400
    elif count <= 60_000:
        base_limit = 300
    elif count <= 250_000:
        base_limit = 200
    else:
        base_limit = 150

    size_px = max(1.0, float(thumbnail_size_px))
    reference_px = max(1.0, float(reference_thumbnail_px))
    area_scale = (reference_px / size_px) ** 2
    return max(1, int(round(float(base_limit) * area_scale)))


def automatic_thumbnail_limit_for_size(thumbnail_size_px: float) -> int:
    """Linearly trade preview count for thumbnail size in Auto Density mode."""

    minimum_size = float(AUTO_DENSITY_MIN_THUMBNAIL_SIZE_PX)
    maximum_size = float(AUTO_DENSITY_MAX_THUMBNAIL_SIZE_PX)
    size = max(minimum_size, min(maximum_size, float(thumbnail_size_px)))
    progress = (size - minimum_size) / (maximum_size - minimum_size)
    return int(
        round(
            AUTO_DENSITY_LIMIT_AT_MIN_SIZE
            + progress
            * (AUTO_DENSITY_LIMIT_AT_MAX_SIZE - AUTO_DENSITY_LIMIT_AT_MIN_SIZE)
        )
    )


def _total_memory_bytes() -> int:
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        page_count = int(os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, TypeError, ValueError):
        return 0
    return max(0, page_size * page_count)


@dataclass(frozen=True)
class ThumbnailSizeParameters:
    """Renderer tuning for one thumbnail display size."""

    size_px: int
    zoom_far_threshold: float
    zoom_close_threshold: float
    max_visible_many_items: int
    max_visible_few_items: int
    visible_items_many_threshold: int
    visible_items_few_threshold: int


# Developer-facing budget scaling. Each supported thumbnail size has explicit
# visible-image limits so renderer behavior stays predictable.
DEFAULT_THUMBNAIL_SIZE_PARAMETERS: tuple[ThumbnailSizeParameters, ...] = (
    ThumbnailSizeParameters(
        size_px=32,
        zoom_far_threshold=30.0,
        zoom_close_threshold=750.0,
        max_visible_many_items=200,
        max_visible_few_items=4000,
        visible_items_many_threshold=40_000,
        visible_items_few_threshold=1_000,
    ),
    ThumbnailSizeParameters(
        size_px=64,
        zoom_far_threshold=30.0,
        zoom_close_threshold=1500.0,
        max_visible_many_items=100,
        max_visible_few_items=1000,
        visible_items_many_threshold=40_000,
        visible_items_few_threshold=1_000,
    ),
    ThumbnailSizeParameters(
        size_px=128,
        zoom_far_threshold=60.0,
        zoom_close_threshold=3000.0,
        max_visible_many_items=20,
        max_visible_few_items=203,
        visible_items_many_threshold=40_000,
        visible_items_few_threshold=1_000,
    ),
)


@dataclass(frozen=True)
class ThumbnailRenderConfig:
    """Consolidated thumbnail rendering parameters."""

    render_every_thumbnail: bool
    size_parameters: tuple[ThumbnailSizeParameters, ...]
    thumbnail_density: float
    prefetch_margin_fraction: float
    prefetch_budget_multiplier: float

    # Native payload compatibility only. Size-specific thresholds in
    # size_parameters are authoritative.
    zoom_mid_threshold: float
    zoom_close_threshold: float

    reference_thumbnail_px: int

    # CPU planning index resolution. The thumbnail planner builds this
    # world-space grid once per discovery and queries it on camera changes.
    planning_grid_divisions: int

    # Hard caps that guard fallback painting and cache eviction paths.
    painted_draw_limit_detail: int
    eviction_keep_scan_limit: int

    # Controller request/decode pacing.
    request_debounce_ms: int
    rolling_request_interval_ms: int
    push_interval_ms: int
    push_batch_size: int
    decode_cache_mb: int
    decode_workers: int

    # Sticky candidates keep thumbnail choices stable while panning.
    sticky_metadata_limit: int
    sticky_metadata_trim_to: int

    # Dormant tile-rendering defaults, kept here so tile budgets can be tested
    # from the same place when the tile path is wired back in.
    tile_size_px: int
    tile_max_lod: int
    tile_cache_mb: int
    tile_decode_workers: int
    tile_max_decode_jobs: int
    tile_min_thumbnails_per_tile: int | None
    tile_max_thumbnails_per_tile: int | None

    def request_policy_for_zoom(
        self,
        zoom_px_per_world: float,
        *,
        canvas_size: tuple[float, float] = (1000.0, 800.0),
        thumbnail_size_px: float | None = None,
        visible_items: int | None = None,
        density: float | None = None,
        prefetch: float | None = None,
    ) -> ThumbnailRequestPolicy:
        _ = canvas_size, prefetch
        normalized_density = self.default_density() if density is None else self.normalized_density(density)
        grid_cell_px = max(12.0, float(thumbnail_size_px or self.reference_thumbnail_px))
        _ = zoom_px_per_world
        visible_items_factor = self.visible_items_factor_for_count(
            visible_items,
            thumbnail_size_px=thumbnail_size_px,
        )
        if self.render_every_thumbnail:
            return ThumbnailRequestPolicy(
                tier="all",
                limit=None,
                candidate_scan_limit=None,
                grid_cell_px=grid_cell_px,
                prefetch_margin_fraction=max(0.0, float(self.prefetch_margin_fraction)),
                prefetch_limit=None,
                visible_items_factor=visible_items_factor,
                visible_items=visible_items,
                density=normalized_density,
            )
        visible_limit = self.visible_limit_for_size_and_count(
            thumbnail_size_px,
            visible_items=visible_items,
            density=normalized_density,
        )
        prefetch_limit = int(round(float(visible_limit) * max(1.0, float(self.prefetch_budget_multiplier))))
        return ThumbnailRequestPolicy(
            tier="scaled",
            limit=visible_limit,
            candidate_scan_limit=None,
            grid_cell_px=grid_cell_px,
            prefetch_margin_fraction=max(0.0, float(self.prefetch_margin_fraction)),
            prefetch_limit=max(visible_limit, prefetch_limit),
            visible_items_factor=visible_items_factor,
            visible_items=visible_items,
            density=normalized_density,
        )

    def native_draw_limits(
        self,
        *,
        canvas_size: tuple[float, float] = (1000.0, 800.0),
        thumbnail_size_px: float | None = None,
        density: float | None = None,
    ) -> tuple[int, int]:
        _ = canvas_size
        if self.render_every_thumbnail:
            return (MAX_NATIVE_THUMBNAIL_BUDGET, MAX_NATIVE_THUMBNAIL_BUDGET)
        size_params = self.parameters_for_thumbnail_size(thumbnail_size_px)
        normalized_density = self.default_density() if density is None else self.normalized_density(density)
        return (
            max(1, int(round(float(size_params.max_visible_many_items) * normalized_density))),
            max(1, int(round(float(size_params.max_visible_few_items) * normalized_density))),
        )

    def visible_limit_for_size_and_count(
        self,
        thumbnail_size_px: float | None = None,
        *,
        visible_items: int | None = None,
        density: float | None = None,
    ) -> int:
        size_params = self.parameters_for_thumbnail_size(thumbnail_size_px)
        visible_factor = self.visible_items_factor_for_count(visible_items, thumbnail_size_px=thumbnail_size_px)
        many_limit = max(0, int(size_params.max_visible_many_items))
        few_limit = max(many_limit, int(size_params.max_visible_few_items))
        base_limit = round(float(many_limit) + (float(few_limit - many_limit) * visible_factor))
        normalized_density = self.default_density() if density is None else self.normalized_density(density)
        limit = round(float(base_limit) * normalized_density)
        if visible_items is not None:
            if int(visible_items) <= 0:
                return 0
            limit = min(limit, max(0, int(visible_items)))
        return max(1, int(limit))

    def visible_limit_for_zoom_and_size(
        self,
        zoom_px_per_world: float,
        thumbnail_size_px: float | None = None,
        *,
        visible_items: int | None = None,
        density: float | None = None,
    ) -> int:
        _ = zoom_px_per_world
        return self.visible_limit_for_size_and_count(
            thumbnail_size_px,
            visible_items=visible_items,
            density=density,
        )

    def zoom_thresholds_for_thumbnail_size(self, thumbnail_size_px: float | None = None) -> tuple[float, float]:
        size_params = self.parameters_for_thumbnail_size(thumbnail_size_px)
        far_value = float(size_params.zoom_far_threshold)
        close_value = max(far_value + 1e-9, float(size_params.zoom_close_threshold))
        return far_value, close_value

    def parameters_for_thumbnail_size(self, thumbnail_size_px: float | None = None) -> ThumbnailSizeParameters:
        target = self._nearest_configured_thumbnail_size(thumbnail_size_px)
        for parameters in self.size_parameters:
            if int(parameters.size_px) == target:
                return parameters
        return ThumbnailSizeParameters(
            size_px=int(self.reference_thumbnail_px),
            zoom_far_threshold=float(self.zoom_mid_threshold),
            zoom_close_threshold=float(self.zoom_close_threshold),
            max_visible_many_items=100,
            max_visible_few_items=1000,
            visible_items_many_threshold=100_000,
            visible_items_few_threshold=1_000,
        )

    def _nearest_configured_thumbnail_size(self, thumbnail_size_px: float | None = None) -> int:
        try:
            target = int(round(float(thumbnail_size_px or self.reference_thumbnail_px)))
        except (TypeError, ValueError):
            target = int(self.reference_thumbnail_px)
        configured = tuple(int(parameters.size_px) for parameters in self.size_parameters)
        if not configured:
            return target
        return min(configured, key=lambda size_px: abs(size_px - target))

    def visible_items_factor_for_count(
        self,
        visible_items: int | None = None,
        *,
        thumbnail_size_px: float | None = None,
    ) -> float:
        if visible_items is None:
            return 0.0
        size_params = self.parameters_for_thumbnail_size(thumbnail_size_px)
        few = max(0.0, float(size_params.visible_items_few_threshold))
        many = max(few + 1.0, float(size_params.visible_items_many_threshold))
        value = (many - float(max(0, int(visible_items)))) / (many - few)
        return max(0.0, min(1.0, value))

    @staticmethod
    def normalized_density(density: float | None = 1.0) -> float:
        try:
            value = float(density)
        except (TypeError, ValueError):
            value = 1.0
        return max(0.1, min(4.0, value))

    def default_density(self) -> float:
        return self.normalized_density(self.thumbnail_density)

THUMBNAIL_CONFIG = ThumbnailRenderConfig(
    render_every_thumbnail=RENDER_EVERY_THUMBNAIL,
    size_parameters=DEFAULT_THUMBNAIL_SIZE_PARAMETERS,
    thumbnail_density=THUMBNAIL_DENSITY,
    prefetch_margin_fraction=THUMBNAIL_PREFETCH_MARGIN_FRACTION,
    prefetch_budget_multiplier=THUMBNAIL_PREFETCH_BUDGET_MULTIPLIER,
    zoom_mid_threshold=30.0,
    zoom_close_threshold=1500.0,
    reference_thumbnail_px=64,
    planning_grid_divisions=96,
    painted_draw_limit_detail=1200,
    eviction_keep_scan_limit=200000,
    request_debounce_ms=150,
    rolling_request_interval_ms=100,
    push_interval_ms=30,
    push_batch_size=256,
    decode_cache_mb=128,
    decode_workers=2,
    sticky_metadata_limit=50000,
    sticky_metadata_trim_to=30000,
    tile_size_px=512,
    tile_max_lod=7,
    tile_cache_mb=512,
    tile_decode_workers=2,
    tile_max_decode_jobs=8,
    tile_min_thumbnails_per_tile=None,
    tile_max_thumbnails_per_tile=None,
)
