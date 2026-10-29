"""System-selected surface colors and small cross-platform contrast fixes."""
from __future__ import annotations

from dataclasses import dataclass
import sys

from PySide6 import QtCore, QtGui, QtWidgets


@dataclass(frozen=True)
class ThemeColors:
    window: str
    area: str
    panel: str
    group: str
    inset_shadow: str
    inset_highlight: str
    grid_line: str
    header_background: str
    text: str
    secondary_text: str
    selection_background: str
    control_border: str
    error_text: str


THEMES = {
    "dark": ThemeColors(
        window="#0F0E0F",
        area="#1D1C1D",
        panel="#2C2B2C",
        group="#3D3C3E",
        inset_shadow="#0B0A0B",
        inset_highlight="#4F4E50",
        grid_line="#5F5E60",
        header_background="#4A494C",
        text="#F4F4F5",
        secondary_text="#AAA8AD",
        selection_background="#555459",
        control_border="#B6B3BC",
        error_text="#FF8A80",
    ),
    "light": ThemeColors(
        window="#D9DADB",
        area="#E3E4E5",
        panel="#ECEDEE",
        group="#F5F5F6",
        inset_shadow="#C2C3C5",
        inset_highlight="#FFFFFF",
        grid_line="#D8D9DB",
        header_background="#D4D5D7",
        text="#202023",
        secondary_text="#5C5E64",
        selection_background="#BFC2C7",
        control_border="#5F6268",
        error_text="#B3261E",
    ),
}
SURFACE_ROLES = ("window", "area", "panel", "group", "view")
INTERACTION_STYLE = """
QWidget[surfaceRole="window"] {{ background-color: {window}; }}
QWidget[surfaceRole="area"] {{ background-color: {area}; }}
QWidget[surfaceRole="panel"] {{ background-color: {panel}; }}
QWidget[surfaceRole="group"] {{ background-color: {group}; }}
QWidget[surfaceRole="view"] {{ background-color: {view_background}; }}
QPushButton, QToolButton {{
    background-color: {group};
    color: {text};
    border: 1px solid {grid_line};
    border-radius: 3px;
    padding: 3px;
}}
QPushButton:disabled, QToolButton:disabled {{ color: {secondary_text}; }}
QPushButton[flat="true"], QToolButton[autoRaise="true"] {{
    background-color: transparent;
    border-color: transparent;
}}
QPushButton:focus:enabled, QToolButton:focus:enabled, QPushButton:default:enabled {{
    border-color: {control_border};
}}
QPushButton:hover:enabled, QPushButton:pressed:enabled, QPushButton:checked:enabled,
QToolButton:hover:enabled, QToolButton:pressed:enabled, QToolButton:checked:enabled {{
    background-color: {selection_background};
    color: {text};
    border-color: {control_border};
}}
QPushButton:focus:enabled, QToolButton:focus:enabled {{
    border-color: {text};
    border-style: dotted;
}}
QAbstractItemView::item:selected:enabled, QAbstractItemView::item:hover:enabled {{
    background-color: {selection_background};
    color: {text};
}}
QLineEdit, QTextEdit, QPlainTextEdit {{
    selection-background-color: {selection_background};
    selection-color: {text};
}}
QHeaderView::section {{ background-color: {header_background}; color: {text}; }}
QTableView[gridRole="subtle"] {{ gridline-color: {grid_line}; }}
QLabel[textRole="primary"], QAbstractItemView[textRole="primary"] {{ color: {text}; }}
QLabel[textRole="secondary"] {{ color: {secondary_text}; }}
QLabel[textRole="error"] {{ color: {error_text}; }}
"""


def prepare_surface_widgets(root: QtWidgets.QWidget) -> None:
    """Apply the current background theme to a newly loaded widget tree."""
    app = QtWidgets.QApplication.instance()
    controller = getattr(app, "_eidora_theme_controller", None) if app is not None else None
    if isinstance(controller, ThemeController):
        controller.apply_to(root)


class ThemeController(QtCore.QObject):
    """Follow the system appearance and apply the central Qt color tokens."""

    def __init__(self, app: QtWidgets.QApplication) -> None:
        super().__init__(app)
        self._app = app
        self._platform_palette = QtGui.QPalette(app.palette())
        self._setting_palette = False
        platform_window = app.palette().color(QtGui.QPalette.ColorRole.Window)
        self._fallback = "dark" if platform_window.lightness() < 128 else "light"
        self.mode: str | None = None
        self._palette_refresh = QtCore.QTimer(self)
        self._palette_refresh.setSingleShot(True)
        self._palette_refresh.timeout.connect(self._refresh_palette)
        style_hints = app.styleHints()
        if hasattr(style_hints, "colorSchemeChanged"):
            style_hints.colorSchemeChanged.connect(self._system_changed)
        app.paletteChanged.connect(self._application_palette_changed)
        self._system_changed(style_hints.colorScheme())

    def _system_changed(self, scheme: QtCore.Qt.ColorScheme) -> None:
        self._color_scheme = scheme
        mode = {QtCore.Qt.ColorScheme.Dark: "dark", QtCore.Qt.ColorScheme.Light: "light"}.get(scheme, self._fallback)
        self.apply(mode)

    def _application_palette_changed(self, palette: QtGui.QPalette) -> None:
        if self._setting_palette:
            return
        self._platform_palette = QtGui.QPalette(palette)
        if sys.platform == "linux" and self._color_scheme == QtCore.Qt.ColorScheme.Unknown:
            window = palette.color(QtGui.QPalette.ColorRole.Window)
            self._fallback = "dark" if window.lightness() < 128 else "light"
        # Qt repolishes stylesheets after emitting paletteChanged; reapply only
        # once that finishes so cached native palettes cannot undo our colors.
        self._palette_refresh.start(0)

    def _refresh_palette(self) -> None:
        if sys.platform == "linux" and self._color_scheme == QtCore.Qt.ColorScheme.Unknown:
            self.apply(self._fallback)
        else:
            self.apply(self.mode or self._fallback)

    def apply(self, mode: str) -> None:
        if mode not in THEMES:
            raise ValueError(f"Unknown theme mode: {mode}")
        self.mode = mode
        if sys.platform == "linux":
            # Linux can report a dark preference while still supplying a light
            # widget palette. Keep the native palette separately so returning
            # to light mode restores it instead of reusing our dark override.
            palette = _linux_palette(self._platform_palette, mode)
            if self._app.palette() != palette:
                self._setting_palette = True
                try:
                    self._app.setPalette(palette)
                finally:
                    self._setting_palette = False
        _apply_interaction_style(self._app, THEMES[mode])
        for root in self._app.topLevelWidgets():
            self.apply_to(root)

    def apply_to(self, root: QtWidgets.QWidget) -> None:
        colors = THEMES[self.mode or self._fallback]
        # Install once per theme; local Designer/controller styles keep priority.
        _apply_interaction_style(self._app, colors)
        widgets = (root, *root.findChildren(QtWidgets.QWidget))
        # Stylesheet-backed controls do not reliably inherit palette roles.
        # Set them explicitly, including on inputs and checked buttons.
        _apply_selection_colors(root, colors)
        for widget in widgets:
            if widget is not root and isinstance(widget, (
                QtWidgets.QAbstractItemView,
                QtWidgets.QAbstractButton,
                QtWidgets.QLineEdit,
                QtWidgets.QTextEdit,
                QtWidgets.QPlainTextEdit,
                QtWidgets.QComboBox,
            )):
                _apply_selection_colors(widget, colors)
            _apply_input_text_colors(widget, colors)

            for header in _item_view_headers(widget):
                _apply_header_colors(header, colors)

            if widget.property("separatorRole") == "inset":
                palette = QtGui.QPalette(widget.palette())
                shadow = QtGui.QColor(colors.inset_shadow)
                highlight = QtGui.QColor(colors.inset_highlight)
                for role in (
                    QtGui.QPalette.ColorRole.Dark,
                    QtGui.QPalette.ColorRole.Mid,
                    QtGui.QPalette.ColorRole.Shadow,
                ):
                    palette.setColor(role, shadow)
                for role in (
                    QtGui.QPalette.ColorRole.Light,
                    QtGui.QPalette.ColorRole.Midlight,
                ):
                    palette.setColor(role, highlight)
                widget.setPalette(palette)
                widget.update()

            role = widget.property("surfaceRole")
            if role not in SURFACE_ROLES:
                continue
            palette = QtGui.QPalette(widget.palette())
            surface_color = (
                self._app.palette().color(QtGui.QPalette.ColorRole.Base)
                if role == "view"
                else QtGui.QColor(getattr(colors, role))
            )
            palette.setColor(
                QtGui.QPalette.ColorRole.Window,
                surface_color,
            )
            widget.setPalette(palette)
            widget.setAutoFillBackground(True)
            widget.update()


def _linux_palette(native: QtGui.QPalette, mode: str) -> QtGui.QPalette:
    """Fill the Linux appearance/palette gap without replacing matching themes."""
    role = QtGui.QPalette.ColorRole
    dark = mode == "dark"
    backgrounds_match = all(
        (native.color(item).lightness() < 128) == dark
        for item in (role.Window, role.Base)
    )
    foregrounds_match = all(
        (native.color(item).lightness() >= 128) == dark
        for item in (role.WindowText, role.Text, role.ButtonText)
    )
    if backgrounds_match and foregrounds_match:
        return QtGui.QPalette(native)

    colors = THEMES[mode]
    palette = QtGui.QPalette(native)
    values = {
        role.Window: colors.window,
        role.WindowText: colors.text,
        role.Base: colors.area if dark else "#FFFFFF",
        role.AlternateBase: colors.panel,
        role.Text: colors.text,
        role.Button: colors.group,
        role.ButtonText: colors.text,
        role.ToolTipBase: colors.group,
        role.ToolTipText: colors.text,
        role.PlaceholderText: colors.secondary_text,
        role.BrightText: "#FFFFFF",
        role.Highlight: colors.selection_background,
        role.HighlightedText: colors.text,
        role.Accent: colors.selection_background,
        role.Light: colors.inset_highlight,
        role.Midlight: colors.group,
        role.Mid: colors.grid_line,
        role.Dark: colors.inset_shadow,
        role.Shadow: colors.inset_shadow,
        role.Link: "#8AB4F8" if dark else "#174EA6",
        role.LinkVisited: "#C5A3FF" if dark else "#681DA8",
    }
    for item, color in values.items():
        palette.setColor(item, QtGui.QColor(color))
    for item in (role.WindowText, role.Text, role.ButtonText, role.HighlightedText):
        palette.setColor(QtGui.QPalette.ColorGroup.Disabled, item,
                         QtGui.QColor(colors.secondary_text))
    return palette


def install_theme(app: QtWidgets.QApplication) -> ThemeController:
    controller = getattr(app, "_eidora_theme_controller", None)
    if controller is None:
        controller = ThemeController(app)
        app._eidora_theme_controller = controller
    return controller


def _apply_interaction_style(app: QtWidgets.QApplication, colors: ThemeColors) -> None:
    """Install shared state colors once; Designer rules keep their precedence."""
    previous_style = app.property("_eidoraInteractionStyle") or ""
    interaction_style = INTERACTION_STYLE.format(
        selection_background=colors.selection_background,
        text=colors.text,
        control_border=colors.control_border,
        secondary_text=colors.secondary_text,
        error_text=colors.error_text,
        header_background=colors.header_background,
        grid_line=colors.grid_line,
        group=colors.group,
        window=colors.window,
        area=colors.area,
        panel=colors.panel,
        view_background=app.palette().color(QtGui.QPalette.ColorRole.Base).name(),
    )
    if previous_style == interaction_style and interaction_style in app.styleSheet():
        return
    app.setStyleSheet(app.styleSheet().replace(previous_style, "") + interaction_style)
    app.setProperty("_eidoraInteractionStyle", interaction_style)


def _apply_selection_colors(widget: QtWidgets.QWidget, colors: ThemeColors) -> None:
    """Pair neutral selection colors; preserve native disabled-state contrast."""
    palette = QtGui.QPalette(widget.palette())
    if isinstance(widget, QtWidgets.QAbstractItemView):
        # Qt can retain the stylesheet's old Base after an application palette change.
        palette.setColor(QtGui.QPalette.ColorRole.Base, QtWidgets.QApplication.palette().color(QtGui.QPalette.ColorRole.Base))
    background = QtGui.QColor(colors.selection_background)
    text = QtGui.QColor(colors.text)
    for group in (QtGui.QPalette.ColorGroup.Active, QtGui.QPalette.ColorGroup.Inactive):
        palette.setColor(group, QtGui.QPalette.ColorRole.Highlight, background)
        palette.setColor(group, QtGui.QPalette.ColorRole.HighlightedText, text)
        palette.setColor(group, QtGui.QPalette.ColorRole.Accent, background)
    if widget.palette() != palette:
        widget.setPalette(palette)
    if isinstance(widget, QtWidgets.QAbstractItemView) and widget.viewport().palette() != palette:
        widget.viewport().setPalette(palette)


def _apply_input_text_colors(
    widget: QtWidgets.QWidget,
    colors: ThemeColors,
) -> None:
    if isinstance(widget, QtWidgets.QComboBox):
        palette = QtGui.QPalette(widget.palette())
        text = QtGui.QColor(colors.text)
        for group in (
            QtGui.QPalette.ColorGroup.Active,
            QtGui.QPalette.ColorGroup.Inactive,
        ):
            palette.setColor(group, QtGui.QPalette.ColorRole.ButtonText, text)
            palette.setColor(group, QtGui.QPalette.ColorRole.Text, text)
        widget.setPalette(palette)
        widget.update()
        return

    if isinstance(widget, QtWidgets.QLineEdit):
        palette = QtGui.QPalette(widget.palette())
        placeholder = QtGui.QColor(colors.secondary_text)
        for group in (
            QtGui.QPalette.ColorGroup.Active,
            QtGui.QPalette.ColorGroup.Inactive,
        ):
            palette.setColor(
                group,
                QtGui.QPalette.ColorRole.PlaceholderText,
                placeholder,
            )
        widget.setPalette(palette)
        widget.update()


def _item_view_headers(widget: QtWidgets.QWidget) -> tuple[QtWidgets.QHeaderView, ...]:
    if isinstance(widget, QtWidgets.QTableView):
        return (widget.horizontalHeader(), widget.verticalHeader())
    if isinstance(widget, QtWidgets.QTreeView):
        return (widget.header(),)
    return ()


def _apply_header_colors(
    header: QtWidgets.QHeaderView,
    colors: ThemeColors,
) -> None:
    palette = QtGui.QPalette(header.palette())
    background = QtGui.QColor(colors.header_background)
    text = QtGui.QColor(colors.text)
    for role in (
        QtGui.QPalette.ColorRole.Button,
        QtGui.QPalette.ColorRole.Window,
        QtGui.QPalette.ColorRole.Base,
    ):
        palette.setColor(role, background)
    for role in (
        QtGui.QPalette.ColorRole.ButtonText,
        QtGui.QPalette.ColorRole.WindowText,
        QtGui.QPalette.ColorRole.Text,
    ):
        palette.setColor(role, text)
    header.setPalette(palette)
