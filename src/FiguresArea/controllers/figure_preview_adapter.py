from __future__ import annotations

from shared.plot_fonts import initialize_plot_fonts

initialize_plot_fonts()

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from PySide6 import QtCore, QtGui, QtWidgets


class FigurePreviewAdapter:
    """Display an already-rendered Matplotlib canvas as a scaled Qt preview."""

    def __init__(self, host: QtWidgets.QWidget) -> None:
        self._image = QtGui.QImage()
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
        rgba, (width, height) = canvas.print_to_buffer()
        self._image = QtGui.QImage(
            rgba,
            int(width),
            int(height),
            int(width) * 4,
            QtGui.QImage.Format.Format_RGBA8888,
        ).copy()

    def update(self, source_size_px: tuple[int, int], scale: float, *, min_scale: float) -> None:
        if self._image.isNull():
            return
        width, height = self.preview_size_px(source_size_px, scale, min_scale=min_scale)
        pixmap = QtGui.QPixmap.fromImage(self._image)
        if (pixmap.width(), pixmap.height()) != (width, height):
            pixmap = pixmap.scaled(
                width,
                height,
                QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            )
        self._label.setPixmap(pixmap)
        self._label.setFixedSize(width, height)
        self._label.move(0, 0)
        self._label.show()

    @staticmethod
    def preview_size_px(source_size_px: tuple[int, int], scale: float, *, min_scale: float) -> tuple[int, int]:
        width, height = source_size_px
        safe_scale = max(float(scale), float(min_scale))
        return max(int(round(width * safe_scale)), 1), max(int(round(height * safe_scale)), 1)
