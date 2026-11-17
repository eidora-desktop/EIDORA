"""Workspace-owned retained-cache limits, independent of render working memory."""
from collections import OrderedDict
from threading import RLock


class ByteCache:
    """Thread-safe LRU; callers keep borrowed values alive during computation."""

    def __init__(self, max_bytes):
        self.max_bytes = max(0, int(max_bytes))
        self._entries = OrderedDict()
        self._bytes = 0
        self._lock = RLock()

    @property
    def retained_bytes(self):
        with self._lock:
            return self._bytes

    def get(self, key):
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is None:
                return None
            self._entries[key] = entry
            return entry[0]

    def put(self, key, value, size):
        with self._lock:
            self.discard(key)
            if size > self.max_bytes:
                return
            self._entries[key] = (value, size)
            self._bytes += size
            while self._bytes > self.max_bytes:
                _, (_, removed_size) = self._entries.popitem(last=False)
                self._bytes -= removed_size

    def discard(self, key):
        with self._lock:
            entry = self._entries.pop(key, None)
            if entry is not None:
                self._bytes -= entry[1]

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._bytes = 0


class FigureCacheBudgets:
    def __init__(self, *, transform_bytes=128 * 1024 * 1024, animation_bytes=64 * 1024 * 1024):
        self.transforms = ByteCache(transform_bytes)
        # QPixmaps are accessed only on the GUI thread.
        self.animation = ByteCache(animation_bytes)

    def clear(self):
        self.transforms.clear()
        self.animation.clear()
