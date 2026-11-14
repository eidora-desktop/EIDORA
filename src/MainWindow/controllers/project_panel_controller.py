from __future__ import annotations

from collections.abc import Callable

from PySide6 import QtCore, QtWidgets

from project.domain import Project
from shared.qt.widgets import require_child


class ProjectPanelController:
    """Bind the shell's project identity and actions to project workflows."""

    def __init__(
        self,
        window: QtWidgets.QMainWindow,
        *,
        switch: Callable[[], object],
        rename: Callable[[Project], object],
        export: Callable[[Project], object],
        delete: Callable[[Project], object],
    ) -> None:
        self._project: Project | None = None
        self._label = require_child(window, QtWidgets.QLabel, "project_label")
        self._label.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        button = require_child(window, QtWidgets.QPushButton, "button_project")
        self._menu = QtWidgets.QMenu(button)
        self._switch_timer = QtCore.QTimer(button)
        self._switch_timer.setSingleShot(True)
        self._switch_timer.timeout.connect(switch)
        switch_action = self._menu.addAction("Switching Projects…")
        font = switch_action.font()
        font.setBold(True)
        switch_action.setFont(font)
        switch_action.triggered.connect(lambda: self._switch_timer.start(0))
        self._menu.addSeparator()
        self._project_actions = []
        for title, callback in (
            ("Rename Project…", rename),
            ("Export Bundle…", export),
            ("Delete From Disk…", delete),
        ):
            if title == "Delete From Disk…":
                self._menu.addSeparator()
            action = self._menu.addAction(title)
            action.triggered.connect(lambda _checked=False, invoke=callback: self._invoke(invoke))
            self._project_actions.append(action)
        button.setMenu(self._menu)
        self.set_project(None)

    def set_project(self, project: Project | None) -> None:
        self._project = project
        self._label.setText(project.name if project is not None else "No project selected")
        self._label.setToolTip(str(project.root) if project is not None else "")
        for action in self._project_actions:
            action.setEnabled(project is not None)

    def _invoke(self, callback: Callable[[Project], object]) -> None:
        if self._project is not None:
            callback(self._project)

    def shutdown(self) -> None:
        self._switch_timer.stop()
        self._menu.close()
