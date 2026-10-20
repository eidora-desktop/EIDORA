from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import math
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtGui, QtWidgets

from EmbeddingsArea.exchange import formats
from EmbeddingsArea.exchange.formats import (
    COMMON_EXPORT_FORMATS,
    FORMATS,
    FORMAT_STRUCTURES,
    STRUCTURE_LABELS,
)
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child

if TYPE_CHECKING:
    from shared.qt.activity import ActivityManager, ActivityToken


class EmbeddingExportDialogController:
    """Bind export options and keep embedding serialization off the UI thread."""

    _COMPRESSIBLE_FORMATS = {".npz", ".parquet", ".h5"}

    def __init__(
        self,
        service,
        run_id: str,
        *,
        activity_manager: ActivityManager | None = None,
        finished_callback: (
            Callable[
                ["EmbeddingExportDialogController", object | None, str | None],
                None,
            ]
            | None
        ) = None,
    ) -> None:
        self.service = service
        self.run_id = run_id
        self._activity_manager = activity_manager
        self._status_activity: ActivityToken | None = None
        self._finished_callback = finished_callback
        self._detached_export = False
        self._disposed = False
        self.dialog = load_ui("EmbeddingsArea", "ui/embedding_export_dialog.ui")
        self.dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)

        self.format_combo = self._widget(QtWidgets.QComboBox, "combo_export_format")
        self.structure_combo = self._widget(QtWidgets.QComboBox, "combo_export_structure")
        self.identifier_label = self._widget(QtWidgets.QLabel, "label_export_identifier")
        self.identifier_combo = self._widget(QtWidgets.QComboBox, "combo_metadata")
        self.dtype_combo = self._widget(QtWidgets.QComboBox, "combo_export_dtype")
        self.compression_check = self._widget(QtWidgets.QCheckBox, "check_export_compression")
        self.shard_size = self._widget(QtWidgets.QSpinBox, "spin_export_shard_size")
        self.dataset_summary = self._widget(QtWidgets.QLabel, "label_export_dataset_summary")
        self.preview_table = self._widget(QtWidgets.QTableView, "table_export_embedding_preview")
        self.preview_text = self._widget(QtWidgets.QPlainTextEdit, "text_export_embedding_preview")
        self.preview_status = self._widget(QtWidgets.QLabel, "label_export_embedding_preview_status")
        self.output_plan = self._widget(QtWidgets.QLabel, "label_export_output_plan")
        self.show_button = self._widget(QtWidgets.QPushButton, "button_show_export")
        self.export_button = self._widget(QtWidgets.QPushButton, "button_apply")
        self.cancel_button = self._widget(QtWidgets.QPushButton, "button_close")

        self.preview_model = QtGui.QStandardItemModel(self.dialog)
        self.preview_table.setModel(self.preview_model)
        fixed_font = QtGui.QFontDatabase.systemFont(
            QtGui.QFontDatabase.SystemFont.FixedFont
        )
        self.preview_table.setFont(fixed_font)
        self.preview_text.setFont(fixed_font)
        self.preview_table.horizontalHeader().setSectionResizeMode(
            QtWidgets.QHeaderView.ResizeMode.Interactive
        )
        self.preview_table.horizontalHeader().setDefaultSectionSize(88)

        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="embedding-export"
        )
        self.future = None
        self.operation = None
        self.export_path = None
        self._last_preview = None
        self._preview_pending = False
        self._identifier_choices = ()
        self._cancel_event = threading.Event()
        self._progress_lock = threading.Lock()
        self._progress_value = (0, 0)
        self.connections = []
        self.timer = QtCore.QTimer(self.dialog)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._poll)

        self._populate_formats()
        self.compression_check.setChecked(True)
        self._configure_dtype_items()
        self._connect_signals()
        self._load_identifier_choices()
        self._format_changed()
        self.format_combo.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
        self.shard_size.lineEdit().deselect()
        self.dialog.installEventFilter(self._close_guard())

    def exec(self):
        self.dialog.exec()
        if self.operation == "export":
            self._detached_export = True
            return None
        self._dispose(wait=True)
        return self.export_path

    @property
    def detached_export(self) -> bool:
        return self._detached_export

    def shutdown(self) -> None:
        self._cancel_event.set()
        self._dispose(wait=True)

    def _populate_formats(self) -> None:
        self.format_combo.clear()
        for suffix in COMMON_EXPORT_FORMATS:
            self.format_combo.addItem(FORMATS[suffix], suffix)
        default_index = self.format_combo.findData(".parquet")
        self.format_combo.setCurrentIndex(max(0, default_index))

    def _load_identifier_choices(self) -> None:
        self.identifier_combo.blockSignals(True)
        self.identifier_combo.setEnabled(False)
        self.identifier_combo.clear()
        self.identifier_combo.addItem("Loading identifiers…", None)
        self.identifier_combo.blockSignals(False)
        self.preview_status.setText("Checking available identifier columns…")
        self.export_button.setEnabled(False)
        self._submit(
            "identifiers",
            self.service.exchange.list_export_identifier_columns,
            self.run_id,
            cancel_check=self._cancel_event.is_set,
        )

    def _apply_identifier_choices(self, choices) -> None:
        self._identifier_choices = tuple(choices)
        self.identifier_combo.blockSignals(True)
        self.identifier_combo.clear()
        for label, name in self._identifier_choices:
            self.identifier_combo.addItem(label, name)
        self.identifier_combo.blockSignals(False)
        if self.identifier_combo.count():
            self._update_identifier_option()
            self._request_preview()
            return
        if self._format_suffix() == ".npy":
            self.identifier_combo.addItem("Not included in NPY", None)
            self._update_identifier_option()
            self._request_preview()
            return
        self.identifier_combo.addItem("No complete unique identifiers", None)
        self.identifier_combo.setEnabled(False)
        self.preview_model.clear()
        self.preview_status.setText(
            "This embedding has no complete unique identifier. Add or repair an "
            "identifier column before exporting."
        )

    def _configure_dtype_items(self) -> None:
        for index, value in enumerate(("preserve", "float32", "float16")):
            self.dtype_combo.setItemData(index, value)

    def _connect_signals(self) -> None:
        self._connect(self.format_combo.currentIndexChanged, self._format_changed)
        self._connect(self.structure_combo.currentIndexChanged, self._request_preview)
        self._connect(self.identifier_combo.currentIndexChanged, self._request_preview)
        self._connect(self.dtype_combo.currentIndexChanged, self._request_preview)
        self._connect(self.compression_check.toggled, self._update_output_plan)
        self._connect(self.shard_size.valueChanged, self._update_output_plan)
        self._connect(self.export_button.clicked, self._export_to_file)
        self._connect(self.cancel_button.clicked, self._cancel_or_close)
        self._connect(self.show_button.clicked, self._show_export_location)

    def _format_changed(self, *_args) -> None:
        suffix = self._format_suffix()
        selected_structure = self.structure_combo.currentData()
        self.structure_combo.blockSignals(True)
        self.structure_combo.clear()
        for structure in FORMAT_STRUCTURES.get(suffix, ()):
            self.structure_combo.addItem(STRUCTURE_LABELS[structure], structure)
        structure_index = self.structure_combo.findData(selected_structure)
        self.structure_combo.setCurrentIndex(max(0, structure_index))
        self.structure_combo.setEnabled(
            self.structure_combo.count() > 1 and self.operation != "export"
        )
        self.structure_combo.blockSignals(False)

        self.dtype_combo.setEnabled(self.operation != "export")
        compression_available = suffix in self._COMPRESSIBLE_FORMATS
        self.compression_check.setEnabled(
            compression_available and self.operation != "export"
        )
        if not compression_available:
            self.compression_check.setChecked(False)
        self._update_identifier_option()
        self._request_preview()

    def _request_preview(self, *_args) -> None:
        self._update_output_plan()
        if not self._identifier_choices and self._format_suffix() != ".npy":
            self._preview_pending = True
            return
        if self.future is not None:
            if self.operation != "export":
                self._preview_pending = True
            return
        self._preview_pending = False
        self.preview_status.setText("Updating export preview…")
        self.export_button.setEnabled(False)
        self._submit(
            "preview",
            self.service.exchange.preview_export,
            self.run_id,
            format_suffix=self._format_suffix(),
            structure=self._structure(),
            metadata_column=self._metadata_column(),
            dtype=str(self.dtype_combo.currentData() or "preserve"),
            include_provenance=True,
            limit=25,
            dimension_limit=50,
            cancel_check=self._cancel_event.is_set,
        )

    def _apply_preview(self, preview: dict) -> None:
        self._last_preview = preview
        tabular = preview["preview_kind"] == "table"
        self.preview_table.setVisible(tabular)
        self.preview_text.setVisible(not tabular)
        self.preview_text.setPlainText(preview["primary_content"])
        self.preview_model.clear()
        self.preview_model.setHorizontalHeaderLabels(list(preview["columns"]))
        for row in preview["rows"]:
            items = []
            for value in row:
                text = f"{value:.7g}" if isinstance(value, float) else str(value)
                item = QtGui.QStandardItem(text)
                if isinstance(value, (int, float)):
                    item.setTextAlignment(
                        QtCore.Qt.AlignmentFlag.AlignRight
                        | QtCore.Qt.AlignmentFlag.AlignVCenter
                    )
                items.append(item)
            self.preview_model.appendRow(items)
        if self.preview_model.columnCount():
            self.preview_table.resizeColumnToContents(0)
        self.preview_table.horizontalHeader().setStretchLastSection(
            self.preview_model.columnCount() == 2
        )

        estimated_size = self._format_bytes(preview["estimated_bytes"])
        dimension_text = (
            f"{preview['shown_dimensions']:,} of {preview['dimensions']:,} dimensions"
        )
        self.dataset_summary.setText(
            f"{preview['label']} · {preview['total']:,} vectors × "
            f"{preview['dimensions']:,} dimensions · {preview['dtype']}"
        )
        self.preview_status.setText(
            f"Previewing the first {preview['shown']:,} of {preview['total']:,} vectors "
            f"and {dimension_text}."
        )
        self.export_button.setEnabled(preview["total"] > 0)
        self._update_output_plan(estimated_size=estimated_size)

    def _update_output_plan(self, *_args, estimated_size=None) -> None:
        preview = self._last_preview
        if preview is None:
            self.output_plan.setText("Output plan will appear after the preview is ready.")
            return
        total = int(preview["total"])
        shard_size = self.shard_size.value()
        shard_count = (
            math.ceil(total / shard_size)
            if shard_size and shard_size < total
            else 1
        )
        parts = [f"{shard_count:,} data file{'s' if shard_count != 1 else ''}"]
        if self._format_suffix() == ".npy":
            parts.append("vectors only; identifiers are not included")
        if self.compression_check.isChecked():
            parts.append("compression enabled")
        raw_size = estimated_size or self._format_bytes(preview["estimated_bytes"])
        self.output_plan.setText(
            f"Output: {' + '.join(parts)} · approximately {raw_size} of raw vector data."
        )

    def _export_to_file(self, *_args) -> None:
        if self.future is not None or self._last_preview is None:
            return
        suffix = self._format_suffix()
        label = FORMATS[suffix]
        path, _selected_filter = QtWidgets.QFileDialog.getSaveFileName(
            self.dialog,
            "Export Embeddings",
            str(Path.home() / f"embeddings{suffix}"),
            f"{label} (*{suffix})",
        )
        if not path:
            return
        output = Path(path).with_suffix(suffix)
        self._start_export(output)

    def _start_export(self, output: Path) -> None:
        if self._last_preview is None:
            return
        self._cancel_event.clear()
        total = int(self._last_preview["total"])
        with self._progress_lock:
            self._progress_value = (0, total)
        self.preview_status.setText("Writing embedding export…")
        self.show_button.setVisible(False)
        self._set_options_enabled(False)
        self.cancel_button.setText("Abort")
        self.cancel_button.setEnabled(True)
        if self._activity_manager is not None:
            self._status_activity = self._activity_manager.progress(
                "Exporting embeddings…",
                value=0,
                total=max(1, total),
                cancel=self._cancel_or_close,
            )

        def report_progress(completed, progress_total):
            with self._progress_lock:
                self._progress_value = (completed, progress_total)

        self._submit(
            "export",
            self.service.exchange.export,
            self.run_id,
            output,
            metadata_column=self._metadata_column(),
            structure=self._structure(),
            dtype=str(self.dtype_combo.currentData() or "preserve"),
            include_provenance=True,
            compression=self.compression_check.isChecked(),
            shard_size=self.shard_size.value(),
            cancel_check=self._cancel_event.is_set,
            progress_callback=report_progress,
        )
        self.dialog.accept()

    def _cancel_or_close(self, *_args) -> None:
        if self.operation == "export":
            self._cancel_event.set()
            self.cancel_button.setEnabled(False)
            self.preview_status.setText("Stopping export safely…")
            return
        self._cancel_event.set()
        self.dialog.reject()

    def _show_export_location(self, *_args) -> None:
        if self.export_path is None:
            return
        path = self.export_path[0] if isinstance(self.export_path, tuple) else self.export_path
        QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(Path(path).parent))
        )

    def _submit(self, operation, callback, *args, **kwargs) -> None:
        if self.future is not None:
            raise RuntimeError("An embedding export operation is already running.")
        self.operation = operation
        self.future = self.executor.submit(callback, *args, **kwargs)
        self.timer.start()

    def _poll(self) -> None:
        if self.operation == "export":
            with self._progress_lock:
                completed, total = self._progress_value
            if self._status_activity is not None:
                self._status_activity.update(
                    message="Writing embedding export…",
                    value=completed,
                    total=max(1, total),
                )
        if self.future is None or not self.future.done():
            return
        operation = self.operation
        future, self.future = self.future, None
        self.operation = None
        self.timer.stop()
        try:
            result = future.result()
        except formats.ExportCancelledError:
            if operation == "export":
                self.preview_status.setText(
                    "Export aborted; no new export files were published."
                )
                self._finish_status_activity()
                if self._detached_export:
                    self._finish_detached_export(None, None)
                else:
                    self._restore_after_export()
            return
        except Exception as exc:
            if operation == "export":
                if self._status_activity is not None:
                    self._status_activity.retain_failed(
                        f"Embedding export failed: {exc}"
                    )
                    self._status_activity = None
                if self._detached_export:
                    self._finish_detached_export(None, str(exc))
                else:
                    self._restore_after_export()
            self.preview_status.setText(str(exc))
            if operation == "preview" and self._preview_pending:
                self._preview_pending = False
                self._request_preview()
            return

        if operation == "identifiers":
            self._apply_identifier_choices(result)
            return
        if operation == "preview":
            if self._preview_pending:
                self._preview_pending = False
                self._request_preview()
            else:
                self._apply_preview(result)
            return
        if operation == "export":
            self.export_path = result
            if self._status_activity is not None:
                with self._progress_lock:
                    _completed, total = self._progress_value
                self._status_activity.update(
                    message="Embedding export complete",
                    value=max(1, total),
                    total=max(1, total),
                )
                self._status_activity.retain_completed()
                self._status_activity = None
            if self._detached_export:
                self._finish_detached_export(result, None)

    def _finish_status_activity(self) -> None:
        if self._status_activity is None:
            return
        self._status_activity.finish()
        self._status_activity = None

    def _finish_detached_export(self, result: object | None, error: str | None) -> None:
        callback = self._finished_callback
        try:
            if callback is not None:
                callback(self, result, error)
        finally:
            self._dispose(wait=False)

    def _dispose(self, *, wait: bool) -> None:
        if self._disposed:
            return
        self._disposed = True
        self.timer.stop()
        self._finish_status_activity()
        for signal, callback in reversed(self.connections):
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError):
                pass
        self.connections.clear()
        self.executor.shutdown(wait=wait)

    def _restore_after_export(self) -> None:
        self._cancel_event.clear()
        self.cancel_button.setText("Cancel")
        self.cancel_button.setEnabled(True)
        self._set_options_enabled(True)
        suffix = self._format_suffix()
        self.structure_combo.setEnabled(self.structure_combo.count() > 1)
        self.dtype_combo.setEnabled(True)
        self.compression_check.setEnabled(suffix in self._COMPRESSIBLE_FORMATS)
        self.export_button.setEnabled(self._last_preview is not None)

    def _set_options_enabled(self, enabled) -> None:
        for widget in (
            self.format_combo,
            self.structure_combo,
            self.identifier_combo,
            self.dtype_combo,
            self.compression_check,
            self.shard_size,
            self.export_button,
        ):
            widget.setEnabled(enabled)
        if enabled:
            self._update_identifier_option()

    def _format_suffix(self) -> str:
        return str(self.format_combo.currentData() or ".parquet")

    def _structure(self) -> str:
        return str(self.structure_combo.currentData() or "matrix")

    def _metadata_column(self) -> str:
        if self._format_suffix() == ".npy":
            return ""
        return str(self.identifier_combo.currentData() or "")

    def _update_identifier_option(self) -> None:
        vectors_only = self._format_suffix() == ".npy"
        self.identifier_label.setText(
            "Identifier (not exported):" if vectors_only else "Identifier:"
        )
        self.identifier_combo.setEnabled(
            bool(self._identifier_choices) and not vectors_only and self.operation != "export"
        )

    def _connect(self, signal, callback) -> None:
        signal.connect(callback)
        self.connections.append((signal, callback))

    def _close_guard(self):
        controller = self

        class Guard(QtCore.QObject):
            def eventFilter(self, watched, event):
                if controller.operation != "export":
                    if event.type() == QtCore.QEvent.Type.Close:
                        controller._cancel_event.set()
                    return False
                if event.type() == QtCore.QEvent.Type.Close:
                    controller._cancel_or_close()
                    return True
                return (
                    event.type() == QtCore.QEvent.Type.KeyPress
                    and event.key() == QtCore.Qt.Key.Key_Escape
                )

        self.guard = Guard(self.dialog)
        return self.guard

    def _widget(self, widget_type: type, name: str):
        return require_child(self.dialog, widget_type, name)

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = float(max(0, value))
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if amount < 1024 or unit == "TiB":
                return f"{amount:.1f} {unit}"
            amount /= 1024
        return f"{amount:.1f} TiB"
