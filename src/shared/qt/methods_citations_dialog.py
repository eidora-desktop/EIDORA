from __future__ import annotations

from pathlib import Path
import re

from PySide6 import QtCore, QtGui, QtWidgets

from shared.methods_report import MethodsReport, MethodsReportExporter
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class MethodsCitationsDialogController:
    """Preview and export a saved methods report."""

    def exec(self, report: MethodsReport) -> tuple[Path, Path] | None:
        dialog = load_ui("shared.qt", "ui/methods_citations_dialog.ui", None)
        if not isinstance(dialog, QtWidgets.QDialog):
            return None
        dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        title = require_child(dialog, QtWidgets.QLabel, "label_methods_citations_title")
        browser = require_child(dialog, QtWidgets.QTextBrowser, "browser_methods_citations")
        button_methods = require_child(dialog, QtWidgets.QPushButton, "button_copy_methods")
        button_caption = require_child(dialog, QtWidgets.QPushButton, "button_copy_caption")
        button_report = require_child(dialog, QtWidgets.QPushButton, "button_copy_report")
        button_bibtex = require_child(dialog, QtWidgets.QPushButton, "button_copy_bibtex")
        button_save = require_child(dialog, QtWidgets.QPushButton, "button_save_report")
        button_close = require_child(dialog, QtWidgets.QPushButton, "button_close")
        title.setText(report.title)

        def refresh_preview(*_args) -> None:
            palette = QtGui.QPalette(browser.palette())
            palette.setColor(QtGui.QPalette.ColorRole.Link, QtWidgets.QApplication.palette().color(QtGui.QPalette.ColorRole.Text))
            browser.setPalette(palette)
            browser.setMarkdown(report.markdown())
            document = browser.document()
            document.setDocumentMargin(20)
            block = document.begin()
            while block.isValid():
                cursor = QtGui.QTextCursor(block)
                block_format = block.blockFormat()
                heading = block_format.headingLevel()
                if heading:
                    block_format.setTopMargin(0 if heading == 1 else 28)
                    block_format.setBottomMargin(12)
                elif cursor.currentTable() is None:
                    block_format.setBottomMargin(10)
                cursor.setBlockFormat(block_format)
                block = block.next()
            for frame in document.rootFrame().childFrames():
                if isinstance(frame, QtGui.QTextTable):
                    table_format = frame.format()
                    table_format.setTopMargin(8)
                    table_format.setCellPadding(6)
                    table_format.setCellSpacing(0)
                    frame.setFormat(table_format)

        refresh_preview()
        button_bibtex.setEnabled(bool(report.bibliography()))
        saved: tuple[Path, Path] | None = None

        copy_buttons = (button_methods, button_caption, button_report, button_bibtex)
        copy_labels = {button: button.text() for button in copy_buttons}
        for button in copy_buttons:
            button.setMinimumWidth(button.sizeHint().width())
        copy_feedback_timer = QtCore.QTimer(dialog)
        copy_feedback_timer.setSingleShot(True)
        copy_feedback_timer.setInterval(2000)

        def reset_copy_labels() -> None:
            for button, label in copy_labels.items():
                button.setText(label)

        def copy(button: QtWidgets.QPushButton, text: str) -> None:
            clipboard = QtGui.QGuiApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(text)
                reset_copy_labels()
                button.setText("Copied ✓")
                copy_feedback_timer.start()

        def save() -> None:
            nonlocal saved
            stem = re.sub(r"[^A-Za-z0-9_-]+", "_", report.title.split(" —", 1)[0]).strip("_") or "methods"
            path, _selected = QtWidgets.QFileDialog.getSaveFileName(
                dialog,
                "Save Methods and Citations",
                str(Path.home() / f"{stem}-methods.md"),
                "Markdown files (*.md)",
            )
            if not path:
                return
            try:
                saved = MethodsReportExporter.save_pair(Path(path), report)
            except Exception as exc:  # noqa: BLE001
                QtWidgets.QMessageBox.warning(dialog, "Methods Export Failed", str(exc))
                return
            dialog.accept()

        connections = [
            (button_methods.clicked, lambda: copy(button_methods, report.methods_text)),
            (button_caption.clicked, lambda: copy(button_caption, report.caption_text())),
            (button_report.clicked, lambda: copy(button_report, report.markdown())),
            (button_bibtex.clicked, lambda: copy(button_bibtex, report.bibliography())),
            (copy_feedback_timer.timeout, reset_copy_labels),
            (button_save.clicked, save),
            (button_close.clicked, dialog.reject),
        ]
        app = QtWidgets.QApplication.instance()
        if app is not None:
            connections.append((app.paletteChanged, refresh_preview))
        for signal, slot in connections:
            signal.connect(slot)
        try:
            dialog.exec()
        finally:
            copy_feedback_timer.stop()
            for signal, slot in connections:
                signal.disconnect(slot)
        return saved
