from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PySide6 import QtCore, QtGui


SUPPORTED_CANVAS_EXPORT_FORMATS = frozenset({"jpg", "jpeg", "png", "gif", "webp", "pdf"})


def save_canvas_pixmap(pixmap: QtGui.QPixmap, path: Path | str) -> Path:
    """Save one already-cropped canvas capture in the requested file format."""

    target = Path(path)
    format_name = target.suffix.lower().lstrip(".")
    if format_name not in SUPPORTED_CANVAS_EXPORT_FORMATS:
        raise ValueError(f"Unsupported canvas export format: {target.suffix or '(none)'}")
    if pixmap.isNull() or pixmap.width() < 1 or pixmap.height() < 1:
        raise ValueError("The canvas capture is empty.")

    if format_name == "pdf":
        _save_pdf(pixmap, target)
    elif format_name == "gif":
        _save_gif(pixmap, target)
    else:
        _save_raster(pixmap, target, format_name)
    return target


def _save_raster(pixmap: QtGui.QPixmap, path: Path, format_name: str) -> None:
    writer = QtGui.QImageWriter(str(path), format_name.encode("ascii"))
    writer.setQuality(95)
    if not writer.write(pixmap.toImage()):
        raise OSError(writer.errorString() or f"Could not write {path.name}")


def _save_gif(pixmap: QtGui.QPixmap, path: Path) -> None:
    from PIL import Image

    buffer = QtCore.QBuffer()
    if not buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly):
        raise OSError("Could not prepare GIF export.")
    if not pixmap.toImage().save(buffer, "PNG"):
        raise OSError("Could not encode canvas capture for GIF export.")
    with Image.open(BytesIO(bytes(buffer.data()))) as image:
        image.convert("RGBA").save(path, format="GIF", save_all=False)


def _save_pdf(pixmap: QtGui.QPixmap, path: Path) -> None:
    dpi = 96
    page_size_mm = QtCore.QSizeF(
        pixmap.width() * 25.4 / dpi,
        pixmap.height() * 25.4 / dpi,
    )
    writer = QtGui.QPdfWriter(str(path))
    writer.setResolution(dpi)
    writer.setPageSize(
        QtGui.QPageSize(
            page_size_mm,
            QtGui.QPageSize.Unit.Millimeter,
            "Discovery canvas crop",
            QtGui.QPageSize.SizeMatchPolicy.ExactMatch,
        )
    )
    writer.setPageMargins(QtCore.QMarginsF(), QtGui.QPageLayout.Unit.Millimeter)
    painter = QtGui.QPainter(writer)
    if not painter.isActive():
        raise OSError(f"Could not write {path.name}")
    painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.drawPixmap(QtCore.QRect(0, 0, writer.width(), writer.height()), pixmap)
    painter.end()
