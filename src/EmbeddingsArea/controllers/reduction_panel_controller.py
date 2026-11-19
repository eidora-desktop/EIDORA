"""Binder for one Vector Data card's Designer-owned PCA tab."""
from pathlib import Path
from PySide6 import QtCore, QtWidgets
from shared.qt.widgets import require_child
from EmbeddingsArea.reduction.domain import ReductionRequest
from EmbeddingsArea.reduction.parameters import storage_estimate


def _size(value):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1000 or unit == "TB":
            return f"{value:,.1f} {unit}"
        value /= 1000


class ReductionPanelController(QtCore.QObject):
    def __init__(self, widget, run_id, coordinator):
        super().__init__(widget)
        self.widget, self.run_id, self.coordinator = widget, run_id, coordinator
        self.dataset = self.run_record = None
        self._summary_key = None
        self._source_itemsize = 4
        self._inspection_error = ""
        self.incremental = require_child(widget, QtWidgets.QCheckBox, "check_reduction_incremental")
        self.target = require_child(widget, QtWidgets.QSpinBox, "spin_reduction_dimensions")
        self.output = require_child(widget, QtWidgets.QComboBox, "combo_reduction_output")
        self.name = require_child(widget, QtWidgets.QLineEdit, "edit_reduction_name")
        self.source = require_child(widget, QtWidgets.QLabel, "label_reduction_source")
        self.estimate = require_child(widget, QtWidgets.QLabel, "label_reduction_storage")
        self.result = require_child(widget, QtWidgets.QLabel, "label_reduction_result")
        self.start = require_child(widget, QtWidgets.QPushButton, "button_reduce_dimensions")
        self.incremental.toggled.connect(self._settings_changed)
        self.target.valueChanged.connect(self._settings_changed)
        self.output.currentIndexChanged.connect(self._settings_changed)
        self.name.textEdited.connect(self._name_edited)
        self.start.clicked.connect(self._start)
        self._automatic_name = True
        widget.installEventFilter(self)

    def update(self, run, dataset, model_label=""):
        self._model_name = (model_label or run.selected_model_id) if run.selected_model_id else run.label
        first = self.dataset is None
        self.run_record, self.dataset = run, dataset
        maximum = max(1, min(dataset.dimensions - 1, dataset.item_count - 1)) if dataset else 1
        self.target.setMaximum(maximum)
        if first:
            self.target.setValue(min(48, maximum))
        self.source.setText(f"{dataset.item_count:,} vectors, {dataset.dimensions:,} dimensions" if dataset else "Reduction is available after vector data is published.")
        if self.widget.isVisible():
            self._inspect()
        self._settings_changed()

    def eventFilter(self, watched, event):
        if watched is self.widget and event.type() == QtCore.QEvent.Type.Show:
            self._inspect()
            self._settings_changed()
        return super().eventFilter(watched, event)

    def _inspect(self):
        if not self.dataset or not self.dataset.artifact_path:
            return
        key = (self.dataset.artifact_path, self.dataset.updated_at)
        if key == self._summary_key:
            return
        try:
            from EmbeddingsArea.reduction.repository import inspect_source
            summary, _, dtype = inspect_source(Path(self.dataset.artifact_path))
            self._source_itemsize = dtype.itemsize
            self._summary_key = key
            self._inspection_error = ""
        except Exception as exc:
            self._inspection_error = f"Cannot inspect vector data: {exc}"

    def _name_edited(self, _text):
        self._automatic_name = False

    def _settings_changed(self, *_args):
        replacing = self.output.currentIndex() == 1
        active = self.coordinator.state("reduction", self.run_id)
        busy = active is not None and active.state in {"queued", "running", "cancelling"}
        if self._automatic_name and self.run_record:
            method = "PCAin" if self.incremental.isChecked() else "PCA"
            base_name = self.run_record.label if replacing else self._model_name
            self.name.setText(f"{base_name} ({method}_{self.target.value()})")
        for control in (self.incremental, self.target, self.output):
            control.setEnabled(self.dataset is not None and not busy)
        self.name.setEnabled(self.dataset is not None and not busy)
        valid = self.dataset is not None and self.dataset.dimensions > 1 and self.dataset.item_count > 1
        blocked = self.coordinator.reduction_source_busy(self.run_id) if hasattr(self.coordinator, "reduction_source_busy") else False
        self.start.setEnabled(valid and not busy and not blocked and not self._inspection_error)
        if self.dataset:
            original, reduced = storage_estimate(self.dataset.item_count, self.dataset.dimensions,
                                                 self.target.value(), self._source_itemsize)
            saving = original - reduced
            ratio = saving / original * 100 if original else 0
            outcome = f"Frees approximately {_size(saving)}" if replacing and saving >= 0 else f"Adds approximately {_size(reduced if not replacing else -saving)}"
            self.estimate.setText(f"Vector values: {_size(original)} → {_size(reduced)} ({ratio:.1f}% smaller).\n{outcome}; excludes metadata and the PCA transform. Temporary disk space is required.")
        else:
            self.estimate.setText("")
        self.result.setText(self._inspection_error or "Progress and cancellation are available in the output card’s Progress tab.")

    def _start(self):
        request = ReductionRequest(method="incremental_pca" if self.incremental.isChecked() else "pca",
                                   dimensions=self.target.value(), replace_original=self.output.currentIndex() == 1,
                                   label=self.name.text().strip())
        if request.replace_original:
            answer = QtWidgets.QMessageBox.question(self.widget, "Replace original vector data",
                f"Replace '{self.run_record.label}' with {request.dimensions}-dimensional vectors?\n\n"
                "The original vectors will be permanently removed after reduction succeeds. "
                "Existing projections, clusters and figures keep their saved results from the original vectors.",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No)
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        try:
            self.coordinator.submit_reduction(self.run_id, request)
        except Exception as exc:
            self.result.setText(str(exc))
