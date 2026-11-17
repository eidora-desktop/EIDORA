from __future__ import annotations

from shared.plot_fonts import initialize_plot_fonts

initialize_plot_fonts()

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from PySide6 import QtCore, QtGui, QtWidgets


class FigurePreviewAdapter:
    """Display an already-rendered Matplotlib canvas as a scaled Qt preview."""

    def __init__(self, host: QtWidgets.QWidget) -> None:
        self._image = QtGui.QImage()
        self._rgba = None
        self._display_size = None
        self.frame_id = None
        self._label = QtWidgets.QLabel(host)
        self._label.setObjectName("canvas_figure_plot")
        self._label.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop)
        self._label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)

    @property
    def widget(self) -> QtWidgets.QLabel:
        return self._label

    def source_size_px(self) -> tuple[int, int]:
        if self._image.isNull():
            return 0, 0
        return self._image.width(), self._image.height()

    def capture(self, canvas: FigureCanvas) -> None:
        # The caller has already drawn. print_to_buffer() would draw it again.
        rgba = canvas.buffer_rgba()
        height, width = rgba.shape[:2]
        self.set_image(rgba, int(width), int(height))

    def set_image(self, rgba, width: int, height: int) -> None:
        # Immutable received bytes own the QImage storage until replacement.
        # Synchronous Agg memoryviews are copied because their canvas is mutable.
        buffer = rgba if isinstance(rgba, bytes) else bytes(rgba)
        if len(buffer) != int(width) * int(height) * 4 or width <= 0 or height <= 0:
            raise ValueError('Invalid figure preview image dimensions')
        self._display_size = None
        self._image = QtGui.QImage(
            buffer,
            int(width),
            int(height),
            int(width) * 4,
            QtGui.QImage.Format.Format_RGBA8888,
        )
        self._rgba = buffer
        self.frame_id = None

    def apply_frame(self, result) -> bool:
        """Apply a full image or patches based on the last accepted frame."""
        if 'patches' not in result:
            self.set_image(result['rgba'], *result['size'])
        else:
            if self.frame_id is None or result['base_frame'] != self.frame_id or tuple(result['size']) != self.source_size_px():
                self.frame_id = None
                return False
            width, height = self.source_size_px()
            patches = result['patches']
            for (x, y, w, h), data in patches:
                if x < 0 or y < 0 or w <= 0 or h <= 0 or x+w > width or y+h > height or len(data) != w*h*4:
                    raise ValueError('Invalid figure preview region')
            if patches:
                import numpy as np
                # The external bytes must never be mutated, even on bindings
                # which select Qt's mutable-buffer constructor overload.
                if self._rgba is not None:
                    self._image = self._image.copy()
                # Subsequent updates reuse Qt-owned storage, never resizing it.
                target = np.frombuffer(self._image.bits(), np.uint8).reshape(height, width, 4)
                self._rgba = None
                for (x, y, w, h), data in patches:
                    target[y:y+h, x:x+w] = np.frombuffer(data, np.uint8).reshape(h, w, 4)
                self._display_size = None
        self.frame_id = result.get('frame_id')
        return True

    def retained_bytes(self):
        pixmap = self._label.pixmap()
        return self._image.sizeInBytes() + pixmap.width()*pixmap.height()*4

    def release_source(self):
        self._image = QtGui.QImage()
        self._rgba = None
        self._display_size = None
        self.frame_id = None

    def clear(self):
        self._label.clear()
        self.release_source()

    def update(self, source_size_px: tuple[int, int], scale: float, *, min_scale: float) -> None:
        if self._image.isNull():
            return
        width, height = self.preview_size_px(source_size_px, scale, min_scale=min_scale)
        if self._display_size == (width, height):
            return
        pixmap = QtGui.QPixmap.fromImage(self._image)
        if (pixmap.width(), pixmap.height()) != (width, height):
            pixmap = pixmap.scaled(
                width,
                height,
                QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            )
        self._label.setPixmap(pixmap)
        self._display_size = (width, height)
        self._label.setFixedSize(width, height)
        self._label.move(0, 0)
        self._label.show()

    @staticmethod
    def preview_size_px(source_size_px: tuple[int, int], scale: float, *, min_scale: float) -> tuple[int, int]:
        width, height = source_size_px
        safe_scale = max(float(scale), float(min_scale))
        return max(int(round(width * safe_scale)), 1), max(int(round(height * safe_scale)), 1)
