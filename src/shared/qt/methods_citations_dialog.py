from __future__ import annotations

from pathlib import Path
import re
from collections.abc import Callable, Sequence

from PySide6 import QtCore, QtGui, QtWidgets

from shared.qt import file_dialogs

from shared.methods_report import MethodsReport, MethodsReportExporter
from shared.persistence.atomic_write import write_text_atomic
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class MethodsCitationsDialogController:
    """Preview and export a saved methods report."""

    def exec(self, report: MethodsReport | None = None, *,
             sources: Sequence[tuple[str, str]] = (),
             load_report: Callable[[str], MethodsReport] | None = None) -> tuple[Path, Path] | None:
        dialog = load_ui("shared.qt", "ui/methods_citations_dialog.ui", None)
        if not isinstance(dialog, QtWidgets.QDialog):
            return None
        dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        title = require_child(dialog, QtWidgets.QLabel, "label_methods_citations_title")
        browser = require_child(dialog, QtWidgets.QTextBrowser, "browser_methods_citations")
        scope = require_child(dialog, QtWidgets.QLabel, "label_report_scope")
        source_panel = require_child(dialog, QtWidgets.QWidget, "panel_report_source")
        source_combo = require_child(dialog, QtWidgets.QComboBox, "combo_report_source")
        details = require_child(dialog, QtWidgets.QWidget, "panel_report_details")
        details_browser = require_child(dialog, QtWidgets.QTextBrowser, "browser_report_details")
        button_details = require_child(dialog, QtWidgets.QToolButton, "button_report_details")
        button_bibtex = require_child(dialog, QtWidgets.QPushButton, "button_copy_bibtex")
        button_save = require_child(dialog, QtWidgets.QPushButton, "button_save_report")
        button_close = require_child(dialog, QtWidgets.QPushButton, "button_close")
        source_panel.setVisible(load_report is not None)
        source_combo.addItem("Choose a saved result…", "")
        for key, label in sources:
            source_combo.addItem(label, key)
        details.hide()

        def set_markdown(view: QtWidgets.QTextBrowser, markdown: str) -> None:
            palette = QtGui.QPalette(view.palette())
            palette.setColor(QtGui.QPalette.ColorRole.Link, QtWidgets.QApplication.palette().color(QtGui.QPalette.ColorRole.Text))
            view.setPalette(palette)
            view.setMarkdown(markdown)
            document = view.document()
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

        saved: tuple[Path, Path] | None = None
        load_error = ""

        copy_label = button_bibtex.text()
        button_bibtex.setMinimumWidth(button_bibtex.sizeHint().width())
        copy_feedback_timer = QtCore.QTimer(dialog)
        copy_feedback_timer.setSingleShot(True)
        copy_feedback_timer.setInterval(2000)

        def reset_copy_label() -> None:
            button_bibtex.setText(copy_label)

        def refresh_preview(*_args) -> None:
            for button in (button_bibtex, button_save, button_details):
                button.setEnabled(report is not None)
            has_details = report is not None and report.has_details
            button_details.setVisible(has_details)
            details.setVisible(has_details and button_details.isChecked())
            if report is None:
                dialog.setWindowTitle("Methods & Citations")
                title.setText("Methods & Citations")
                scope.setText("Choose a saved result to include the analysis steps that produced it." if sources
                              else "No saved analysis results are available in this project yet.")
                browser.clear()
                details_browser.clear()
                if load_error:
                    scope.setText(source_combo.currentText())
                    browser.setPlainText(load_error)
                return
            title.setText(report.title)
            scope.setText("Scope: " + (report.scope or report.title.split(" —", 1)[0]))
            dialog.setWindowTitle("Citation" if report.is_reference_only else "Methods & Citations")
            set_markdown(browser, report.brief_markdown(reference_links=True))
            set_markdown(details_browser, report.details_markdown())
            button_bibtex.setEnabled(bool(report.publication_references()))
            export_bibtex.setEnabled(button_bibtex.isEnabled())
            export_brief.setText("Reference Text + BibTeX…" if report.is_reference_only else "Brief Methods + References…")
            export_full.setVisible(report.has_details)

        def toggle_details(expanded: bool) -> None:
            details.setVisible(expanded)
            button_details.setArrowType(QtCore.Qt.ArrowType.DownArrow if expanded else QtCore.Qt.ArrowType.RightArrow)

        def select_report(*_args) -> None:
            nonlocal report, load_error
            report = None
            copy_feedback_timer.stop()
            reset_copy_label()
            button_details.setChecked(False)
            key = source_combo.currentData()
            load_error = ""
            if key and load_report is not None:
                try:
                    report = load_report(key)
                except Exception as exc:  # A failed source must never leave another result copyable.
                    load_error = f"Methods report unavailable: {exc}"
            refresh_preview()

        def copy_bibtex() -> None:
            if report is None:
                return
            clipboard = QtGui.QGuiApplication.clipboard()
            if clipboard is not None:
                clipboard.setText(report.bibliography())
                button_bibtex.setText("Copied ✓")
                copy_feedback_timer.start()

        def open_link(url: QtCore.QUrl) -> None:
            if url.scheme() == "eidora-reference" and report is not None:
                try:
                    index = int(url.path())
                except ValueError:
                    return
                if 0 <= index < len(report.publication_references()):
                    MethodsCitationsDialogController().exec(report.single_reference(index))
            elif url.scheme() in {"https", "http", "mailto"}:
                QtGui.QDesktopServices.openUrl(url)

        def save(*, brief: bool = False, bibtex_only: bool = False) -> None:
            nonlocal saved
            if report is None:
                return
            stem = re.sub(r"[^A-Za-z0-9_-]+", "_", report.title.split(" —", 1)[0]).strip("_") or "methods"
            suffix = "citation" if report.is_reference_only else "methods"
            path, _selected = file_dialogs.get_save_file_name(
                dialog,
                "Save Citation" if report.is_reference_only else "Save Methods and Citations",
                str(Path.home() / (f"{stem}-references.bib" if bibtex_only else f"{stem}-{suffix}.md")),
                "BibTeX files (*.bib)" if bibtex_only else "Markdown files (*.md)",
            )
            if not path:
                return
            try:
                if bibtex_only:
                    write_text_atomic(Path(path).with_suffix(".bib"), report.bibliography())
                else:
                    saved = MethodsReportExporter.save_pair(Path(path), report, brief=brief)
            except Exception as exc:  # noqa: BLE001
                QtWidgets.QMessageBox.warning(dialog, "Methods Export Failed", str(exc))
                return
            dialog.accept()

        export_menu = QtWidgets.QMenu(button_save)
        export_brief = export_menu.addAction("Brief Methods + References…")
        export_full = export_menu.addAction("Full Report + References…")
        export_bibtex = export_menu.addAction("BibTeX…")
        button_save.setMenu(export_menu)
        connections = [
            (button_bibtex.clicked, copy_bibtex),
            (copy_feedback_timer.timeout, reset_copy_label),
            (browser.anchorClicked, open_link),
            (export_brief.triggered, lambda: save(brief=True)),
            (export_full.triggered, lambda: save()),
            (export_bibtex.triggered, lambda: save(bibtex_only=True)),
            (button_details.toggled, toggle_details),
            (source_combo.currentIndexChanged, select_report),
            (button_close.clicked, dialog.reject),
        ]
        app = QtWidgets.QApplication.instance()
        if app is not None:
            connections.append((app.paletteChanged, refresh_preview))
        for signal, slot in connections:
            signal.connect(slot)
        refresh_preview()
        try:
            dialog.exec()
        finally:
            copy_feedback_timer.stop()
            for signal, slot in connections:
                signal.disconnect(slot)
        return saved
