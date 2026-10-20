from __future__ import annotations

from PySide6 import QtWidgets


def show_error(parent: QtWidgets.QWidget | None, title: str, message: str) -> None:
    QtWidgets.QMessageBox.critical(parent, title, message)


def show_info(parent: QtWidgets.QWidget | None, title: str, message: str) -> None:
    QtWidgets.QMessageBox.information(parent, title, message)

