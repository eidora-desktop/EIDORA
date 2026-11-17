"""Redraw affected artist groups on the existing opaque Agg page."""
from __future__ import annotations

import math

import numpy as np
from matplotlib.axes import Axes


def _intersects(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _merge(rectangles):
    merged = []
    for rectangle in rectangles:
        index = 0
        while index < len(merged):
            other = merged[index]
            if _intersects(rectangle, other):
                rectangle = (min(rectangle[0], other[0]), min(rectangle[1], other[1]),
                             max(rectangle[2], other[2]), max(rectangle[3], other[3]))
                merged.pop(index)
                index = 0
            else:
                index += 1
        merged.append(rectangle)
    return merged


class PartialPage:
    """Retain signatures/bounds only; the canvas remains the sole pixel owner."""

    def __init__(self):
        self.clear()

    def clear(self):
        self._layout = None
        self._groups = {}
        self.last_draw = {'partial': False, 'groups': 0, 'pixels': 0}

    @staticmethod
    def _bounds(artist, renderer, width, height, dpi):
        bbox = artist.get_tightbbox(renderer)
        if bbox is None or not np.isfinite(bbox.extents).all():
            raise ValueError('Unbounded preview artist')
        # Include stroke widths, italic glyph overhangs and Agg's clip fringe.
        # Unsupported effects use a complete redraw rather than guessed bounds.
        padding = 3.0
        for child in artist.findobj():
            if child.get_path_effects() or child.get_agg_filter() is not None:
                raise ValueError('Unbounded preview effect')
            if hasattr(child, 'get_linewidth'):
                widths = np.asarray(child.get_linewidth())
                if widths.size:
                    padding = max(padding, float(np.max(widths)) * dpi / 144 + 2)
            if hasattr(child, 'get_fontsize'):
                padding = max(padding, child.get_fontsize() * dpi / 72 * .25 + 2)
        x0, y0, x1, y1 = bbox.extents
        if isinstance(artist, Axes):
            # Cached point artists have no standalone window extent.
            x0, y0 = min(x0, artist.bbox.x0), min(y0, artist.bbox.y0)
            x1, y1 = max(x1, artist.bbox.x1), max(y1, artist.bbox.y1)
        return (min(width, max(0, math.floor(x0-padding))), min(height, max(0, math.floor(y0-padding))),
                max(0, min(width, math.ceil(x1+padding))), max(0, min(height, math.ceil(y1+padding))))

    def draw(self, figure, layout, groups):
        """Groups are (stable identity, immutable signature, current artist)."""
        canvas = figure.canvas
        renderer = canvas.get_renderer()
        width, height = int(renderer.width), int(renderer.height)
        background = figure.patch.get_facecolor()
        layout = (layout, tuple(background), figure.patch.get_edgecolor(), figure.patch.get_linewidth(),
                  tuple((identity, artist.get_zorder()) for identity, _, artist in groups))
        compatible = layout == self._layout
        current = {}
        safe = (background[3] == 1 and figure.patch.get_linewidth() == 0
                and figure.get_layout_engine() is None)
        supported = {id(artist) for _, _, artist in groups}
        safe = safe and all(id(artist) in supported for artist in figure.get_children()
                            if artist is not figure.patch and artist.get_visible())
        safe = safe and not any(artist.get_animated() for _, _, artist in groups)
        safe = safe and len({identity for identity, _, _ in groups}) == len(groups)
        for identity, signature, artist in groups:
            safe = safe and not artist.get_path_effects() and artist.get_agg_filter() is None
            previous = self._groups.get(identity) if compatible else None
            try:
                bounds = previous[1] if previous and signature == previous[0] else self._bounds(
                    artist, renderer, width, height, figure.dpi)
            except (ValueError, TypeError, OverflowError):
                safe = False
                bounds = None
            current[identity] = (signature, bounds)
        selected = set()
        regions = []
        if compatible and safe:
            for identity in self._groups.keys() | current.keys():
                old, new = self._groups.get(identity), current.get(identity)
                if old != new:
                    selected.add(identity)
                    regions.extend(item[1] for item in (old, new) if item is not None)
            # Closing over overlapping old/new footprints preserves painter
            # order, including pins crossing axes and removed/moved labels.
            while True:
                regions = _merge(regions)
                added = False
                for identity, (_, bounds) in current.items():
                    if identity not in selected and any(_intersects(bounds, region) for region in regions):
                        selected.add(identity)
                        regions.append(bounds)
                        added = True
                if not added:
                    break
        area = sum(max(0, x1-x0)*max(0, y1-y0) for x0, y0, x1, y1 in regions)
        partial = compatible and safe and area < width*height*.6
        if partial:
            image = np.asarray(canvas.buffer_rgba())
            # Agg rounds channel halves upward, unlike Python's ties-to-even.
            color = np.asarray([int(value*255 + .5) for value in background], dtype=np.uint8)
            for x0, y0, x1, y1 in regions:
                if x1 > x0 and y1 > y0:
                    image[height-y1:height-y0, x0:x1] = color
            # Figure.draw sorts its children stably by z-order. Keep that exact
            # order without clearing the untouched parts of the Agg renderer.
            artists = {id(artist): identity for identity, _, artist in groups}
            for artist in sorted(figure.get_children(), key=lambda item: item.get_zorder()):
                if artists.get(id(artist)) in selected:
                    artist.draw(renderer)
            figure.stale = False
        else:
            canvas.draw()
        self._layout = layout if safe else None
        self._groups = current if safe else {}
        self.last_draw = {'partial': partial, 'groups': len(selected & current.keys()) if partial else len(groups),
                          'pixels': area if partial else width*height}
        return self.last_draw
