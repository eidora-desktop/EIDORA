from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from EmbeddingsArea.domain import MediaContentType, MediaLinkRequest
from MetadataArea.repository import MetadataRepository
from MetadataArea.service import MetadataService
from AssetsArea.media_service import preview_links, suggest_links
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class MediaLinkDialogController:
    """Suggest a match, preview it, and let the user adjust before applying."""

    def __init__(self, project, source, columns, saved_request, jobs):
        self.dialog = load_ui("AssetsArea", "ui/media_link_dialog.ui", None)
        self.dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self.dialog.setWindowTitle("Link Items")
        self._project, self._source, self._jobs = project, source, jobs
        self._alive, self._ticket, self._job_id = True, 0, None
        self._saved_request = saved_request
        self._suggest_on_open = saved_request is None
        self._connections = []
        self.plan = None
        self.source_field = self._child(QtWidgets.QComboBox, "combo_source_field")
        self.columns = self._child(QtWidgets.QListWidget, "list_metadata_columns")
        self._column_checks = {}
        self.comparison_combo = self._child(QtWidgets.QComboBox, "combo_comparison")
        self.coverage = self._child(QtWidgets.QLabel, "label_match_coverage")
        self.relationship = self._child(QtWidgets.QLabel, "label_match_summary")
        self.summary = self._child(QtWidgets.QLabel, "label_preview_status")
        self.preview_count = self._child(QtWidgets.QLabel, "label_preview_count")
        self.examples = self._child(QtWidgets.QComboBox, "combo_preview_filter")
        self.table = self._child(QtWidgets.QTableWidget, "table_match_preview")
        self.apply_button = self._child(QtWidgets.QPushButton, "button_link")
        self.options_button = self._child(QtWidgets.QToolButton, "button_link_options")
        self.options_panel = self._child(QtWidgets.QWidget, "panel_link_options")
        self._child(QtWidgets.QLabel, "label_source_name").setText(source.label)
        self.replace_existing = self._child(QtWidgets.QCheckBox, "check_replace_existing_links")
        # Replacing links is an explicit choice for this visit, not a restored preference.
        self.replace_existing.setChecked(False)
        for label, key in [("Exact identifier or path", "exact"), ("Filename", "filename"),
                           ("Filename without extension", "stem"),
                           ("Remove final numbered suffix from records", "stem_suffix")]:
            self.comparison_combo.addItem(label, key)
        self.source_field.addItem("Relative path", "relative_path")
        if source.content_type == MediaContentType.TEXT:
            self.source_field.addItem("Record ID", "identifier")
        labels = {column.name: column.label for column in
                  MetadataService(MetadataRepository(project.paths.metadata_database)).list_columns()}
        for name in columns:
            item = QtWidgets.QListWidgetItem(self.columns)
            checkbox = QtWidgets.QCheckBox(labels.get(name, name), self.columns)
            checkbox.setChecked(name == columns[0])
            item.setSizeHint(checkbox.sizeHint())
            self.columns.setItemWidget(item, checkbox)
            self._column_checks[name] = checkbox
        for label, key in [("All records", "all"), ("Matched", "matched"),
                           ("Unmatched media", "unmatched"), ("Ambiguous", "ambiguous")]:
            self.examples.addItem(label, key)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
        for index in (0, 2):
            header.setSectionResizeMode(index, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for index in (1, 3):
            header.setSectionResizeMode(index, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().hide()
        self.table.setColumnHidden(1, True)
        self.table.setColumnHidden(3, True)
        supported_saved = saved_request is not None and (
            self.source_field.findData(saved_request.source_field) >= 0
            and self.comparison_combo.findData(saved_request.comparison) >= 0
            and not saved_request.ignore_case
            and set(saved_request.selected_columns) <= set(columns)
        )
        if supported_saved:
            self._select_request(saved_request)
        elif saved_request is not None:
            # Unsupported saved rules need an explicit choice, never reinterpretation.
            self.source_field.setCurrentIndex(-1)
            self._set_columns(())
        self._debounce = QtCore.QTimer(self.dialog)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(200)
        self._connect(self._debounce.timeout, self._start_preview)
        for combo in (self.source_field, self.comparison_combo):
            self._connect(combo.currentIndexChanged, self._configuration_changed)
        for checkbox in self._column_checks.values():
            self._connect(checkbox.toggled, self._configuration_changed)
        self._connect(self.replace_existing.toggled, self._configuration_changed)
        self._connect(self.options_button.toggled, self._show_matching)
        self._connect(self.examples.currentIndexChanged, self._render_examples)
        self._connect(self.apply_button.clicked, self.dialog.accept)
        self._connect(self._child(QtWidgets.QPushButton, "button_cancel").clicked, self.dialog.reject)
        self._connect(self._child(QtWidgets.QPushButton, "button_detect_match").clicked, self._find_best_match)
        self._connect(self.dialog.destroyed, self._destroyed)
        if saved_request is not None and not supported_saved:
            self.options_button.setChecked(True)

    def _show_matching(self, visible):
        self.options_panel.setVisible(visible)
        self.options_button.setArrowType(QtCore.Qt.ArrowType.DownArrow if visible else QtCore.Qt.ArrowType.RightArrow)

    def _child(self, kind, name):
        return require_child(self.dialog, kind, name)

    def _connect(self, signal, handler):
        signal.connect(handler)
        self._connections.append((signal, handler))

    def exec(self):
        if self._suggest_on_open:
            self._find_best_match()
        else:
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

    def _invalidate(self):
        self._ticket += 1
        self._debounce.stop()
        if self._job_id:
            self._jobs.cancel(self._job_id)
        self.plan = None
        self.apply_button.setEnabled(False)
        self.apply_button.setText("Apply Links")
        self.coverage.setText("Checking matches…")
        self.preview_count.setText("Updating preview…")
        self.table.setRowCount(0)

    def _show_relationship(self):
        self.relationship.setToolTip("\n".join(
            checkbox.text() for checkbox in self._column_checks.values() if checkbox.isChecked()))
        self.relationship.setText(
            f"{self.source_field.currentText()} ({self._source.label}) → "
            f"{self._column_label()} (Items)\n{self.comparison_combo.currentText()}"
        )

    def _configuration_changed(self, *_):
        self._invalidate()
        if self.source_field.currentIndex() < 0 or not self._selected_columns():
            self.coverage.setText("No matching setting selected")
            self.relationship.setText("Choose a source field and item columns in Link options.")
            self.summary.setText("Use Find best match to check the available values.")
            self.preview_count.clear()
            self.options_button.setChecked(True)
            return
        self._show_relationship()
        self.summary.setText("Checking matches in the background…")
        self._debounce.start()

    def _selected_columns(self):
        return tuple(name for name, checkbox in self._column_checks.items() if checkbox.isChecked())

    def _column_label(self):
        labels = [checkbox.text() for checkbox in self._column_checks.values() if checkbox.isChecked()]
        return ", ".join(labels) if len(labels) <= 3 else f"{len(labels)} metadata columns"

    def _set_columns(self, names):
        for name, checkbox in self._column_checks.items():
            with QtCore.QSignalBlocker(checkbox):
                checkbox.setChecked(name in names)

    def _select_request(self, request):
        self._set_columns(request.selected_columns)
        for combo, value in ((self.source_field, request.source_field),
                             (self.comparison_combo, request.comparison)):
            with QtCore.QSignalBlocker(combo):
                combo.setCurrentIndex(combo.findData(value))

    def _find_best_match(self):
        self._invalidate()
        self.relationship.setText("Comparing media fields with item metadata…")
        self.summary.setText("Finding the strongest match…")
        project, source_id, replace_existing = self._project, self._source.id, self.replace_existing.isChecked()
        self._submit(lambda progress, cancel: suggest_links(project, source_id, replace_existing, progress, cancel),
                     suggested=True)

    def _start_preview(self):
        if not self._alive or not self._selected_columns() or self.source_field.currentIndex() < 0:
            return
        request = MediaLinkRequest(
            self._source.id, self.source_field.currentData(), self._selected_columns()[0],
            comparison=self.comparison_combo.currentData(), replace_existing=self.replace_existing.isChecked(),
            metadata_columns=self._selected_columns(),
        )
        project = self._project
        self._submit(lambda progress, cancel: preview_links(project, request, progress, cancel))

    def _submit(self, work, *, suggested=False):
        ticket = self._ticket

        def finished(plan):
            if not self._alive or ticket != self._ticket:
                return
            if plan is None:
                self._set_columns(())
                self.coverage.setText("No suggested match")
                self.relationship.setText("Choose a source field and item columns in Link options.")
                self.summary.setText("No unambiguous matching values found. Items need matching metadata first.")
                self.preview_count.clear()
                self.options_button.setChecked(True)
                return
            if suggested:
                self._select_request(plan.request)
            self.plan = plan
            self._show_plan()

        def failed(message, cancelled):
            if self._alive and ticket == self._ticket and not cancelled:
                self.coverage.setText("Could not check matches")
                self.preview_count.clear()
                self.summary.setText(message)
                self.options_button.setChecked(True)

        self._job_id = self._jobs.submit(self._source.id, "Matching", work, finished, failed)

    def _show_plan(self):
        plan = self.plan
        self._show_relationship()
        matched = plan.matched_item_count
        total = matched + plan.unmatched_metadata_count
        state = "All Matched" if matched and matched == total else "Partly Matched" if matched else "None Matched"
        self.coverage.setText(f"{state} ({matched:,}/{total:,} items)")
        self.summary.setText(
            f"Links: {plan.added_link_count:,} added · {plan.kept_link_count:,} kept · {plan.removed_link_count:,} removed\n"
            f"{plan.matched_asset_count:,} linked assets · "
            f"{plan.unmatched_count + plan.blank_count:,} unmatched media · {plan.ambiguous_count:,} ambiguous"
        )
        saved = self._saved_request
        settings_changed = saved is None or (
            saved.selected_columns, saved.source_field, saved.comparison
        ) != (plan.request.selected_columns, plan.request.source_field, plan.request.comparison)
        save_settings = bool(plan.kept_link_count and settings_changed)
        changes = bool(plan.added_link_count or plan.removed_link_count or save_settings)
        label = "Apply Links"
        if save_settings and not plan.added_link_count and not plan.removed_link_count:
            label = "Save Link Options"
        if not changes:
            label = "Already linked" if plan.kept_link_count else "No matches"
        if plan.removed_link_count and not plan.added_link_count and not plan.kept_link_count:
            label = f"Remove {plan.removed_link_count:,} " + ("Link" if plan.removed_link_count == 1 else "Links")
        self.apply_button.setText(label)
        self.apply_button.setEnabled(changes)
        self._render_examples()

    def _render_examples(self, *_):
        if not self.plan:
            return
        plan = self.plan
        counts = {"matched": len(plan.matches) + plan.preserved_count,
                  "unmatched": plan.unmatched_count + plan.blank_count, "ambiguous": plan.ambiguous_count}
        counts["all"] = sum(counts.values())
        with QtCore.QSignalBlocker(self.examples):
            for index, label in enumerate(("All records", "Matched", "Unmatched media", "Ambiguous")):
                self.examples.setItemText(index, f"{label} ({counts[self.examples.itemData(index)]:,})")
        kind = self.examples.currentData()
        rows = (tuple(row for group in plan.examples.values() for row in group)
                if kind == "all" else plan.examples.get(kind, ()))
        self.preview_count.setText(f"Showing {len(rows):,} of {counts[kind]:,} rows (up to 25 per result)")
        self.table.setHorizontalHeaderLabels([
            f"{self.source_field.currentText()} ({self._source.label})", "Item ID",
            "Matched item value", "Result",
        ])
        self.table.setRowCount(len(rows))
        ambiguous = set(plan.examples.get("ambiguous", ()))
        for index, (value, row_id, detail) in enumerate(rows):
            target = plan.example_matches.get((value, row_id), plan.example_values.get(row_id, "No match"))
            if (value, row_id, detail) in ambiguous:
                target = "Ambiguous match"
            values = (value, "—" if row_id is None else str(row_id), target, detail)
            for column, text in enumerate(values):
                item = QtWidgets.QTableWidgetItem(text)
                item.setToolTip(f"Item {row_id}: {detail}" if row_id is not None else detail)
                self.table.setItem(index, column, item)
