from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from shared.qt.ui_loader import load_ui


def adjacent_card_rect(
    bounds: QtCore.QRect,
    highlights: list[QtCore.QRect],
    size: QtCore.QSize,
    *,
    below: bool = False,
) -> QtCore.QRect | None:
    """Fit beside the main target, excluding every spotlight and the window edge."""
    target = highlights[0]
    available = bounds.adjusted(12, 12, -12, -12)
    width, height, gap = size.width(), size.height(), 14
    x = max(available.left(), min(target.center().x() - width // 2, available.right() - width + 1))
    y = max(available.top(), min(target.center().y() - height // 2, available.bottom() - height + 1))
    top_y = max(available.top(), min(target.top(), available.bottom() - height + 1))
    # A short panel's centered card can reach into the workspace-bar highlight.
    # Try aligning with the panel top before abandoning either side.
    side_positions = [
        QtCore.QPoint(target.right() + gap, y),
        QtCore.QPoint(target.right() + gap, top_y),
        QtCore.QPoint(target.left() - gap - width, y),
        QtCore.QPoint(target.left() - gap - width, top_y),
    ]
    vertical_positions = [
        QtCore.QPoint(x, target.bottom() + gap),
        QtCore.QPoint(x, target.top() - gap - height),
    ]
    positions = vertical_positions + side_positions if below else side_positions + vertical_positions
    for point in positions:
        rect = QtCore.QRect(point, size)
        if available.contains(rect) and not any(rect.intersects(h.adjusted(-6, -6, 6, 6)) for h in highlights):
            return rect
    return None


class TourSpotlight(QtWidgets.QDialog):
    """A native transient surface covers the Quick canvas as well as Qt widgets."""

    def __init__(self, owner: QtWidgets.QMainWindow) -> None:
        super().__init__(None, QtCore.Qt.WindowType.Tool | QtCore.Qt.WindowType.FramelessWindowHint
                         | QtCore.Qt.WindowType.NoDropShadowWindowHint)
        self.setObjectName("WorkspaceTourSpotlight")
        self.setWindowTitle("Workspace Tour")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowModality(QtCore.Qt.WindowModality.NonModal)
        self.card = load_ui("MainWindow", "ui/workspace_tour.ui", self)
        self.highlights: list[QtCore.QRect] = []
        owner.winId()
        self.winId()
        self.windowHandle().setTransientParent(owner.windowHandle())

    def place_card(self, highlights: list[QtCore.QRect], *, intro: bool) -> bool:
        self.highlights = highlights
        for width in (360, 320, 280, 240):
            self.card.setFixedWidth(width)
            layout = self.card.layout()
            layout.activate()
            height = max(layout.minimumSize().height(), layout.totalHeightForWidth(width))
            size = QtCore.QSize(width, height)
            rect = adjacent_card_rect(self.rect(), highlights, size, below=intro)
            if rect is not None:
                self.card.setGeometry(rect)
                self.update()
                return True
        return False

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        shade = QtGui.QPainterPath()
        shade.addRect(QtCore.QRectF(self.rect()))
        holes = QtGui.QPainterPath()
        for rect in self.highlights:
            path = QtGui.QPainterPath()
            path.addRoundedRect(QtCore.QRectF(rect), 6, 6)
            holes = holes.united(path)
        painter.fillPath(shade.subtracted(holes), QtGui.QColor(10, 14, 22, 175))
        # Almost transparent fill keeps the spotlight surface hit-testable on
        # Windows too, so a tour click cannot accidentally edit the collection.
        painter.fillPath(holes, QtGui.QColor(255, 255, 255, 1))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.setPen(QtGui.QPen(QtGui.QColor(239, 68, 68, 60), 8))
        painter.drawPath(holes)
        painter.setPen(QtGui.QPen(QtGui.QColor("#ef4444"), 4))
        painter.drawPath(holes)
