from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable

from PySide6 import QtCore, QtGui, QtWidgets

if TYPE_CHECKING:
    from project.domain import Project


PROJECT_ROW_HEIGHT = 58


class _ElidedLabel(QtWidgets.QLabel):
    """Single-line label that preserves the row action space when text is long."""

    def __init__(self, text: str, mode: QtCore.Qt.TextElideMode, parent=None) -> None:
        super().__init__(text, parent)
        self._elide_mode = mode

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(QtGui.QPalette.ColorRole.WindowText))
        text = self.fontMetrics().elidedText(
            self.text(),
            self._elide_mode,
            self.contentsRect().width(),
        )
        painter.drawText(
            self.contentsRect(),
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            text,
        )


class ProjectRowWidget(QtWidgets.QWidget):
    """Interactive contents for one recent-project list row."""

    def __init__(
        self,
        project: Project,
        *,
        select_callback: Callable[[Path], None],
        open_callback: Callable[[], None],
        rename_callback: Callable[[Path], None],
        remove_callback: Callable[[Path], None],
        parent: QtWidgets.QWidget,
    ) -> None:
        super().__init__(parent)
        self._project_path = project.root
        self._select_callback = select_callback
        self._open_callback = open_callback
        self.setMinimumHeight(PROJECT_ROW_HEIGHT)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setToolTip(str(project.root))

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 8, 4)
        layout.setSpacing(8)

        text_layout = QtWidgets.QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(3)
        self._label_name = _ElidedLabel(
            project.name,
            QtCore.Qt.TextElideMode.ElideRight,
            self,
        )
        self._label_name.setObjectName("label_project_name")
        self._label_name.setFont(self._title_font(self._label_name.font()))
        self._prepare_label(self._label_name)
        self._label_path = _ElidedLabel(
            str(project.root),
            QtCore.Qt.TextElideMode.ElideMiddle,
            self,
        )
        self._label_path.setObjectName("label_project_path")
        self._prepare_label(self._label_path)
        text_layout.addWidget(self._label_name)
        text_layout.addWidget(self._label_path)
        layout.addLayout(text_layout, 1)

        button = QtWidgets.QToolButton(self)
        button.setObjectName("button_project_actions")
        button.setText("...")
        button.setAccessibleName(f"Actions for {project.name}")
        button.setToolTip(f"Actions for {project.name}")
        button.setAutoRaise(True)
        button.setStyleSheet(
            "QToolButton#button_project_actions::menu-indicator {"
            " image: none; width: 0px; }"
        )
        button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QtWidgets.QMenu(button)
        action_rename = menu.addAction("Rename…")
        action_show = menu.addAction("Open Project Folder")
        action_show.triggered.connect(
            lambda _checked=False: QtGui.QDesktopServices.openUrl(
                QtCore.QUrl.fromLocalFile(str(self._project_path))
            )
        )
        menu.addSeparator()
        action_remove = menu.addAction("Remove")
        action_rename.triggered.connect(
            lambda _checked=False: rename_callback(self._project_path)
        )
        action_remove.triggered.connect(
            lambda _checked=False: remove_callback(self._project_path)
        )
        button.setMenu(menu)
        layout.addWidget(
            button,
            0,
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignTop,
        )

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        self._select_callback(self._project_path)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QtGui.QMouseEvent) -> None:
        self._select_callback(self._project_path)
        self._open_callback()
        event.accept()

    @staticmethod
    def _prepare_label(label: QtWidgets.QLabel) -> None:
        label.setProperty("textRole", "primary")
        label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.NoTextInteraction)
        label.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        label.setMinimumWidth(0)
        label.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )

    @staticmethod
    def _title_font(base_font: QtGui.QFont) -> QtGui.QFont:
        font = QtGui.QFont(base_font)
        if font.pointSizeF() > 0:
            font.setPointSizeF(font.pointSizeF() + 2.0)
        elif font.pixelSize() > 0:
            font.setPixelSize(font.pixelSize() + 3)
        font.setWeight(QtGui.QFont.Weight.Medium)
        return font
