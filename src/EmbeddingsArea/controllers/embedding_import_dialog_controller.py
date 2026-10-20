from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import queue
import threading

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from EmbeddingsArea.exchange.domain import (
    EmbeddingImportCompanionProbe,
    EmbeddingImportInspection,
    EmbeddingImportInterpretation,
    EmbeddingImportPlan,
    EmbeddingImportProbe,
)
from EmbeddingsArea.exchange.importer import (
    EmbeddingImportCancelled,
    IDENTIFIER_FILE_FILTER,
    SIDECAR_FILE_FILTER,
)
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class EmbeddingImportDialogController:
    """Inspect, link, and publish one already-selected embedding file."""

    def __init__(self, service, source_path: Path) -> None:
        self.service = service
        self.source_path = Path(source_path)
        self.dialog = load_ui("EmbeddingsArea", "ui/embedding_import_dialog.ui")
        self.dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)

        self.file_label = self._widget(QtWidgets.QLabel, "label_import_file_value")
        self.source_summary_label = self._widget(
            QtWidgets.QLabel, "label_import_format_value"
        )
        self.linking_label = self._widget(QtWidgets.QLabel, "label_import_linking_mode")
        self.preview_status = self._widget(
            QtWidgets.QLabel, "label_import_embedding_preview_status"
        )
        self.name_edit = self._widget(QtWidgets.QLineEdit, "edit_name")
        self.identifier_file_edit = self._widget(
            QtWidgets.QLineEdit, "edit_identifier_file"
        )
        self.sidecar_file_edit = self._widget(QtWidgets.QLineEdit, "edit_sidecar_file")
        self.identifier_file_combo = self._widget(
            QtWidgets.QComboBox, "combo_identifier_file_key"
        )
        self.identifier_file_key_label = self.dialog.findChild(
            QtWidgets.QLabel, "label_identifier_file_key"
        )
        self.identifier_file_label = self._widget(
            QtWidgets.QLabel, "label_identifier_file"
        )
        self.metadata_column_label = self.dialog.findChild(
            QtWidgets.QLabel, "label_metadata_column"
        )
        self.metadata_combo = self._widget(QtWidgets.QComboBox, "combo_metadata")
        self.linking_group = self._widget(
            QtWidgets.QGroupBox, "group_import_linking"
        )
        self.skip_check = self._widget(QtWidgets.QCheckBox, "check_skip")
        self.progress = self._widget(QtWidgets.QProgressBar, "progress_exchange")
        self.apply_button = self._widget(QtWidgets.QPushButton, "button_apply")
        self.abort_button = self._widget(QtWidgets.QPushButton, "button_abort")
        self.close_button = self._widget(QtWidgets.QPushButton, "button_close")
        self.identifier_file_button = self._widget(
            QtWidgets.QPushButton, "button_identifier_file"
        )
        self.sidecar_file_button = self._widget(
            QtWidgets.QPushButton, "button_sidecar_file"
        )
        self.preview_table = self._widget(
            QtWidgets.QTableView, "table_import_embedding_preview"
        )
        self.preview_model = QtGui.QStandardItemModel(self.dialog)
        self.preview_table.setModel(self.preview_model)
        self.preview_table.verticalHeader().setVisible(False)
        self.preview_table.horizontalHeader().setStretchLastSection(True)

        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="embedding-import"
        )
        self.future = None
        self.future_kind = ""
        self.cancel_event = threading.Event()
        self.progress_events: queue.SimpleQueue[tuple[int, str]] = queue.SimpleQueue()
        self.timer = QtCore.QTimer(self.dialog)
        self.timer.setInterval(75)
        self.timer.timeout.connect(self._poll)
        self.connections: list[tuple[object, object]] = []
        self.probe: EmbeddingImportProbe | None = None
        self.inspection: EmbeddingImportInspection | None = None
        self.plan: EmbeddingImportPlan | None = None
        self.identifier_path: Path | None = None
        self.sidecar_path: Path | None = None
        self.identifier_probe: EmbeddingImportCompanionProbe | None = None
        self.changed = False
        self.close_after_abort = False
        self._populating = False

        self._connect(self.identifier_file_button.clicked, self._toggle_identifier_file)
        self._connect(self.sidecar_file_button.clicked, self._toggle_sidecar_file)
        self._connect(
            self.identifier_file_combo.currentIndexChanged,
            self._schema_changed,
        )
        self._connect(self.metadata_combo.currentIndexChanged, self._linking_changed)
        self._connect(self.skip_check.toggled, self._linking_changed)
        self._connect(self.apply_button.clicked, self._commit)
        self._connect(self.abort_button.clicked, self._request_abort)
        self._connect(self.close_button.clicked, self._close_requested)
        self.dialog.installEventFilter(self._close_guard())
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.apply_button.setEnabled(False)
        self.abort_button.setEnabled(False)
        self.identifier_file_combo.hide()
        if self.identifier_file_key_label is not None:
            self.identifier_file_key_label.hide()

    def exec(self) -> bool:
        QtCore.QTimer.singleShot(0, lambda: self._load_source(self.source_path))
        try:
            self.dialog.exec()
            return self.changed
        finally:
            self.timer.stop()
            self.cancel_event.set()
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.service.exchange.cleanup_import(self.inspection)
            for signal, callback in reversed(self.connections):
                try:
                    signal.disconnect(callback)
                except (RuntimeError, TypeError):
                    pass

    def _load_source(self, path: Path) -> None:
        if self.future is not None:
            return
        self.service.exchange.cleanup_import(self.inspection)
        self.source_path = Path(path)
        self.probe = None
        self.inspection = None
        self.plan = None
        self.preview_model.clear()
        self.apply_button.setEnabled(False)
        self.file_label.setText(self.source_path.name)
        self.file_label.setToolTip(str(self.source_path))
        self.name_edit.setText(self.source_path.stem)
        self.source_summary_label.setText("Inspecting…")
        try:
            self.probe = self.service.exchange.probe_import(
                self.source_path, sidecar_path=self.sidecar_path
            )
        except Exception as exc:
            self.source_summary_label.setText("Unsupported or unreadable")
            self.preview_status.setText(str(exc))
            self.progress.setValue(0)
            return
        self._update_source_summary()
        self._populate_options(self.probe)
        if self._interpretation_ready():
            self._prepare()
        else:
            self.preview_status.setText(
                "Vector data is ambiguous. Add a schema sidecar that names the vector field."
            )

    def _populate_options(self, probe: EmbeddingImportProbe) -> None:
        self._populating = True
        try:
            self._set_identifier_file_visible(
                not bool(probe.default_identifier_key)
                or self.identifier_path is not None
            )

            self.metadata_combo.clear()
            self.metadata_combo.addItem("By metadata row order", "")
            for label, name in self.service.exchange.list_import_identifier_columns():
                self.metadata_combo.addItem(label, name)
        finally:
            self._populating = False

    def _interpretation(self) -> EmbeddingImportInterpretation:
        if self.probe is None:
            return EmbeddingImportInterpretation()
        return EmbeddingImportInterpretation(
            vector_key=self.probe.default_vector_key,
            identifier_key=self.probe.default_identifier_key,
            vector_columns=(
                ()
                if self.probe.default_vector_key
                else self.probe.wide_vector_columns
            ),
            identifier_path=self.identifier_path,
            identifier_file_key=str(
                self.identifier_file_combo.currentData() or ""
            ),
            sidecar_path=self.sidecar_path,
        )

    def _interpretation_ready(self) -> bool:
        vector_ready = self.probe is not None and bool(
            self.probe.default_vector_key or self.probe.wide_vector_columns
        )
        identifier_ready = (
            self.identifier_path is None
            or bool(self.identifier_file_combo.currentData())
        )
        return vector_ready and identifier_ready

    def _prepare(self) -> None:
        if (
            self.future is not None
            or self.probe is None
            or not self._interpretation_ready()
        ):
            return
        self.service.exchange.cleanup_import(self.inspection)
        self.inspection = None
        self.plan = None
        probe = self.probe
        interpretation = self._interpretation()
        self._start(
            "prepare",
            lambda: self.service.exchange.prepare_import(
                probe,
                interpretation,
                progress_callback=self._queue_progress,
                should_cancel=self.cancel_event.is_set,
            ),
            "Inspecting and validating vectors…",
        )

    def _request_link(self) -> None:
        if self.inspection is None or self.future is not None:
            return
        empty = (
            not self.service.exchange.metadata.is_initialized()
            or self.service.exchange.metadata.count_rows() == 0
        )
        if empty:
            strategy = "create_metadata"
            column = ""
        else:
            column = str(self.metadata_combo.currentData() or "")
            strategy = "identifier" if column else "row_order"
        self._start(
            "link",
            lambda: self.service.exchange.preview_import_links(
                self.inspection,
                strategy=strategy,
                metadata_column=column,
                allow_partial=self.skip_check.isChecked(),
            ),
            "Checking metadata links…",
        )

    def _commit(self) -> None:
        if self.plan is None or not self.plan.can_import:
            return
        plan = self.plan
        self._start(
            "commit",
            lambda: self.service.exchange.commit_import(
                plan,
                label=self.name_edit.text(),
                progress_callback=self._queue_progress,
                should_cancel=self.cancel_event.is_set,
            ),
            "Saving vectors and metadata links…",
        )

    def _start(self, kind: str, function, message: str) -> None:
        if self.future is not None:
            return
        self.cancel_event.clear()
        self.future_kind = kind
        self.abort_button.setText("Abort")
        self.progress.setRange(0, 0 if kind in {"prepare", "link"} else 100)
        self.progress.setValue(0)
        self.preview_status.setText(message)
        self.abort_button.setEnabled(kind in {"prepare", "commit"})
        self.apply_button.setEnabled(False)
        self.identifier_file_button.setEnabled(False)
        self.sidecar_file_button.setEnabled(False)
        self.identifier_file_combo.setEnabled(False)
        self.metadata_combo.setEnabled(False)
        self.skip_check.setEnabled(False)
        self.future = self.executor.submit(function)
        self.timer.start()

    def _poll(self) -> None:
        while True:
            try:
                value, message = self.progress_events.get_nowait()
            except queue.Empty:
                break
            self.progress.setRange(0, 100 if value else 0)
            if value:
                self.progress.setValue(value)
            self.preview_status.setText(message)
        if self.future is None or not self.future.done():
            return
        future, self.future = self.future, None
        kind = self.future_kind
        self.timer.stop()
        self.abort_button.setEnabled(False)
        self.identifier_file_button.setEnabled(True)
        self.sidecar_file_button.setEnabled(True)
        self.identifier_file_combo.setEnabled(
            self.identifier_path is not None
            and self.identifier_file_combo.count() > 1
        )
        has_metadata = (
            self.service.exchange.metadata.is_initialized()
            and self.service.exchange.metadata.count_rows() > 0
        )
        has_identifiers = (
            self.inspection is not None
            and self.inspection.source_has_identifiers
        )
        self.metadata_combo.setEnabled(has_metadata and has_identifiers)
        self.skip_check.setEnabled(
            has_metadata
            and has_identifiers
            and bool(self.metadata_combo.currentData())
        )
        self.progress.setRange(0, 100)
        try:
            result = future.result()
            self.progress.setValue(100)
            if kind == "prepare":
                self.inspection = result
                self._update_source_summary(result)
                self._configure_linking_after_prepare()
            elif kind == "link":
                self.plan = result
                self.linking_label.setText(result.message)
                self.skip_check.setVisible(
                    result.strategy == "identifier"
                    and result.unmatched_count > 0
                )
                self.preview_status.setText(
                    f"Showing {min(result.inspection.row_count, 25):,} of "
                    f"{result.inspection.row_count:,} vectors · {result.message}"
                )
                self._populate_preview(result)
                noun = "Vector" if result.selected_count == 1 else "Vectors"
                self.apply_button.setText(
                    f"Import {result.selected_count:,} {noun}"
                )
                self.apply_button.setEnabled(result.can_import)
            else:
                self.changed = True
                self.inspection = None
                self.plan = None
                self.dialog.accept()
                return
        except EmbeddingImportCancelled:
            self.preview_status.setText("Embedding import was aborted; staged files were removed.")
            self.progress.setValue(0)
            if kind == "prepare":
                self.inspection = None
            if self.close_after_abort:
                self.dialog.reject()
                return
        except Exception as exc:
            self.plan = None
            self.apply_button.setEnabled(False)
            self.preview_status.setText(str(exc))
            self.progress.setValue(0)
        finally:
            self.close_after_abort = False

    def _configure_linking_after_prepare(self) -> None:
        if self.inspection is None:
            return
        empty = (
            not self.service.exchange.metadata.is_initialized()
            or self.service.exchange.metadata.count_rows() == 0
        )
        for widget in (
            self.metadata_combo,
            self.skip_check,
        ):
            widget.setVisible(not empty)
        if self.metadata_column_label is not None:
            self.metadata_column_label.setVisible(not empty)
        has_identifiers = self.inspection.source_has_identifiers
        counts_match = (
            not empty
            and self.service.exchange.metadata.count_rows()
            == self.inspection.row_count
        )
        self.metadata_combo.setEnabled(not empty and has_identifiers)
        self.metadata_combo.setToolTip(
            "Leave this set to row order to link vector row 1 to metadata row 1."
            if counts_match
            else "Row-order matching is unavailable because the row counts differ."
        )
        self._set_identifier_file_visible(not has_identifiers or self.identifier_path is not None)
        if empty:
            self.linking_label.setText(
                f"Create {self.inspection.row_count:,} metadata rows using the embedding identifiers."
            )
        elif not has_identifiers:
            self.metadata_combo.setEnabled(False)
            self.skip_check.setEnabled(False)
            if not counts_match:
                self.linking_label.setText(
                    "Add an identifier/index file; row-order matching requires equal counts."
                )
                self.apply_button.setEnabled(False)
                self._populate_preview(None)
                return
        elif len(self.inspection.complete_metadata_columns) == 1:
            index = self.metadata_combo.findData(
                self.inspection.complete_metadata_columns[0]
            )
            self.metadata_combo.setCurrentIndex(index)
        else:
            self.metadata_combo.setCurrentIndex(0)
        self._request_link()

    def _populate_preview(self, plan: EmbeddingImportPlan | None) -> None:
        self.preview_model.clear()
        self.preview_model.setHorizontalHeaderLabels(
            ["source row", "identifier", "embedding sample", "metadata link"]
        )
        if self.inspection is None:
            return
        matches = (
            np.load(plan.match_index_path, mmap_mode="r")
            if plan is not None and plan.match_index_path is not None
            else None
        )
        for sample in self.inspection.samples:
            if plan is None:
                status = "not linked"
            elif plan.strategy == "create_metadata":
                status = "new metadata row"
            else:
                metadata_id = int(matches[sample.row_index]) if matches is not None else -1
                status = str(metadata_id) if metadata_id >= 0 else "unmatched"
            vector = json.dumps(list(sample.vector))
            if self.inspection.dimensions > len(sample.vector):
                vector = vector[:-1] + ", …]"
            self.preview_model.appendRow(
                [
                    QtGui.QStandardItem(str(sample.row_index + 1)),
                    QtGui.QStandardItem(sample.identifier),
                    QtGui.QStandardItem(vector),
                    QtGui.QStandardItem(status),
                ]
            )
        self.preview_table.resizeColumnsToContents()

    def _schema_changed(self, *_args) -> None:
        if not self._populating and self.future is None:
            self._prepare()

    def _linking_changed(self, *_args) -> None:
        if not self._populating and self.future is None:
            matching_by_field = bool(self.metadata_combo.currentData())
            self.skip_check.setEnabled(matching_by_field)
            if not matching_by_field and self.skip_check.isChecked():
                self.skip_check.setChecked(False)
                return
            self._request_link()

    def _set_identifier_file_visible(self, visible: bool) -> None:
        for widget in (
            self.identifier_file_label,
            self.identifier_file_edit,
            self.identifier_file_button,
        ):
            widget.setVisible(visible)

    def _toggle_identifier_file(self) -> None:
        if self.identifier_path is None:
            self._choose_identifier_file()
        else:
            self._remove_identifier_file()

    def _choose_identifier_file(self) -> None:
        path, _selected = QtWidgets.QFileDialog.getOpenFileName(
            self.dialog,
            "Choose Identifiers or Index",
            str(self.source_path.parent),
            IDENTIFIER_FILE_FILTER,
        )
        if path:
            try:
                probe = self.service.exchange.probe_import_identifiers(Path(path))
            except Exception as exc:
                self.preview_status.setText(str(exc))
                return
            self.identifier_path = Path(path)
            self.identifier_probe = probe
            self.identifier_file_edit.setText(Path(path).name)
            self.identifier_file_edit.setToolTip(path)
            self.identifier_file_button.setText("Remove")
            self._populating = True
            try:
                self.identifier_file_combo.clear()
                if len(probe.identifier_candidates) > 1 and not probe.default_identifier_key:
                    self.identifier_file_combo.addItem("Choose identifier field…", "")
                for name in probe.identifier_candidates:
                    label = "Ordered values" if name == "<values>" else name
                    self.identifier_file_combo.addItem(label, name)
                index = self.identifier_file_combo.findData(probe.default_identifier_key)
                if index >= 0:
                    self.identifier_file_combo.setCurrentIndex(index)
            finally:
                self._populating = False
            ambiguous = len(probe.identifier_candidates) > 1
            self.identifier_file_combo.setVisible(ambiguous)
            if self.identifier_file_key_label is not None:
                self.identifier_file_key_label.setVisible(ambiguous)
            self._prepare()

    def _remove_identifier_file(self) -> None:
        if self.future is not None:
            return
        self.identifier_path = None
        self.identifier_probe = None
        self.identifier_file_edit.clear()
        self.identifier_file_combo.clear()
        self.identifier_file_combo.hide()
        if self.identifier_file_key_label is not None:
            self.identifier_file_key_label.hide()
        self.identifier_file_button.setText("Add…")
        self._prepare()

    def _toggle_sidecar_file(self) -> None:
        if self.sidecar_path is None:
            self._choose_sidecar_file()
        else:
            self._remove_sidecar_file()

    def _choose_sidecar_file(self) -> None:
        path, _selected = QtWidgets.QFileDialog.getOpenFileName(
            self.dialog,
            "Choose Schema Sidecar",
            str(self.source_path.parent),
            SIDECAR_FILE_FILTER,
        )
        if path:
            self.sidecar_path = Path(path)
            self.sidecar_file_edit.setText(Path(path).name)
            self.sidecar_file_edit.setToolTip(path)
            self.sidecar_file_button.setText("Remove")
            self._load_source(self.source_path)

    def _remove_sidecar_file(self) -> None:
        if self.future is not None:
            return
        self.sidecar_path = None
        self.sidecar_file_edit.clear()
        self.sidecar_file_button.setText("Add…")
        self._load_source(self.source_path)

    def _request_abort(self) -> None:
        if self.future is not None:
            self.cancel_event.set()
            self.abort_button.setEnabled(False)
            self.abort_button.setText("Aborting…")

    def _close_requested(self) -> None:
        if self.future is not None:
            self.close_after_abort = True
            self._request_abort()
            return
        self.service.exchange.cleanup_import(self.inspection)
        self.inspection = None
        self.dialog.reject()

    def _queue_progress(self, value: int, message: str) -> None:
        self.progress_events.put((max(0, min(100, int(value))), str(message)))

    def _update_source_summary(
        self,
        inspection: EmbeddingImportInspection | None = None,
    ) -> None:
        if self.probe is None:
            self.source_summary_label.setText("Inspecting…")
            return
        details = [
            self.probe.format_label,
            self._format_bytes(self.probe.size_bytes),
        ]
        if inspection is not None:
            details.extend(
                (
                    f"{inspection.row_count:,} × {inspection.dimensions:,}",
                    inspection.dtype,
                )
            )
        self.source_summary_label.setText(" · ".join(details))

    def _connect(self, signal, callback) -> None:
        signal.connect(callback)
        self.connections.append((signal, callback))

    def _widget(self, widget_type, name: str):
        return require_child(self.dialog, widget_type, name)

    def _close_guard(self):
        controller = self

        class Guard(QtCore.QObject):
            def eventFilter(self, watched, event):
                if event.type() == QtCore.QEvent.Type.Close and controller.future is not None:
                    controller.close_after_abort = True
                    controller._request_abort()
                    return True
                if (
                    event.type() == QtCore.QEvent.Type.KeyPress
                    and event.key() == QtCore.Qt.Key.Key_Escape
                    and controller.future is not None
                ):
                    controller.close_after_abort = True
                    controller._request_abort()
                    return True
                return False

        self.guard = Guard(self.dialog)
        return self.guard

    @staticmethod
    def _format_bytes(value: int) -> str:
        size = float(value)
        for unit in ("B", "KiB", "MiB", "GiB"):
            if size < 1024 or unit == "GiB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{value:,} B"
