from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import datetime as dt
from dataclasses import dataclass
import hashlib
import json
import math
import threading
from typing import Callable, Iterable

import numpy as np
from PIL import Image

from DiscoveryArea.thumbnail_config import THUMBNAIL_CONFIG
from DiscoveryArea.thumbnails.repository import (
    ThumbnailTilePage,
    ThumbnailTilePyramid,
    ThumbnailVariantRepository,
)


TILE_SIZE_PX = THUMBNAIL_CONFIG.tile_size_px
DEFAULT_MAX_LOD = THUMBNAIL_CONFIG.tile_max_lod
TILE_RENDER_POLICY_VERSION = "tile-v6-dynamic-size"
BASE_THUMBNAIL_TILE_PX = 4
MIN_THUMBNAIL_BUDGET_MULTIPLIER = 0.25
MAX_THUMBNAIL_BUDGET_MULTIPLIER = 8.0
THUMBNAIL_SIZE_MIN_PX = 16
THUMBNAIL_SIZE_MAX_PX = 256
THUMBNAIL_SIZE_STEP_PX = 16


@dataclass(frozen=True)
class TileKey:
    lod: int
    tile_x: int
    tile_y: int


@dataclass(frozen=True)
class TilePlan:
    lod: int
    visible_tiles: tuple[TileKey, ...]
    parent_tiles: tuple[TileKey, ...]
    request_tiles: tuple[TileKey, ...]
    bounds: tuple[float, float, float, float]


@dataclass(frozen=True)
class TileReady:
    variant_id: str
    discovery_cache_id: str
    lod: int
    tile_x: int
    tile_y: int
    path: str
    mtime_ns: int
    bounds: tuple[float, float, float, float]
    tile_size_px: int


class ThumbnailTilePlanner:
    def __init__(self, *, tile_size_px: int = TILE_SIZE_PX, max_lod: int = DEFAULT_MAX_LOD) -> None:
        self._tile_size_px = max(64, int(tile_size_px))
        self._max_lod = max(0, int(max_lod))

    def lod_for_viewport(
        self,
        *,
        viewport: tuple[float, float, float, float],
        canvas_size: tuple[int, int],
        bounds: tuple[float, float, float, float],
        zooming: bool,
    ) -> int:
        x_min, x_max, _y_min, _y_max = viewport
        bounds_x_min, bounds_x_max, _bounds_y_min, _bounds_y_max = bounds
        viewport_span_x = max(1e-9, float(x_max - x_min))
        bounds_span_x = max(1e-9, float(bounds_x_max - bounds_x_min))
        zoom_px_per_world = max(1.0, float(canvas_size[0]) / viewport_span_x)
        desired_tiles = max(1.0, (bounds_span_x * zoom_px_per_world) / float(self._tile_size_px))
        lod = int(math.ceil(math.log2(desired_tiles)))
        if zooming:
            lod -= 1
        return max(0, min(self._max_lod, lod))

    def plan(
        self,
        *,
        viewport: tuple[float, float, float, float],
        canvas_size: tuple[int, int],
        bounds: tuple[float, float, float, float],
        zooming: bool,
        margin_tiles: int = 1,
    ) -> TilePlan:
        lod = self.lod_for_viewport(
            viewport=viewport,
            canvas_size=canvas_size,
            bounds=bounds,
            zooming=zooming,
        )
        visible = self._tiles_for_viewport(viewport, bounds, lod, margin=0)
        requested = self._tiles_for_viewport(viewport, bounds, lod, margin=margin_tiles)
        parents = self._parent_tiles(visible)
        return TilePlan(
            lod=lod,
            visible_tiles=visible,
            parent_tiles=parents,
            request_tiles=self._prioritized_tiles(requested, viewport, bounds, lod),
            bounds=bounds,
        )

    def tile_bounds(self, bounds: tuple[float, float, float, float], key: TileKey) -> tuple[float, float, float, float]:
        x_min, x_max, y_min, y_max = bounds
        grid = 1 << max(0, int(key.lod))
        tile_w = (x_max - x_min) / float(grid)
        tile_h = (y_max - y_min) / float(grid)
        left = x_min + tile_w * float(key.tile_x)
        right = left + tile_w
        bottom = y_min + tile_h * float(key.tile_y)
        top = bottom + tile_h
        return (left, right, bottom, top)

    def _tiles_for_viewport(
        self,
        viewport: tuple[float, float, float, float],
        bounds: tuple[float, float, float, float],
        lod: int,
        *,
        margin: int,
    ) -> tuple[TileKey, ...]:
        vx_min, vx_max, vy_min, vy_max = viewport
        bx_min, bx_max, by_min, by_max = bounds
        grid = 1 << max(0, int(lod))
        span_x = max(1e-9, bx_max - bx_min)
        span_y = max(1e-9, by_max - by_min)
        min_x = math.floor(((vx_min - bx_min) / span_x) * grid) - margin
        max_x = math.floor(((vx_max - bx_min) / span_x) * grid) + margin
        min_y = math.floor(((vy_min - by_min) / span_y) * grid) - margin
        max_y = math.floor(((vy_max - by_min) / span_y) * grid) + margin
        min_x = max(0, min(grid - 1, min_x))
        max_x = max(0, min(grid - 1, max_x))
        min_y = max(0, min(grid - 1, min_y))
        max_y = max(0, min(grid - 1, max_y))
        return tuple(
            TileKey(lod=lod, tile_x=x, tile_y=y)
            for y in range(min_y, max_y + 1)
            for x in range(min_x, max_x + 1)
        )

    def _parent_tiles(self, tiles: Iterable[TileKey]) -> tuple[TileKey, ...]:
        parents: list[TileKey] = []
        seen: set[TileKey] = set()
        for tile in tiles:
            lod = int(tile.lod)
            x = int(tile.tile_x)
            y = int(tile.tile_y)
            while lod > 0:
                lod -= 1
                x //= 2
                y //= 2
                parent = TileKey(lod=lod, tile_x=x, tile_y=y)
                if parent in seen:
                    continue
                seen.add(parent)
                parents.append(parent)
        return tuple(parents)

    def _prioritized_tiles(
        self,
        tiles: tuple[TileKey, ...],
        viewport: tuple[float, float, float, float],
        bounds: tuple[float, float, float, float],
        lod: int,
    ) -> tuple[TileKey, ...]:
        cx = (viewport[0] + viewport[1]) * 0.5
        cy = (viewport[2] + viewport[3]) * 0.5

        def score(tile: TileKey) -> float:
            left, right, bottom, top = self.tile_bounds(bounds, tile)
            tx = (left + right) * 0.5
            ty = (bottom + top) * 0.5
            return (tx - cx) * (tx - cx) + (ty - cy) * (ty - cy)

        return tuple(sorted(tiles, key=lambda tile: (0 if tile.lod == lod else 1, score(tile))))


class ThumbnailTileService:
    """Builds and resolves durable discovery thumbnail tiles without Qt dependencies."""

    def __init__(
        self,
        repo: ThumbnailVariantRepository,
        *,
        max_workers: int = THUMBNAIL_CONFIG.tile_decode_workers,
        tile_size_px: int = TILE_SIZE_PX,
        max_lod: int = DEFAULT_MAX_LOD,
        min_thumbnails_per_tile: int | None = THUMBNAIL_CONFIG.tile_min_thumbnails_per_tile,
        max_thumbnails_per_tile: int | None = THUMBNAIL_CONFIG.tile_max_thumbnails_per_tile,
        thumbnail_size_px: int = 64,
    ) -> None:
        self._repo = repo
        self._tile_size_px = max(64, int(tile_size_px))
        self._max_lod = max(0, int(max_lod))
        self._thumbnail_budget_range = self._normalize_thumbnail_budget_range(
            min_thumbnails_per_tile,
            max_thumbnails_per_tile,
        )
        self._thumbnail_size_px = self._normalize_thumbnail_size_px(thumbnail_size_px)
        self._planner = ThumbnailTilePlanner(tile_size_px=self._tile_size_px, max_lod=self._max_lod)
        self._executor = ThreadPoolExecutor(max_workers=max(1, int(max_workers)), thread_name_prefix="discovery-tile")
        self._callbacks: list[Callable[[TileReady | None], None]] = []
        self._in_flight: set[tuple[str, str, int, int, int]] = set()
        self._lock = threading.Lock()
        self._points = np.empty((0, 2), dtype=np.float32)
        self._metadata_ids = np.empty((0,), dtype=np.int64)
        self._visibility = np.empty((0,), dtype=np.float32)
        self._variant_id = "none"
        self._discovery_cache_id = ""
        self._fingerprint = ""
        self._bounds = (0.0, 1.0, 0.0, 1.0)

    @property
    def planner(self) -> ThumbnailTilePlanner:
        return self._planner

    def subscribe_tile_ready(self, callback: Callable[[TileReady | None], None]) -> None:
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def set_max_thumbnails_per_tile(self, limit: int | None) -> None:
        self._thumbnail_budget_range = self._normalize_thumbnail_budget_range(None, limit)

    def set_thumbnail_budget_range(self, min_limit: int | None, max_limit: int | None) -> None:
        self._thumbnail_budget_range = self._normalize_thumbnail_budget_range(min_limit, max_limit)

    def set_thumbnail_size_px(self, size_px: int | float | None) -> None:
        self._thumbnail_size_px = self._normalize_thumbnail_size_px(size_px)

    def configure_discovery(
        self,
        *,
        variant_id: str,
        discovery_cache_id: str,
        fingerprint: str,
        points: np.ndarray,
        metadata_ids: np.ndarray,
        visibility: np.ndarray,
        bounds: tuple[float, float, float, float],
    ) -> None:
        self._variant_id = str(variant_id or "none")
        self._discovery_cache_id = str(discovery_cache_id)
        variant_fingerprint = self._repo.variant_items_fingerprint(self._variant_id)
        tile_budget = self._thumbnail_budget_fingerprint(self._thumbnail_budget_range)
        self._fingerprint = (
            f"{fingerprint}:{variant_fingerprint}:tile_budget={tile_budget}:thumb_size={self._thumbnail_size_px}"
            f":render_all={int(THUMBNAIL_CONFIG.render_every_thumbnail)}"
        )
        self._points = np.asarray(points[:, :2], dtype=np.float32).copy()
        self._metadata_ids = np.asarray(metadata_ids, dtype=np.int64).reshape(-1).copy()
        self._visibility = np.asarray(visibility, dtype=np.float32).reshape(-1).copy()
        self._bounds = tuple(float(value) for value in bounds)
        self._ensure_current_pyramid()

    def plan(
        self,
        *,
        viewport: tuple[float, float, float, float],
        canvas_size: tuple[int, int],
        zooming: bool,
    ) -> TilePlan:
        return self._planner.plan(
            viewport=viewport,
            canvas_size=canvas_size,
            bounds=self._bounds,
            zooming=zooming,
        )

    def available_tiles(self, keys: Iterable[TileKey]) -> list[TileReady]:
        pages = self._repo.list_tile_pages(
            self._variant_id,
            self._discovery_cache_id,
            [(key.lod, key.tile_x, key.tile_y) for key in keys],
        )
        output: list[TileReady] = []
        for page in pages:
            path = self._repo.thumbnails_root / page.relative_path
            if not path.exists():
                continue
            output.append(self._ready_from_page(page))
        return output

    def request_tiles(self, keys: Iterable[TileKey]) -> None:
        key_list = list(keys)
        if not key_list:
            return
        existing_pages = self._repo.list_tile_pages(
            self._variant_id,
            self._discovery_cache_id,
            [(key.lod, key.tile_x, key.tile_y) for key in key_list],
        )
        existing_by_key = {
            (int(page.lod), int(page.tile_x), int(page.tile_y)): page
            for page in existing_pages
        }
        for key in key_list:
            page = existing_by_key.get((int(key.lod), int(key.tile_x), int(key.tile_y)))
            if page is not None:
                continue
            flight_key = (
                self._variant_id,
                self._discovery_cache_id,
                int(key.lod),
                int(key.tile_x),
                int(key.tile_y),
            )
            with self._lock:
                if flight_key in self._in_flight:
                    continue
                self._in_flight.add(flight_key)
            self._executor.submit(self._build_tile_and_publish, key, flight_key)

    def ensure_tiles(self, keys: Iterable[TileKey]) -> list[TileReady]:
        key_list = list(keys)
        if not key_list:
            return []
        existing_pages = self._repo.list_tile_pages(
            self._variant_id,
            self._discovery_cache_id,
            [(key.lod, key.tile_x, key.tile_y) for key in key_list],
        )
        existing_by_key = {
            (int(page.lod), int(page.tile_x), int(page.tile_y)): page
            for page in existing_pages
        }
        output: list[TileReady] = []
        for key in key_list:
            page = existing_by_key.get((int(key.lod), int(key.tile_x), int(key.tile_y)))
            if page is None:
                page = self._build_tile(key)
            if page is not None:
                output.append(self._ready_from_page(page))
        return output

    def _ensure_current_pyramid(self) -> None:
        if self._variant_id == "none" or not self._discovery_cache_id:
            return
        existing = self._repo.resolve_tile_pyramid(self._variant_id, self._discovery_cache_id)
        if existing is not None and existing.fingerprint != self._fingerprint:
            self._repo.invalidate_tile_pyramid(self._variant_id, self._discovery_cache_id)
            existing = None
        if existing is not None:
            return
        pyramid = ThumbnailTilePyramid(
            variant_id=self._variant_id,
            discovery_cache_id=self._discovery_cache_id,
            fingerprint=self._fingerprint,
            bounds_json=json.dumps(
                {
                    "x_min": self._bounds[0],
                    "x_max": self._bounds[1],
                    "y_min": self._bounds[2],
                    "y_max": self._bounds[3],
                },
                sort_keys=True,
            ),
            tile_size_px=self._tile_size_px,
            max_lod=self._max_lod,
            created_at=dt.datetime.now().isoformat(timespec="seconds"),
        )
        self._repo.save_tile_pyramid(pyramid)

    def _build_tile_and_publish(
        self,
        key: TileKey,
        flight_key: tuple[str, str, int, int, int],
    ) -> None:
        try:
            try:
                page = self._build_tile(key)
            except Exception:
                page = None
            self._emit(None if page is None else self._ready_from_page(page))
        finally:
            with self._lock:
                self._in_flight.discard(flight_key)

    def _build_tile(self, key: TileKey) -> ThumbnailTilePage | None:
        if self._variant_id == "none" or not self._discovery_cache_id or self._points.size == 0:
            return None
        tile_bounds = self._planner.tile_bounds(self._bounds, key)
        image = self._render_tile_image(key, tile_bounds)
        output_path = self._repo.absolute_tile_path(
            self._variant_id,
            self._discovery_cache_id,
            key.lod,
            key.tile_x,
            key.tile_y,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(output_path, "PNG", optimize=False)
        image.close()
        page = ThumbnailTilePage(
            variant_id=self._variant_id,
            discovery_cache_id=self._discovery_cache_id,
            lod=int(key.lod),
            tile_x=int(key.tile_x),
            tile_y=int(key.tile_y),
            relative_path=str(output_path.relative_to(self._repo.thumbnails_root)),
            mtime_ns=int(output_path.stat().st_mtime_ns),
            status="ok",
        )
        self._repo.save_tile_page(page)
        return page

    def _render_tile_image(
        self,
        key: TileKey,
        tile_bounds: tuple[float, float, float, float],
    ) -> Image.Image:
        tile = Image.new("RGBA", (self._tile_size_px, self._tile_size_px), (0, 0, 0, 0))
        x_min, x_max, y_min, y_max = tile_bounds
        span_x = max(1e-9, x_max - x_min)
        span_y = max(1e-9, y_max - y_min)
        in_tile = (
            (self._points[:, 0] >= x_min)
            & (self._points[:, 0] <= x_max)
            & (self._points[:, 1] >= y_min)
            & (self._points[:, 1] <= y_max)
        )
        if self._visibility.shape[0] == self._points.shape[0]:
            in_tile = in_tile & (self._visibility > 0.0)
        indices = np.where(in_tile)[0]
        if indices.size == 0:
            return tile
        target_px = self._target_px_for_lod(key.lod, thumbnail_size_px=self._thumbnail_size_px)
        draw_px = self._draw_px_for_lod(key.lod, thumbnail_size_px=self._thumbnail_size_px)
        cell_px = max(24, int(draw_px * 0.9))
        thumbnail_limit = self._thumbnail_limit_for_lod(key.lod)
        use_cell_sampling = thumbnail_limit is None and not THUMBNAIL_CONFIG.render_every_thumbnail
        occupied: set[tuple[int, int]] = set()
        rendered_count = 0
        for index in indices.tolist():
            if thumbnail_limit is not None and rendered_count >= thumbnail_limit:
                break
            if index >= self._metadata_ids.shape[0]:
                continue
            point = self._points[index]
            sx = int(round(((float(point[0]) - x_min) / span_x) * self._tile_size_px))
            sy = int(round(self._tile_size_px - (((float(point[1]) - y_min) / span_y) * self._tile_size_px)))
            cell = (sx // cell_px, sy // cell_px)
            if use_cell_sampling and cell in occupied:
                continue
            if use_cell_sampling:
                occupied.add(cell)
            metadata_id = int(self._metadata_ids[index])
            item = self._repo.resolve_item(self._variant_id, metadata_id, target_px=target_px)
            if item is None:
                continue
            path = self._repo.thumbnails_root / item.relative_path
            if not path.exists():
                continue
            try:
                with Image.open(path) as thumb:
                    thumb.load()
                    thumb_rgba = thumb.convert("RGBA")
                    thumb_rgba.thumbnail((draw_px, draw_px), Image.Resampling.LANCZOS)
                    px = int(sx - thumb_rgba.width * 0.5)
                    py = int(sy - thumb_rgba.height * 0.5)
                    tile.alpha_composite(thumb_rgba, (px, py))
                    thumb_rgba.close()
                    rendered_count += 1
            except Exception:
                continue
        return tile

    def _thumbnail_limit_for_lod(self, lod: int) -> int | None:
        if self._thumbnail_budget_range is None:
            return None
        min_limit, max_limit = self._thumbnail_budget_range
        multiplier = self._thumbnail_budget_multiplier_for_lod(lod)
        span = MAX_THUMBNAIL_BUDGET_MULTIPLIER - MIN_THUMBNAIL_BUDGET_MULTIPLIER
        weight = (multiplier - MIN_THUMBNAIL_BUDGET_MULTIPLIER) / span if span > 0.0 else 1.0
        limit = float(min_limit) + (float(max_limit - min_limit) * max(0.0, min(1.0, weight)))
        return max(1, int(round(limit)))

    def _thumbnail_budget_multiplier_for_lod(self, lod: int) -> float:
        draw_px = ThumbnailTileService._draw_px_for_lod(
            lod,
            thumbnail_size_px=int(getattr(self, "_thumbnail_size_px", 64)),
        )
        if draw_px <= 0:
            return 1.0
        display_multiplier = (64.0 / float(draw_px)) ** 2
        return max(MIN_THUMBNAIL_BUDGET_MULTIPLIER, min(MAX_THUMBNAIL_BUDGET_MULTIPLIER, display_multiplier))

    @staticmethod
    def _normalize_thumbnail_budget_range(
        min_limit: int | None,
        max_limit: int | None,
    ) -> tuple[int, int] | None:
        if min_limit is None and max_limit is None:
            return None
        if min_limit is None:
            base = max(1, int(max_limit if max_limit is not None else 1))
            min_limit = int(round(float(base) * MIN_THUMBNAIL_BUDGET_MULTIPLIER))
            max_limit = int(round(float(base) * MAX_THUMBNAIL_BUDGET_MULTIPLIER))
        if max_limit is None:
            max_limit = min_limit
        low = max(1, int(min_limit))
        high = max(1, int(max_limit))
        if high < low:
            low, high = high, low
        return (low, high)

    @staticmethod
    def _thumbnail_budget_fingerprint(budget_range: tuple[int, int] | None) -> str:
        if budget_range is None:
            return "unlimited"
        return f"{budget_range[0]}:{budget_range[1]}"

    @staticmethod
    def _target_px_for_lod(lod: int, *, thumbnail_size_px: int = 64) -> int:
        _ = lod
        return ThumbnailTileService._draw_px_for_lod(lod, thumbnail_size_px=thumbnail_size_px)

    @staticmethod
    def _draw_px_for_lod(lod: int, *, thumbnail_size_px: int = 64) -> int:
        _ = lod
        detail_size = float(ThumbnailTileService._normalize_thumbnail_size_px(thumbnail_size_px))
        return int(round(max(1.0, min(float(TILE_SIZE_PX) * 0.5, detail_size))))

    @staticmethod
    def _normalize_thumbnail_size_px(size_px: int | float | None) -> int:
        try:
            numeric = int(round(float(size_px)))
        except (TypeError, ValueError):
            numeric = 64
        rounded = int(((numeric + THUMBNAIL_SIZE_STEP_PX // 2) // THUMBNAIL_SIZE_STEP_PX) * THUMBNAIL_SIZE_STEP_PX)
        return max(THUMBNAIL_SIZE_MIN_PX, min(THUMBNAIL_SIZE_MAX_PX, rounded))

    def _ready_from_page(self, page: ThumbnailTilePage) -> TileReady:
        key = TileKey(page.lod, page.tile_x, page.tile_y)
        return TileReady(
            variant_id=page.variant_id,
            discovery_cache_id=page.discovery_cache_id,
            lod=int(page.lod),
            tile_x=int(page.tile_x),
            tile_y=int(page.tile_y),
            path=str(self._repo.thumbnails_root / page.relative_path),
            mtime_ns=int(page.mtime_ns),
            bounds=self._planner.tile_bounds(self._bounds, key),
            tile_size_px=self._tile_size_px,
        )

    def _emit(self, payload: TileReady | None) -> None:
        for callback in tuple(self._callbacks):
            callback(payload)


def discovery_tile_cache_id(
    points: np.ndarray,
    metadata_ids: np.ndarray,
    visibility: np.ndarray | None = None,
    min_thumbnails_per_tile: int | None = None,
    max_thumbnails_per_tile: int | None = None,
    thumbnail_size_px: int | float = 64,
) -> tuple[str, str]:
    point_array = np.ascontiguousarray(np.asarray(points[:, :2], dtype=np.float32))
    id_array = np.ascontiguousarray(np.asarray(metadata_ids, dtype=np.int64).reshape(-1))
    digest = hashlib.sha1()
    digest.update(point_array.shape[0].to_bytes(8, "little", signed=False))
    digest.update(TILE_RENDER_POLICY_VERSION.encode("ascii"))
    budget_range = ThumbnailTileService._normalize_thumbnail_budget_range(
        min_thumbnails_per_tile,
        max_thumbnails_per_tile,
    )
    digest.update(f":tile_budget={ThumbnailTileService._thumbnail_budget_fingerprint(budget_range)}".encode("ascii"))
    digest.update(f":thumb_size={ThumbnailTileService._normalize_thumbnail_size_px(thumbnail_size_px)}".encode("ascii"))
    digest.update(f":render_all={int(THUMBNAIL_CONFIG.render_every_thumbnail)}".encode("ascii"))
    digest.update(point_array.tobytes())
    digest.update(id_array.tobytes())
    if visibility is not None:
        visibility_array = np.ascontiguousarray(np.asarray(visibility, dtype=np.float32).reshape(-1))
        digest.update(visibility_array.tobytes())
    fingerprint = digest.hexdigest()
    return fingerprint[:16], fingerprint


def discovery_bounds(points: np.ndarray) -> tuple[float, float, float, float]:
    point_array = np.asarray(points[:, :2], dtype=np.float32)
    if point_array.size == 0:
        return (0.0, 1.0, 0.0, 1.0)
    x_min = float(np.nanmin(point_array[:, 0]))
    x_max = float(np.nanmax(point_array[:, 0]))
    y_min = float(np.nanmin(point_array[:, 1]))
    y_max = float(np.nanmax(point_array[:, 1]))
    if abs(x_max - x_min) < 1e-9:
        x_max = x_min + 1.0
    if abs(y_max - y_min) < 1e-9:
        y_max = y_min + 1.0
    pad_x = max(1e-6, (x_max - x_min) * 0.05)
    pad_y = max(1e-6, (y_max - y_min) * 0.05)
    return (x_min - pad_x, x_max + pad_x, y_min - pad_y, y_max + pad_y)
