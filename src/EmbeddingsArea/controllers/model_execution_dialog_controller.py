"""Descriptor-driven configuration dialog for an embedding run."""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from EmbeddingsArea.domain import (
    EmbeddingModelDescriptor,
    EmbeddingModelParameter,
    EmbeddingRunState,
)
from DiscoveryArea.thumbnails.repository import ThumbnailVariantRepository
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class ModelExecutionDialogController:
    def __init__(self, session, project, *, run_id: str | None = None, model_id: str = "", discard_on_cancel: bool = False, activity_manager=None) -> None:
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
        self._execution = require_child(self.dialog, QtWidgets.QFormLayout, "form_execution")
        self._parameters = require_child(self.dialog, QtWidgets.QFormLayout, "form_parameters")
        self._batch_mode = require_child(self.dialog, QtWidgets.QComboBox, "combo_batch_mode")
        self._advanced_button = require_child(self.dialog, QtWidgets.QToolButton, "button_advanced_settings")
        self._advanced_panel = require_child(self.dialog, QtWidgets.QWidget, "panel_advanced_settings")
        self._advanced_button.toggled.connect(self._toggle_advanced_settings)
        self._thumbnails = require_child(self.dialog, QtWidgets.QCheckBox, "check_create_thumbnails")
        self._error = require_child(self.dialog, QtWidgets.QLabel, "label_error")
        self._run = require_child(self.dialog, QtWidgets.QPushButton, "button_run")
        self._input_combos: dict[str, QtWidgets.QComboBox] = {}
        self._required_input_ids: set[str] = set()
        self._parameter_readers: dict[tuple[str, str], callable] = {}
        self._parameter_widgets = {}
        self._finished = False
        self.dialog.finished.connect(self._finish_dialog)
        self._load(configuration)
        from EmbeddingsArea.controllers.cuda_setup_controller import CudaSetupController

        self._cuda_setup = CudaSetupController(
            require_child(self.dialog, QtWidgets.QPushButton, "button_enable_cuda"),
            activity_manager=activity_manager,
            declared=bool(self._model and "CUDAExecutionProvider" in self._model.runtime_contract.get("execution_providers", [])),
        )
        self._cuda_setup.ready.connect(self._cuda_ready)
        require_child(self.dialog, QtWidgets.QPushButton, "button_reset_preparation").clicked.connect(self._reset_preparation)
        self._name.textChanged.connect(self._update_availability)
        self._run.clicked.connect(self._accept)
        require_child(self.dialog, QtWidgets.QPushButton, "button_cancel").clicked.connect(self.dialog.reject)
        self._update_availability()
        first_input = next(iter(self._input_combos.values()), self._name)
        first_input.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
        self._fit_dialog_height()

    def exec(self) -> str | None:
        try:
            accepted = self.dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted
        finally:
            self._cuda_setup.close()
        if accepted:
            self._created = False
            return self.run_id
        self._discard_unused_draft()
        return None

    def _cuda_ready(self) -> None:
        device = self._device_combo
        if device.findData("cuda") < 0:
            device.addItem("CUDA (NVIDIA GPU)", "cuda")
        device.setCurrentIndex(device.findData("cuda"))

    def _toggle_advanced_settings(self, expanded: bool) -> None:
        self._advanced_panel.setVisible(expanded)
        self._advanced_button.setArrowType(
            QtCore.Qt.ArrowType.DownArrow if expanded else QtCore.Qt.ArrowType.RightArrow
        )
        self._fit_dialog_height()

    def _fit_dialog_height(self) -> None:
        self.dialog.ensurePolished()
        form = require_child(self.dialog, QtWidgets.QWidget, "panel_run_form")
        footer = require_child(self.dialog, QtWidgets.QWidget, "panel_footer")
        # Refresh dynamic rows before measuring their containing panels.
        for layout in reversed(form.findChildren(QtWidgets.QLayout)):
            layout.invalidate()
            layout.activate()
        footer.layout().activate()
        form_height = form.layout().totalHeightForWidth(self.dialog.width())
        footer_height = footer.layout().totalHeightForWidth(self.dialog.width())
        height = (form_height if form_height >= 0 else form.sizeHint().height()) + (
            footer_height if footer_height >= 0 else footer.sizeHint().height()
        )
        screen = self.dialog.screen()
        if screen is not None:
            height = min(height, max(300, screen.availableGeometry().height() - 80))
        self.dialog.resize(self.dialog.width(), height)

    def _load(self, configuration) -> None:
        self._name.setText(configuration.run.label)
        self._model_label.setText(self._model.label if self._model is not None else self._model_id or "Unavailable model")
        self._model_label.setToolTip(self._model_id)
        details = []
        if self._model is not None:
            modalities = " + ".join(value.value for value in self._model.compatible_content_types)
            details.append(f"{modalities.capitalize()} embeddings" if modalities else "Embeddings")
            if self._model.output_dimensions:
                details.append(f"{self._model.output_dimensions:,} dimensions")
        subtitle = require_child(self.dialog, QtWidgets.QLabel, "label_model_details")
        subtitle.setText(" · ".join(details))
        subtitle.setVisible(bool(details))
        self._rebuild(configuration)
        variants = ThumbnailVariantRepository(self._project.paths.root).list_variants()
        saved = {
            (value.scope, value.parameter_id): value.value
            for value in configuration.parameter_values
        }
        default = not variants
        self._thumbnails.setChecked(
            saved.get(("run", "create_thumbnails"), str(default)).lower() in {"1", "true", "yes"}
        )
        supports_thumbnails = bool(
            self._model
            and any(value.value in {"image", "video"} for value in self._model.compatible_content_types)
        )
        show_thumbnails = not bool(variants) and supports_thumbnails
        self._thumbnails.setVisible(show_thumbnails)
        require_child(self.dialog, QtWidgets.QWidget, "panel_thumbnails").setVisible(show_thumbnails)

    def _rebuild(self, configuration) -> None:
        self._clear_form(self._inputs)
        self._clear_form(self._execution)
        self._clear_form(self._parameters)
        self._input_combos = {}
        self._required_input_ids = set()
        self._parameter_readers = {}
        self._parameter_widgets = {}
        self._device_combo = None
        model = self._model
        if model is None:
            return
        saved_bindings = {value.input_id: value.source_id for value in configuration.input_bindings}
        sources = self._session.service.list_embedding_input_sources()
        self._source_summaries = {summary.source.id: summary for summary in sources}
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
            combo.setObjectName(f"combo_input_{requirement.id}")
            combo.setSizeAdjustPolicy(
                QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            combo.setMinimumContentsLength(24)
            combo.setToolTip("Process available inputs from a registered file-based Media Source.")
            combo.addItem("Select a Media Source" if requirement.required else "Not used", "")
            placeholder = combo.model().item(0)
            if requirement.required and isinstance(placeholder, QtGui.QStandardItem):
                placeholder.setForeground(QtGui.QBrush(QtGui.QColor("#e53935")))
            usable_source_ids: list[str] = []
            for summary in sources:
                if summary.source.content_type == requirement.modality:
                    item_count = max(0, summary.asset_count - summary.missing_count - summary.error_count)
                    usable = item_count > 0
                    if usable:
                        usable_source_ids.append(summary.source.id)
                    suffix = (
                        f"{item_count} {'input' if item_count == 1 else 'inputs'}"
                        if usable
                        else "no available inputs"
                    )
                    combo.addItem(summary.source.label, summary.source.id)
                    item = combo.model().item(combo.count() - 1)
                    if isinstance(item, QtGui.QStandardItem):
                        item.setEnabled(usable)
                        item.setToolTip(f"{suffix}\n{summary.source.path}")
                        if not usable:
                            item.setForeground(QtGui.QBrush(QtGui.QColor("#e53935")))
            selected = saved_bindings.get(requirement.id, "")
            if not selected and len(configuration.run.selected_source_ids) == 1:
                selected = configuration.run.selected_source_ids[0]
            if selected not in usable_source_ids:
                selected = ""
            if not selected and not configuration.run.selected_source_ids and len(usable_source_ids) == 1:
                selected = usable_source_ids[0]
            index = combo.findData(selected)
            combo.setCurrentIndex(index if index >= 0 else 0)
            label = "Media Source" if len(requirements) == 1 else requirement.label
            combo.setAccessibleName(label)
            self._inputs.addRow(label, combo)
            self._input_combos[requirement.id] = combo
            combo.currentIndexChanged.connect(self._update_availability)
        saved = {(value.scope, value.parameter_id): value.value for value in configuration.parameter_values}
        if model.runtime_adapter_id.startswith("onnx") or model.backend.casefold() == "onnx":
            device = QtWidgets.QComboBox(self.dialog)
            self._device_combo = device
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
            self._execution.addRow("Execution device", device)
            self._parameter_readers[("run", "device")] = lambda widget=device: str(widget.currentData())
        batch_value = saved.get(("run", "batch_size"), "auto").strip().casefold()
        options = ('auto', 'high', 'low', '4', '8', '16', '32', '64')
        for index, value in enumerate(options):
            self._batch_mode.setItemData(index, value)
        # Preserve saved sizes outside today's choices (for example batch one).
        if batch_value not in options:
            self._batch_mode.addItem(batch_value, batch_value)
        self._batch_mode.setCurrentIndex(self._batch_mode.findData(batch_value))
        self._parameter_readers[("run", "batch_size")] = lambda: str(self._batch_mode.currentData())
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
        self._update_source_summary()
        self._set_tab_order()

    def _update_source_summary(self) -> None:
        lines = []
        for combo in self._input_combos.values():
            summary = self._source_summaries.get(combo.currentData())
            if summary is not None:
                count = max(0, summary.asset_count - summary.missing_count - summary.error_count)
                lines.append(f"{count:,} {'input' if count == 1 else 'inputs'} · {summary.source.path}")
        label = require_child(self.dialog, QtWidgets.QLabel, "label_source_summary")
        text = "\n".join(lines) if lines else "Select a registered Media Source."
        changed = label.text() != text
        label.setText(text)
        label.setToolTip("\n".join(lines))
        if changed and self.dialog.isVisible():
            self._fit_dialog_height()

    def _set_tab_order(self) -> None:
        controls = [*self._input_combos.values(), require_child(self.dialog, QtWidgets.QLineEdit, "edit_output_name")]
        if self._device_combo is not None:
            controls.append(self._device_combo)
        controls.extend(require_child(self.dialog, QtWidgets.QWidget, name) for name in (
            "button_enable_cuda", "check_create_thumbnails", "button_advanced_settings",
            "combo_batch_mode",
        ))
        for row in range(self._parameters.rowCount()):
            field = self._parameters.itemAt(row, QtWidgets.QFormLayout.ItemRole.FieldRole)
            widget = field.widget() if field else None
            if widget is not None and widget.focusPolicy() & QtCore.Qt.FocusPolicy.TabFocus:
                controls.append(widget)
        controls.extend(require_child(self.dialog, QtWidgets.QWidget, name) for name in (
            "button_reset_preparation", "button_cancel", "button_run",
        ))
        for previous, following in zip(controls, controls[1:]):
            QtWidgets.QWidget.setTabOrder(previous, following)

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
        if self._input_combos:
            self._update_source_summary()
        reason = ""
        if not self._name.text().strip():
            reason = "Enter an output name."
        elif self._model is None:
            reason = "The selected model is no longer installed."
        elif not self._input_combos:
            reason = "Select a compatible registered Media Source."
        elif self._required_input_ids and any(
            not self._input_combos[input_id].currentData()
            for input_id in self._required_input_ids
        ):
            reason = "Select sources for all required model inputs."
        elif not self._required_input_ids and not any(
            combo.currentData() for combo in self._input_combos.values()
        ):
            reason = "Select a compatible registered Media Source."
        self._run.setEnabled(not reason and not self._finished)
        self._run.setToolTip(reason)

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
        if self._finished:
            return
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
                or available[source_id].asset_count <= available[source_id].missing_count + available[source_id].error_count
            )
        ]
        if invalid:
            self._show_error("Selected sources no longer contain available inputs.")
            return
        parameters = {key: read() for key, read in self._parameter_readers.items()}
        parameters[("run", "input_scope")] = "assets"
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
        self._show_error("")
        self.dialog.accept()

    def _finish_dialog(self, result):
        self._finished = True
        if result == QtWidgets.QDialog.DialogCode.Accepted:
            self._created = False

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
        self._error.setVisible(bool(message))
        self._fit_dialog_height()

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
