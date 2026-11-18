from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import logging
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


class EmbeddingImportDialogController(QtCore.QObject):
    """Inspect, link, and publish one already-selected embedding file."""

    def __init__(self, service, source_path: Path, *, process_manager=None) -> None:
        super().__init__()
        self.connections: list[tuple[object, object]] = []
        self.process_manager = process_manager
        self.publication_reserved = False
        self.service = service
        self.exchange = service.exchange
        self.source_paths = tuple(Path(path) for path in source_path) if isinstance(source_path, (list, tuple)) else (Path(source_path),)
        self.source_path = self.source_paths[0]
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
        self.name_error = self._widget(QtWidgets.QLabel, "label_name_error")
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
        self.options_button = self._widget(QtWidgets.QToolButton, "button_import_options")
        self.options_panel = self._widget(QtWidgets.QWidget, "panel_import_options")
        self.preview_stack = self._widget(QtWidgets.QStackedWidget, "stack_preview")
        self.preview_error = self._widget(QtWidgets.QLabel, "label_preview_error")
        self.preview_count = self._widget(QtWidgets.QLabel, "label_preview_count")
        self.conflict_combo = self._widget(QtWidgets.QComboBox, "combo_conflict_policy")
        for label,key in [("Resolve different metadata values…", "ask"), ("Keep existing values", "keep"), ("Use incoming nonempty values", "incoming")]:
            self.conflict_combo.addItem(label,key)
        self._connect(self.conflict_combo.currentIndexChanged, self._linking_changed)
        self.vector_combo = self._widget(QtWidgets.QComboBox, "combo_vector_source")
        self.identifier_combo = self._widget(QtWidgets.QComboBox, "combo_identifier_source")
        self._connect(self.options_button.toggled, self._toggle_options)
        self.progress = self._widget(QtWidgets.QProgressBar, "progress_exchange")
        self.apply_button = self._widget(QtWidgets.QPushButton, "button_apply")
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
        self.preview_table.horizontalHeader().setDefaultAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)

        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="embedding-import"
        )
        self.future = None
        self.future_kind = ""
        self.cancel_event = threading.Event()
        self.progress_events: queue.Queue[tuple[int, str]] = queue.Queue(maxsize=1)
        self.timer = QtCore.QTimer(self.dialog)
        self.timer.setInterval(75)
        self._connect(self.timer.timeout, self._poll)
        self.probe: EmbeddingImportProbe | None = None
        self.inspection: EmbeddingImportInspection | None = None
        self.plan: EmbeddingImportPlan | None = None
        self.identifier_path: Path | None = None
        self.sidecar_path: Path | None = None
        self.identifier_probe: EmbeddingImportCompanionProbe | None = None
        self.changed = False
        self.imported_run_id = ""
        self.close_after_abort = False
        self._populating = False

        self._connect(self.identifier_file_button.clicked, self._toggle_identifier_file)
        self._connect(self.sidecar_file_button.clicked, self._toggle_sidecar_file)
        self._connect(
            self.identifier_file_combo.currentIndexChanged,
            self._schema_changed,
        )
        self._connect(self.vector_combo.currentIndexChanged, self._schema_changed)
        self._connect(self.identifier_combo.currentIndexChanged, self._schema_changed)
        self._connect(self.apply_button.clicked, self._commit)
        self._connect(self.name_edit.textChanged, self._validate_name)
        self._connect(self.close_button.clicked, self._close_requested)
        self.dialog.installEventFilter(self)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.hide()
        self.apply_button.setEnabled(False)
        self.identifier_file_combo.hide()
        if self.identifier_file_key_label is not None:
            self.identifier_file_key_label.hide()

    def exec(self) -> bool:
        try:
            self._load_source(self.source_path)
            self.dialog.exec()
            return self.changed
        finally:
            self.timer.stop()
            self.cancel_event.set()
            try:
                self.executor.shutdown(wait=True, cancel_futures=True)
                self.exchange.cleanup_import(self.inspection)
            finally:
                self.dialog.removeEventFilter(self)
                for signal, callback in reversed(self.connections):
                    signal.disconnect(callback)
                self.connections.clear()
                self.dialog.deleteLater()

    def _load_source(self, path: Path) -> None:
        if self.future is not None:
            return
        self.exchange.cleanup_import(self.inspection)
        self.source_path = Path(path)
        self.probe = None
        self.inspection = None
        self.plan = None
        self.preview_model.clear()
        self.apply_button.setEnabled(False)
        self.file_label.setText(self.source_path.name if len(self.source_paths) == 1 else f"{len(self.source_paths):,} embedding files")
        self.file_label.setToolTip("\n".join(str(path) for path in self.source_paths))
        self.name_edit.setText(self.source_path.stem)
        self.source_summary_label.setText("Inspecting…")
        self._start("probe", lambda: self.exchange.probe_import(
            self.source_paths if len(self.source_paths) > 1 else self.source_path,
            sidecar_path=self.sidecar_path,
        ), "Detecting vector fields…")

    def _populate_options(self, probe: EmbeddingImportProbe) -> None:
        self._populating = True
        try:
            self._set_identifier_file_visible(
                not bool(probe.default_identifier_key)
                or self.identifier_path is not None
            )

            self.vector_combo.clear()
            if not probe.default_vector_key:
                self.vector_combo.addItem("Choose vector field…", "")
            for key in probe.vector_candidates:
                self.vector_combo.addItem(key, key)
            if probe.wide_vector_columns:
                self.vector_combo.addItem(f"{len(probe.wide_vector_columns)} numeric columns", "<columns>")
            selected = self.vector_combo.findData(probe.default_vector_key or ("<columns>" if probe.wide_vector_columns else ""))
            self.vector_combo.setCurrentIndex(max(0, selected))
            self.identifier_combo.clear()
            self.identifier_combo.addItem("Generate Item IDs", "")
            for key in probe.identifier_candidates:
                self.identifier_combo.addItem(key, key)
            self.identifier_combo.setCurrentIndex(max(0, self.identifier_combo.findData(probe.default_identifier_key)))
        finally:
            self._populating = False

    def _interpretation(self) -> EmbeddingImportInterpretation:
        if self.probe is None:
            return EmbeddingImportInterpretation()
        return EmbeddingImportInterpretation(
            vector_key=(str(self.vector_combo.currentData() or "") if self.vector_combo.currentData() != "<columns>" else ""),
            identifier_key=str(self.identifier_combo.currentData() or ""),
            vector_columns=(
                ()
                if self.vector_combo.currentData() != "<columns>"
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
            self.vector_combo.currentData()
        )
        identifier_ready = (
            self.identifier_path is None
            or bool(self.identifier_file_combo.currentData())
        )
        return vector_ready and identifier_ready

    def _prepare(self) -> None:
        if self.future is not None or self.probe is None:
            return
        self.exchange.cleanup_import(self.inspection)
        self.inspection = None
        self.plan = None
        self.apply_button.setEnabled(False)
        if not self._interpretation_ready():
            self.preview_model.clear()
            self._show_import_error("Choose the vector and identifier fields under Import options.")
            self.options_button.setChecked(True)
            return
        probe = self.probe
        interpretation = self._interpretation()
        self._start(
            "prepare",
            lambda: self.exchange.prepare_import(
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
        inspection = self.inspection
        conflict_policy = self.conflict_combo.currentData()
        self._start(
            "link",
            lambda: self.exchange.preview_import_links(
                inspection,
                conflict_policy=conflict_policy,
            ),
            "Checking Items…",
        )

    def _commit(self) -> None:
        if self.future is not None or self.plan is None or not self.plan.can_import:
            return
        if not self._validate_name():
            self.name_edit.setFocus()
            return
        if self.process_manager is not None:
            try:
                self.process_manager.begin_metadata_publication()
            except ValueError as exc:
                self._show_import_error(str(exc))
                return
            self.publication_reserved = True
        plan = self.plan
        label = self.name_edit.text()
        self._start(
            "commit",
            lambda: self.exchange.commit_import(
                plan,
                label=label,
                progress_callback=self._queue_progress,
                should_cancel=self.cancel_event.is_set,
            ),
            "Saving vectors and Items…",
        )

    def _start(self, kind: str, function, message: str) -> None:
        if self.future is not None:
            return
        self.cancel_event.clear()
        self.future_kind = kind
        self.preview_stack.setCurrentIndex(0)
        self.preview_error.clear()
        self.preview_count.show()
        self.preview_status.show()
        self.progress.show()
        self.linking_label.hide()
        self.progress.setRange(0, 0 if kind in {"probe", "prepare", "link"} else 100)
        self.progress.setValue(0)
        self.preview_status.setText(message)
        self.apply_button.setEnabled(False)
        self.identifier_file_button.setEnabled(False)
        self.sidecar_file_button.setEnabled(False)
        self.identifier_file_combo.setEnabled(False)
        self.name_edit.setEnabled(False)
        self.conflict_combo.setEnabled(False)
        self.vector_combo.setEnabled(False)
        self.identifier_combo.setEnabled(False)
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
        self.name_edit.setEnabled(True)
        self.close_button.setText("Cancel")
        self.close_button.setEnabled(True)
        self.progress.hide()
        self.conflict_combo.setEnabled(True)
        self.vector_combo.setEnabled(True)
        self.identifier_combo.setEnabled(True)
        self.identifier_file_button.setEnabled(True)
        self.sidecar_file_button.setEnabled(True)
        self.identifier_file_combo.setEnabled(
            self.identifier_path is not None
            and self.identifier_file_combo.count() > 1
        )
        self.progress.setRange(0, 100)
        try:
            result = future.result()
            if self.close_after_abort and kind != "commit":
                if kind == "prepare":
                    self.exchange.cleanup_import(result)
                self.dialog.reject()
                return
            if kind == "probe":
                self.probe = result
                self._update_source_summary()
                self._populate_options(result)
                if self._interpretation_ready():
                    self._prepare()
                else:
                    self.preview_status.setText("Choose the vector field under Import options.")
                    self.options_button.setChecked(True)
            elif kind == "prepare":
                self.inspection = result
                self._update_source_summary(result)
                self._set_identifier_file_visible(not result.source_has_identifiers or self.identifier_path is not None)
                self._request_link()
            elif kind == "link":
                self.plan = result
                self.conflict_combo.setVisible(bool(result.conflicts))
                self.linking_label.setText(result.message)
                self.linking_label.show()
                self.preview_status.hide()
                self.preview_count.setText(
                    f"Showing {len(result.inspection.samples):,} of {result.inspection.row_count:,} vectors"
                )
                if not result.can_import:
                    self._show_import_error(result.message)
                    self.options_button.setChecked(True)
                self._populate_preview(result)
                self._validate_name()
            else:
                self.changed = True
                self.imported_run_id = result.id
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
            logging.getLogger(__name__).exception("Embedding import %s failed", kind)
            self.plan = None
            self.apply_button.setEnabled(False)
            self._show_import_error(str(exc))
            self.progress.setValue(0)
            if self.close_after_abort:
                self.dialog.reject()
        finally:
            if kind == "commit" and self.publication_reserved:
                self.publication_reserved = False
                self.process_manager.end_metadata_publication()
            self.close_after_abort = False

    def _toggle_options(self, expanded: bool) -> None:
        self.options_panel.setVisible(expanded)
        self.options_button.setArrowType(
            QtCore.Qt.ArrowType.DownArrow if expanded else QtCore.Qt.ArrowType.RightArrow
        )

    def _validate_name(self, *_args) -> bool:
        try:
            self.exchange.validate_import_name(self.name_edit.text())
        except ValueError as exc:
            self.name_error.setText(str(exc))
            self.name_error.show()
            valid = False
        else:
            self.name_error.clear()
            self.name_error.hide()
            valid = True
        self.apply_button.setEnabled(
            valid and self.future is None and self.plan is not None and self.plan.can_import
        )
        return valid

    def _show_import_error(self, message: str) -> None:
        self.preview_error.setText(f"Import failed: {message}")
        self.preview_stack.setCurrentIndex(1)
        self.preview_count.hide()
        self.preview_status.hide()
        self.linking_label.hide()

    def _populate_preview(self, plan: EmbeddingImportPlan | None) -> None:
        self.preview_model.clear()
        self.preview_model.setHorizontalHeaderLabels(
            ["Source row", "Identifier", "Embedding sample", "Item"]
        )
        if self.inspection is None:
            return
        matches = (
            np.load(plan.match_index_path, mmap_mode="r")
            if plan is not None and plan.match_index_path is not None
            else None
        )
        for sample in self.inspection.samples:
            item_id = int(matches[sample.row_index]) if matches is not None else -1
            status = str(item_id) if item_id >= 0 else "New Item"
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
                probe = self.exchange.probe_import_identifiers(Path(path))
            except Exception as exc:
                self._show_import_error(str(exc))
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
            self.close_button.setEnabled(False)
            self.close_button.setText("Cancelling…")
            self.preview_status.setText("Cancelling embedding import…")
            self.preview_status.show()

    def _close_requested(self) -> None:
        if self.future is not None:
            self.close_after_abort = True
            self._request_abort()
            return
        self.exchange.cleanup_import(self.inspection)
        self.inspection = None
        self.dialog.reject()

    def _queue_progress(self, value: int, message: str) -> None:
        event = (max(0, min(99, int(value))), str(message))
        try:
            self.progress_events.put_nowait(event)
        except queue.Full:
            try:
                self.progress_events.get_nowait()
            except queue.Empty:
                pass
            try:
                self.progress_events.put_nowait(event)
            except queue.Full:
                pass

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

    def eventFilter(self, watched, event):
        if self.future is not None and (
            event.type() == QtCore.QEvent.Type.Close
            or (event.type() == QtCore.QEvent.Type.KeyPress and event.key() == QtCore.Qt.Key.Key_Escape)
        ):
            self._close_requested()
            return True
        return False

    @staticmethod
    def _format_bytes(value: int) -> str:
        size = float(value)
        for unit in ("B", "KiB", "MiB", "GiB"):
            if size < 1024 or unit == "GiB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{value:,} B"
