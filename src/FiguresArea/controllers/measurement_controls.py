"""Numeric editor that can represent different printed sizes across plots."""
from PySide6 import QtCore, QtGui, QtWidgets


class MeasurementSpinBox(QtWidgets.QDoubleSpinBox):
    mixedEdited = QtCore.Signal()

    def __init__(self, parent=None):
        self._mixed = False
        super().__init__(parent)

    def set_measurement(self, value, *, mixed=False):
        self._mixed = False
        self.setValue(value)
        self._mixed = mixed
        self.lineEdit().setText(self.textFromValue(self.value()) + ('' if mixed else self.suffix()))

    def textFromValue(self, value):
        return 'Mixed' if self._mixed else super().textFromValue(value)

    def valueFromText(self, text):
        if text.strip().removesuffix(self.suffix()).strip() == 'Mixed':
            return self.value()
        was_mixed = self._mixed
        self._mixed = False
        if was_mixed:
            self.mixedEdited.emit()
        return super().valueFromText(text)

    def validate(self, text, position):
        if text.strip().removesuffix(self.suffix()).strip() == 'Mixed':
            return QtGui.QValidator.State.Intermediate, text, position
        return super().validate(text, position)

    def stepBy(self, steps):
        self._mixed = False
        super().stepBy(steps)
