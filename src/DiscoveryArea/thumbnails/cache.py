from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True)
class CacheKey:
    path: str
    mtime_ns: int | None
    target_px: int


class LruBytesCache:
    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max(max_bytes, 1)
        self._items: OrderedDict[CacheKey, bytes | None] = OrderedDict()
        self._size_bytes = 0

    def get(self, key: CacheKey) -> bytes | None | object:
        if key not in self._items:
            return KeyError
        value = self._items.pop(key)
        self._items[key] = value
        return value

    def put(self, key: CacheKey, value: bytes | None) -> None:
        if key in self._items:
            old = self._items.pop(key)
            self._size_bytes -= 0 if old is None else len(old)
        self._items[key] = value
        self._size_bytes += 0 if value is None else len(value)
        self._evict()

    def _evict(self) -> None:
        while self._size_bytes > self._max_bytes and self._items:
            _, value = self._items.popitem(last=False)
            self._size_bytes -= 0 if value is None else len(value)
