"""Small binders for the transform rows shown by the Transform panel."""
from __future__ import annotations

from PySide6 import QtWidgets

from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class TransformGridElementController:
    """Own the Designer form and controls for the Snap To Grid row."""

    def __init__(self, parent: QtWidgets.QWidget) -> None:
        self.widget = load_ui(
            "DiscoveryArea", "ui/transform_grid_element.ui", parent
        )
        self.percentage = require_child(
            self.widget, QtWidgets.QSpinBox, "spin_grid_percentage"
        )
        self.format = require_child(
            self.widget, QtWidgets.QComboBox, "combo_grid_format"
        )
        self.apply_button = require_child(
            self.widget, QtWidgets.QPushButton, "button_apply_grid"
        )


class TransformOverlapElementController:
    """Own the Designer form and controls for the No Overlap row."""

    def __init__(self, parent: QtWidgets.QWidget) -> None:
        self.widget = load_ui(
            "DiscoveryArea", "ui/transform_overlap_element.ui", parent
        )
        self.minimum_spacing = require_child(
            self.widget, QtWidgets.QDoubleSpinBox, "spin_minimum_spacing"
        )
        self.progress = require_child(
            self.widget,
            QtWidgets.QProgressBar,
            "progress_no_overlap_calculation",
        )
        self.apply_button = require_child(
            self.widget, QtWidgets.QPushButton, "button_apply_overlap"
        )
