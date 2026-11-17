"""Cache Agg point layers without changing the authoritative figure layout."""
from __future__ import annotations

from collections import OrderedDict
import json
import math
import hashlib
import zlib
import sys

import numpy as np

from matplotlib.artist import Artist
from matplotlib.transforms import Bbox


class _PointRegion:
    """Lossless Agg pixels, compressed when smaller than the original region."""

    def __init__(self, region):
        pixels = memoryview(region)
        encoded = zlib.compress(pixels, level=1)
        self.compressed = len(encoded) < pixels.nbytes
        # An incompressible layer keeps its original region buffer, avoiding
        # another raw-sized copy on top of the attempted compressed result.
        self.data = encoded if self.compressed else pixels.cast('B').toreadonly()

    def restore(self, renderer, rectangle):
        x, y, width, height = rectangle
        data = zlib.decompress(self.data) if self.compressed else self.data
        # Copy the original RGBA bytes, rather than alpha-compositing them a
        # second time. Regions include the validated underlying background.
        pixels = np.frombuffer(data, dtype=np.uint8).reshape(height, width, 4)
        target = np.asarray(renderer.buffer_rgba())
        top = int(renderer.height) - y - height
        target[top:top + height, x:x + width] = pixels


class PointPreviewCache:
    def __init__(self, max_bytes: int = 128 * 1024 * 1024, *, max_entries: int = 512) -> None:
        self.max_bytes = max_bytes
        self.max_entries = max(0, int(max_entries))
        self._entries = OrderedDict()
        self._bytes = 0
        self.hits = 0
        self.misses = 0

    def add(self, axes, cell, settings, create_collections) -> None:
        bounds = axes.bbox
        # Agg's antialiased clip edge can touch the pixel beyond the axes
        # rectangle. Include that fringe, bounded by the actual canvas.
        x, y = max(0, math.floor(bounds.x0) - 2), max(0, math.floor(bounds.y0) - 2)
        width = min(int(axes.figure.bbox.width), math.ceil(bounds.x1) + 2) - x
        height = min(int(axes.figure.bbox.height), math.ceil(bounds.y1) + 2) - y
        state = {name: cell.render_state.get(name) for name in ("appearance", "filters", "slicer_selection")}
        key = (
            cell.data_key or cell.data_ref,
            json.dumps(state, sort_keys=True, separators=(",", ":")),
            settings.point_size, settings.point_size_factor_percent,
            settings.grid_mode, settings.grid_line_width, settings.grid_cell_size_mm,
            tuple(axes.get_xlim()), tuple(axes.get_ylim()), axes.figure.dpi,
            width, height, tuple(round(v, 6) for v in (bounds.x0-x, bounds.y0-y, bounds.width, bounds.height)),
        )
        # Unidentified data is deliberately never shared between cells.
        if not key[0]:
            create_collections()
            return
        entry = self._entry(key)
        if entry is not None:
            collections = []
        else:
            collections = create_collections()
            for collection in collections:
                collection.remove()
                collection.axes = axes
                collection.set_figure(axes.figure)
        axes.add_artist(_CachedPoints(self, key, entry, collections, (x, y, width, height), create_collections))

    def _entry(self, key):
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._entries[key] = entry
            self.hits += 1
        else:
            self.misses += 1
        return entry

    def remember(self, key, region, background) -> None:
        # Count the large serialized style keys as well as encoded pixels.
        size = len(region.data) + sys.getsizeof(key) + sum(sys.getsizeof(part) for part in key)
        if size > self.max_bytes:
            return
        old = self._entries.pop(key, None)
        if old is not None:
            self._bytes -= old[1]
        self._entries[key] = (region, size, background)
        self._bytes += size
        while self._bytes > self.max_bytes or len(self._entries) > self.max_entries:
            _, removed = self._entries.popitem(last=False)
            self._bytes -= removed[1]

    def bind_data(self, axes, create_collections) -> None:
        for artist in axes.artists:
            if isinstance(artist, _CachedPoints):
                artist._create_collections = create_collections
                entry = self._entry(artist._key)
                artist._region = entry[0] if entry else None
                artist._background = entry[2] if entry else None

    @staticmethod
    def release_data(axes) -> None:
        for artist in axes.artists:
            if isinstance(artist, _CachedPoints):
                artist._collections = []
                artist._create_collections = None
                # The LRU is the sole owner of idle raster layers. A retained
                # page must not keep evicted regions beyond its byte budget.
                artist._region = None
                artist._background = None


class _CachedPoints(Artist):
    zorder = 1

    def __init__(self, cache, key, entry, collections, rectangle, create_collections):
        super().__init__()
        self._cache, self._key = cache, key
        self._region = entry[0] if entry else None
        self._background = entry[2] if entry else None
        self._collections = collections
        self._rectangle = rectangle
        self._create_collections = create_collections

    def draw(self, renderer):
        x, y, width, height = self._rectangle
        bounds = Bbox.from_bounds(x, y, width, height)
        # Pins may extend from an earlier subplot underneath this plot. Never
        # restore an opaque region over changed underlying content.
        background = hashlib.blake2b(np.asarray(renderer.copy_from_bbox(bounds)), digest_size=16).digest()
        if self._region is not None and background != self._background:
            self._region = None
        if self._region is None:
            if not self._collections:
                self._collections = self._create_collections()
                for collection in self._collections:
                    axes = collection.axes
                    figure = collection.figure
                    collection.remove()
                    collection.axes = axes
                    collection.set_figure(figure)
            for collection in self._collections:
                collection.draw(renderer)
            self._region = _PointRegion(renderer.copy_from_bbox(bounds))
            self._background = background
            self._cache.remember(self._key, self._region, background)
        else:
            self._region.restore(renderer, self._rectangle)
        self.stale = False
