from __future__ import annotations

from importlib import resources
from pathlib import Path

from PySide6 import QtCore, QtWidgets
from PySide6.QtUiTools import QUiLoader

from shared.qt.icons import apply_icon_color
from shared.qt.theme import prepare_surface_widgets


class UiLoadError(RuntimeError):
    """Raised when a Qt Designer file cannot be loaded."""


def ui_path(package: str, relative_path: str) -> Path:
    path = resources.files(package).joinpath(relative_path)
    if not path.is_file():
        raise UiLoadError(f"UI file not found: {package}:{relative_path}")
    return Path(str(path))


def load_ui(package: str, relative_path: str, parent: QtWidgets.QWidget | None = None) -> QtWidgets.QWidget:
    path = ui_path(package, relative_path)
    file = QtCore.QFile(str(path))
    if not file.open(QtCore.QIODevice.OpenModeFlag.ReadOnly):
        raise UiLoadError(f"Could not open UI file: {path}")

    loader = QUiLoader()
    previous_cwd = QtCore.QDir.currentPath()
    QtCore.QDir.setCurrent(str(path.parent))
    try:
        widget = loader.load(file, parent)
    finally:
        QtCore.QDir.setCurrent(previous_cwd)
        file.close()

    if widget is None:
        raise UiLoadError(f"Could not load UI file: {path}")
    prepare_surface_widgets(widget)
    apply_icon_color(widget)
    return widget
