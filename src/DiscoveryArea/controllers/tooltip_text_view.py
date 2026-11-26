"""Bounded, theme-aware plain-text layout for the Designer-owned tooltip label."""
import unicodedata

from PySide6 import QtCore, QtGui, QtWidgets

from DiscoveryArea.domain import DiscoveryPinDisplaySettings
from shared.qt.text_layout import wrapped_text_lines


class TooltipTextView(QtCore.QObject):
    geometry_changed = QtCore.Signal()

    def __init__(self, label):
        super().__init__(label)
        self.label = label
        self._text = ""
        self._style = DiscoveryPinDisplaySettings()
        self._rendering = False
        self._refresh = QtCore.QTimer(self)
        self._refresh.setSingleShot(True)
        self._refresh.timeout.connect(self._render)
        label.installEventFilter(self)
        label.hide()

    def set_style(self, payload):
        style = DiscoveryPinDisplaySettings.from_payload(payload)
        if style != self._style:
            self._style = style
            self._render()

    def set_text(self, text):
        self._text = str(text)
        self._render()

    def _render(self):
        if self._rendering:
            return
        self._rendering = True
        try:
            style = self._style
            font = QtGui.QFont(QtWidgets.QApplication.font())
            if style.tooltip_text_font:
                font.setFamily(style.tooltip_text_font)
            font.setPointSize(style.tooltip_text_size)
            self.label.setFont(font)
            direction = QtCore.Qt.LayoutDirection.LeftToRight
            for character in self._text:
                bidi = unicodedata.bidirectional(character)
                if bidi in {"R", "AL", "L"}:
                    if bidi != "L":
                        direction = QtCore.Qt.LayoutDirection.RightToLeft
                    break
            self.label.setLayoutDirection(direction)
            self.label.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeading | QtCore.Qt.AlignmentFlag.AlignTop)
            width = style.tooltip_thumbnail_size
            metrics = QtGui.QFontMetricsF(font)
            limit = max(1, min(8, int(224 / metrics.lineSpacing())))
            lines = wrapped_text_lines(self._text, font, width - 16, limit + 1)
            if len(lines) > limit:
                lines = lines[:limit]
                lines[-1] = metrics.elidedText(lines[-1].rstrip() + "…", QtCore.Qt.TextElideMode.ElideRight, width - 16)
            self.label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
            self.label.setText("\n".join(lines))
            self.label.setFixedSize(width, int(metrics.height() + max(0, len(lines) - 1) * metrics.lineSpacing() + 17))
        finally:
            self._rendering = False
        self.geometry_changed.emit()

    def eventFilter(self, obj, event):
        if not self._rendering and event.type() == QtCore.QEvent.Type.ApplicationFontChange:
            self._refresh.start(0)
        return False
