"""Keep keyboard focus visible without changing pointer-use styling or layout."""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets
from shiboken6 import isValid


class _KeyboardFocusFrame(QtWidgets.QFocusFrame):
    """Qt follows the target's geometry and lifetime without taking layout space."""

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        handled = super().eventFilter(watched, event)
        # Native styles can mask the frame to just a checkbox's indicator.
        # Our outline surrounds the whole control, including its label.
        self.clearMask()
        return handled

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(self.palette().color(QtGui.QPalette.ColorRole.WindowText), 2))
        painter.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1), 3, 3)


class KeyboardFocusController(QtCore.QObject):
    def __init__(self, app: QtWidgets.QApplication) -> None:
        super().__init__(app)
        self._app = app
        self._keyboard_navigation = False
        self._frame: _KeyboardFocusFrame | None = None
        app.installEventFilter(self)
        app.focusChanged.connect(self._focus_changed)

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        kind = event.type()
        if kind in (
            QtCore.QEvent.Type.MouseButtonPress,
            QtCore.QEvent.Type.MouseButtonDblClick,
            QtCore.QEvent.Type.TouchBegin,
        ):
            self._set_keyboard_navigation(False)
        elif kind == QtCore.QEvent.Type.KeyPress:
            modifiers = event.modifiers() & ~QtCore.Qt.KeyboardModifier.KeypadModifier
            if not modifiers & (
                QtCore.Qt.KeyboardModifier.ControlModifier
                | QtCore.Qt.KeyboardModifier.AltModifier
                | QtCore.Qt.KeyboardModifier.MetaModifier
            ):
                if event.key() in (QtCore.Qt.Key.Key_Tab, QtCore.Qt.Key.Key_Backtab):
                    self._set_keyboard_navigation(True)
                elif isinstance(watched, QtWidgets.QAbstractButton) and event.key() in (
                    QtCore.Qt.Key.Key_Left, QtCore.Qt.Key.Key_Right,
                    QtCore.Qt.Key.Key_Up, QtCore.Qt.Key.Key_Down,
                    QtCore.Qt.Key.Key_Space, QtCore.Qt.Key.Key_Return,
                    QtCore.Qt.Key.Key_Enter,
                ):
                    self._set_keyboard_navigation(True)
        elif kind == QtCore.QEvent.Type.FocusIn and event.reason() in (
            QtCore.Qt.FocusReason.TabFocusReason,
            QtCore.Qt.FocusReason.BacktabFocusReason,
            QtCore.Qt.FocusReason.ShortcutFocusReason,
        ):
            self._set_keyboard_navigation(True)
        return False

    def _set_keyboard_navigation(self, enabled: bool) -> None:
        self._keyboard_navigation = enabled
        self._set_cue(self._app.focusWidget(), enabled)

    def _focus_changed(self, previous: QtWidgets.QWidget | None, current: QtWidgets.QWidget | None) -> None:
        if isinstance(current, QtWidgets.QAbstractButton):
            group = current.group()
            if group is not None and group.property("tabAllButtons"):
                # Exclusive groups normally leave only one Tab stop after focus
                # enters. Workspace navigation opts into an individual stop for
                # each button, retaining Qt's exclusive selection and arrows.
                for button in group.buttons():
                    button.setFocusPolicy(button.focusPolicy() | QtCore.Qt.FocusPolicy.TabFocus)
        self._set_cue(previous, False)
        self._set_cue(current, self._keyboard_navigation)

    def _set_cue(self, widget: QtWidgets.QWidget | None, enabled: bool) -> None:
        if self._frame is not None and isValid(self._frame):
            self._frame.setWidget(None)
        if widget is None:
            return
        if not isinstance(widget, (QtWidgets.QPushButton, QtWidgets.QToolButton)):
            if enabled and widget.isVisible():
                # Keep one cue per window. Moving a QFocusFrame from a modal
                # dialog to the workspace can leave PySide ownership attached
                # to the dialog and invalidate the watched workspace wrappers
                # when that dialog is destroyed.
                window = widget.window()
                frame = getattr(window, "_eidora_keyboard_focus_frame", None)
                if frame is None or not isValid(frame):
                    frame = _KeyboardFocusFrame(window)
                    window._eidora_keyboard_focus_frame = frame
                self._frame = frame
                self._frame.setWidget(widget)
                self._frame.clearMask()
            return
        if bool(widget.property("keyboardFocus")) == enabled:
            return
        widget.setProperty("keyboardFocus", enabled)
        # Dynamic stylesheet properties need repolishing. Only the old/new
        # focused buttons are touched; no tree scan or layout changes are needed.
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()


def install_keyboard_focus(app: QtWidgets.QApplication) -> KeyboardFocusController:
    controller = getattr(app, "_eidora_keyboard_focus_controller", None)
    if controller is None:
        controller = KeyboardFocusController(app)
        app._eidora_keyboard_focus_controller = controller
    return controller
