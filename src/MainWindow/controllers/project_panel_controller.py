from __future__ import annotations

from collections.abc import Callable

from PySide6 import QtCore, QtWidgets
from shiboken6 import isValid

from project.domain import Project
from shared.qt.widgets import require_child


class ProjectPanelController(QtCore.QObject):
    """Bind the shell's project identity and actions to project workflows."""

    def __init__(
        self,
        window: QtWidgets.QMainWindow,
        *,
        switch: Callable[[], object],
        rename: Callable[[Project], object],
        export: Callable[[Project], object],
        delete: Callable[[Project], object],
        methods: Callable[[Project], object],
    ) -> None:
        super().__init__(window)
        self._project: Project | None = None
        self._label = require_child(window, QtWidgets.QPushButton, "project_label")
        button = self._label
        self._menu = QtWidgets.QMenu(button)
        self._switch_timer = QtCore.QTimer(button)
        self._switch_timer.setSingleShot(True)
        self._switch_timer.timeout.connect(switch)
        switch_action = self._menu.addAction("Switch Projects…")
        font = switch_action.font()
        font.setBold(True)
        switch_action.setFont(font)
        switch_action.triggered.connect(lambda: self._switch_timer.start(0))
        self._menu.addSeparator()
        self._project_actions = []
        methods_action = self._menu.addAction("Methods && Citations…")
        methods_action.triggered.connect(lambda: self._invoke(methods))
        self._project_actions.append(methods_action)
        self._menu.addSeparator()
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
        self._menu.aboutToHide.connect(self._reset_button)
        button.installEventFilter(self)
        self.set_project(None)

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if watched is self._label and event.type() == QtCore.QEvent.Type.Paint:
            option = QtWidgets.QStyleOptionButton()
            self._label.initStyleOption(option)
            content = self._label.style().subElementRect(
                QtWidgets.QStyle.SubElement.SE_PushButtonContents, option, self._label
            )
            text = self._label.fontMetrics().elidedText(
                self._label.accessibleName(), QtCore.Qt.TextElideMode.ElideRight, content.width()
            )
            option.text = text.replace("&", "&&")
            painter = QtWidgets.QStylePainter(self._label)
            painter.drawControl(QtWidgets.QStyle.ControlElement.CE_PushButton, option)
            return True
        if watched is self._label and event.type() in (
            QtCore.QEvent.Type.MouseButtonPress,
            QtCore.QEvent.Type.MouseButtonRelease,
        ) and event.button() == QtCore.Qt.MouseButton.LeftButton:
            if event.type() == QtCore.QEvent.Type.MouseButtonPress:
                self._label.setFocus(QtCore.Qt.FocusReason.MouseFocusReason)
                self._label.setDown(True)
                self._menu.popup(event.globalPosition().toPoint())
            return True
        return False

    @QtCore.Slot()
    def _reset_button(self) -> None:
        if isValid(self._label):
            self._label.setDown(False)

    def set_project(self, project: Project | None) -> None:
        self._project = project
        name = project.name if project is not None else "No project selected"
        self._label.setText(name.replace("&", "&&"))
        self._label.setAccessibleName(name)
        self._label.setToolTip(f"{name}\n{project.root}" if project is not None else "")
        for action in self._project_actions:
            action.setEnabled(project is not None)

    def _invoke(self, callback: Callable[[Project], object]) -> None:
        if self._project is not None:
            callback(self._project)

    def shutdown(self) -> None:
        self._label.removeEventFilter(self)
        self._switch_timer.stop()
        self._menu.close()
