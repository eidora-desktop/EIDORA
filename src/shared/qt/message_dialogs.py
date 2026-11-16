from __future__ import annotations

import sys

from PySide6 import QtWidgets
from shared.diagnostics.service import record_failure


def show_error(parent: QtWidgets.QWidget | None, title: str, message: str) -> None:
    error = sys.exception()
    guidance = record_failure(title, error if error is not None else message)
    parts = [str(message)]
    if guidance:
        parts.append(guidance)
    parts.append("To help improve EIDORA, use Diagnostics... in Help or the project chooser "
                 "and email the report to mail@eidora.org.")
    QtWidgets.QMessageBox.critical(parent, title, "\n\n".join(parts))


def show_info(parent: QtWidgets.QWidget | None, title: str, message: str) -> None:
    QtWidgets.QMessageBox.information(parent, title, message)

