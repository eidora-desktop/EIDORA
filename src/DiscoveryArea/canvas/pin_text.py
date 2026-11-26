"""Cached plain-text layout shared by native pin rendering and canvas exports."""
from dataclasses import asdict, dataclass
from functools import lru_cache

from PySide6 import QtCore, QtGui

from shared.qt.text_layout import wrapped_text_lines


@dataclass(frozen=True)
class PinTextLayout:
    lines: tuple[str, ...]
    width: int
    height: int
    family: str
    font_size: int
    line_spacing: float

    def payload(self, foreground, background):
        return {**asdict(self), "lines": list(self.lines),
                "foreground": foreground, "background": background}


@lru_cache(maxsize=256)
def layout_pin_text(text, width, family, font_size):
    width = max(32, min(500, int(width)))
    font_size = max(8, min(36, int(font_size)))
    font = QtGui.QFont(family)
    font.setPointSize(font_size)
    metrics = QtGui.QFontMetricsF(font)
    line_spacing = metrics.lineSpacing()
    # Keep the excerpt bounded while allowing its height to follow wrapping.
    limit = max(1, min(12, int(240 / line_spacing)))
    lines = wrapped_text_lines(text, font, width - 12, limit + 1)
    if len(lines) > limit:
        lines = lines[:limit]
        lines[-1] = metrics.elidedText(lines[-1].rstrip() + "…", QtCore.Qt.TextElideMode.ElideRight, width - 12)
    height = int(metrics.height() + max(0, len(lines) - 1) * line_spacing + 13)
    return PinTextLayout(tuple(lines), width, height, family, font_size, line_spacing)


def paint_pin_text(painter, rect, layout, foreground, background):
    painter.save()
    painter.fillRect(rect, QtGui.QColor(background))
    painter.setClipRect(rect.adjusted(6, 6, -6, -6), QtCore.Qt.ClipOperation.IntersectClip)
    painter.setPen(QtGui.QColor(foreground))
    font = QtGui.QFont(layout.family)
    font.setPointSize(layout.font_size)
    for index, text in enumerate(layout.lines):
        line_layout = QtGui.QTextLayout(text, font)
        line_layout.beginLayout()
        line = line_layout.createLine()
        if line.isValid():
            line.setLineWidth(layout.width - 12)
        line_layout.endLayout()
        line_layout.draw(painter, rect.topLeft() + QtCore.QPointF(6, 6 + index * layout.line_spacing))
    painter.restore()
