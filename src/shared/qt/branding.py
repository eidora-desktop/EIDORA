"""Qt helpers for EIDORA's application and About-dialog branding."""
from __future__ import annotations

import re
import sys
from importlib import resources

from PySide6 import QtCore, QtGui


_WORDMARK_INK = re.compile(br"#303830", re.IGNORECASE)


def install_application_icon(app: QtGui.QGuiApplication) -> None:
    """Let installed Mac apps retain the layered icon selected by IconServices."""
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        return
    app.setWindowIcon(application_icon())


def application_icon() -> QtGui.QIcon:
    """Return the platform-appropriate runtime application icon."""
    if sys.platform == "win32":
        name = "eidora_icon_windows.ico"
    elif sys.platform == "darwin":
        name = "eidora_icon_macos.png"
    else:
        name = "eidora_icon_linux.png"
    return QtGui.QIcon(str(_branding_resource(name)))


def branding_pixmap(
    name: str,
    logical_size: QtCore.QSize,
    *,
    device_pixel_ratio: float = 1.0,
) -> QtGui.QPixmap:
    """Load raster branding artwork at the requested logical size."""
    source = QtGui.QPixmap(str(_branding_resource(name)))
    if source.isNull():
        raise RuntimeError(f"Could not load branding asset: {name}")
    ratio = max(1.0, float(device_pixel_ratio))
    pixel_size = QtCore.QSize(
        max(1, round(logical_size.width() * ratio)),
        max(1, round(logical_size.height() * ratio)),
    )
    pixmap = source.scaled(
        pixel_size,
        QtCore.Qt.AspectRatioMode.KeepAspectRatio,
        QtCore.Qt.TransformationMode.SmoothTransformation,
    )
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def branding_svg_pixmap(
    name: str,
    logical_size: QtCore.QSize,
    *,
    device_pixel_ratio: float = 1.0,
    ink: QtGui.QColor | str | None = None,
) -> QtGui.QPixmap:
    """Render a branding SVG crisply at the requested logical size."""
    from PySide6 import QtSvg

    data = _branding_resource(name).read_bytes()
    if ink is not None:
        color = QtGui.QColor(ink)
        if not color.isValid():
            raise ValueError(f"Invalid branding color: {ink}")
        data = _WORDMARK_INK.sub(color.name().encode("ascii"), data)

    ratio = max(1.0, float(device_pixel_ratio))
    pixel_size = QtCore.QSize(
        max(1, round(logical_size.width() * ratio)),
        max(1, round(logical_size.height() * ratio)),
    )
    renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(data))
    if not renderer.isValid():
        raise RuntimeError(f"Could not render branding asset: {name}")

    pixmap = QtGui.QPixmap(pixel_size)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pixmap)
    try:
        renderer.render(
            painter,
            QtCore.QRectF(0.0, 0.0, pixel_size.width(), pixel_size.height()),
        )
    finally:
        painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def _branding_resource(name: str):
    path = resources.files("shared").joinpath("branding", name)
    if not path.is_file():
        raise FileNotFoundError(f"Branding asset is missing: {name}")
    return path
