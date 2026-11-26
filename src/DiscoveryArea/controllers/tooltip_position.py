"""Place settings feedback beside the controls without covering them."""
from PySide6 import QtCore


def settings_preview_position(size, preferred, screen, settings, gap=16):
    def bounded(value, minimum, maximum):
        return max(minimum, min(value, maximum))

    x = bounded(preferred.x(), screen.left(), screen.right() - size.width() + 1)
    y = bounded(preferred.y(), screen.top(), screen.bottom() - size.height() + 1)
    preferred = QtCore.QPoint(x, y)
    reserved = settings.adjusted(-gap, -gap, gap, gap)
    if not QtCore.QRect(preferred, size).intersects(reserved):
        return preferred
    candidates = [
        QtCore.QPoint(reserved.right() + 1, y),
        QtCore.QPoint(reserved.left() - size.width(), y),
        QtCore.QPoint(x, reserved.bottom() + 1),
        QtCore.QPoint(x, reserved.top() - size.height()),
    ]
    for point in candidates:
        if screen.contains(QtCore.QRect(point, size)):
            return point
    # On a screen too small for both boxes, keep the settings unobstructed
    # and expose as much of the preview as the remaining space allows.
    def visible_area(point):
        visible = screen.intersected(QtCore.QRect(point, size))
        return visible.width() * visible.height()
    return max(candidates, key=visible_area)
