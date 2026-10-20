from __future__ import annotations

from PySide6 import QtCore, QtWidgets
from EmbeddingsArea.domain import MediaContentType, MediaLinkRequest
from ProjectArea.media_service import preview_links
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class MediaLinkDialogController:
    """A bounded preview of one user-selected relationship."""
    def __init__(self, project, source, columns, saved_request, jobs, *, relink=False):
        self.dialog = load_ui("ProjectArea", "ui/media_link_dialog.ui", None)
        self.dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self._project, self._source, self._jobs = project, source, jobs
        self._alive, self._ticket, self._job_id = True, 0, None
        self.plan = None
        self._relink = relink
        self.dialog.setWindowTitle("Relink Media to Metadata" if relink else "Link Media to Metadata")
        self._connections = []
        self.source_field = self._child(QtWidgets.QComboBox, "combo_source_field")
        self.column = self._child(QtWidgets.QComboBox, "combo_metadata_column")
        self.relationship = self._child(QtWidgets.QLabel, "label_relationship")
        self.summary = self._child(QtWidgets.QLabel, "label_summary")
        self.examples = self._child(QtWidgets.QComboBox, "combo_examples")
        self.table = self._child(QtWidgets.QTableWidget, "table_examples")
        self.buttons = self._child(QtWidgets.QDialogButtonBox, "button_box")
        self.apply_button = self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Apply)
        self.apply_button.setEnabled(False)
        self.source_field.addItem("Relative path", "relative_path")
        if source.content_type == MediaContentType.TEXT:
            self.source_field.addItem("Record ID", "identifier")
        for name in columns:
            self.column.addItem(name, name)
        for value in ("matched", "unmatched", "ambiguous"):
            self.examples.addItem(value.title(), value)
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Source value", "Metadata ID", "Result"])
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        supported_saved = saved_request is not None and (
            saved_request.source_field in {"relative_path", "identifier"}
            and saved_request.comparison == "exact"
            and not saved_request.ignore_case
            and not saved_request.create_missing
            and self.source_field.findData(saved_request.source_field) >= 0
            and saved_request.metadata_column in columns
        )
        if supported_saved:
            self.source_field.setCurrentIndex(self.source_field.findData(saved_request.source_field))
            self.column.setCurrentIndex(self.column.findData(saved_request.metadata_column))
        elif saved_request is not None:
            # Never reinterpret a saved filename/stem rule as a relative path.
            self.source_field.setCurrentIndex(-1)
            self.column.setCurrentIndex(-1)
        else:
            preferred = "relative_path" if "relative_path" in columns else "source_key"
            if preferred in columns:
                if preferred == "source_key" and self.source_field.findData("identifier") >= 0:
                    self.source_field.setCurrentIndex(self.source_field.findData("identifier"))
                self.column.setCurrentIndex(self.column.findData(preferred))
        self._debounce = QtCore.QTimer(self.dialog)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(200)
        self._connect(self._debounce.timeout, self._start_preview)
        for combo in (self.source_field, self.column):
            self._connect(combo.currentIndexChanged, self._configuration_changed)
        self._connect(self.examples.currentIndexChanged, self._render_examples)
        self._connect(self.apply_button.clicked, self.dialog.accept)
        self._connect(self.buttons.rejected, self.dialog.reject)
        refresh = self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Reset)
        refresh.setText("Refresh Preview")
        self._connect(refresh.clicked, self._configuration_changed)
        self._connect(self.dialog.destroyed, self._destroyed)

    def _child(self, kind, name):
        return require_child(self.dialog, kind, name)

    def _connect(self, signal, handler):
        signal.connect(handler)
        self._connections.append((signal, handler))

    def exec(self):
        self._configuration_changed()
        self._debounce.start(0)
        try:
            return self.plan if self.dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted else None
        finally:
            self.dispose()

    def dispose(self):
        if not self._alive:
            return
        self._alive = False
        self._debounce.stop()
        if self._job_id:
            self._jobs.cancel(self._job_id)
        for signal, handler in self._connections:
            signal.disconnect(handler)
        self._connections.clear()

    def _destroyed(self):
        self._alive = False
        if self._job_id:
            self._jobs.cancel(self._job_id)
        self._connections.clear()

    def _configuration_changed(self, *_):
        self._ticket += 1
        if self._job_id:
            self._jobs.cancel(self._job_id)
        self.plan = None
        self.apply_button.setEnabled(False)
        self.relationship.setText(f"{self.source_field.currentText()} → {self.column.currentText()}")
        self.summary.setText(
            "Choose a relative path or record ID and a metadata column. Existing links are unchanged."
            if self.source_field.currentIndex() < 0 or self.column.currentIndex() < 0
            else "Analyzing matches in the background…"
        )
        self.table.setRowCount(0)
        self._debounce.start()

    def _start_preview(self):
        if not self._alive or self.column.currentIndex() < 0 or self.source_field.currentIndex() < 0:
            return
        request = MediaLinkRequest(
            self._source.id, self.source_field.currentData(), self.column.currentData(),
            replace_existing=self._relink,
        )
        ticket = self._ticket
        project = self._project
        def finished(plan):
            if not self._alive or ticket != self._ticket:
                return
            self.plan = plan
            self.summary.setText(
                f"Link {len(plan.matches):,} · Keep {plan.preserved_count:,} existing · "
                f"Rebuild {plan.removed_count:,} · No longer linked {plan.disappearing_count:,}\n"
                f"Unmatched {plan.unmatched_count:,} · "
                f"Ambiguous {plan.ambiguous_count:,} · Blank {plan.blank_count:,}"
            )
            effects = []
            if plan.matches:
                effects.append(f"Link {len(plan.matches):,}")
            if plan.removed_count:
                effects.append(f"Replace {plan.removed_count:,} Links")
            self.apply_button.setText(" · ".join(effects) or "No Changes")
            self.apply_button.setEnabled(bool(effects))
            self._render_examples()
        def failed(message, cancelled):
            if self._alive and ticket == self._ticket and not cancelled:
                self.summary.setText(message)
        self._job_id = self._jobs.submit(self._source.id, "Matching",
            lambda progress, cancel: preview_links(project, request, progress, cancel), finished, failed)

    def _render_examples(self, *_):
        rows = self.plan.examples.get(self.examples.currentData(), ()) if self.plan else ()
        self.table.setRowCount(len(rows))
        for index, (value, row_id, detail) in enumerate(rows):
            for column, text in enumerate((value, "—" if row_id is None else str(row_id), detail)):
                self.table.setItem(index, column, QtWidgets.QTableWidgetItem(text))
