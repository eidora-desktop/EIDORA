from __future__ import annotations

import math

from PySide6 import QtCore, QtGui, QtWidgets


_MIN_TICK_SPACING_PX = 28
_MAX_TICK_INTERVALS = 12


def _update_tick_interval(slider: QtWidgets.QSlider) -> None:
    value_span = slider.maximum() - slider.minimum()
    if value_span <= 0:
        slider.setTickInterval(0)
        return
    option = QtWidgets.QStyleOptionSlider()
    slider.initStyleOption(option)
    handle_length = slider.style().pixelMetric(
        QtWidgets.QStyle.PixelMetric.PM_SliderLength,
        option,
        slider,
    )
    usable_width = max(1, slider.width() - handle_length)
    interval_count = max(
        2,
        min(_MAX_TICK_INTERVALS, usable_width // _MIN_TICK_SPACING_PX),
    )
    raw_interval = value_span / interval_count
    magnitude = 10.0 ** math.floor(math.log10(raw_interval))
    normalized = raw_interval / magnitude
    factor = next(
        value
        for value in (1.0, 1.5, 2.0, 2.5, 5.0, 10.0)
        if normalized <= value
    )
    slider.setTickInterval(max(1, int(math.ceil(factor * magnitude))))


class ThresholdSlider(QtWidgets.QSlider):
    """Standard slider that can suppress its directional value highlight."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(QtCore.Qt.Orientation.Horizontal, parent)
        self._highlight_visible = True

    def highlightVisible(self) -> bool:
        return self._highlight_visible

    def setHighlightVisible(self, visible: bool) -> None:
        visible = bool(visible)
        if visible == self._highlight_visible:
            return
        self._highlight_visible = visible
        self.update()

    def setRange(self, minimum: int, maximum: int) -> None:
        super().setRange(minimum, maximum)
        _update_tick_interval(self)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        _update_tick_interval(self)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        if self._highlight_visible:
            super().paintEvent(event)
            return
        painter = QtWidgets.QStylePainter(self)
        groove_option = QtWidgets.QStyleOptionSlider()
        self.initStyleOption(groove_option)
        groove_option.sliderPosition = self.minimum()
        groove_option.sliderValue = self.minimum()
        groove_option.subControls = (
            QtWidgets.QStyle.SubControl.SC_SliderGroove
            | QtWidgets.QStyle.SubControl.SC_SliderTickmarks
        )
        painter.drawComplexControl(
            QtWidgets.QStyle.ComplexControl.CC_Slider,
            groove_option,
        )
        handle_option = QtWidgets.QStyleOptionSlider()
        self.initStyleOption(handle_option)
        handle_option.subControls = QtWidgets.QStyle.SubControl.SC_SliderHandle
        painter.drawComplexControl(
            QtWidgets.QStyle.ComplexControl.CC_Slider,
            handle_option,
        )


class RangeSlider(QtWidgets.QSlider):
    """Horizontal integer slider with independently movable lower and upper handles."""

    lowerValueChanged = QtCore.Signal(int)
    upperValueChanged = QtCore.Signal(int)
    valuesChanged = QtCore.Signal(int, int)

    _LOWER_HANDLE = "lower"
    _UPPER_HANDLE = "upper"

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(QtCore.Qt.Orientation.Horizontal, parent)
        self._lower_value = self.minimum()
        self._upper_value = self.maximum()
        self._active_handle = self._LOWER_HANDLE
        self._pressed_handle: str | None = None
        self._press_offset = 0
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

    def lowerValue(self) -> int:
        return self._lower_value

    def upperValue(self) -> int:
        return self._upper_value

    def setLowerValue(self, value: int) -> None:
        self.setValues(value, self._upper_value)

    def setUpperValue(self, value: int) -> None:
        self.setValues(self._lower_value, value)

    def setValues(self, lower: int, upper: int) -> None:
        bounded_lower = max(self.minimum(), min(self.maximum(), int(lower)))
        bounded_upper = max(self.minimum(), min(self.maximum(), int(upper)))
        if bounded_lower > bounded_upper:
            bounded_lower, bounded_upper = bounded_upper, bounded_lower
        lower_changed = bounded_lower != self._lower_value
        upper_changed = bounded_upper != self._upper_value
        if not lower_changed and not upper_changed:
            return
        self._lower_value = bounded_lower
        self._upper_value = bounded_upper
        self.update()
        if lower_changed:
            self.lowerValueChanged.emit(bounded_lower)
        if upper_changed:
            self.upperValueChanged.emit(bounded_upper)
        self.valuesChanged.emit(bounded_lower, bounded_upper)

    def setRange(self, minimum: int, maximum: int) -> None:
        super().setRange(minimum, maximum)
        self.setValues(self._lower_value, self._upper_value)
        _update_tick_interval(self)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        _update_tick_interval(self)

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtWidgets.QStylePainter(self)
        groove_option = self._style_option(self.minimum())
        groove_option.subControls = (
            QtWidgets.QStyle.SubControl.SC_SliderGroove
            | QtWidgets.QStyle.SubControl.SC_SliderTickmarks
        )
        painter.drawComplexControl(
            QtWidgets.QStyle.ComplexControl.CC_Slider,
            groove_option,
        )

        lower_rect = self._handle_rect(self._lower_value)
        upper_rect = self._handle_rect(self._upper_value)
        selection_left = min(lower_rect.center().x(), upper_rect.center().x())
        selection_right = max(lower_rect.center().x(), upper_rect.center().x())
        highlight_option = self._style_option(self._upper_value)
        highlight_option.subControls = QtWidgets.QStyle.SubControl.SC_SliderGroove
        painter.save()
        painter.setClipRect(
            QtCore.QRect(
                selection_left,
                0,
                selection_right - selection_left + 1,
                self.height(),
            )
        )
        painter.drawComplexControl(
            QtWidgets.QStyle.ComplexControl.CC_Slider,
            highlight_option,
        )
        painter.restore()

        handles = (
            (self._LOWER_HANDLE, self._lower_value),
            (self._UPPER_HANDLE, self._upper_value),
        )
        for handle, value in handles:
            option = self._style_option(value)
            option.subControls = QtWidgets.QStyle.SubControl.SC_SliderHandle
            if handle == self._pressed_handle:
                option.activeSubControls = QtWidgets.QStyle.SubControl.SC_SliderHandle
                option.state |= QtWidgets.QStyle.StateFlag.State_Sunken
            painter.drawComplexControl(
                QtWidgets.QStyle.ComplexControl.CC_Slider,
                option,
            )

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() != QtCore.Qt.MouseButton.LeftButton:
            event.ignore()
            return
        position = event.position().toPoint()
        lower_rect = self._handle_rect(self._lower_value)
        upper_rect = self._handle_rect(self._upper_value)
        if lower_rect.contains(position) and upper_rect.contains(position):
            handle = (
                self._UPPER_HANDLE
                if self._active_handle == self._LOWER_HANDLE
                else self._LOWER_HANDLE
            )
        elif lower_rect.contains(position):
            handle = self._LOWER_HANDLE
        elif upper_rect.contains(position):
            handle = self._UPPER_HANDLE
        else:
            handle = (
                self._LOWER_HANDLE
                if abs(position.x() - lower_rect.center().x())
                <= abs(position.x() - upper_rect.center().x())
                else self._UPPER_HANDLE
            )
        self._active_handle = handle
        self._pressed_handle = handle
        rect = lower_rect if handle == self._LOWER_HANDLE else upper_rect
        self._press_offset = position.x() - rect.center().x()
        if not rect.contains(position):
            self._move_active_handle(self._value_from_pixel(position.x()))
            self._press_offset = 0
        self.setFocus()
        self.update()
        event.accept()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._pressed_handle is None:
            event.ignore()
            return
        self._move_active_handle(
            self._value_from_pixel(event.position().toPoint().x() - self._press_offset)
        )
        event.accept()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._pressed_handle = None
            self.update()
            event.accept()
            return
        event.ignore()

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        step = self.pageStep() if event.key() in {
            QtCore.Qt.Key.Key_PageUp,
            QtCore.Qt.Key.Key_PageDown,
        } else self.singleStep()
        if event.key() in {QtCore.Qt.Key.Key_Left, QtCore.Qt.Key.Key_Down, QtCore.Qt.Key.Key_PageDown}:
            self._move_active_handle(self._active_value() - step)
        elif event.key() in {QtCore.Qt.Key.Key_Right, QtCore.Qt.Key.Key_Up, QtCore.Qt.Key.Key_PageUp}:
            self._move_active_handle(self._active_value() + step)
        elif event.key() == QtCore.Qt.Key.Key_Home:
            self._move_active_handle(self.minimum())
        elif event.key() == QtCore.Qt.Key.Key_End:
            self._move_active_handle(self.maximum())
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def _active_value(self) -> int:
        return (
            self._lower_value
            if self._active_handle == self._LOWER_HANDLE
            else self._upper_value
        )

    def _move_active_handle(self, value: int) -> None:
        if self._active_handle == self._LOWER_HANDLE:
            self.setLowerValue(min(value, self._upper_value))
        else:
            self.setUpperValue(max(value, self._lower_value))

    def _style_option(self, value: int) -> QtWidgets.QStyleOptionSlider:
        option = QtWidgets.QStyleOptionSlider()
        self.initStyleOption(option)
        option.sliderPosition = int(value)
        option.sliderValue = int(value)
        return option

    def _handle_rect(self, value: int) -> QtCore.QRect:
        option = self._style_option(value)
        return self.style().subControlRect(
            QtWidgets.QStyle.ComplexControl.CC_Slider,
            option,
            QtWidgets.QStyle.SubControl.SC_SliderHandle,
            self,
        )

    def _value_from_pixel(self, pixel: int) -> int:
        option = self._style_option(self._active_value())
        handle = self.style().subControlRect(
            QtWidgets.QStyle.ComplexControl.CC_Slider,
            option,
            QtWidgets.QStyle.SubControl.SC_SliderHandle,
            self,
        )
        slider_length = handle.width()
        span = max(0, self.width() - slider_length)
        position = max(0, min(span, int(pixel) - slider_length // 2))
        return QtWidgets.QStyle.sliderValueFromPosition(
            self.minimum(),
            self.maximum(),
            position,
            span,
            option.upsideDown,
        )
