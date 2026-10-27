from __future__ import annotations

from collections.abc import Callable

from PySide6 import QtCore, QtGui, QtWidgets

from MainWindow.controllers.tour_spotlight import TourSpotlight
from MainWindow.navigation import NavigationItem
from MainWindow.tour_steps import TourStep, WORKSPACE_NAMES, load_tour_steps


class WorkspaceTourController(QtCore.QObject):
    """Guide navigation with one event-positioned spotlight and a Designer card."""

    active_changed = QtCore.Signal(bool)
    started = QtCore.Signal()

    def __init__(
        self,
        window: QtWidgets.QMainWindow,
        action: QtGui.QAction,
        navigation: list[NavigationItem],
        show_area: Callable[[int], bool],
        show_status: Callable[[str], None],
    ) -> None:
        super().__init__(window)
        self._window = window
        self._action = action
        self._navigation = navigation
        self._show_area = show_area
        self._show_status = show_status
        self._dialog: TourSpotlight | None = None
        self._steps: tuple[TourStep, ...] = ()
        self._step = 0
        self._connections = []
        self._tracked_widgets = set()
        self._application_active = True
        self._has_started = False
        self._position_timer = QtCore.QTimer(self)
        self._position_timer.setSingleShot(True)
        self._position_timer.timeout.connect(self._present)
        self._action.triggered.connect(self.start)

    @property
    def is_active(self) -> bool:
        return self._dialog is not None

    def update_availability(self, workspaces_loaded: bool) -> None:
        ready = workspaces_loaded and len(self._navigation) == len(WORKSPACE_NAMES)
        self._action.setEnabled(ready)
        self._action.setToolTip(
            "Explore the five workspaces and their panels." if ready else
            "The tour will be available when all workspaces have finished loading."
        )
        if not ready and self._dialog is not None:
            self._stop("The workspaces are no longer available.")

    def _workspaces_available(self) -> bool:
        return len(self._navigation) == len(WORKSPACE_NAMES) and all(
            item.button.isEnabled() for item in self._navigation
        )

    def start(self) -> None:
        if not self._action.isEnabled():
            return
        self.close()
        try:
            self._steps = load_tour_steps()
        except (OSError, ValueError) as error:
            self._show_status(f"Cannot open tour: check tour_text.toml. {error}")
            return
        self._step = 0
        self._application_active = True
        self._has_started = False
        dialog = self._dialog = TourSpotlight(self._window)
        self.active_changed.emit(True)
        self._connections = [
            (dialog.findChild(QtWidgets.QPushButton, "button_next").clicked, self.next_step),
            (dialog.findChild(QtWidgets.QPushButton, "button_back").clicked, self.previous_step),
            (dialog.findChild(QtWidgets.QToolButton, "button_close").clicked, self.close),
            (dialog.finished, self.close),
            (QtWidgets.QApplication.instance().paletteChanged, self._schedule_position),
        ]
        for signal, slot in self._connections:
            signal.connect(slot)
        QtWidgets.QApplication.instance().installEventFilter(self)
        self._show_step()
        if self._dialog is not None:
            dialog.activateWindow()
            dialog.findChild(QtWidgets.QPushButton, "button_next").setFocus()

    def next_step(self) -> None:
        if self._dialog is None:
            return
        if self._step == len(self._steps) - 1:
            self.close()
            self._show_status("Tour complete")
            return
        self._step += 1
        self._show_step()

    def previous_step(self) -> None:
        if self._dialog is not None and self._step > 0:
            self._step -= 1
            self._show_step()

    def _show_step(self) -> None:
        step = self._steps[self._step]
        if not self._action.isEnabled() or not self._workspaces_available() or not self._show_area(step.workspace):
            self._stop("This workspace is no longer available.")
            return
        # Navigation can synchronously refresh availability and close the tour.
        if self._dialog is None:
            return
        self._tracked_widgets = {self._window}
        page = self._navigation[step.workspace].widget
        target = page.findChild(QtWidgets.QWidget, step.target) if step.target else None
        if step.target and target is None:
            self._stop("This panel is no longer available.")
            return
        for widget in [target, page, *(item.button for item in self._navigation)]:
            while widget is not None:
                self._tracked_widgets.add(widget)
                widget = widget.parentWidget()
        self._present()
        # Settle stacked-page and splitter layouts once; there is no polling.
        self._schedule_position()

    def _update_card(self, paused: bool) -> None:
        step = self._steps[self._step]
        dialog = self._dialog
        texts = {
            "label_workspace": WORKSPACE_NAMES[step.workspace].upper() if step.target else "WORKSPACES",
            "label_title": "Tour paused" if paused else step.title,
            "label_description": "You switched workspaces. Next continues the tour, or use Back to revisit a step." if paused else step.description,
            "label_progress": f"{self._step + 1} of {len(self._steps)}",
        }
        for name, text in texts.items():
            dialog.findChild(QtWidgets.QLabel, name).setText(text)
        dialog.findChild(QtWidgets.QPushButton, "button_next").setText(
            "Finish" if self._step == len(self._steps) - 1 else "Next"
        )
        dialog.findChild(QtWidgets.QPushButton, "button_back").setEnabled(self._step > 0)

    def _widget_rect(self, widget: QtWidgets.QWidget) -> QtCore.QRect:
        rect = QtCore.QRect(widget.mapToGlobal(QtCore.QPoint()), widget.size())
        parent = widget.parentWidget()
        while parent is not None:
            rect = rect.intersected(QtCore.QRect(parent.mapToGlobal(QtCore.QPoint()), parent.size()))
            parent = parent.parentWidget()
        rect.translate(-self._dialog.mapToGlobal(QtCore.QPoint()))
        return rect

    def _present(self) -> None:
        dialog = self._dialog
        if dialog is None:
            return
        if not self._application_active or not self._window.isVisible() or self._window.isMinimized():
            dialog.hide()
            return
        if not self._workspaces_available():
            self._stop("A required workspace is no longer available.")
            return
        bounds = QtCore.QRect(self._window.mapToGlobal(QtCore.QPoint()), self._window.size())
        screen = self._window.screen()
        if screen is not None:
            bounds = bounds.intersected(screen.availableGeometry())
        dialog.setGeometry(bounds)
        step = self._steps[self._step]
        page = self._navigation[step.workspace].widget
        paused = not page.isVisibleTo(self._window)
        if paused:
            active = next((item for item in self._navigation if item.widget.isVisibleTo(self._window)), None)
            highlights = [self._widget_rect((active or self._navigation[step.workspace]).button)]
        elif step.target is None:
            bar = QtCore.QRect()
            for item in self._navigation:
                bar = bar.united(self._widget_rect(item.button))
            highlights = [bar]
        else:
            target = page.findChild(QtWidgets.QWidget, step.target)
            if target is None or not target.isVisibleTo(page) or target.width() < 10 or target.height() < 10:
                self._stop("This panel is hidden. Reopen it before restarting the tour.")
                return
            highlights = [self._widget_rect(target), self._widget_rect(self._navigation[step.workspace].button)]
        self._update_card(paused)
        if not dialog.place_card(highlights, intro=step.target is None or paused):
            self._stop("There isn't enough room beside this panel. Enlarge the window or adjust its panels, then restart the tour.")
            return
        dialog.show()
        dialog.raise_()
        if not self._has_started:
            self._has_started = True
            self.started.emit()

    def _schedule_position(self, *_args) -> None:
        if self._dialog is not None:
            self._position_timer.start(0)

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if self._dialog is None:
            return False
        kind = event.type()
        if kind == QtCore.QEvent.Type.ApplicationDeactivate:
            self._application_active = False
            self._dialog.hide()
        elif kind == QtCore.QEvent.Type.ApplicationActivate:
            self._application_active = True
            self._present()
        elif watched is self._window and kind == QtCore.QEvent.Type.Close:
            self.close()
        elif watched is self._window and kind == QtCore.QEvent.Type.Hide:
            self._dialog.hide()
        elif watched in self._tracked_widgets and kind in (
            QtCore.QEvent.Type.Move, QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.LayoutRequest,
            QtCore.QEvent.Type.Show, QtCore.QEvent.Type.Hide, QtCore.QEvent.Type.WindowStateChange,
            QtCore.QEvent.Type.EnabledChange,
        ):
            self._schedule_position()
        elif kind == QtCore.QEvent.Type.KeyPress:
            target = watched.window() if isinstance(watched, QtWidgets.QWidget) else watched
            # Keyboard focus can also be inside Discovery's native QQuickWindow.
            while isinstance(target, QtGui.QWindow) and target.parent() is not None:
                target = target.parent()
            if target in (self._window, self._dialog, self._window.windowHandle(), self._dialog.windowHandle()):
                key = event.key()
                if key == QtCore.Qt.Key.Key_Escape:
                    self.close()
                    return True
                if not self._application_active or not self._dialog.isVisible():
                    return False
                modifiers = event.modifiers()
                forward = modifiers == QtCore.Qt.KeyboardModifier.NoModifier and key in (
                    QtCore.Qt.Key.Key_Tab, QtCore.Qt.Key.Key_Right, QtCore.Qt.Key.Key_Down,
                )
                backward = (
                    modifiers == QtCore.Qt.KeyboardModifier.NoModifier
                    and key in (QtCore.Qt.Key.Key_Left, QtCore.Qt.Key.Key_Up, QtCore.Qt.Key.Key_Backtab)
                ) or (
                    modifiers == QtCore.Qt.KeyboardModifier.ShiftModifier
                    and key in (QtCore.Qt.Key.Key_Tab, QtCore.Qt.Key.Key_Backtab)
                )
                if forward or backward:
                    # A held key must not skip steps or finish the tour.
                    if not event.isAutoRepeat():
                        (self.next_step if forward else self.previous_step)()
                    return True
        return False

    def _stop(self, reason: str) -> None:
        self.close()
        self._show_status(f"Tour stopped: {reason}")

    def close(self) -> None:
        self._position_timer.stop()
        self._tracked_widgets.clear()
        if self._dialog is None:
            return
        QtWidgets.QApplication.instance().removeEventFilter(self)
        dialog, self._dialog = self._dialog, None
        for signal, slot in self._connections:
            signal.disconnect(slot)
        self._connections.clear()
        dialog.hide()
        dialog.deleteLater()
        self.active_changed.emit(False)

    def shutdown(self) -> None:
        self.close()
        self._action.triggered.disconnect(self.start)
