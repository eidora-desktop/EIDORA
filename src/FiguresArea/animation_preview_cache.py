from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from PySide6 import QtCore, QtGui


class AnimationPreviewCache:
    """Bounded decoded cache backed by existing snapshot preview PNGs."""

    def __init__(self, *, max_decoded_items: int = 6, max_decoded_bytes: int = 64 * 1024 * 1024) -> None:
        self._max_decoded_items = max(1, int(max_decoded_items))
        self._max_decoded_bytes = max(1, int(max_decoded_bytes))
        self._sources: dict[str, Path] = {}
        self._decoded: OrderedDict[str, QtGui.QPixmap] = OrderedDict()
        self._decoded_bytes = 0

    def register_source(self, frame_id: str, path: Path) -> None:
        source = Path(path)
        if source.exists():
            cached = self._decoded.pop(frame_id, None)
            if cached is not None:
                self._decoded_bytes -= self._pixmap_bytes(cached)
            self._sources[frame_id] = source

    def has_source(self, frame_id: str) -> bool:
        path = self._sources.get(frame_id)
        return path is not None and path.exists()

    def pixmap(self, frame_id: str) -> QtGui.QPixmap:
        cached = self._decoded.pop(frame_id, None)
        if cached is not None:
            self._decoded[frame_id] = cached
            return cached
        path = self._sources.get(frame_id)
        if path is None or not path.exists():
            return QtGui.QPixmap()
        pixmap = QtGui.QPixmap(str(path))
        if not pixmap.isNull():
            self._put_decoded(frame_id, pixmap)
        return pixmap

    def thumbnail(self, frame_id: str, size: int) -> QtGui.QPixmap:
        path = self._sources.get(frame_id)
        if path is None or not path.exists():
            return QtGui.QPixmap()
        reader = QtGui.QImageReader(str(path))
        source_size = reader.size()
        target = max(16, int(size))
        if source_size.isValid():
            source_size.scale(
                QtCore.QSize(target, target),
                QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            )
            reader.setScaledSize(source_size)
        image = reader.read()
        return QtGui.QPixmap.fromImage(image) if not image.isNull() else QtGui.QPixmap()

    def close(self) -> None:
        self._sources.clear()
        self._decoded.clear()
        self._decoded_bytes = 0

    def _put_decoded(self, frame_id: str, pixmap: QtGui.QPixmap) -> None:
        previous = self._decoded.pop(frame_id, None)
        if previous is not None:
            self._decoded_bytes -= self._pixmap_bytes(previous)
        self._decoded[frame_id] = pixmap
        self._decoded_bytes += self._pixmap_bytes(pixmap)
        while (
            len(self._decoded) > self._max_decoded_items
            or self._decoded_bytes > self._max_decoded_bytes
        ):
            _old_id, old_pixmap = self._decoded.popitem(last=False)
            self._decoded_bytes -= self._pixmap_bytes(old_pixmap)

    @staticmethod
    def _pixmap_bytes(pixmap: QtGui.QPixmap) -> int:
        return max(0, pixmap.width()) * max(0, pixmap.height()) * 4
