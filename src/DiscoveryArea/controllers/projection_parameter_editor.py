"""Typed scalar controls and free-text compound projection parameters."""
from __future__ import annotations

import json

from PySide6 import QtCore, QtWidgets


class OptionalNumberEditor(QtWidgets.QWidget):
    valueChanged = QtCore.Signal()

    def __init__(self, spec):
        super().__init__()
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.mode = QtWidgets.QComboBox(self)
        self.mode.addItem("Automatic" if "automatic" in spec else "None", spec.get("automatic"))
        self.mode.addItem("Custom")
        self.number = number_editor(spec)
        layout.addWidget(self.mode)
        layout.addWidget(self.number, 1)
        custom = isinstance(spec.get("default"), (int, float))
        self.mode.setCurrentIndex(int(custom))
        self.number.setEnabled(custom)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.number.valueChanged.connect(self.valueChanged.emit)

    def _mode_changed(self, index):
        self.number.setEnabled(index == 1)
        self.valueChanged.emit()

    def value(self):
        return self.number.value() if self.mode.currentIndex() == 1 else self.mode.currentData()

    def setValue(self, value):
        if isinstance(value, (int, float)):
            self.number.setValue(value)
            self.mode.setCurrentIndex(1)
        else:
            self.mode.setCurrentIndex(0)


def number_editor(spec):
    if spec.get("type") == "int":
        editor = QtWidgets.QSpinBox()
        editor.setRange(int(spec.get("min", -2_147_483_648)), int(spec.get("max", 2_147_483_647)))
    else:
        editor = QtWidgets.QDoubleSpinBox()
        editor.setDecimals(max(0, min(12, int(spec.get("decimals", 6)))))
        editor.setRange(float(spec.get("min", -1_000_000_000_000)), float(spec.get("max", 1_000_000_000_000)))
        editor.setSingleStep(0.1)
    default = spec.get("default")
    editor.setValue(default if isinstance(default, (int, float)) else max(0, editor.minimum()))
    return editor


def create_parameter_editor(spec, tooltip=""):
    kind, default, choices = spec.get("type"), spec.get("default"), spec.get("choices")
    if choices:
        editor = QtWidgets.QComboBox()
        for choice in choices:
            editor.addItem("None (automatic)" if choice is None else str(choice), choice)
        editor.setCurrentIndex(max(0, editor.findData(default)))
    elif kind == "bool":
        editor = QtWidgets.QCheckBox()
        editor.setChecked(bool(default))
    elif kind in {"int", "float"}:
        editor = OptionalNumberEditor(spec) if spec.get("nullable") or "automatic" in spec else number_editor(spec)
    elif kind == "json":
        editor = QtWidgets.QLineEdit("None" if default is None else json.dumps(default))
    else:
        # Schemas provide the finite choices for nonnumeric values.
        editor = QtWidgets.QComboBox()
        editor.addItem("None" if default is None else str(default), default)
    editor.setToolTip(tooltip)
    editor.setEnabled(not spec.get("readonly", False))
    return editor


def parameter_value(editor):
    if isinstance(editor, QtWidgets.QLineEdit):
        return editor.text().strip()
    if isinstance(editor, QtWidgets.QCheckBox):
        return editor.isChecked()
    if isinstance(editor, QtWidgets.QComboBox):
        return editor.currentData()
    return editor.value()


def parameter_signal(editor):
    if isinstance(editor, QtWidgets.QLineEdit):
        return editor.textChanged
    if isinstance(editor, QtWidgets.QCheckBox):
        return editor.toggled
    if isinstance(editor, QtWidgets.QComboBox):
        return editor.currentIndexChanged
    return editor.valueChanged
