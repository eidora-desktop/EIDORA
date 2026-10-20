from __future__ import annotations

from dataclasses import dataclass

from PySide6 import QtWidgets


@dataclass(frozen=True)
class NavigationItem:
    """One navigable area in the main window."""

    title: str
    button: QtWidgets.QAbstractButton
    widget: QtWidgets.QWidget

