from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from PySide6 import QtCore, QtGui, QtSvg, QtWidgets


class IconTheme(StrEnum):
    LIGHT = "light"
    DARK = "dark"


def current_icon_theme() -> IconTheme:
    """Choose icon contrast from the active Qt palette."""
    app = QtWidgets.QApplication.instance()
    palette = app.palette() if app is not None else QtGui.QPalette()
    window = palette.color(QtGui.QPalette.ColorRole.Window)
    return IconTheme.DARK if window.lightness() < 128 else IconTheme.LIGHT


def icon_path(name: str, theme: IconTheme | str = IconTheme.LIGHT) -> str:
    return str(_icon_file(name))


def icon(name: str, theme: IconTheme | str = IconTheme.LIGHT) -> QtGui.QIcon:
    return QtGui.QIcon(str(_icon_file(name)))


def apply_icon_theme(root: QtWidgets.QWidget, theme: IconTheme | str) -> None:
    """Tint UI-level icons from the single SVG source set."""
    color = _theme_icon_color(IconTheme(theme))
    apply_icon_color(root, color)


def apply_icon_color(
    root: QtWidgets.QWidget,
    color: QtGui.QColor | str | None = None,
) -> None:
    resolved_color = QtGui.QColor(color) if color is not None else current_icon_color()
    for widget in (root, *root.findChildren(QtWidgets.QWidget)):
        name = widget.property("iconName")
        if not isinstance(name, str) or not name:
            continue
        widget_color = resolved_color
        if color is None:
            override = widget.property("iconColorOverride")
            if isinstance(override, (QtGui.QColor, str)):
                candidate = QtGui.QColor(override)
                if candidate.isValid():
                    widget_color = candidate
        if isinstance(widget, QtWidgets.QLabel):
            size = widget.property("iconSize")
            extent = int(size) if isinstance(size, int) else 20
            widget.setPixmap(
                tinted_icon_pixmap(
                    name,
                    QtCore.QSize(extent, extent),
                    widget_color,
                )
            )
            widget.setText("")
        elif isinstance(widget, QtWidgets.QAbstractButton):
            size = widget.iconSize()
            if not size.isValid() or size.isEmpty():
                size = QtCore.QSize(20, 20)
            pixmap = tinted_icon_pixmap(name, size, widget_color)
            widget_icon = QtGui.QIcon()
            # Supplying every enabled state prevents native styles from adding
            # an accent tint to selected/checked icons. Qt still dims disabled icons.
            for mode in (QtGui.QIcon.Mode.Normal, QtGui.QIcon.Mode.Active, QtGui.QIcon.Mode.Selected):
                for state in (QtGui.QIcon.State.Off, QtGui.QIcon.State.On):
                    widget_icon.addPixmap(pixmap, mode, state)
            widget.setIcon(widget_icon)


class _IconPaletteChangeFilter(QtCore.QObject):
    """Refresh fixed icon pixmaps after Qt adopts a new system palette."""

    def __init__(self, app: QtWidgets.QApplication) -> None:
        super().__init__(app)
        self._app = app
        self._refresh_pending = False
        app.installEventFilter(self)
        style_hints = app.styleHints()
        if hasattr(style_hints, "colorSchemeChanged"):
            style_hints.colorSchemeChanged.connect(self._schedule_refresh)

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if watched is self._app and event.type() in {
            QtCore.QEvent.Type.ApplicationPaletteChange,
            QtCore.QEvent.Type.ThemeChange,
        }:
            self._schedule_refresh()
        return False

    def _schedule_refresh(self, *_args: object) -> None:
        if self._refresh_pending:
            return
        self._refresh_pending = True
        QtCore.QTimer.singleShot(0, self._refresh_icons)

    def _refresh_icons(self) -> None:
        self._refresh_pending = False
        for widget in self._app.topLevelWidgets():
            apply_icon_color(widget)


def install_icon_palette_refresh(app: QtWidgets.QApplication) -> None:
    """Keep semantic widget icons synchronized with the application palette."""
    attribute = "_eidora_icon_palette_change_filter"
    if getattr(app, attribute, None) is None:
        setattr(app, attribute, _IconPaletteChangeFilter(app))


def current_icon_color() -> QtGui.QColor:
    app = QtWidgets.QApplication.instance()
    palette = app.palette() if app is not None else QtGui.QPalette()
    return palette.color(QtGui.QPalette.ColorRole.ButtonText)


def tinted_icon_pixmap(
    name: str,
    size: QtCore.QSize,
    color: QtGui.QColor | str,
) -> QtGui.QPixmap:
    # Render bundled SVGs directly: an unavailable/lazily loaded icon-engine
    # plugin must not leave every semantic control with an empty fixed pixmap.
    renderer = QtSvg.QSvgRenderer(str(_icon_file(name)))
    if not renderer.isValid():
        raise ValueError(f"Invalid semantic icon: {name}")
    renderer.setAspectRatioMode(QtCore.Qt.AspectRatioMode.KeepAspectRatio)
    app = QtWidgets.QApplication.instance()
    ratio = app.devicePixelRatio() if app is not None else 1.0
    tinted = QtGui.QPixmap(size * ratio)
    tinted.setDevicePixelRatio(ratio)
    tinted.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(tinted)
    try:
        bounds = QtCore.QRectF(0, 0, size.width(), size.height())
        renderer.render(painter, bounds)
        painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(bounds, QtGui.QColor(color))
    finally:
        painter.end()
    return tinted


def _icon_file(name: str) -> Path:
    return Path(__file__).resolve().parents[2] / "shared" / "icons" / "material" / f"{name}.svg"


def _theme_icon_color(theme: IconTheme) -> QtGui.QColor:
    return QtGui.QColor("#f1f3f4" if theme == IconTheme.DARK else "#000000")
