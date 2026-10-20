"""Descriptor-driven configuration dialog for an embedding run."""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from EmbeddingsArea.domain import (
    EmbeddingModelDescriptor,
    EmbeddingModelParameter,
    EmbeddingRunState,
    MediaSourceKind,
)
from DiscoveryArea.thumbnails.repository import ThumbnailVariantRepository
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class ModelExecutionDialogController:
    def __init__(self, session, project, *, run_id: str | None = None, model_id: str = "", discard_on_cancel: bool = False) -> None:
        self._session = session
        self._project = project
        self._created = run_id is None or discard_on_cancel
        if run_id is None:
            if not model_id:
                raise ValueError("Choose an installed model before configuring an embedding run.")
            label = session.service.next_default_embedding_run_label(model_id)
            run = session.service.create_draft_embedding_run(label)
            run = session.service.update_embedding_run_configuration(run.id, model_id=model_id)
            run_id = run.id
        self.run_id = run_id
        configuration = self._session.service.get_embedding_run_configuration(self.run_id)
        self._model_id = configuration.run.selected_model_id or model_id
        self._model = self._session.model_catalog.get_model(self._model_id) if self._model_id else None
        self.dialog = load_ui("EmbeddingsArea", "ui/model_execution_dialog.ui", None)
        self.dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self._name = require_child(self.dialog, QtWidgets.QLineEdit, "edit_output_name")
        self._model_label = require_child(self.dialog, QtWidgets.QLabel, "label_model_value")
        self._inputs = require_child(self.dialog, QtWidgets.QFormLayout, "form_inputs")
        self._parameters = require_child(self.dialog, QtWidgets.QFormLayout, "form_parameters")
        self._thumbnails = require_child(self.dialog, QtWidgets.QCheckBox, "check_create_thumbnails")
        self._error = require_child(self.dialog, QtWidgets.QLabel, "label_error")
        self._run = require_child(self.dialog, QtWidgets.QPushButton, "button_run")
        self._input_combos: dict[str, QtWidgets.QComboBox] = {}
        self._required_input_ids: set[str] = set()
        self._parameter_readers: dict[tuple[str, str], callable] = {}
        self._parameter_widgets = {}
        self._load(configuration)
        require_child(self.dialog, QtWidgets.QPushButton, "button_reset_preparation").clicked.connect(self._reset_preparation)
        self._name.textChanged.connect(self._update_availability)
        self._run.clicked.connect(self._accept)
        require_child(self.dialog, QtWidgets.QPushButton, "button_cancel").clicked.connect(self.dialog.reject)
        self._update_availability()

    def exec(self) -> str | None:
        accepted = self.dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted
        if accepted:
            self._created = False
            return self.run_id
        self._discard_unused_draft()
        return None

    def _load(self, configuration) -> None:
        self._name.setText(configuration.run.label)
        self._model_label.setText(self._model.label if self._model is not None else self._model_id or "Unavailable model")
        self._model_label.setToolTip(self._model_id)
        if self._model is not None:
            self.dialog.setWindowTitle(f"Run {self._model.label}")
        self._rebuild(configuration)
        has_completed = any(
            run.state == EmbeddingRunState.COMPLETED
            for run in self._session.service.list_embedding_runs()
            if run.id != self.run_id
        )
        variants = ThumbnailVariantRepository(self._project.paths.root).list_variants()
        saved = {
            (value.scope, value.parameter_id): value.value
            for value in configuration.parameter_values
        }
        default = not has_completed and not variants
        self._thumbnails.setChecked(
            saved.get(("run", "create_thumbnails"), str(default)).lower() in {"1", "true", "yes"}
        )
        supports_thumbnails = bool(
            self._model
            and any(value.value in {"image", "video"} for value in self._model.compatible_content_types)
        )
        self._thumbnails.setVisible(not bool(variants) and supports_thumbnails)

    def _rebuild(self, configuration) -> None:
        self._clear_form(self._inputs)
        self._clear_form(self._parameters)
        self._input_combos = {}
        self._required_input_ids = set()
        self._parameter_readers = {}
        self._parameter_widgets = {}
        model = self._model
        if model is None:
            return
        saved_bindings = {value.input_id: value.source_id for value in configuration.input_bindings}
        sources = self._session.service.list_embedding_input_sources()
        requirements = [
            value for value in model.input_requirements
            if value.visible and value.source_kind == "media_source"
        ]
        if not requirements and model.compatible_content_types:
            requirements = [type("Input", (), {"id": "source", "label": "Media source", "modality": model.compatible_content_types[0], "required": True})()]
        for requirement in requirements:
            if requirement.required:
                self._required_input_ids.add(requirement.id)
            combo = QtWidgets.QComboBox(self.dialog)
            combo.setSizeAdjustPolicy(
                QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(24)
            combo.setToolTip("Run on linked assets or a metadata text column.")
            combo.addItem("Select an input source" if requirement.required else "Not used", "")
            placeholder = combo.model().item(0)
            if requirement.required and isinstance(placeholder, QtGui.QStandardItem):
                placeholder.setForeground(QtGui.QBrush(QtGui.QColor("#e53935")))
            usable_source_ids: list[str] = []
            for summary in sources:
                if summary.source.content_type == requirement.modality:
                    metadata_text = summary.source.source_kind == MediaSourceKind.METADATA_COLUMN
                    item_count = summary.asset_count if metadata_text else summary.linked_count
                    usable = item_count > 0
                    if usable:
                        usable_source_ids.append(summary.source.id)
                    item_label = "item" if item_count == 1 else "items"
                    suffix = (
                        f"{item_count} {item_label}"
                        if metadata_text and usable
                        else f"{item_count} linked {item_label}"
                        if usable
                        else "no linked items"
                    )
                    combo.addItem(f"{summary.source.label} ({suffix})", summary.source.id)
                    item = combo.model().item(combo.count() - 1)
                    if isinstance(item, QtGui.QStandardItem):
                        item.setEnabled(usable)
                        if not usable:
                            item.setForeground(QtGui.QBrush(QtGui.QColor("#e53935")))
                            item.setToolTip("Link assets to metadata before running an embedding model.")
            selected = saved_bindings.get(requirement.id, "")
            if not selected and len(configuration.run.selected_source_ids) == 1:
                selected = configuration.run.selected_source_ids[0]
            if selected not in usable_source_ids:
                selected = ""
            if not selected and not configuration.run.selected_source_ids and len(usable_source_ids) == 1:
                selected = usable_source_ids[0]
            index = combo.findData(selected)
            combo.setCurrentIndex(index if index >= 0 else 0)
            self._inputs.addRow(requirement.label, combo)
            self._input_combos[requirement.id] = combo
            combo.currentIndexChanged.connect(
                lambda _index, target=combo: self._input_changed(target)
            )
        saved = {(value.scope, value.parameter_id): value.value for value in configuration.parameter_values}
        if model.runtime_adapter_id.startswith("onnx") or model.backend.casefold() == "onnx":
            device = QtWidgets.QComboBox(self.dialog)
            from EmbeddingsArea.runtime.adapters.onnx import execution_device_availability, unavailable_device_reason
            availability = execution_device_availability(model.runtime_contract.get("execution_providers", ["CPUExecutionProvider"]))
            for label, key in (("Auto", "auto"), ("CPU", "cpu"), ("CUDA (NVIDIA GPU)", "cuda"), ("CoreML (Apple GPU / Neural Engine)", "coreml")):
                enabled = availability.get(key, False)
                if key not in {"auto", "cpu"} and not enabled:
                    continue
                device.addItem(label if enabled else f"{label} — unavailable", key)
                item = device.model().item(device.count() - 1)
                if isinstance(item, QtGui.QStandardItem):
                    item.setEnabled(enabled)
                    item.setToolTip(unavailable_device_reason(key) if not enabled else "")
            device.setCurrentIndex(max(0, device.findData(saved.get(("run", "device"), "auto"))))
            self._parameters.addRow("Execution device", device)
            self._parameter_readers[("run", "device")] = lambda widget=device: str(widget.currentData())
        batch = QtWidgets.QSpinBox(self.dialog)
        fixed_batch_one = bool(model.runtime_contract) and any(
            list(item.get("shape", []))[:1] == [1]
            for item in [*model.runtime_contract.get("inputs", []), model.runtime_contract.get("output", {})]
            if isinstance(item, dict)
        )
        batch.setRange(1, 1 if fixed_batch_one else 512)
        batch.setValue(1 if fixed_batch_one else int(saved.get(("run", "batch_size"), "32")))
        self._parameters.addRow("Inference batch size", batch)
        self._parameter_readers[("run", "batch_size")] = lambda widget=batch: str(widget.value())
        if model.runtime_contract:
            preparation = QtWidgets.QLabel(self.dialog)
            preparation.setWordWrap(True)
            preparation.setText(_preparation_summary(model.runtime_contract))
            preparation.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            self._parameters.addRow("Model preparation", preparation)
        for parameter in _editable_model_parameters(model):
            widget, read = self._parameter_editor(parameter, saved.get((parameter.scope, parameter.id), parameter.default))
            self._parameters.addRow(parameter.label, widget)
            widget.setToolTip(parameter.description)
            self._parameter_widgets[parameter.id] = widget
            self._parameter_readers[(parameter.scope, parameter.id)] = read
            signal = (widget.currentIndexChanged if isinstance(widget, QtWidgets.QComboBox)
                      else widget.valueChanged if isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox))
                      else widget.textChanged if isinstance(widget, QtWidgets.QLineEdit) else widget.toggled)
            signal.connect(self._update_parameter_conditions)
        self._update_parameter_conditions()
        require_child(self.dialog, QtWidgets.QPushButton, "button_reset_preparation").setVisible(bool(self._parameter_widgets))

    def _update_parameter_conditions(self, *_args):
        from EmbeddingsArea.preparation_contract import OPTIONS
        for identity, widget in self._parameter_widgets.items():
            prefix, _, key = identity.rpartition(".")
            conditions = OPTIONS.get(key, {}).get("enabled_when", {})
            component = self._model.runtime_contract.get("components", {}).get(prefix, self._model.runtime_contract)
            enabled = True
            for dependency, choices in conditions.items():
                target = f"{prefix}.{dependency}" if prefix else dependency
                reader = self._parameter_readers.get(("preprocess", target))
                prep = component.get("preparation", {})
                value = reader() if reader else prep.get("parameters", {}).get(dependency, {}).get("default", prep.get(dependency))
                enabled = enabled and value in choices
            widget.setEnabled(enabled)

    def _reset_preparation(self):
        for parameter in _editable_model_parameters(self._model):
            widget = self._parameter_widgets[parameter.id]
            value = parameter.default
            if isinstance(widget, QtWidgets.QComboBox):
                widget.setCurrentIndex(widget.findData(value))
            elif isinstance(widget, QtWidgets.QSpinBox):
                widget.setValue(-1 if value == "mono" else int(value))
            elif isinstance(widget, QtWidgets.QDoubleSpinBox):
                widget.setValue(float(value))
            elif isinstance(widget, QtWidgets.QLineEdit):
                widget.setText(value)
        self._update_parameter_conditions()

    def _update_availability(self, *_args) -> None:
        reason = ""
        if not self._name.text().strip():
            reason = "Enter an output name."
        elif self._model is None:
            reason = "The selected model is no longer installed."
        elif not self._input_combos:
            reason = "Select a compatible asset source or metadata column."
        elif self._required_input_ids and any(
            not self._input_combos[input_id].currentData()
            for input_id in self._required_input_ids
        ):
            reason = "Select sources for all required model inputs."
        elif not self._required_input_ids and not any(
            combo.currentData() for combo in self._input_combos.values()
        ):
            reason = "Select a compatible asset source or metadata column."
        self._run.setEnabled(not reason)
        self._run.setToolTip(reason)

    def _input_changed(self, combo: QtWidgets.QComboBox) -> None:
        self._update_availability()

    def _parameter_editor(self, parameter: EmbeddingModelParameter, selected: str):
        if parameter.choices or parameter.kind in {"choice", "select"}:
            widget = QtWidgets.QComboBox(self.dialog)
            for choice in parameter.choices or (parameter.default,):
                widget.addItem(str(choice).replace("_", " "), str(choice))
            widget.setCurrentIndex(max(0, widget.findData(selected)))
            return widget, lambda: str(widget.currentData() or "")
        if parameter.kind in {"integer", "number", "channel"}:
            if parameter.kind == "number":
                widget = QtWidgets.QDoubleSpinBox(self.dialog)
                widget.setDecimals(12)
                widget.setRange(parameter.minimum if parameter.minimum is not None else 0,
                                parameter.maximum if parameter.maximum is not None else 86400)
                widget.setValue(float(selected))
                return widget, lambda: str(widget.value())
            widget = QtWidgets.QSpinBox(self.dialog)
            widget.setRange(-1 if parameter.kind == "channel" else int(parameter.minimum or 0), int(parameter.maximum if parameter.maximum is not None else 16384))
            if parameter.kind == "channel":
                widget.setSpecialValueText("Mono")
            widget.setValue(-1 if selected == "mono" else int(selected))
            return widget, lambda: "mono" if widget.value() == -1 else str(widget.value())
        if parameter.kind in {"bool", "boolean"}:
            widget = QtWidgets.QCheckBox(self.dialog)
            widget.setChecked(str(selected).lower() in {"1", "true", "yes"})
            return widget, lambda: "true" if widget.isChecked() else "false"
        widget = QtWidgets.QLineEdit(str(selected), self.dialog)
        return widget, widget.text

    def _accept(self) -> None:
        name = self._name.text().strip()
        model_id = self._model_id
        if not name:
            self._show_error("Enter an output name.")
            return
        if self._model is None:
            self._show_error("The selected model is no longer installed.")
            return
        duplicate = next(
            (
                run for run in self._session.runs.list_runs()
                if run.id != self.run_id and run.label.strip().casefold() == name.casefold()
            ),
            None,
        )
        if duplicate is not None:
            self._show_error("An embedding output with this name already exists.")
            return
        model = self._model
        bindings = {key: str(combo.currentData() or "") for key, combo in self._input_combos.items()}
        available = {
            summary.source.id: summary
            for summary in self._session.service.list_embedding_input_sources()
        }
        required = [value.id for value in model.input_requirements if value.required and value.source_kind == "media_source"]
        if required and any(not bindings.get(key) for key in required):
            self._show_error("Select media sources for all required inputs.")
            return
        if not required and not any(bindings.values()):
            self._show_error("Select an input source.")
            return
        invalid = [
            source_id
            for source_id in bindings.values()
            if source_id
            and (
                source_id not in available
                or (
                    available[source_id].asset_count <= 0
                    if available[source_id].source.source_kind == MediaSourceKind.METADATA_COLUMN
                    else available[source_id].linked_count <= 0
                )
            )
        ]
        if invalid:
            self._show_error("Selected inputs no longer contain linked items.")
            return
        parameters = {key: read() for key, read in self._parameter_readers.items()}
        parameters[("run", "create_thumbnails")] = "true" if self._thumbnails.isChecked() else "false"
        try:
            self._session.service.rename_embedding_run(self.run_id, name)
            self._session.service.update_embedding_run_configuration(
                self.run_id,
                model_id=model_id,
                input_bindings={key: value for key, value in bindings.items() if value},
                parameter_values=parameters,
            )
        except Exception as exc:  # noqa: BLE001
            self._show_error(str(exc))
            return
        self.dialog.accept()

    def _discard_unused_draft(self) -> None:
        if not self._created:
            return
        try:
            run = self._session.service.get_embedding_run_configuration(self.run_id).run
            if run.state == EmbeddingRunState.DRAFT:
                self._session.service.delete_embedding_run(self.run_id)
        except Exception:
            pass

    def _show_error(self, message: str) -> None:
        self._error.setText(message)

    @staticmethod
    def _clear_form(form: QtWidgets.QFormLayout) -> None:
        while form.count():
            item = form.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()


def _editable_model_parameters(model: EmbeddingModelDescriptor) -> list[EmbeddingModelParameter]:
    if model.backend.strip().casefold() != "onnx":
        return []
    visible = [
        parameter
        for parameter in (*model.preprocess_parameters, *model.postprocess_parameters)
        if parameter.visible
    ]
    referenced = {
        preprocess_id
        for requirement in model.input_requirements
        for preprocess_id in requirement.preprocess
        if preprocess_id
    }
    if referenced:
        filtered = [
            parameter
            for parameter in visible
            if parameter.scope != "preprocess" or parameter.id in referenced
        ]
        if filtered:
            return filtered
    return visible


def _preparation_summary(contract: dict[str, object]) -> str:
    if contract.get("components"):
        return (
            "Select one or more modalities. Only items with every selected input are included, "
            "matched by item ID. Each selected encoder contributes equally to one normalized "
            "embedding per item."
        )
    modality = str(contract.get("modality") or "media").title()
    preparation = contract.get("preparation")
    if not isinstance(preparation, dict):
        return modality
    details = ", ".join(f"{key.replace('_', ' ')}: {value}" for key, value in preparation.items() if key != "parameters")
    return f"{modality} · {details}"
