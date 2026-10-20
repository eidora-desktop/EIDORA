from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from typing import TYPE_CHECKING

from PySide6 import QtWidgets

from shared.qt.ui_loader import load_ui

if TYPE_CHECKING:
    from project.domain import Project
    from shared.qt.activity import ActivityManager


class UiPanelController:
    """Small owner for one Designer UI file."""

    package: str
    ui_path: str

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        status_callback: Callable[[str], None] | None = None,
        activity_manager: ActivityManager | None = None,
    ) -> None:
        self._widget = load_ui(self.package, self.ui_path, parent)
        self._project: Project | None = None
        self._status_callback = status_callback
        self._activity_manager = activity_manager
        self._wire()

    @property
    def widget(self) -> QtWidgets.QWidget:
        return self._widget

    def set_project(self, project: Project | None) -> None:
        self._project = project

    def shutdown(self) -> None:
        pass

    def _wire(self) -> None:
        pass

    def _show_status(self, message: str) -> None:
        if self._status_callback is not None:
            self._status_callback(message)

    def _busy(self, message: str):
        if self._activity_manager is None:
            return nullcontext()
        return self._activity_manager.busy(message)
