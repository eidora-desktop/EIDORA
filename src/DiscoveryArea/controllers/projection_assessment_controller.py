"""Inline projection assessment; workers receive plain project/selection data only."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from PySide6 import QtCore, QtGui, QtWidgets

from DiscoveryArea.projection_assessment import (
    assess_projection, inspect_projection_selection, memory_text, normalize_projection_settings,
)
from shared.qt.widgets import require_child
from DiscoveryArea.controllers.projection_parameter_editor import parameter_signal


class ProjectionAssessmentController(QtCore.QObject):
    def __init__(self, dialog, read_parameters):
        super().__init__(dialog)
        self._dialog = dialog
        self._read_parameters = read_parameters
        self._panel = require_child(dialog, QtWidgets.QWidget, "panel_projection_assessment")
        self._title = require_child(dialog, QtWidgets.QLabel, "label_assessment_title")
        self._values = {key: require_child(dialog, QtWidgets.QLabel, f"label_assessment_{key}")
                        for key in ("memory", "budget")}
        self._details = require_child(dialog, QtWidgets.QTextBrowser, "edit_assessment_details")
        # Native text browsers can choose a different font from ordinary labels.
        body_font = require_child(dialog, QtWidgets.QLabel, "label_vectors").font()
        for label in self._panel.findChildren(QtWidgets.QLabel):
            label.setFont(body_font)
        title_font = QtGui.QFont(body_font)
        title_font.setBold(True)
        self._title.setFont(title_font)
        self._details.setFont(body_font)
        self._details.document().setDefaultFont(body_font)
        self._details.document().setDocumentMargin(0)
        self._run = require_child(dialog, QtWidgets.QPushButton, "button_run")
        self._source = require_child(dialog, QtWidgets.QComboBox, "combo_embeddings_source")
        self._filter = require_child(dialog, QtWidgets.QComboBox, "combo_filter_preset")
        self._method = require_child(dialog, QtWidgets.QComboBox, "combo_discovery_method")
        self._source.currentIndexChanged.connect(self.refresh)
        self._filter.currentIndexChanged.connect(self.refresh)
        self._method.currentIndexChanged.connect(self.settings_changed)
        require_child(dialog, QtWidgets.QToolButton, "button_refresh_assessment").clicked.connect(self.refresh)
        dialog.finished.connect(self.deactivate)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._poll)
        self._debounce = QtCore.QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(200)
        self._debounce.timeout.connect(self._queue_inspection)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="projection-assessment")
        executor = self._executor
        self.destroyed.connect(lambda: executor.shutdown(wait=False, cancel_futures=True))
        self._future = None
        self._future_revision = 0
        self._pending = None
        self._revision = 0
        self._inputs = None
        self._project = None
        self._active = False
        self._allowed = True
        self._settings_invalid = False
        self._editor_signals = []

    def activate(self, project):
        self._project = project
        self._active = True
        self._panel.setVisible(project is not None)
        self.refresh()

    def deactivate(self, *_):
        self._active = False
        self._revision += 1
        self._inputs = self._pending = None
        self._debounce.stop()
        self._timer.stop()
        # An active task may finish, but holds no controller/widgets. Reopening
        # reuses this single executor and collects/discards its stale result.

    def shutdown(self):
        self.deactivate()
        self._disconnect_editors()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _disconnect_editors(self):
        for signal in self._editor_signals:
            try:
                signal.disconnect(self.settings_changed)
            except (RuntimeError, TypeError):
                pass  # Replaced Designer table editors may already be deleted.
        self._editor_signals.clear()

    def bind_parameter_editors(self):
        self._disconnect_editors()
        for name in ("table_discovery_parameters", "table_advanced_parameters"):
            table = require_child(self._dialog, QtWidgets.QTableWidget, name)
            for row in range(table.rowCount()):
                editor = table.cellWidget(row, 1)
                if editor is None:
                    continue
                signal = parameter_signal(editor)
                signal.connect(self.settings_changed)
                self._editor_signals.append(signal)
        self.settings_changed()

    def refresh(self, *_):
        if not self._active:
            return
        self._debounce.stop()
        self._revision += 1
        self._inputs = self._pending = None
        if self._project is None:
            self._allowed = True
            self._run.setEnabled(True)
            return
        if not self._source.currentData():
            self._render("Cannot run", "Select completed vector data.", "", False)
            return
        self._render("Checking selection…", "Estimating memory…", "", False)
        self._debounce.start()

    def _queue_inspection(self):
        if not self._active or self._project is None:
            return
        self._pending = (self._revision, self._project, str(self._source.currentData() or ""),
                         str(self._filter.currentData() or ""))
        self._poll()
        self._timer.start()

    def _poll(self):
        if not self._active:
            return
        if self._future is not None and self._future.done():
            future, revision = self._future, self._future_revision
            self._future = None
            try:
                inputs = future.result()
            except Exception as exc:
                if revision == self._revision:
                    known_failure = isinstance(exc, (ValueError, FileNotFoundError))
                    self._render("Cannot run" if known_failure else "Assessment unavailable",
                                 str(exc) if known_failure else "Unable to assess this selection. Try refreshing.",
                                 "", not known_failure)
            else:
                if revision == self._revision:
                    self._inputs = inputs
                    self.settings_changed()
        if self._future is None and self._pending is not None:
            revision, project, source, filter_key = self._pending
            self._pending = None
            self._future_revision = revision
            self._future = self._executor.submit(inspect_projection_selection, project, source, filter_key)
        if self._future is None:
            self._timer.stop()

    def settings_changed(self, *_):
        if not self._active or self._project is None:
            return
        if self._inputs is None:
            try:
                normalize_projection_settings(str(self._method.currentData() or ""), self._read_parameters())
            except (ValueError, TypeError, OverflowError) as exc:
                self._render("Cannot run with these settings", str(exc), "", False)
            else:
                if self._settings_invalid:
                    self.refresh()
            return
        try:
            assessment = assess_projection(str(self._method.currentData() or ""),
                                           self._read_parameters(), self._inputs)
        except Exception:
            self._render("Assessment unavailable", "Unable to assess these settings. Try refreshing.", "", True)
            return
        self._render(assessment.title, "", "\n\n".join(assessment.messages),
                     assessment.state != "blocked", assessment=assessment)

    def can_submit(self):
        # Re-evaluate settings and current memory immediately before queuing.
        self.settings_changed()
        return self._allowed

    def _render(self, title, summary, details, allowed, *, assessment=None):
        self._allowed = allowed
        self._settings_invalid = title == "Cannot run with these settings"
        display_title = {"No known resource issue": "Ready", "Review before running": "Review recommended"}.get(title, title)
        self._title.setText("Cannot run" if title.startswith("Cannot run") else display_title)
        role = "error" if title.startswith("Cannot run") else "primary"
        if self._title.property("textRole") != role:
            self._title.setProperty("textRole", role)
            self._title.style().unpolish(self._title)
            self._title.style().polish(self._title)
        self._values["memory"].setText(f"~{memory_text(assessment.estimated_bytes)}" if assessment and assessment.estimated_bytes else "—")
        self._values["budget"].setText(memory_text(assessment.budget_bytes) if assessment and assessment.budget_bytes else "—")
        details = "\n\n".join(text for text in (summary, details) if text)
        self._details.setPlainText(details)
        self._details.document().setTextWidth(max(100, self._details.viewport().width()))
        self._details.setFixedHeight(min(112, int(self._details.document().size().height()) + 4))
        self._details.setVisible(bool(details))
        self._run.setEnabled(allowed)
