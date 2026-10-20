"""Descriptor-driven clustering configuration in a transient dialog."""
from __future__ import annotations
from PySide6 import QtCore, QtWidgets
from EmbeddingsArea.clustering.parameters import PREPROCESSING
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class _DecimalSpinBox(QtWidgets.QDoubleSpinBox):
    def textFromValue(self, value):
        return self.locale().toString(value, "f", self.decimals()).rstrip("0").rstrip(self.locale().decimalPoint())


class ClusteringDialogController:
    def __init__(self, jobs, record):
        self.widget = load_ui("EmbeddingsArea", "ui/new_clusters_dialog.ui")
        self.widget.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self.jobs, self.service, self.record = jobs, jobs.service, record
        self._connections = []
        self.editors = {}
        self._building = True
        self.name = self.find(QtWidgets.QLineEdit, "edit_name")
        self.name.setText(record["label"])
        self._auto_named = bool(record.get("auto_named", False))
        # Records created before method-specific preprocessing defaults are
        # treated as explicit so opening them never changes their geometry.
        self._preprocessing_custom = bool(record.get("preprocessing_custom", True))
        self._name_warning = ""
        self._bind(self.name.textEdited, self._mark_name_custom)
        self._bind(self.name.textChanged, self.validate_name)
        self._bind(self.name.editingFinished, self.changed)
        self.combo = self.find(QtWidgets.QComboBox, "combo_method")
        self.combo.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.combo.setMinimumContentsLength(16)
        for method in self.service.list_methods():
            self.combo.addItem(method.name, method.id)
        self.combo.setCurrentIndex(max(0, self.combo.findData(record["method_id"])))
        self._bind(self.combo.currentIndexChanged, self.method_changed)
        self._bind(self.find(QtWidgets.QPushButton, "button_defaults").clicked, self.defaults)
        self._bind(self.find(QtWidgets.QPushButton, "button_run").clicked, self.run)
        self._bind(self.find(QtWidgets.QPushButton, "button_close").clicked, self.widget.reject)
        self.build_fields()
        self._building = False
        self.show_error(record.get("error", ""))
        self._bind(jobs.changed, self.update_run)
        self.validate_name(self.name.text())
        self.update_run()

    def find(self, kind, name):
        return require_child(self.widget, kind, name)

    def _bind(self, signal, slot):
        signal.connect(slot)
        self._connections.append((signal, slot))

    def dispose(self):
        # Disconnect Python handlers before releasing the unparented dialog.
        for signal, slot in reversed(self._connections):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._connections.clear()

    def show_error(self, message):
        label = self.find(QtWidgets.QLabel, "label_error")
        label.setText(message)
        label.setVisible(bool(message))

    def _mark_name_custom(self, _text):
        self._auto_named = False

    def validate_name(self, text):
        previous = self._name_warning
        self._name_warning = self.service.cluster_name_error(
            text,
            exclude_result_id=self.record["id"],
        )
        if self._name_warning:
            self.show_error(self._name_warning)
        elif previous and self.find(QtWidgets.QLabel, "label_error").text() == previous:
            self.show_error("")
        self.update_run()

    def update_run(self):
        button = self.find(QtWidgets.QPushButton, "button_run")
        base_text = "Run" if self.record["state"] == "draft" else "Retry"
        button.setText("Add to queue" if self.jobs.busy else base_text)
        button.setEnabled(
            not self._name_warning
            and not self.jobs.initializing
            and not self.jobs.initialization_error
        )
        button.setToolTip(
            "This clustering job will start when the active job finishes."
            if self.jobs.busy else self.jobs.initialization_error or ""
        )

    def run(self):
        if not self.save():
            return
        try:
            self.jobs.start(self.record["id"])
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.widget.accept()

    def build_fields(self):
        previous_building = self._building
        self._building = True
        self.editors.clear()
        for name in ("table_basic", "table_advanced"):
            table = self.find(QtWidgets.QTableWidget, name)
            table.setRowCount(0)
            table.setColumnCount(2)
            table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            table.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            table.setSizeAdjustPolicy(
                QtWidgets.QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored
            )
            table.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )
            table.setMinimumHeight(0)
            table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
            table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
            table.setWordWrap(False)
        self.method = self.service.require_method(self.record["method_id"])
        self.find(QtWidgets.QLabel, "label_description").setText(self.method.description)
        for scope, schema, values in (
            ("method", self.method.parameters, self.record["method_settings"].get(self.method.id, {})),
            ("preprocessing", PREPROCESSING, self.record["preprocessing"]),
        ):
            for parameter in schema:
                value = values.get(parameter.id, parameter.default)
                editor, read = self.editor(parameter, value, scope)
                table = self.find(QtWidgets.QTableWidget, "table_advanced" if parameter.advanced else "table_basic")
                row = table.rowCount()
                table.insertRow(row)
                label = QtWidgets.QTableWidgetItem(parameter.label + ":")
                label.setToolTip(parameter.description)
                label.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
                table.setItem(row, 0, label)
                table.setCellWidget(row, 1, editor)
                table.setRowHeight(row, max(30, editor.sizeHint().height() + 4))
                self.editors[(scope, parameter.id)] = (editor, read, parameter)
        for name in ("table_basic", "table_advanced"):
            table = self.find(QtWidgets.QTableWidget, name)
            table.setFixedHeight(
                sum(table.rowHeight(row) for row in range(table.rowCount()))
                + 2 * table.frameWidth()
            )
        self.find(QtWidgets.QTableWidget, "table_advanced").show()
        self._building = previous_building
        self.update_constraints()

    def editor(self, parameter, value, scope):
        changed = lambda *_: self.changed(scope)
        if parameter.kind == "bool":
            editor = QtWidgets.QCheckBox(self.widget)
            editor.setChecked(bool(value))
            self._bind(editor.toggled, changed)
            read = editor.isChecked
        elif parameter.choices:
            editor = QtWidgets.QComboBox(self.widget)
            editor.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            editor.setMinimumContentsLength(12)
            for choice in parameter.choices:
                editor.addItem(str(choice), choice)
            editor.setCurrentIndex(max(0, editor.findData(value)))
            self._bind(editor.currentIndexChanged, changed)
            read = editor.currentData
        else:
            editor = QtWidgets.QSpinBox(self.widget) if parameter.kind == "int" else _DecimalSpinBox(self.widget)
            if parameter.kind == "float":
                editor.setDecimals(12)
                editor.setStepType(QtWidgets.QAbstractSpinBox.StepType.AdaptiveDecimalStepType)
            editor.setRange(parameter.minimum if parameter.minimum is not None else -2147483647,
                            parameter.maximum if parameter.maximum is not None else 1e12)
            editor.setValue(value if value is not None else parameter.minimum or 1)
            self._bind(editor.editingFinished, changed)
            read = editor.value
        editor.setToolTip(parameter.description)
        if not parameter.nullable:
            return editor, read
        container = QtWidgets.QWidget(self.widget)
        layout = QtWidgets.QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        auto = QtWidgets.QCheckBox(parameter.nullable, container)
        auto.setChecked(value is None)
        editor.setEnabled(value is not None)
        layout.addWidget(auto)
        layout.addWidget(editor)
        self._bind(auto.toggled, lambda checked: editor.setEnabled(not checked))
        self._bind(auto.toggled, changed)
        return container, lambda: None if auto.isChecked() else read()

    def values(self, scope):
        return {key: read() for (section, key), (_, read, _) in self.editors.items() if section == scope}

    def update_constraints(self):
        values = self.values("method")
        for (scope, _), (editor, _, parameter) in self.editors.items():
            editor.setEnabled(parameter.enabled(values if scope == "method" else self.values("preprocessing")))
        # Compatibility is described by the method, not by dialog-specific algorithm branches.
        for key, allowed in self.method.available_choices(values).items():
            editor = self.editors[("method", key)][0]
            for index in range(editor.count()):
                editor.model().item(index).setEnabled(editor.itemData(index) in allowed)
            if editor.currentData() not in allowed:
                editor.blockSignals(True)
                editor.setCurrentIndex(editor.findData(allowed[0]))
                editor.blockSignals(False)
            editor.setToolTip("Available choices depend on the linkage, metric, or hierarchy cut.")

    def changed(self, scope=None, *_):
        if self._building:
            return
        if scope == "preprocessing":
            self._preprocessing_custom = True
        self.update_constraints()

    def save(self):
        settings = dict(self.record["method_settings"])
        settings[self.record["method_id"]] = self.values("method")
        try:
            self.record = self.service.update_draft(self.record["id"], label=self.name.text(),
                method_id=self.record["method_id"], method_settings=settings,
                preprocessing=self.values("preprocessing"), auto_named=self._auto_named,
                preprocessing_custom=self._preprocessing_custom)
            self.find(QtWidgets.QLabel, "label_error").clear()
            self.find(QtWidgets.QLabel, "label_error").hide()
            return True
        except Exception as exc:
            self.find(QtWidgets.QLabel, "label_error").setText(str(exc))
            self.find(QtWidgets.QLabel, "label_error").show()
            return False

    def method_changed(self, *_):
        if self._building:
            return
        settings = dict(self.record["method_settings"])
        settings[self.record["method_id"]] = self.values("method")
        self.record["method_settings"] = settings
        self.record["preprocessing"] = self.values("preprocessing")
        self.record["method_id"] = self.combo.currentData()
        if not self._preprocessing_custom:
            self.record["preprocessing"] = self.service.default_preprocessing(
                self.record["method_id"]
            )
        if self._auto_named:
            self.name.setText(self.service.default_cluster_name(
                self.record["run_id"],
                self.record["method_id"],
                exclude_result_id=self.record["id"],
            ))
        self.build_fields()

    def defaults(self):
        self.record["method_settings"][self.record["method_id"]] = {}
        self._preprocessing_custom = False
        self.record["preprocessing"] = self.service.default_preprocessing(
            self.record["method_id"]
        )
        self.build_fields()
