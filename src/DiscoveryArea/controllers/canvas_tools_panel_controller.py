from __future__ import annotations

from collections.abc import Callable

from PySide6 import QtCore, QtGui, QtWidgets

from shared.qt.panel_controller import UiPanelController
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class _FindItemPopoverDismissFilter(QtCore.QObject):
    def __init__(self, callback: Callable[[str, QtCore.QPoint | None], None], parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._callback = callback

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        del obj
        if event.type() == QtCore.QEvent.Type.KeyPress:
            key_event = event
            if key_event.key() == QtCore.Qt.Key.Key_Escape:
                self._callback("escape", None)
        elif event.type() == QtCore.QEvent.Type.MouseButtonPress:
            mouse_event = event
            self._callback("mouse", mouse_event.globalPosition().toPoint())
        elif event.type() == QtCore.QEvent.Type.ApplicationDeactivate:
            self._callback("deactivate", None)
        return False


class CanvasToolsPanelController(UiPanelController):
    package = "DiscoveryArea"
    ui_path = "ui/canvas_tools.ui"

    def _wire(self) -> None:
        self._mode_changed_callback: Callable[[str], None] | None = None
        self._fit_callback: Callable[[], None] | None = None
        self._find_item_callback: Callable[[str], bool] | None = None
        self._clear_find_item_callback: Callable[[], None] | None = None
        self._background_color_callback: Callable[[QtGui.QColor], None] | None = None
        self._current_background_color_callback: Callable[[], QtGui.QColor] | None = None
        self._quick_export_callback: Callable[[], bool] | None = None
        self._find_item_event_filter: _FindItemPopoverDismissFilter | None = None
        self._mode_buttons = {
            "pan": require_child(self._widget, QtWidgets.QToolButton, "button_pan_tool"),
            "zoom": require_child(self._widget, QtWidgets.QToolButton, "button_zoom_tool"),
            "lasso": require_child(self._widget, QtWidgets.QToolButton, "button_lasso_tool"),
            "selection": require_child(self._widget, QtWidgets.QToolButton, "button_selection_tool"),
        }
        self._button_group = QtWidgets.QButtonGroup(self._widget)
        self._button_group.setExclusive(True)
        for index, (mode, button) in enumerate(self._mode_buttons.items()):
            button.setCheckable(True)
            self._button_group.addButton(button, index)
            button.clicked.connect(lambda _checked=False, name=mode: self._activate_mode(name))
        self._mode_buttons["pan"].setChecked(True)
        self.active_mode = "pan"

        require_child(self._widget, QtWidgets.QToolButton, "button_fit_screen").clicked.connect(self._on_fit_clicked)
        require_child(self._widget, QtWidgets.QToolButton, "button_canvas_background").clicked.connect(
            self._pick_canvas_background
        )
        require_child(self._widget, QtWidgets.QToolButton, "button_quick_export").clicked.connect(
            self._on_quick_export_clicked
        )
        self._find_dialog: QtWidgets.QWidget | None = None
        require_child(self._widget, QtWidgets.QToolButton, "button_find_item").clicked.connect(self._toggle_find_item_popover)

    def shutdown(self) -> None:
        self._uninstall_find_item_popover_dismiss_filter()
        if self._find_dialog is not None:
            self._find_dialog.close()

    def _activate_mode(self, mode: str) -> None:
        self.set_active_mode(mode)
        self._show_status(f"Canvas tool mode: {mode}")

    def set_active_mode(self, mode: str) -> None:
        if mode not in self._mode_buttons:
            raise ValueError(f"Unsupported canvas tool mode: {mode}")
        self._mode_buttons[mode].setChecked(True)
        self.active_mode = mode
        if self._mode_changed_callback is not None:
            self._mode_changed_callback(mode)

    def _on_fit_clicked(self) -> None:
        if self._fit_callback is not None:
            self._fit_callback()
        else:
            self._show_status("Canvas fit placeholder triggered.")

    def _pick_canvas_background(self) -> None:
        current = QtGui.QColor("#ffffff")
        if self._current_background_color_callback is not None:
            current = self._current_background_color_callback()
        color = QtWidgets.QColorDialog.getColor(current, self._widget, "Select Canvas Background Color")
        if not color.isValid():
            return
        if self._background_color_callback is not None:
            self._background_color_callback(color)
        self._show_status(f"Canvas background color: {color.name()}")

    def _on_quick_export_clicked(self) -> None:
        if self._quick_export_callback is None:
            self._show_status("Canvas export is unavailable.")
            return
        self._quick_export_callback()

    def _toggle_find_item_popover(self) -> None:
        if self._find_dialog is not None and self._find_dialog.isVisible():
            self._hide_find_item_popover()
            return
        if self._find_dialog is None:
            self._find_dialog = load_ui(self.package, "ui/find_item_dialog.ui", self._widget)
            self._find_dialog.destroyed.connect(lambda *_: setattr(self, "_find_dialog", None))
            self._configure_find_item_popover(self._find_dialog)
            line_edit = self._find_dialog.findChild(QtWidgets.QLineEdit, "edit_item_id")
            if line_edit is not None:
                line_edit.textChanged.connect(self._on_find_item_text_changed)
        self._position_find_item_popover(self._find_dialog)
        self._install_find_item_popover_dismiss_filter()
        self._find_dialog.show()
        self._find_dialog.raise_()
        line_edit = self._find_dialog.findChild(QtWidgets.QLineEdit, "edit_item_id")
        if line_edit is not None:
            line_edit.setFocus()
            line_edit.selectAll()
            self._on_find_item_text_changed(line_edit.text())
        self._show_status("Find item popover opened.")

    def _hide_find_item_popover(self) -> None:
        if self._find_dialog is not None:
            self._find_dialog.hide()
        self._uninstall_find_item_popover_dismiss_filter()
        if self._clear_find_item_callback is not None:
            self._clear_find_item_callback()
        self._show_status("Find item popover closed.")

    def _on_find_item_text_changed(self, text: str) -> None:
        value = str(text or "").strip()
        if not value:
            if self._clear_find_item_callback is not None:
                self._clear_find_item_callback()
            return
        if self._find_item_callback is None:
            return
        found = self._find_item_callback(value)
        line_edit = self._find_dialog.findChild(QtWidgets.QLineEdit, "edit_item_id") if self._find_dialog is not None else None
        if line_edit is not None:
            line_edit.setProperty("findState", "found" if found else "missing")
            line_edit.style().unpolish(line_edit)
            line_edit.style().polish(line_edit)
            line_edit.update()

    def _configure_find_item_popover(self, dialog: QtWidgets.QWidget) -> None:
        parent = self._widget.window() if self._widget.window() is not None else self._widget
        dialog.setParent(parent)
        # The canvas can be a native QQuickView hosted by createWindowContainer().
        # A QWidget subwindow cannot reliably stack above that native child, so
        # keep this control in its own transient popup window.
        dialog.setWindowFlags(
            QtCore.Qt.WindowType.Popup
            | QtCore.Qt.WindowType.FramelessWindowHint
        )
        dialog.setModal(False)
        dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        dialog.setObjectName("findItemPopover")
        dialog.setStyleSheet(
            "#findItemPopover {"
            " border: 1px solid palette(mid);"
            " border-radius: 6px;"
            " background: palette(base);"
            "}"
            "QLineEdit[findState='missing'] {"
            " border: 1px solid #c62828;"
            "}"
        )

    def _position_find_item_popover(self, dialog: QtWidgets.QWidget) -> None:
        button = self._widget.findChild(QtWidgets.QAbstractButton, "button_find_item")
        if button is None:
            return
        button_bottom = button.mapToGlobal(QtCore.QPoint(0, button.height()))
        dialog.adjustSize()
        hint = dialog.sizeHint().expandedTo(dialog.minimumSizeHint())
        width = max(hint.width(), button.width())
        height = max(hint.height(), dialog.minimumSizeHint().height())
        screen = button.screen() or QtGui.QGuiApplication.screenAt(button_bottom)
        available = (
            screen.availableGeometry()
            if screen is not None
            else QtCore.QRect(button_bottom, QtCore.QSize(width, height))
        )
        max_x = max(available.left(), available.right() - width + 1)
        max_y = max(available.top(), available.bottom() - height + 1)
        x = min(max(button_bottom.x(), available.left()), max_x)
        y = min(max(button_bottom.y(), available.top()), max_y)
        dialog.resize(width, height)
        dialog.move(x, y)

    def _install_find_item_popover_dismiss_filter(self) -> None:
        app = QtWidgets.QApplication.instance()
        if app is None or self._find_item_event_filter is not None:
            return
        self._find_item_event_filter = _FindItemPopoverDismissFilter(self._on_find_item_popover_event, app)
        app.installEventFilter(self._find_item_event_filter)

    def _uninstall_find_item_popover_dismiss_filter(self) -> None:
        app = QtWidgets.QApplication.instance()
        if app is not None and self._find_item_event_filter is not None:
            app.removeEventFilter(self._find_item_event_filter)
        self._find_item_event_filter = None

    def _on_find_item_popover_event(self, event_kind: str, global_pos: QtCore.QPoint | None) -> None:
        dialog = self._find_dialog
        if dialog is None or dialog.isHidden():
            self._uninstall_find_item_popover_dismiss_filter()
            return
        if event_kind in {"escape", "deactivate"}:
            self._hide_find_item_popover()
            return
        if event_kind != "mouse" or global_pos is None:
            return
        if self._global_pos_hits_widget(dialog, global_pos):
            return
        button = self._widget.findChild(QtWidgets.QAbstractButton, "button_find_item")
        if button is not None and self._global_pos_hits_widget(button, global_pos):
            return
        self._hide_find_item_popover()

    @staticmethod
    def _global_pos_hits_widget(widget: QtWidgets.QWidget, global_pos: QtCore.QPoint) -> bool:
        if widget.isHidden():
            return False
        return widget.rect().contains(widget.mapFromGlobal(global_pos))

    def set_mode_changed_callback(self, callback: Callable[[str], None] | None) -> None:
        self._mode_changed_callback = callback

    def set_fit_callback(self, callback: Callable[[], None] | None) -> None:
        self._fit_callback = callback

    def set_find_item_callback(self, callback: Callable[[str], bool] | None) -> None:
        self._find_item_callback = callback

    def set_clear_find_item_callback(self, callback: Callable[[], None] | None) -> None:
        self._clear_find_item_callback = callback

    def set_background_color_callback(self, callback: Callable[[QtGui.QColor], None] | None) -> None:
        self._background_color_callback = callback

    def set_current_background_color_callback(self, callback: Callable[[], QtGui.QColor] | None) -> None:
        self._current_background_color_callback = callback

    def set_quick_export_callback(self, callback: Callable[[], bool] | None) -> None:
        self._quick_export_callback = callback
