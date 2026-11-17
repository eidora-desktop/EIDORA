"""Describe lossless pixel updates without retaining a second full page."""
from __future__ import annotations

import hashlib
from uuid import uuid4


class PreviewTransfer:
    TILE_WIDTH = 256
    TILE_HEIGHT = 64

    def __init__(self):
        self.frame_id = None
        self._size = None
        self._hashes = []

    def prepare(self, pixels, base_frame):
        """Return changed rectangles, or None for a complete replacement.

        Only the last transmitted frame's tile digests survive a request. The
        caller acknowledges the frame actually displayed, not merely received.
        """
        height, width = pixels.shape[:2]
        compatible = base_frame is not None and base_frame == self.frame_id and self._size == (width, height)
        hashes, regions = [], []
        index = 0
        for y in range(0, height, self.TILE_HEIGHT):
            h = min(self.TILE_HEIGHT, height - y)
            start = None
            for x in range(0, width, self.TILE_WIDTH):
                w = min(self.TILE_WIDTH, width - x)
                digest = hashlib.sha256(pixels[y:y+h, x:x+w].tobytes()).digest()
                hashes.append(digest)
                changed = not compatible or digest != self._hashes[index]
                index += 1
                if changed and start is None:
                    start = x
                if not changed and start is not None:
                    regions.append((start, y, x - start, h))
                    start = None
            if start is not None:
                regions.append((start, y, width - start, h))
        self._size, self._hashes = (width, height), hashes
        self.frame_id = uuid4().hex
        # Large updates use the existing contiguous, bounded-packet transfer.
        if not compatible or sum(w*h for _, _, w, h in regions) >= width*height*.6:
            return None
        return regions
