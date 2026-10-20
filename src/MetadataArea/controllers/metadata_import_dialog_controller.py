from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
import queue
import threading

from PySide6 import QtCore, QtWidgets

from MetadataArea.importing import (
    MetadataColumnMapping,
    MetadataImportCancelled,
    MetadataImportInspection,
    MetadataImportInterpretation,
    MetadataImportPlan,
    MetadataImportPreset,
    MetadataImportResult,
    MetadataImportService,
)
from shared.qt.message_dialogs import show_error
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class MetadataImportDialogController:
    """UI binder for metadata imports, additions, and full-table replacement."""

    _VALUE_TYPES = (
        ('string ("text")', "text"),
        ("integer (0, 1, 2)", "integer"),
        ("float (0.92, 3.14)", "float"),
        ("bool (True/False)", "boolean"),
        ("datetime (YYYY-MM-DD)", "datetime"),
        ("list ([item1, item2])", "list"),
    )

    _DATETIME_FORMATS = (
        ("Choose format…", ""),
        ("ISO 8601", "iso8601"),
        ("YYYY-MM-DD", "YYYY-MM-DD"),
        ("YYYY-MM-DD HH:mm:ss", "YYYY-MM-DD HH:mm:ss"),
        ("YYYY-MM-DDTHH:mm:ss", "YYYY-MM-DDTHH:mm:ss"),
        ("DD/MM/YYYY", "DD/MM/YYYY"),
        ("MM/DD/YYYY", "MM/DD/YYYY"),
        ("DD.MM.YYYY", "DD.MM.YYYY"),
        ("Custom", "custom"),
    )
    _LIST_FORMATS = (
        ("Auto-detect", "auto"),
        ("Comma", "comma"),
        ("Semicolon", "semicolon"),
        ("Pipe", "pipe"),
        ("Custom", "custom"),
    )

    def __init__(
        self,
        service: MetadataImportService,
        source_path: Path | None,
        preset: MetadataImportPreset,
        *,
        process_manager=None,
    ) -> None:
        if preset not in {
            MetadataImportPreset.INITIAL,
            MetadataImportPreset.UPDATE,
            MetadataImportPreset.APPEND,
            MetadataImportPreset.REPLACE_TABLE,
        }:
            raise ValueError("The metadata import dialog does not support this operation.")
        self.service = service
        self.source_path = None if source_path is None else Path(source_path)
        self.preset = preset
        self._process_manager = process_manager
        self._publication_reserved = False
        self.dialog = load_ui("MetadataArea", "ui/metadata_import_dialog.ui", None)
        self.dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)

        self.file_label = self._widget(QtWidgets.QLabel, "label_import_file_value")
        self.format_label = self._widget(QtWidgets.QLabel, "label_import_format_value")
        self.table_label = self._widget(QtWidgets.QLabel, "label_table")
        self.table_combo = self._widget(QtWidgets.QComboBox, "combo_table")
        self.source_settings_button = self._widget(
            QtWidgets.QToolButton, "button_source_settings"
        )
        self.file_settings_panel = self._widget(QtWidgets.QWidget, "panel_file_settings")
        self.record_label = self._widget(QtWidgets.QLabel, "label_record_path")
        self.record_edit = self._widget(QtWidgets.QLineEdit, "edit_record_path")
        self.header_label = self._widget(QtWidgets.QLabel, "label_header_row")
        self.header_spin = self._widget(QtWidgets.QSpinBox, "spin_header_row")
        self.encoding_label = self._widget(QtWidgets.QLabel, "label_encoding")
        self.encoding_combo = self._widget(QtWidgets.QComboBox, "combo_encoding")
        self.delimiter_label = self._widget(QtWidgets.QLabel, "label_delimiter")
        self.delimiter_edit = self._widget(QtWidgets.QLineEdit, "edit_delimiter")

        self.matching_groups = self._widget(QtWidgets.QWidget, "matching_groups")
        self.match_metadata_group = self._widget(QtWidgets.QGroupBox, "group_match_metadata")
        self.source_identifier = self._widget(QtWidgets.QComboBox, "combo_source_identifier")
        self.project_identifier = self._widget(QtWidgets.QComboBox, "combo_project_identifier")

        self.mapping_table = self._widget(QtWidgets.QTableWidget, "table_mappings")
        self.summary_label = self._widget(QtWidgets.QLabel, "label_import_summary")
        self.status_label = self._widget(QtWidgets.QLabel, "label_status")
        self.progress = self._widget(QtWidgets.QProgressBar, "progress_exchange")
        self.cancel_button = self._widget(QtWidgets.QPushButton, "button_close")
        self.import_button = self._widget(QtWidgets.QPushButton, "button_apply")

        title = preset.label
        self.dialog.setWindowTitle(title)
        self.import_button.setText(title)
        mapping_header = self.mapping_table.horizontalHeader()
        mapping_header.setStretchLastSection(False)
        mapping_header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Fixed)
        mapping_header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        mapping_header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        mapping_header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Fixed)
        mapping_header.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Fixed)
        mapping_header.resizeSection(0, 62)
        mapping_header.resizeSection(3, 155)
        mapping_header.resizeSection(4, 180)

        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="metadata-import")
        self.future: Future | None = None
        self.future_kind = ""
        self.cancel_event = threading.Event()
        self.events: queue.SimpleQueue[tuple[int, str]] = queue.SimpleQueue()
        self.connections: list[tuple[object, object]] = []
        self.timer = QtCore.QTimer(self.dialog)
        self.timer.setInterval(75)
        self._connect(self.timer.timeout, self._poll)
        self.probe = None
        self.inspection: MetadataImportInspection | None = None
        self.matching_required = False
        self.advanced_source_available = False
        self.plan: MetadataImportPlan | None = None
        self.result: MetadataImportResult | None = None
        self._populating = False
        self._project_unique_columns: set[str] = set()
        self._close_after_cancel = False

        for label, value in (
            ("UTF-8", "utf-8-sig"),
            ("UTF-16", "utf-16"),
            ("Windows-1252", "cp1252"),
            ("ISO-8859-1", "iso-8859-1"),
        ):
            self.encoding_combo.addItem(label, value)

        self._connect(self.table_combo.currentIndexChanged, self._structure_changed)
        self._connect(self.source_settings_button.toggled, self.file_settings_panel.setVisible)
        self._connect(self.record_edit.editingFinished, self._structure_changed)
        self._connect(self.header_spin.editingFinished, self._structure_changed)
        self._connect(self.delimiter_edit.editingFinished, self._structure_changed)
        self._connect(self.encoding_combo.currentIndexChanged, self._structure_changed)
        for control in (
            self.source_identifier,
            self.project_identifier,
        ):
            self._connect(control.currentIndexChanged, self._configuration_changed)
        self._connect(self.mapping_table.itemChanged, self._configuration_changed)
        self._connect(self.import_button.clicked, self._commit)
        self._connect(self.cancel_button.clicked, self._cancel)
        self.dialog.installEventFilter(self._close_guard())
        self.table_label.hide()
        self.table_combo.hide()
        self.source_settings_button.hide()
        self.file_settings_panel.hide()
        self.matching_groups.hide()
        self.progress.hide()

    def exec(self) -> MetadataImportResult | None:
        if self.source_path is not None:
            source_path = self.source_path
            QtCore.QTimer.singleShot(0, lambda: self._load_source(source_path))
        else:
            self.file_label.setText("Choose a metadata source")
            self.format_label.setText("—")
            self.status_label.setText("Choose a file or TEI folder to begin.")
        try:
            self.dialog.exec()
            return self.result
        finally:
            self.timer.stop()
            self.cancel_event.set()
            self.executor.shutdown(wait=True, cancel_futures=True)
            self._release_publication()
            self.service.cleanup_import(self.inspection)
            for signal, callback in reversed(self.connections):
                try:
                    signal.disconnect(callback)
                except (RuntimeError, TypeError):
                    pass

    def _load_source(self, path: Path) -> None:
        if self.future is not None:
            return
        self.service.cleanup_import(self.inspection)
        self.probe = None
        self.inspection = None
        self.plan = None
        self.matching_required = False
        self.advanced_source_available = False
        self.source_path = Path(path)
        self.file_label.setText(self.source_path.name)
        self.file_label.setToolTip(str(self.source_path))
        self.format_label.setText("Inspecting…")
        self.mapping_table.setRowCount(0)
        self.summary_label.setText("Inspecting metadata…")
        self.matching_groups.hide()
        self.table_label.hide()
        self.table_combo.hide()
        self.source_settings_button.setChecked(False)
        self.source_settings_button.hide()
        self._start(
            "probe",
            lambda: self.service.probe_import(self.source_path),
            "Inspecting metadata source…",
        )

    def _configure_structure(self, probe) -> None:
        self._populating = True
        try:
            self.table_combo.clear()
            for name in probe.table_names:
                self.table_combo.addItem(name, name)
            self.table_combo.setCurrentIndex(max(0, self.table_combo.findData(probe.default_table)))
            self.record_edit.setText(probe.default_record_path)
            self.delimiter_edit.setText("\\t" if probe.default_delimiter == "\t" else probe.default_delimiter)
            encoding_index = self.encoding_combo.findData(probe.default_encoding)
            self.encoding_combo.setCurrentIndex(max(0, encoding_index))
            sheet = probe.format_key in {"xlsx", "ods"}
            records = probe.format_key in {"json", "xml", "tei", "xml_zip", "tei_zip", "tei_collection"}
            delimited = probe.format_key == "delimited"
            multiple_sheets = sheet and len(probe.table_names) > 1
            self.table_label.setVisible(multiple_sheets)
            self.table_combo.setVisible(multiple_sheets)
            self.record_label.setVisible(records and not probe.is_tei)
            self.record_edit.setVisible(records and not probe.is_tei)
            self.header_label.setVisible(sheet or delimited)
            self.header_spin.setVisible(sheet or delimited)
            self.encoding_label.setVisible(delimited)
            self.encoding_combo.setVisible(delimited)
            self.delimiter_label.setVisible(delimited)
            self.delimiter_edit.setVisible(delimited)
            has_settings = sheet or (records and not probe.is_tei) or delimited
            self.advanced_source_available = has_settings
            self.source_settings_button.setVisible(has_settings)
            self.source_settings_button.setEnabled(has_settings)
            if not has_settings:
                self.source_settings_button.setChecked(False)
        finally:
            self._populating = False

    def _interpretation(self) -> MetadataImportInterpretation:
        delimiter = self.delimiter_edit.text()
        if delimiter == "\\t":
            delimiter = "\t"
        return MetadataImportInterpretation(
            table_name=str(self.table_combo.currentData() or ""),
            record_path=self.record_edit.text().strip(),
            delimiter=delimiter,
            encoding=str(self.encoding_combo.currentData() or "utf-8-sig"),
            header_row=self.header_spin.value(),
        )

    def _prepare(self) -> None:
        if self.probe is None or self.future is not None:
            return
        self.cancel_event.clear()
        interpretation = self._interpretation()

        def prepare():
            inspection = self.service.prepare_import(
                self.probe,
                interpretation,
                progress_callback=self._queue_progress,
                should_cancel=self.cancel_event.is_set,
            )
            requires_replacement_matching = (
                self.preset == MetadataImportPreset.REPLACE_TABLE
                and self.service.replacement_requires_matching()
            )
            return (
                inspection,
                set(self.service.repository.complete_unique_column_names()),
                requires_replacement_matching,
            )

        self._start("prepare", prepare, "Inspecting metadata…")

    def _prepared(
        self,
        inspection: MetadataImportInspection,
        unique_columns: set[str],
        requires_replacement_matching: bool,
    ) -> None:
        self.inspection = inspection
        self._project_unique_columns = unique_columns
        self._update_source_summary()
        self._populate_identifiers()
        self._populate_mappings()
        self.matching_required = self.preset in {
            MetadataImportPreset.APPEND,
            MetadataImportPreset.UPDATE,
        } or requires_replacement_matching
        self.matching_groups.setVisible(self.matching_required)
        self._update_match_controls()
        self._refresh_plan()

    def _populate_identifiers(self) -> None:
        if self.inspection is None:
            return
        self._populating = True
        try:
            self.source_identifier.clear()
            self.source_identifier.addItem("Choose incoming identifier…", "")
            for name in self.inspection.columns:
                self.source_identifier.addItem(name, name)
            self.project_identifier.clear()
            self.project_identifier.addItem("Choose existing identifier…", "")
            project_columns = [column.name for column in self.service.repository.list_columns()]
            for name in project_columns:
                self.project_identifier.addItem("ID" if name == "id" else name, name)
            incoming_unique = {
                schema.name
                for schema in self.inspection.schemas
                if not schema.nullable and schema.unique_values == self.inspection.row_count
            }
            project_unique = self._project_unique_columns
            preferred_names = ("identifier", "id_imported", "source_filename", "filename", "sample", "key")
            shared = incoming_unique & project_unique
            preferred = next((name for name in preferred_names if name in shared), "")
            if not preferred:
                preferred = next((name for name in self.inspection.columns if name in shared), "")
            if preferred:
                self.source_identifier.setCurrentIndex(self.source_identifier.findData(preferred))
                self.project_identifier.setCurrentIndex(self.project_identifier.findData(preferred))
        finally:
            self._populating = False
        self._update_match_controls()

    def _populate_mappings(self) -> None:
        if self.inspection is None:
            return
        mappings = self.service.default_mappings(self.inspection, self.preset)
        self._populating = True
        try:
            self.mapping_table.setRowCount(len(mappings))
            for row, mapping in enumerate(mappings):
                include = QtWidgets.QTableWidgetItem()
                include.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                include.setCheckState(
                    QtCore.Qt.CheckState.Checked
                    if mapping.enabled
                    else QtCore.Qt.CheckState.Unchecked
                )
                self.mapping_table.setItem(row, 0, include)
                source = QtWidgets.QTableWidgetItem(mapping.source_name)
                source.setFlags(source.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                source.setData(QtCore.Qt.ItemDataRole.UserRole, mapping.source_name)
                self.mapping_table.setItem(row, 1, source)
                self.mapping_table.setItem(row, 2, QtWidgets.QTableWidgetItem(mapping.target_name))
                type_combo = QtWidgets.QComboBox()
                for label, kind in self._VALUE_TYPES:
                    type_combo.addItem(label, kind)
                type_combo.setCurrentIndex(max(0, type_combo.findData(mapping.value_type)))
                self._connect(
                    type_combo.currentIndexChanged,
                    lambda _index, mapping_row=row: self._mapping_type_changed(mapping_row),
                )
                self.mapping_table.setCellWidget(row, 3, type_combo)
                format_combo = QtWidgets.QComboBox()
                format_combo.setEditable(True)
                format_combo.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
                format_combo.setSizeAdjustPolicy(
                    QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
                )
                format_combo.setMinimumContentsLength(18)
                self._connect(
                    format_combo.currentIndexChanged,
                    lambda _index, mapping_row=row: self._mapping_format_changed(mapping_row),
                )
                if format_combo.lineEdit() is not None:
                    self._connect(format_combo.lineEdit().editingFinished, self._configuration_changed)
                self.mapping_table.setCellWidget(row, 4, format_combo)
                self._configure_format_combo(row, mapping.value_type, mapping.value_format)
        finally:
            self._populating = False

    def _mappings(self) -> tuple[MetadataColumnMapping, ...]:
        existing = {
            column.name.casefold(): column.name
            for column in self.service.repository.list_columns()
            if column.name != "id"
        }
        output: list[MetadataColumnMapping] = []
        for row in range(self.mapping_table.rowCount()):
            include = self.mapping_table.item(row, 0)
            source = self.mapping_table.item(row, 1)
            target = self.mapping_table.item(row, 2)
            type_combo = self.mapping_table.cellWidget(row, 3)
            format_combo = self.mapping_table.cellWidget(row, 4)
            if (
                include is None
                or source is None
                or target is None
                or not isinstance(type_combo, QtWidgets.QComboBox)
                or not isinstance(format_combo, QtWidgets.QComboBox)
            ):
                continue
            enabled = include.checkState() == QtCore.Qt.CheckState.Checked
            target_name = target.text().strip()
            if target_name.casefold() in existing:
                target_name = existing[target_name.casefold()]
            output.append(
                MetadataColumnMapping(
                    source_name=str(source.data(QtCore.Qt.ItemDataRole.UserRole) or source.text()),
                    target_name=target_name,
                    value_type=str(type_combo.currentData()),
                    value_format=self._format_value(format_combo),
                    enabled=enabled,
                )
            )
        return tuple(output)

    def _mapping_type_changed(self, row: int) -> None:
        if self._populating:
            return
        type_combo = self.mapping_table.cellWidget(row, 3)
        if not isinstance(type_combo, QtWidgets.QComboBox):
            return
        self._configure_format_combo(row, str(type_combo.currentData() or "text"), "")
        self._configuration_changed()

    def _configure_format_combo(self, row: int, value_type: str, selected: str) -> None:
        combo = self.mapping_table.cellWidget(row, 4)
        if not isinstance(combo, QtWidgets.QComboBox):
            return
        combo.blockSignals(True)
        combo.clear()
        options = (
            self._DATETIME_FORMATS
            if value_type == "datetime"
            else self._LIST_FORMATS
            if value_type == "list"
            else (("—", ""),)
        )
        for label, value in options:
            combo.addItem(label, value)
        custom_value = selected.removeprefix("custom:") if selected.startswith("custom:") else ""
        index = combo.findData("custom" if custom_value else selected)
        combo.setCurrentIndex(index if index >= 0 else 0)
        if custom_value:
            combo.setEditText(custom_value)
        combo.setEnabled(value_type in {"datetime", "list"})
        if value_type == "datetime":
            combo.setToolTip(
                "Choose the source format. Custom tokens: YYYY, YY, MM, DD, HH, mm, ss."
            )
        elif value_type == "list":
            combo.setToolTip("Choose how items are separated in the source values.")
        else:
            combo.setToolTip("")
        combo.blockSignals(False)

    def _mapping_format_changed(self, row: int) -> None:
        if self._populating:
            return
        combo = self.mapping_table.cellWidget(row, 4)
        if not isinstance(combo, QtWidgets.QComboBox):
            return
        if combo.currentData() == "custom":
            combo.blockSignals(True)
            combo.setEditText("")
            combo.blockSignals(False)
        self._configuration_changed()

    @staticmethod
    def _format_value(combo: QtWidgets.QComboBox) -> str:
        value = str(combo.currentData() or "")
        if value and value != "custom":
            return value
        custom = combo.currentText().strip()
        return f"custom:{custom}" if custom and custom != "Custom…" else ""

    def _configuration_changed(self, *_args) -> None:
        if self._populating:
            return
        self._update_match_controls()
        self._refresh_plan()

    def _refresh_plan(self) -> None:
        if self.inspection is None or self.future is not None:
            return
        source_identifier = str(self.source_identifier.currentData() or "") if self.matching_required else ""
        project_identifier = str(self.project_identifier.currentData() or "") if self.matching_required else ""
        mappings = self._mappings()
        inspection = self.inspection
        self._start(
            "plan",
            lambda: self.service.preview_import(
                inspection,
                self.preset,
                mappings=mappings,
                source_identifier_column=source_identifier,
                project_identifier_column=project_identifier,
                should_cancel=self.cancel_event.is_set,
            ),
            "Checking import…",
        )

    def _planned(self, plan: MetadataImportPlan) -> None:
        self.plan = plan
        self._populate_summary()
        self._update_import_enabled()

    def _populate_summary(self) -> None:
        if self.plan is None:
            self.summary_label.clear()
            return
        impact = self.plan.impact
        result_rows = impact.existing_rows + impact.appended_rows - impact.removed_rows
        current_columns = max(0, len(self.service.repository.list_columns()) - 1)
        result_columns = (
            current_columns
            + len(impact.columns_added)
            - len(impact.columns_removed)
        )
        summary = f"Result: {result_rows:,} rows and {result_columns:,} columns."
        if self.preset == MetadataImportPreset.APPEND:
            summary += f"\n{impact.matched_rows:,} existing matches skipped; {impact.appended_rows:,} items appended."
        elif self.preset == MetadataImportPreset.UPDATE:
            summary += f"\n{impact.matched_rows:,} matched; {impact.incoming_rows - impact.matched_rows:,} unmatched rows ignored. Blanks keep existing values."
        elif self.preset == MetadataImportPreset.REPLACE_TABLE:
            summary += f"\n{impact.matched_rows:,} matched; {impact.appended_rows:,} added; {impact.removed_rows:,} removed."
        if impact.columns_added:
            summary += f" New columns: {len(impact.columns_added):,}."
        if impact.affected_links:
            summary += f" {impact.affected_links:,} linked records are affected."
        self.summary_label.setText(summary)
        self.summary_label.setToolTip("New columns: " + ", ".join(impact.columns_added) if impact.columns_added else "")

        status = "Ready to import metadata." if self.plan.can_import else ("\n".join(self.plan.messages) if self.plan.messages else "Review the import settings.")
        self.status_label.setText(status)

    def _update_import_enabled(self, *_args) -> None:
        enabled = self.plan is not None and self.plan.can_import and self.future is None
        self.import_button.setEnabled(enabled)

    def _commit(self) -> None:
        if not self.import_button.isEnabled() or self.plan is None:
            return
        if self._process_manager is not None:
            try:
                self._process_manager.begin_metadata_publication()
            except ValueError as exc:
                self.status_label.setText(str(exc))
                return
            self._publication_reserved = True
        plan = self.plan
        self.cancel_event.clear()
        self._start(
            "commit",
            lambda: self.service.commit_import(
                plan,
                progress_callback=self._queue_progress,
                should_cancel=self.cancel_event.is_set,
            ),
            {
                MetadataImportPreset.INITIAL: "Importing metadata…",
                MetadataImportPreset.UPDATE: "Updating metadata…",
                MetadataImportPreset.APPEND: "Appending items…",
                MetadataImportPreset.REPLACE_TABLE: "Replacing metadata…",
            }[self.preset],
        )

    def _start(self, kind: str, callback, message: str) -> None:
        self.future_kind = kind
        self.future = self.executor.submit(callback)
        if kind == "plan":
            self.summary_label.setText("Checking import…")
        self.progress.show()
        self.progress.setRange(0, 0 if kind in {"probe", "prepare", "plan"} else 100)
        self.progress.setValue(0)
        self.status_label.setText(message)
        self.import_button.setEnabled(False)
        self._set_configuration_enabled(False)
        self.timer.start()

    def _release_publication(self) -> None:
        if self._publication_reserved:
            self._publication_reserved = False
            self._process_manager.end_metadata_publication()

    def _poll(self) -> None:
        while True:
            try:
                value, message = self.events.get_nowait()
            except queue.Empty:
                break
            self.progress.setRange(0, 100 if value else 0)
            if value:
                self.progress.setValue(value)
            self.status_label.setText(message)
        if self.future is None or not self.future.done():
            return
        future, kind = self.future, self.future_kind
        self.future = None
        self.future_kind = ""
        if kind == "commit":
            self._release_publication()
        self.timer.stop()
        self.progress.hide()
        self._set_configuration_enabled(True)
        self.cancel_button.setText("Cancel")
        self.cancel_button.setEnabled(True)
        try:
            value = future.result()
        except MetadataImportCancelled:
            self.status_label.setText("Metadata import was cancelled.")
            if self._close_after_cancel:
                self.dialog.reject()
            return
        except Exception as exc:
            self.status_label.setText(str(exc))
            if kind == "probe":
                self.format_label.setText("Unsupported or unreadable")
            show_error(self.dialog, "Metadata Import Failed", str(exc))
            if self._close_after_cancel:
                self.dialog.reject()
            return
        if self._close_after_cancel and kind != "commit":
            self.dialog.reject()
            return
        if kind == "probe":
            self.probe = value
            self._update_source_summary()
            self._configure_structure(value)
            self._prepare()
        elif kind == "prepare":
            inspection, unique_columns, requires_replacement_matching = value
            self._prepared(
                inspection,
                unique_columns,
                requires_replacement_matching,
            )
        elif kind == "plan":
            self._planned(value)
        else:
            self.result = value
            self.dialog.accept()

    def _structure_changed(self, *_args) -> None:
        if self._populating or self.probe is None or self.future is not None:
            return
        self.service.cleanup_import(self.inspection)
        self.inspection = None
        self.plan = None
        self.mapping_table.setRowCount(0)
        self.summary_label.setText("Inspecting metadata…")
        self._prepare()

    def _cancel(self) -> None:
        if self.future is None:
            self.dialog.reject()
            return
        self._close_after_cancel = True
        self.cancel_event.set()
        self.cancel_button.setText("Cancelling…")
        self.cancel_button.setEnabled(False)
        self.status_label.setText("Cancelling metadata import…")

    def _update_match_controls(self) -> None:
        self.match_metadata_group.setEnabled(self.matching_required and self.future is None)
        key = self.source_identifier.currentData() if self.matching_required else ""
        for row in range(self.mapping_table.rowCount()):
            source = self.mapping_table.item(row, 1)
            combo = self.mapping_table.cellWidget(row, 3)
            if source is None or combo is None:
                continue
            is_key = source.data(QtCore.Qt.ItemDataRole.UserRole) == key
            combo.setEnabled(not is_key)
            combo.setToolTip("Matching keys use text to preserve leading zeros." if is_key else "")
            if is_key:
                combo.blockSignals(True)
                combo.setCurrentIndex(combo.findData("text"))
                combo.blockSignals(False)
                self._configure_format_combo(row, "text", "")

    def _set_configuration_enabled(self, enabled: bool) -> None:
        for widget in (
            self.table_combo,
            self.record_edit,
            self.header_spin,
            self.encoding_combo,
            self.delimiter_edit,
            self.matching_groups,
            self.mapping_table,
        ):
            widget.setEnabled(enabled)
        self.source_settings_button.setEnabled(enabled and self.advanced_source_available)
        self._update_match_controls()

    def _close_guard(self):
        controller = self

        class Guard(QtCore.QObject):
            def eventFilter(self, watched, event):
                if event.type() == QtCore.QEvent.Type.Close and controller.future is not None:
                    controller._cancel()
                    return True
                if event.type() == QtCore.QEvent.Type.KeyPress and event.key() == QtCore.Qt.Key.Key_Escape and controller.future is not None:
                    controller._cancel()
                    return True
                return False

        self.guard = Guard(self.dialog)
        return self.guard

    def _update_source_summary(self) -> None:
        if self.probe is None:
            self.format_label.setText("—")
            return
        details = [self.probe.format_label, self._format_bytes(self.probe.size_bytes)]
        if self.inspection is not None:
            details.extend((f"{self.inspection.row_count:,} rows", f"{len(self.inspection.columns):,} columns"))
        details_text = " · ".join(details)
        self.format_label.setText(details_text)
        if self.source_path is not None:
            self.file_label.setToolTip(f"{self.source_path}\n{details_text}")

    def _queue_progress(self, value: int, message: str) -> None:
        self.events.put((max(0, min(100, int(value))), str(message)))

    def _connect(self, signal, callback) -> None:
        signal.connect(callback)
        self.connections.append((signal, callback))

    def _widget(self, widget_type, name: str):
        return require_child(self.dialog, widget_type, name)

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = float(value)
        for unit in ("B", "KiB", "MiB", "GiB"):
            if amount < 1024 or unit == "GiB":
                return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
            amount /= 1024
        return f"{value} B"
