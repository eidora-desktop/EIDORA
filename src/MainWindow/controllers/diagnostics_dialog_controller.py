"""Review and save a plain-text report for manual email submission."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from shared.qt import file_dialogs

from shared.diagnostics.service import build_report, failure_guidance
from shared.qt.diagnostics import report_repository
from shared.qt.ui_loader import load_ui


class DiagnosticsDialogController(QtCore.QObject):
    def __init__(self, repository=None) -> None:
        super().__init__()
        self.repository = repository if repository is not None else report_repository()
        self.dialog = load_ui("MainWindow", "ui/diagnostics_dialog.ui")
        self.dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self._instructions = self.dialog.findChild(QtWidgets.QLabel, "label_instructions")
        self._instructions_text = self._instructions.text()
        self.dialog.installEventFilter(self)
        self._refresh_link_color()
        self.report = self.dialog.findChild(QtWidgets.QPlainTextEdit, "edit_report")
        self.status = self.dialog.findChild(QtWidgets.QLabel, "label_status")
        self.status.hide()
        self.report.setPlainText(build_report(self.repository))
        self._save_button = self.dialog.findChild(QtWidgets.QPushButton, "button_save_report")
        self._copy_button = self.dialog.findChild(QtWidgets.QPushButton, "button_copy_report")
        self._save_button.clicked.connect(self.save)
        self._copy_button.clicked.connect(self.copy)

    def eventFilter(self, watched, event) -> bool:
        if watched is self.dialog and event.type() == QtCore.QEvent.Type.PaletteChange:
            self._refresh_link_color()
        return False

    def _refresh_link_color(self) -> None:
        dark = self.dialog.palette().color(QtGui.QPalette.ColorRole.Window).lightness() < 128
        color = "#7cb7ff" if dark else "#005bbb"
        self._instructions.setText(self._instructions_text.replace(
            '<a href=', f'<a style="color: {color}" href=',
        ))

    def exec(self) -> None:
        try:
            self.repository.mark_reviewed()
            self.dialog.exec()
        finally:
            self.dialog.removeEventFilter(self)
            self._save_button.clicked.disconnect(self.save)
            self._copy_button.clicked.disconnect(self.copy)

    def save(self) -> None:
        filename = f"EIDORA-report-{datetime.now():%Y%m%d-%H%M%S}.txt"
        destination, _ = file_dialogs.get_save_file_name(
            self.dialog, "Save Diagnostic Report", filename, "Text files (*.txt)",
        )
        if not destination:
            return
        self.status.show()
        try:
            self.repository.save_report(Path(destination), self.report.toPlainText())
        except OSError as error:
            self.status.setText("Report could not be saved. " + (failure_guidance(error) or str(error)) +
                                " You can copy the report and paste it into an email instead.")
            return
        self.status.setText("Report saved.")

    def copy(self) -> None:
        QtWidgets.QApplication.clipboard().setText(self.report.toPlainText())
        self.status.setText("Report copied.")
        self.status.show()


def show_diagnostics() -> None:
    DiagnosticsDialogController().exec()
