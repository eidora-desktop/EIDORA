from __future__ import annotations

from collections.abc import Sequence

from PySide6 import QtCore, QtWidgets


DetailEntry = tuple[str, str, str]
DetailSection = tuple[str, Sequence[DetailEntry]]


def create_tree_details_panel(
    parent: QtWidgets.QWidget,
    object_name: str,
    sections: Sequence[DetailSection],
) -> tuple[QtWidgets.QWidget, dict[str, QtWidgets.QLabel]]:
    """Build a transparent property panel for a full-width tree child row."""

    panel = QtWidgets.QWidget(parent)
    panel.setObjectName(object_name)
    panel.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    panel.setAutoFillBackground(False)
    layout = QtWidgets.QGridLayout(panel)
    layout.setContentsMargins(10, 8, 10, 8)
    layout.setHorizontalSpacing(14)
    layout.setVerticalSpacing(4)
    layout.setColumnMinimumWidth(0, 96)
    layout.setColumnStretch(1, 1)

    value_labels: dict[str, QtWidgets.QLabel] = {}
    row = 0
    for title, entries in sections:
        if not entries:
            continue
        if row:
            layout.setRowMinimumHeight(row, 4)
            row += 1
        heading = QtWidgets.QLabel(title, panel)
        heading.setProperty("detailRole", "section")
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        layout.addWidget(heading, row, 0, 1, 2)
        row += 1
        for key, name, value in entries:
            name_label = QtWidgets.QLabel(name, panel)
            name_label.setProperty("detailRole", "name")
            name_label.setAlignment(
                QtCore.Qt.AlignmentFlag.AlignLeft
                | QtCore.Qt.AlignmentFlag.AlignTop
            )
            value_label = QtWidgets.QLabel(str(value), panel)
            value_label.setProperty("detailRole", "value")
            value_label.setWordWrap(True)
            layout.addWidget(name_label, row, 0)
            layout.addWidget(value_label, row, 1)
            value_labels[key] = value_label
            row += 1
    return panel, value_labels
