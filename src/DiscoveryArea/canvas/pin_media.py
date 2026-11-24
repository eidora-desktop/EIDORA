"""Small, transient media surfaces for visible pins; never part of pin persistence.

Only the playing pin receives frame updates. Occlusion and input regions use the
canvas's existing card geometry, including cards without media.
"""
from dataclasses import dataclass, field

from PySide6 import QtCore, QtGui, QtQuick, QtWidgets
from shared.qt.icons import tinted_icon_pixmap


@dataclass
class PinMediaVisual:
    rect: QtCore.QRect = field(default_factory=QtCore.QRect)
    region: QtGui.QRegion = field(default_factory=QtGui.QRegion)
    image: QtGui.QImage = field(default_factory=QtGui.QImage)
    state: str = "stopped"
    position: int = 0
    duration: int = 0
    audio: bool = False
    enabled: bool = True

    def controls(self):
        height = min(24, self.rect.height())
        return QtCore.QRect(0, self.rect.height() - height, self.rect.width(), height)

    def seek_rect(self):
        controls = self.controls()
        return QtCore.QRect(27, controls.top(), max(0, controls.width() - 33), controls.height())

    def paint(self, painter, icons):
        painter.setClipRegion(self.region)
        bounds = QtCore.QRect(0, 0, self.rect.width(), self.rect.height())
        if not self.image.isNull():
            size = self.image.size().scaled(bounds.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
            target = QtCore.QRect(QtCore.QPoint(), size)
            target.moveCenter(bounds.center())
            painter.fillRect(bounds, QtGui.QColor("#202830"))
            painter.drawImage(target, self.image)
        progress = max(0.0, min(1.0, self.position / self.duration)) if self.duration else 0
        if self.audio and self.duration:
            painter.fillRect(QtCore.QRect(round(progress * max(0, bounds.width()-2)), 0, 2, bounds.height()), QtGui.QColor("#ff3b30"))
        controls = self.controls()
        painter.fillRect(controls, QtGui.QColor(24, 30, 38, 225))
        painter.setOpacity(1.0 if self.enabled else .45)
        if self.state in {"loading", "error"}:
            painter.setPen(QtGui.QColor("white"))
            painter.drawText(controls, QtCore.Qt.AlignmentFlag.AlignCenter,
                             "Loading…" if self.state == "loading" else "Unavailable")
            return
        name = "animation_pause" if self.state == "playing" else "animation_play"
        painter.drawPixmap(3, controls.top() + max(0, (controls.height()-18)//2), icons[name])
        seek = self.seek_rect()
        if seek.width() > 0:
            y = seek.center().y()
            painter.fillRect(QtCore.QRect(seek.x(), y-1, seek.width(), 3), QtGui.QColor("#87949d"))
            painter.fillRect(QtCore.QRect(seek.x(), y-1, round(progress*seek.width()), 3), QtGui.QColor("#ff3b30"))


class _QuickMediaItem(QtQuick.QQuickPaintedItem):
    def __init__(self, root, visual, icons):
        super().__init__(root)
        self.visual, self.icons = visual, icons
        self.setZ(111)
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)

    def paint(self, painter):
        self.visual.paint(painter, self.icons)

    def place(self, rect):
        self.setPosition(QtCore.QPointF(rect.topLeft()))
        self.setSize(QtCore.QSizeF(rect.size()))


class _WidgetMediaItem(QtWidgets.QWidget):
    def __init__(self, root, visual, icons):
        super().__init__(root)
        self.visual, self.icons = visual, icons
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)

    def paintEvent(self, _event):
        painter = QtGui.QPainter(self)
        self.visual.paint(painter, self.icons)

    def place(self, rect):
        self.setGeometry(rect)


class PinMediaLayer:
    def __init__(self):
        self.items = {}
        self._root = None
        self._icons = None
        self._pressed = None

    def sync(self, root, layouts, *, audio, enabled):
        if root is not self._root:
            self.clear()
            self._root = root
        wanted = set(layouts)
        for key in set(self.items) - wanted:
            self._remove(key)
        for key, (rect, region) in layouts.items():
            item = self.items.get(key)
            if item is None:
                if self._icons is None:
                    self._icons = {name: tinted_icon_pixmap(name, QtCore.QSize(18, 18), QtGui.QColor("white"))
                                   for name in ("animation_play", "animation_pause")}
                visual = PinMediaVisual()
                factory = _QuickMediaItem if isinstance(root, QtQuick.QQuickItem) else _WidgetMediaItem
                item = self.items[key] = factory(root, visual, self._icons)
            visual = item.visual
            changed = (visual.rect != rect or visual.region != region or visual.enabled != enabled or visual.audio != audio)
            visual.rect, visual.region, visual.audio, visual.enabled = rect, region, audio, enabled
            item.place(rect)
            item.setVisible(True)
            if changed:
                item.update()
        return wanted

    def update(self, key, *, state, position=0, duration=0, image=None):
        item = self.items.get(key)
        if item is None:
            return
        visual = item.visual
        visual.state, visual.position, visual.duration = state, position, duration
        if image is not None:
            visual.image = image
        item.update()

    def hit(self, position):
        for key, item in reversed(tuple(self.items.items())):
            visual = item.visual
            local = position.toPoint() - visual.rect.topLeft()
            if not visual.enabled or not visual.region.contains(local) or not visual.controls().contains(local):
                continue
            if visual.state == "loading":
                return key, "busy", 0.0
            seek = visual.seek_rect()
            if seek.width() > 0 and seek.contains(local) and visual.duration > 0:
                return key, "seek", max(0.0, min(1.0, (local.x()-seek.x()) / max(1, seek.width()-1)))
            return key, "toggle", 0.0
        return None

    def press(self, position):
        self._pressed = self.hit(position)
        return self._pressed is not None

    def release(self, position):
        pressed, self._pressed = self._pressed, None
        if pressed is None:
            return False, None
        hit = self.hit(position)
        if hit is None or hit[:2] != pressed[:2] or hit[1] == "busy":
            return True, None
        return True, hit

    def cancel_press(self):
        self._pressed = None

    @property
    def pressed(self):
        return self._pressed is not None

    def set_visible(self, visible):
        for item in self.items.values():
            item.setVisible(visible)

    def _remove(self, key):
        item = self.items.pop(key)
        item.setVisible(False)
        if isinstance(item, QtQuick.QQuickItem):
            item.setParentItem(None)
        item.deleteLater()

    def clear(self):
        self.cancel_press()
        for key in tuple(self.items):
            self._remove(key)
        self._root = None
