from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
import numpy as np

from DiscoveryArea.domain import DiscoveryOption
from DiscoveryArea.discovery_importer import DiscoveryImportPreview, import_discovery_coordinates, preview_discovery_import
from DiscoveryArea.discovery_methods.base import DiscoveryMethodDefinition
from DiscoveryArea.controllers.methods_citations_dialog_controller import MethodsCitationsDialogController
from DiscoveryArea.provenance import MethodsReport, PROVENANCE_SCHEMA_VERSION
from shared.qt.item_views import PANEL_ITEM_ROW_HEIGHT, configure_panel_item_view
from shared.qt.panel_controller import UiPanelController
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child

RUN_ID_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1
SAVED_TOOLTIP_ROLE = QtCore.Qt.ItemDataRole.UserRole + 2
SAVED_LABEL_ROLE = QtCore.Qt.ItemDataRole.UserRole + 3


@dataclass
class _ProjectionRunRow:
    name: str
    payload: dict[str, object]
    state: str = "queued"
    value: int = 0
    message: str = "Waiting for the previous projection to finish."
    option_key: str | None = None
    error: str = ""


class DiscoveryPanelController(UiPanelController):
    package = "DiscoveryArea"
    ui_path = "ui/projections_panel.ui"

    def _wire(self) -> None:
        self._list = require_child(self._widget, QtWidgets.QListWidget, "list_projections")
        configure_panel_item_view(self._list)
        self._options_button = require_child(self._widget, QtWidgets.QToolButton, "button_discovery_options")
        self._add_projection_button = require_child(
            self._widget, QtWidgets.QAbstractButton, "button_add_new_projection"
        )
        self._dialog: QtWidgets.QWidget | None = None
        self._selection_changed_callback: Callable[[str], None] | None = None
        self._create_discovery_callback: Callable[[dict[str, object]], bool | None] | None = None
        self._abort_discovery_callback: Callable[[str], bool | None] | None = None
        self._import_discovery_callback: Callable[[dict[str, object]], bool] | None = None
        self._export_discovery_callback: Callable[[dict[str, object]], bool] | None = None
        self._methods_report_callback: Callable[[str], MethodsReport | None] | None = None
        self._rename_discovery_callback: Callable[[str, str], bool] | None = None
        self._delete_discovery_callback: Callable[[str], None] | None = None
        self._discovery_sources_provider: Callable[[], list[tuple[str, str]]] | None = None
        self._filter_presets_provider: Callable[[], list[tuple[str, str]]] | None = None
        self._discovery_metadata_ids_provider: Callable[[], np.ndarray] | None = None
        self._discovery_metadata_columns_provider: Callable[[], list[str]] | None = None
        self._discovery_metadata_values_provider: Callable[[str], dict[int, str] | None] | None = None
        self._method_definitions: list[DiscoveryMethodDefinition] = []
        self._existing_discovery_names: set[str] = set()
        self._run_rows: dict[str, _ProjectionRunRow] = {}
        self._options_by_key: dict[str, DiscoveryOption] = {}
        self._projection_item_height = PANEL_ITEM_ROW_HEIGHT

        self._configure_list()
        self._configure_options_menu()
        self.set_discovery_options([], enabled=False)
        self._add_projection_button.clicked.connect(self._open_new_discovery_stub)
        self._list.currentRowChanged.connect(self._on_discovery_selected)

    def set_project(self, project) -> None:
        super().set_project(project)
        has_project = project is not None
        self._list.setEnabled(has_project)
        self._add_projection_button.setEnabled(has_project)
        if not has_project:
            self.clear_runs()
            self.set_discovery_options([], enabled=False)

    def shutdown(self) -> None:
        if self._dialog is not None:
            self._dialog.close()
            self._dialog = None
        self.clear_runs()

    def _configure_list(self) -> None:
        self._list.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_projection_context_menu)

    def _configure_options_menu(self) -> None:
        menu = QtWidgets.QMenu(self._options_button)
        menu.setToolTipsVisible(True)
        self._action_create = menu.addAction("New Projection…")
        self._action_create.triggered.connect(self._open_new_discovery_stub)
        self._action_import = menu.addAction("Import Data…")
        self._action_import.triggered.connect(self._open_import_discovery_dialog)
        menu.addSeparator()
        self._action_export = menu.addAction("Export Data…")
        self._action_export.triggered.connect(self._export_current_discovery)
        self._action_methods = menu.addAction("Citations…")
        self._action_methods.triggered.connect(self._show_methods_and_citations)
        self._action_rename = menu.addAction("Rename…")
        self._action_rename.triggered.connect(self._rename_current_discovery)
        menu.addSeparator()
        self._action_delete = menu.addAction("Delete…")
        self._action_delete.triggered.connect(self._delete_current_discovery)
        menu.aboutToShow.connect(self._refresh_menu_state)
        self._options_button.setMenu(menu)
        self._options_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self._options_button.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
        self._discovery_options_menu = menu

    def set_discovery_options(
        self,
        options: list[DiscoveryOption],
        *,
        enabled: bool,
        selected_key: str | None = None,
    ) -> None:
        self._options_by_key = {option.key: option for option in options}
        with QtCore.QSignalBlocker(self._list):
            self._list.clear()
            saved_items: dict[str, QtWidgets.QListWidgetItem] = {}
            random_option = next((option for option in options if option.is_random), None)
            projection_options = [option for option in options if not option.is_random]
            random_item = self._add_projection_list_item(
                random_option.label if random_option is not None else "Random",
                random_option.key if random_option is not None else "random",
                option=random_option,
            )
            saved_items[str(random_item.data(QtCore.Qt.ItemDataRole.UserRole))] = random_item
            for option in projection_options:
                saved_items[option.key] = self._add_projection_list_item(
                    option.label,
                    option.key,
                    option=option,
                )
            run_items: dict[str, QtWidgets.QListWidgetItem] = {}
            for run_id, run in self._run_rows.items():
                item = saved_items.get(run.option_key or "")
                if item is None:
                    item = QtWidgets.QListWidgetItem(run.name)
                    item.setData(QtCore.Qt.ItemDataRole.UserRole, None)
                    item.setData(SAVED_LABEL_ROLE, run.name)
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable)
                    self._list.addItem(item)
                item.setData(RUN_ID_ROLE, run_id)
                run_items[run_id] = item
            for target_row, (run_id, run) in enumerate(
                self._ordered_runs(),
                start=self._transient_start_row(),
            ):
                item = run_items[run_id]
                current_row = self._list.row(item)
                if current_row != target_row:
                    item = self._list.takeItem(current_row)
                    self._list.insertItem(target_row, item)
                self._render_run_row(item, run_id, run)
            index = 0
            if selected_key:
                candidate = self._find_projection_row(selected_key)
                if candidate >= 0:
                    index = candidate
            self._list.setCurrentRow(index)
        self.set_existing_discovery_names([option.label for option in options if not option.is_random])
        self._list.setEnabled(enabled)

    def _add_projection_list_item(
        self,
        label: str,
        key: str,
        *,
        option: DiscoveryOption | None,
    ) -> QtWidgets.QListWidgetItem:
        item = QtWidgets.QListWidgetItem(label)
        item.setData(QtCore.Qt.ItemDataRole.UserRole, key)
        item.setData(SAVED_LABEL_ROLE, label)
        tooltip = self._projection_tooltip(option, fallback_label=label)
        item.setData(SAVED_TOOLTIP_ROLE, tooltip)
        item.setToolTip(tooltip)
        item.setSizeHint(QtCore.QSize(0, self._projection_item_height))
        self._list.addItem(item)
        return item

    def _render_run_row(
        self,
        item: QtWidgets.QListWidgetItem,
        run_id: str,
        run: _ProjectionRunRow,
    ) -> None:
        row = QtWidgets.QWidget(self._list.viewport())
        row.setFixedHeight(self._projection_item_height)
        if item.data(SAVED_LABEL_ROLE) is None:
            item.setData(SAVED_LABEL_ROLE, item.text() or run.name)
        item.setText("")
        item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable)
        row.setObjectName("row_projection_run")
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(4, 1, 4, 1)
        layout.setSpacing(8)
        label = QtWidgets.QLabel(run.name, row)
        label.setObjectName("label_projection_run_name")
        label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        label_font = label.font()
        label_font.setItalic(True)
        label.setFont(label_font)
        pending_color = QtGui.QColor("gray")
        label.setStyleSheet("color: gray; font-style: italic;")
        label_palette = label.palette()
        label_palette.setColor(QtGui.QPalette.ColorRole.WindowText, pending_color)
        label.setPalette(label_palette)
        layout.addWidget(label)
        if run.state == "queued":
            status = QtWidgets.QLabel("queued...", row)
            status.setObjectName("label_projection_run_status")
            status.setAlignment(
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
            status.setFixedWidth(120)
            status.setStyleSheet("color: gray; font-style: italic;")
            status_palette = status.palette()
            status_palette.setColor(QtGui.QPalette.ColorRole.WindowText, pending_color)
            status.setPalette(status_palette)
            layout.addWidget(status)
        else:
            progress = QtWidgets.QProgressBar(row)
            progress.setObjectName("progress_projection_run")
            progress.setRange(0, 100)
            progress.setValue(max(0, min(100, int(run.value))))
            progress.setFixedWidth(120)
            if run.state == "cancelling":
                progress.setFormat("Cancelling…")
            elif run.state == "failed":
                progress.setFormat("Failed")
            else:
                progress.setFormat("%p%")
            layout.addWidget(progress)
        tooltip = self._run_tooltip(run)
        item.setToolTip(tooltip)
        for widget in row.findChildren(QtWidgets.QWidget) + [row]:
            widget.setToolTip(tooltip)
            widget.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
            widget.customContextMenuRequested.connect(
                lambda position, widget=widget, run_id=run_id: self._show_run_context_menu(
                    run_id,
                    widget.mapToGlobal(position),
                )
            )
        item.setSizeHint(QtCore.QSize(0, self._projection_item_height))
        self._list.setItemWidget(item, row)

    def _run_tooltip(self, run: _ProjectionRunRow) -> str:
        payload = run.payload
        lines = [
            f"Name: {run.name}",
            f"Method: {payload.get('method_label') or payload.get('method') or 'unknown'}",
        ]
        if payload.get("source_label"):
            lines.append(f"Embeddings: {payload['source_label']}")
        lines.append(f"Filter: {payload.get('filter_preset_label') or 'None'}")
        lines.append("Parameters:")
        parameters = payload.get("parameters")
        if isinstance(parameters, dict) and parameters:
            lines.extend(f"  {key}: {self._tooltip_value(value)}" for key, value in parameters.items())
        else:
            lines.append("  none")
        lines.append(f"Status: {run.message}")
        if run.error:
            lines.append(f"Error: {run.error}")
        return "\n".join(lines)

    @staticmethod
    def _projection_tooltip(option: DiscoveryOption | None, *, fallback_label: str) -> str:
        if option is None:
            return f"Name: {fallback_label}\nType: random\nFilter: None\nParameters: none"
        lines = [f"Name: {option.label}", f"Type: {option.discovery_type}"]
        if option.embeddings_label:
            lines.append(f"Embeddings: {option.embeddings_label}")
        filter_label = option.source.get("filter_preset") or option.source.get("filter_preset_label")
        lines.append(f"Filter: {filter_label or 'None'}")
        lines.append("Parameters:")
        if option.parameters:
            lines.extend(
                f"  {name}: {DiscoveryPanelController._tooltip_value(value)}"
                for name, value in option.parameters.items()
            )
        else:
            lines.append("  none")
        return "\n".join(lines)

    @staticmethod
    def _tooltip_value(value: object) -> str:
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
        return str(value)

    def _find_projection_row(self, key: str) -> int:
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item is not None and item.data(QtCore.Qt.ItemDataRole.UserRole) == key:
                return row
        return -1

    def _on_discovery_selected(self, _row: int) -> None:
        selection = self.current_selection_key()
        if selection is not None and self._selection_changed_callback is not None:
            self._selection_changed_callback(selection)

    def _on_projection_context_menu(self, position: QtCore.QPoint) -> None:
        item = self._list.itemAt(position)
        if item is None:
            return
        run_id = str(item.data(RUN_ID_ROLE) or "")
        if run_id:
            self._show_run_context_menu(run_id, self._list.viewport().mapToGlobal(position))
            return
        self._list.setCurrentItem(item)
        menu = self._projection_context_menu(item)
        if menu is not None:
            menu.exec(self._list.viewport().mapToGlobal(position))

    def _projection_context_menu(self, item: QtWidgets.QListWidgetItem) -> QtWidgets.QMenu | None:
        key = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if key in (None, "random"):
            return None
        menu = QtWidgets.QMenu(self._list)
        menu.setToolTipsVisible(True)
        action_methods = menu.addAction("Citations…")
        option = self._options_by_key.get(str(key))
        has_provenance = self._has_report_provenance(option)
        action_methods.setEnabled(has_provenance)
        if not has_provenance:
            action_methods.setToolTip("Rerun this projection to create methods and citation provenance.")
        action_methods.triggered.connect(self._show_methods_and_citations)
        menu.addSeparator()
        action_rename = menu.addAction("Rename…")
        menu.addSeparator()
        action_delete = menu.addAction("Delete…")
        action_rename.triggered.connect(self._rename_current_discovery)
        action_delete.triggered.connect(self._delete_current_discovery)
        return menu

    def _show_run_context_menu(self, run_id: str, global_position: QtCore.QPoint) -> None:
        run = self._run_rows.get(run_id)
        if run is None:
            return
        menu = QtWidgets.QMenu(self._list)
        if run.state == "failed":
            action = menu.addAction("Remove")
            action.triggered.connect(lambda _checked=False, run_id=run_id: self.remove_run(run_id))
        elif run.state == "cancelling":
            action = menu.addAction("Cancelling…")
            action.setEnabled(False)
        else:
            action = menu.addAction("Cancel Projection")
            action.triggered.connect(
                lambda _checked=False, run_id=run_id: self._request_run_abort(run_id)
            )
        menu.exec(global_position)

    def _open_new_discovery_stub(self) -> None:
        if self._dialog is not None:
            self._reset_discovery_dialog(self._dialog)
            self._dialog.show()
            self._dialog.raise_()
            self._dialog.activateWindow()
            return
        dialog = load_ui(self.package, "ui/new_projection_dialog.ui", None)
        if not isinstance(dialog, QtWidgets.QDialog):
            self._show_status("New projection dialog is unavailable.")
            return
        self._dialog = dialog
        dialog.setWindowTitle("New Projection")
        dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        combo_method = dialog.findChild(QtWidgets.QComboBox, "combo_discovery_method")
        combo_vectors = dialog.findChild(QtWidgets.QComboBox, "combo_embeddings_source")
        combo_filter = dialog.findChild(QtWidgets.QComboBox, "combo_filter_preset")
        edit_name = dialog.findChild(QtWidgets.QLineEdit, "edit_run_name")
        cancel_button = dialog.findChild(QtWidgets.QPushButton, "button_cancel")
        run_button = dialog.findChild(QtWidgets.QPushButton, "button_run")
        table_parameters = dialog.findChild(QtWidgets.QTableWidget, "table_discovery_parameters")
        if combo_method is not None:
            combo_method.currentIndexChanged.connect(
                lambda _index: self._handle_discovery_method_changed(combo_method, table_parameters, edit_name)
            )
        if combo_vectors is not None:
            combo_vectors.currentIndexChanged.connect(
                lambda _index: self._handle_discovery_source_changed(combo_method, combo_vectors, edit_name)
            )
        if combo_filter is not None:
            combo_filter.currentIndexChanged.connect(
                lambda _index: self._handle_discovery_filter_changed(
                    combo_method,
                    combo_vectors,
                    combo_filter,
                    edit_name,
                )
            )
        if cancel_button is not None:
            cancel_button.clicked.connect(dialog.reject)
        if run_button is not None:
            run_button.clicked.connect(
                lambda: self._submit_new_projection(
                    combo_method=combo_method,
                    combo_filter=combo_filter,
                    edit_name=edit_name,
                    table_parameters=table_parameters,
                )
            )
        self._reset_discovery_dialog(dialog)
        dialog.show()
        self._show_status("New projection dialog opened.")

    def _reset_discovery_dialog(self, dialog: QtWidgets.QWidget) -> None:
        combo_method = dialog.findChild(QtWidgets.QComboBox, "combo_discovery_method")
        combo_vectors = dialog.findChild(QtWidgets.QComboBox, "combo_embeddings_source")
        combo_filter = dialog.findChild(QtWidgets.QComboBox, "combo_filter_preset")
        edit_name = dialog.findChild(QtWidgets.QLineEdit, "edit_run_name")
        table_parameters = dialog.findChild(QtWidgets.QTableWidget, "table_discovery_parameters")
        self._configure_parameter_table(table_parameters)

        if combo_method is not None:
            with QtCore.QSignalBlocker(combo_method):
                combo_method.clear()
                for method in self._method_definitions:
                    combo_method.addItem(method.name, method.method_id)
                combo_method.setCurrentIndex(0 if combo_method.count() else -1)
            self._populate_parameter_table(combo_method, table_parameters)
        if combo_vectors is not None:
            with QtCore.QSignalBlocker(combo_vectors):
                combo_vectors.clear()
                if self._discovery_sources_provider is not None:
                    for label, key in self._discovery_sources_provider():
                        combo_vectors.addItem(label, key)
                if combo_vectors.count() == 0:
                    combo_vectors.addItem("No completed embeddings", "")
        if combo_filter is not None:
            with QtCore.QSignalBlocker(combo_filter):
                combo_filter.clear()
                combo_filter.addItem("None", "")
                if self._filter_presets_provider is not None:
                    for label, key in self._filter_presets_provider():
                        combo_filter.addItem(label, key)
        if edit_name is not None:
            edit_name.setText(self._default_discovery_name(combo_method, combo_vectors, combo_filter))

    @staticmethod
    def _configure_parameter_table(table: QtWidgets.QTableWidget | None) -> None:
        if table is None:
            return
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(["Parameter", "Value"])
        table.horizontalHeader().setStretchLastSection(True)
        table.verticalHeader().setVisible(False)

    def _submit_new_projection(
        self,
        *,
        combo_method: QtWidgets.QComboBox | None,
        combo_filter: QtWidgets.QComboBox | None = None,
        edit_name: QtWidgets.QLineEdit | None,
        table_parameters: QtWidgets.QTableWidget | None,
    ) -> None:
        method = str(combo_method.currentData()) if combo_method is not None else "random"
        name = edit_name.text().strip() if edit_name is not None else "Projection"
        payload: dict[str, object] = {
            "method": method or "random",
            "name": name or "Projection",
            "parameters": self._parameter_values_from_table(table_parameters),
        }
        if self._discovery_name_exists(str(payload["name"])):
            if edit_name is not None:
                edit_name.setFocus()
                edit_name.selectAll()
            self._show_status("Projection name already exists.")
            return
        if combo_method is not None:
            payload["method_label"] = str(combo_method.currentText() or "")
        source_combo = self._dialog.findChild(QtWidgets.QComboBox, "combo_embeddings_source") if self._dialog is not None else None
        if source_combo is not None:
            payload["source_key"] = str(source_combo.currentData() or "")
            payload["source_label"] = str(source_combo.currentText() or "")
        if combo_filter is not None and combo_filter.currentData():
            payload["filter_preset_key"] = str(combo_filter.currentData())
            payload["filter_preset_label"] = str(combo_filter.currentText() or "")
        if method == "custom_coordinates":
            path, _selected_filter = QtWidgets.QFileDialog.getOpenFileName(
                self._dialog,
                "Select Coordinates",
                "",
                "Coordinate files (*.csv *.tsv *.xlsx)",
            )
            if not path:
                self._show_status("Coordinate import cancelled.")
                return
            payload["coordinate_file"] = path
        if self._create_discovery_callback is None:
            self._show_status("Projection creation is unavailable.")
            return
        accepted = self._create_discovery_callback(payload)
        if accepted is False:
            return
        if isinstance(self._dialog, QtWidgets.QDialog):
            self._dialog.accept()
        self._show_status(f"Queued projection: {payload['name']}")

    def _refresh_menu_state(self) -> None:
        has_project = self._project is not None
        selection_key = self.current_selection_key()
        saved_discovery_selected = selection_key not in (None, "random")
        self._action_create.setEnabled(has_project)
        self._action_import.setEnabled(has_project)
        self._action_export.setEnabled(has_project and selection_key is not None)
        option = self._options_by_key.get(str(selection_key or ""))
        has_report = self._has_report_provenance(option)
        self._action_methods.setEnabled(has_project and has_report)
        self._action_methods.setToolTip(
            "" if has_report else "Rerun this projection to create methods and citation provenance."
        )
        self._action_rename.setEnabled(saved_discovery_selected)
        self._action_delete.setEnabled(saved_discovery_selected)

    def set_discovery_selection_callback(self, callback: Callable[[str], None] | None) -> None:
        self._selection_changed_callback = callback

    def set_create_discovery_callback(
        self,
        callback: Callable[[dict[str, object]], bool | None] | None,
    ) -> None:
        self._create_discovery_callback = callback

    def set_abort_discovery_callback(self, callback: Callable[[str], bool | None] | None) -> None:
        self._abort_discovery_callback = callback

    def set_import_discovery_callback(self, callback: Callable[[dict[str, object]], bool] | None) -> None:
        self._import_discovery_callback = callback

    def set_export_discovery_callback(self, callback: Callable[[dict[str, object]], bool] | None) -> None:
        self._export_discovery_callback = callback

    def set_methods_report_callback(
        self, callback: Callable[[str], MethodsReport | None] | None
    ) -> None:
        self._methods_report_callback = callback

    def _show_methods_and_citations(self) -> None:
        key = self.current_selection_key()
        option = self._options_by_key.get(str(key or ""))
        if not self._has_report_provenance(option):
            self._show_status("Rerun this projection to create methods and citation provenance.")
            return
        if self._methods_report_callback is None or key is None:
            self._show_status("Methods and citation reporting is unavailable.")
            return
        try:
            report = self._methods_report_callback(key)
        except Exception as exc:  # noqa: BLE001
            self._show_status(f"Could not prepare methods and citations: {exc}")
            return
        if report is None:
            return
        saved = MethodsCitationsDialogController().exec(report)
        if saved is not None:
            self._show_status(f"Saved methods report and bibliography: {saved[0].name}, {saved[1].name}")

    @staticmethod
    def _has_report_provenance(option: DiscoveryOption | None) -> bool:
        return bool(
            option is not None
            and not option.is_random
            and int(option.provenance.get("schema_version") or 0) == PROVENANCE_SCHEMA_VERSION
        )

    def set_rename_discovery_callback(self, callback: Callable[[str, str], bool] | None) -> None:
        self._rename_discovery_callback = callback

    def set_delete_discovery_callback(self, callback: Callable[[str], None] | None) -> None:
        self._delete_discovery_callback = callback

    def set_discovery_sources_provider(self, provider: Callable[[], list[tuple[str, str]]] | None) -> None:
        self._discovery_sources_provider = provider

    def set_filter_presets_provider(self, provider: Callable[[], list[tuple[str, str]]] | None) -> None:
        self._filter_presets_provider = provider

    def set_discovery_metadata_ids_provider(self, provider: Callable[[], np.ndarray] | None) -> None:
        self._discovery_metadata_ids_provider = provider

    def set_discovery_metadata_columns_provider(self, provider: Callable[[], list[str]] | None) -> None:
        self._discovery_metadata_columns_provider = provider

    def set_discovery_metadata_values_provider(self, provider: Callable[[str], dict[int, str] | None] | None) -> None:
        self._discovery_metadata_values_provider = provider

    def set_discovery_methods(self, methods: list[DiscoveryMethodDefinition]) -> None:
        self._method_definitions = list(methods)

    def set_existing_discovery_names(self, names: list[str]) -> None:
        self._existing_discovery_names = {
            name.strip().casefold()
            for name in names
            if name.strip()
        }

    def add_queued_run(self, run_id: str, payload: dict[str, object]) -> None:
        name = str(payload.get("name") or "Projection").strip() or "Projection"
        for existing_id, run in list(self._run_rows.items()):
            if run.state == "failed" and run.name.casefold() == name.casefold():
                self.remove_run(existing_id)
        self._run_rows[run_id] = _ProjectionRunRow(name=name, payload=dict(payload))
        self._append_or_refresh_run(run_id)

    def set_run_started(self, run_id: str) -> None:
        run = self._run_rows.get(run_id)
        if run is None:
            return
        run.state = "running"
        run.message = "Preparing projection request..."
        self._append_or_refresh_run(run_id)

    def set_run_progress(self, run_id: str, value: int, message: str | None = None) -> None:
        run = self._run_rows.get(run_id)
        if run is None:
            return
        run.state = "running"
        run.value = max(run.value, min(99, max(0, int(value))))
        if message:
            run.message = str(message).strip()
        self._append_or_refresh_run(run_id)

    def bind_run_to_projection(self, run_id: str, option_key: str) -> None:
        run = self._run_rows.get(run_id)
        if run is not None:
            run.option_key = str(option_key)

    def set_run_finished(self, run_id: str) -> None:
        run = self._run_rows.pop(run_id, None)
        if run is None:
            return
        row = self._find_run_row(run_id)
        if row >= 0:
            item = self._list.item(row)
            self._list.removeItemWidget(item)
            item.setData(RUN_ID_ROLE, None)
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsSelectable)
            if run.option_key is None:
                self._list.takeItem(row)
            else:
                item.setText(str(item.data(SAVED_LABEL_ROLE) or run.name))
                item.setToolTip(str(item.data(SAVED_TOOLTIP_ROLE) or run.name))

    def set_run_failed(self, run_id: str, message: str) -> None:
        run = self._run_rows.get(run_id)
        if run is None:
            return
        run.state = "failed"
        run.error = str(message)
        run.message = "Projection failed."
        self._append_or_refresh_run(run_id)

    def set_run_cancelling(self, run_id: str) -> None:
        run = self._run_rows.get(run_id)
        if run is None:
            return
        run.state = "cancelling"
        run.message = "Cancellation requested; waiting for a safe checkpoint."
        self._append_or_refresh_run(run_id)

    def remove_run(self, run_id: str) -> None:
        run = self._run_rows.pop(run_id, None)
        row = self._find_run_row(run_id)
        if row >= 0:
            item = self._list.item(row)
            if run is not None and run.option_key is not None:
                self._list.removeItemWidget(item)
                item.setData(RUN_ID_ROLE, None)
                item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsSelectable)
                item.setText(str(item.data(SAVED_LABEL_ROLE) or run.name))
                item.setToolTip(str(item.data(SAVED_TOOLTIP_ROLE) or run.name))
            else:
                self._list.takeItem(row)

    def clear_runs(self) -> None:
        for run_id in list(self._run_rows):
            self.remove_run(run_id)

    def active_run_names(self) -> set[str]:
        return {
            run.name.casefold()
            for run in self._run_rows.values()
            if run.state in {"queued", "running", "cancelling"}
        }

    def _find_run_row(self, run_id: str) -> int:
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item is not None and item.data(RUN_ID_ROLE) == run_id:
                return row
        return -1

    def _append_or_refresh_run(self, run_id: str) -> None:
        run = self._run_rows.get(run_id)
        if run is None:
            return
        row = self._find_run_row(run_id)
        if row < 0:
            item = QtWidgets.QListWidgetItem(run.name)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, None)
            item.setData(RUN_ID_ROLE, run_id)
            item.setData(SAVED_LABEL_ROLE, run.name)
            item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable)
            self._list.insertItem(self._run_target_row(run_id), item)
        else:
            item = self._list.item(row)
            target_row = self._run_target_row(run_id)
            if row != target_row:
                self._list.removeItemWidget(item)
                item = self._list.takeItem(row)
                self._list.insertItem(target_row, item)
        self._render_run_row(item, run_id, run)

    def _ordered_runs(self) -> list[tuple[str, _ProjectionRunRow]]:
        entries = list(self._run_rows.items())
        processing = [entry for entry in entries if entry[1].state in {"running", "cancelling"}]
        waiting = [entry for entry in reversed(entries) if entry[1].state not in {"running", "cancelling"}]
        return [*processing, *waiting]

    def _run_target_row(self, run_id: str) -> int:
        for row, (candidate_id, _run) in enumerate(
            self._ordered_runs(),
            start=self._transient_start_row(),
        ):
            if candidate_id == run_id:
                return row
        return self._transient_start_row()

    def _transient_start_row(self) -> int:
        return 1 if self._find_projection_row("random") >= 0 else 0

    def close_discovery_dialog(self) -> None:
        if isinstance(self._dialog, QtWidgets.QDialog):
            self._dialog.reject()

    def _request_run_abort(self, run_id: str) -> None:
        if self._abort_discovery_callback is None:
            self._show_status("Projection abort is unavailable.")
            return
        accepted = self._abort_discovery_callback(run_id)
        if accepted is False:
            return
        self.set_run_cancelling(run_id)

    def current_selection_key(self) -> str | None:
        item = self._list.currentItem()
        data = item.data(QtCore.Qt.ItemDataRole.UserRole) if item is not None else None
        return str(data) if data is not None else None

    def set_selected_key(self, key: str, *, emit_signal: bool) -> bool:
        index = self._find_projection_row(key)
        if index < 0:
            return False
        if emit_signal:
            self._list.setCurrentRow(index)
        else:
            with QtCore.QSignalBlocker(self._list):
                self._list.setCurrentRow(index)
        return True

    def _rename_current_discovery(self) -> None:
        key = self.current_selection_key()
        if key in (None, "random"):
            return
        if self._rename_discovery_callback is None:
            self._show_status("Projection rename is unavailable.")
            return
        current_item = self._list.currentItem()
        current_name = current_item.text().strip() if current_item is not None else ""
        new_name, accepted = QtWidgets.QInputDialog.getText(
            self._widget,
            "Rename Projection",
            "Projection name:",
            QtWidgets.QLineEdit.EchoMode.Normal,
            current_name,
        )
        if not accepted:
            self._show_status("Projection rename cancelled.")
            return
        new_name = new_name.strip()
        if not new_name:
            self._show_status("Projection name is required.")
            return
        if new_name.casefold() != current_name.casefold() and self._discovery_name_exists(new_name):
            self._show_status("Projection name already exists.")
            return
        if self._rename_discovery_callback(key, new_name):
            self._show_status(f"Projection renamed to {new_name}.")
        else:
            self._show_status("Projection could not be renamed.")

    def _delete_current_discovery(self) -> None:
        key = self.current_selection_key()
        if key in (None, "random"):
            return
        if self._delete_discovery_callback is None:
            self._show_status("Projection delete is unavailable.")
            return
        reply = QtWidgets.QMessageBox.question(
            self._widget,
            "Delete Projection",
            "Delete the selected projection?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            self._show_status("Projection delete cancelled.")
            return
        self._delete_discovery_callback(key)

    def _export_current_discovery(self) -> None:
        selection_key = self.current_selection_key()
        if selection_key is None:
            self._show_status("Select a projection before exporting coordinates.")
            return
        if self._export_discovery_callback is None:
            self._show_status("Projection coordinate export is unavailable.")
            return
        formats = {
            "CSV files (*.csv)": "csv",
            "TSV files (*.tsv)": "tsv",
            "JSON files (*.json)": "json",
            "XML files (*.xml)": "xml",
        }
        current_item = self._list.currentItem()
        current_label = current_item.text() if current_item is not None else "projection"
        destination, selected_filter = QtWidgets.QFileDialog.getSaveFileName(
            self._widget,
            "Export Projection Coordinates",
            str(Path.home() / f"{self._safe_export_stem(current_label)}.csv"),
            ";;".join(formats),
        )
        if not destination:
            self._show_status("Projection coordinate export cancelled.")
            return
        export_format = formats.get(selected_filter)
        if export_format is None:
            suffix = Path(destination).suffix.lower().lstrip(".")
            export_format = suffix if suffix in formats.values() else "csv"
        target = Path(destination).with_suffix(f".{export_format}")
        self._export_discovery_callback(
            {
                "selection_key": selection_key,
                "destination": str(target),
                "export_format": export_format,
                "id_column": "metadata_id",
                "coordinate_source": "current",
            }
        )

    def _open_import_discovery_dialog(self) -> None:
        dialog = load_ui(self.package, "ui/import_discovery_dialog.ui", None)
        if not isinstance(dialog, QtWidgets.QDialog):
            self._show_status("Projection import dialog is unavailable.")
            return
        dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        edit_file = require_child(dialog, QtWidgets.QLineEdit, "edit_import_file")
        edit_name = require_child(dialog, QtWidgets.QLineEdit, "edit_import_discovery_name")
        button_browse = require_child(dialog, QtWidgets.QPushButton, "button_browse_import_file")
        combo_id = require_child(dialog, QtWidgets.QComboBox, "combo_import_id_column")
        combo_metadata = require_child(dialog, QtWidgets.QComboBox, "combo_import_metadata_column")
        combo_x = require_child(dialog, QtWidgets.QComboBox, "combo_import_x_column")
        combo_y = require_child(dialog, QtWidgets.QComboBox, "combo_import_y_column")
        table_preview = require_child(dialog, QtWidgets.QTableWidget, "table_import_preview")
        validation = require_child(dialog, QtWidgets.QTextBrowser, "text_import_validation")
        button_box = require_child(dialog, QtWidgets.QDialogButtonBox, "button_box_import_discovery")
        current_preview: dict[str, DiscoveryImportPreview | None] = {"preview": None}
        edit_name.setText(self._next_available_discovery_name("Imported projection"))
        table_preview.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        table_preview.setAlternatingRowColors(True)
        ok_button = button_box.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            ok_button.setEnabled(False)

        def load_preview(path: str) -> DiscoveryImportPreview | None:
            try:
                preview = preview_discovery_import(Path(path))
            except Exception as exc:
                validation.setPlainText(str(exc))
                if ok_button is not None:
                    ok_button.setEnabled(False)
                return None
            self._populate_import_dialog_preview(
                preview,
                combo_id=combo_id,
                combo_metadata=combo_metadata,
                combo_x=combo_x,
                combo_y=combo_y,
                table_preview=table_preview,
                validation=validation,
            )
            current_preview["preview"] = preview
            refresh_validation()
            if ok_button is not None:
                ok_button.setEnabled(True)
            return preview

        def browse() -> None:
            path, _selected_filter = QtWidgets.QFileDialog.getOpenFileName(
                dialog,
                "Import Projection Data",
                "",
                "Projection data (*.csv *.tsv *.xlsx *.json)",
            )
            if not path:
                return
            edit_file.setText(path)
            load_preview(path)

        def refresh_validation(_index: int | None = None) -> None:
            preview = current_preview["preview"]
            if preview is None or self._discovery_metadata_ids_provider is None:
                return
            try:
                result = import_discovery_coordinates(
                    preview.path,
                    metadata_ids=self._discovery_metadata_ids_provider(),
                    id_column=str(combo_id.currentText() or ""),
                    x_column=str(combo_x.currentText() or ""),
                    y_column=str(combo_y.currentText() or ""),
                    metadata_column=str(combo_metadata.currentText() or ""),
                    metadata_key_values_by_id=self._metadata_values_for_import_combo(combo_metadata),
                )
                validation.setPlainText("\n".join(result.validation_report.messages))
                if ok_button is not None:
                    ok_button.setEnabled(True)
            except Exception as exc:
                validation.setPlainText(str(exc))
                if ok_button is not None:
                    ok_button.setEnabled(False)

        def accept() -> None:
            name = edit_name.text().strip() or "Projection"
            if self._discovery_name_exists(name):
                validation.setPlainText("Projection name already exists.")
                edit_name.setFocus()
                edit_name.selectAll()
                return
            if self._import_discovery_callback is None:
                validation.setPlainText("Projection import is unavailable.")
                return
            payload = {
                "name": name,
                "coordinate_file": edit_file.text().strip(),
                "id_column": str(combo_id.currentText() or ""),
                "metadata_column": str(combo_metadata.currentText() or ""),
                "x_column": str(combo_x.currentText() or ""),
                "y_column": str(combo_y.currentText() or ""),
            }
            if self._import_discovery_callback(payload):
                dialog.accept()
            else:
                validation.setPlainText("Projection import failed. Check the selected file and columns.")

        button_browse.clicked.connect(browse)
        combo_id.currentIndexChanged.connect(refresh_validation)
        combo_metadata.currentIndexChanged.connect(refresh_validation)
        combo_x.currentIndexChanged.connect(refresh_validation)
        combo_y.currentIndexChanged.connect(refresh_validation)
        button_box.accepted.connect(accept)
        button_box.rejected.connect(dialog.reject)
        dialog.exec()

    def _populate_import_dialog_preview(
        self,
        preview: DiscoveryImportPreview,
        *,
        combo_id: QtWidgets.QComboBox,
        combo_metadata: QtWidgets.QComboBox,
        combo_x: QtWidgets.QComboBox,
        combo_y: QtWidgets.QComboBox,
        table_preview: QtWidgets.QTableWidget,
        validation: QtWidgets.QTextBrowser,
    ) -> None:
        numeric_columns = self._numeric_import_columns(preview)
        with QtCore.QSignalBlocker(combo_id):
            combo_id.clear()
            combo_id.addItems(preview.columns)
            self._select_import_column(combo_id, self._ID_COLUMN_KEYS, fallback_columns=preview.columns)
        selected_id = str(combo_id.currentText() or "")
        with QtCore.QSignalBlocker(combo_x):
            combo_x.clear()
            combo_x.addItems(preview.columns)
            self._select_import_column(
                combo_x,
                self._X_COLUMN_KEYS,
                excluded_columns={selected_id},
                fallback_columns=numeric_columns,
            )
        selected_x = str(combo_x.currentText() or "")
        with QtCore.QSignalBlocker(combo_y):
            combo_y.clear()
            combo_y.addItems(preview.columns)
            self._select_import_column(
                combo_y,
                self._Y_COLUMN_KEYS,
                excluded_columns={selected_id, selected_x},
                fallback_columns=numeric_columns,
            )
        with QtCore.QSignalBlocker(combo_metadata):
            combo_metadata.clear()
            metadata_columns = ["metadata_id"]
            if self._discovery_metadata_columns_provider is not None:
                metadata_columns.extend(
                    column for column in self._discovery_metadata_columns_provider() if column not in metadata_columns
                )
            combo_metadata.addItems(metadata_columns)
            import_identifier = str(combo_id.currentText() or "")
            index = combo_metadata.findText(import_identifier, QtCore.Qt.MatchFlag.MatchFixedString)
            if index >= 0:
                combo_metadata.setCurrentIndex(index)
        table_preview.setColumnCount(len(preview.columns))
        table_preview.setHorizontalHeaderLabels(preview.columns)
        table_preview.setRowCount(len(preview.preview_rows))
        for row_index, row in enumerate(preview.preview_rows):
            for column_index, column in enumerate(preview.columns):
                table_preview.setItem(row_index, column_index, QtWidgets.QTableWidgetItem(str(row.get(column, ""))))
        table_preview.resizeColumnsToContents()
        validation.setPlainText(
            f"Loaded {len(preview.rows)} rows from {preview.path.name}.\n"
            f"Detected columns: {', '.join(preview.columns) if preview.columns else 'none'}"
        )

    def _metadata_values_for_import_combo(self, combo_metadata: QtWidgets.QComboBox) -> dict[int, str] | None:
        metadata_column = str(combo_metadata.currentText() or "").strip()
        if metadata_column in {"", "metadata_id", "id"}:
            return None
        if self._discovery_metadata_values_provider is None:
            return None
        return self._discovery_metadata_values_provider(metadata_column)

    _ID_COLUMN_KEYS = {
        "id",
        "metadataid",
        "metadata_id",
        "objectid",
        "object_id",
        "rowid",
        "row_id",
        "file",
        "filename",
        "file_name",
        "filepath",
        "file_path",
        "image",
        "imagefile",
        "image_file",
        "imagepath",
        "image_path",
        "source",
    }
    _X_COLUMN_KEYS = {
        "x",
        "x1",
        "xcoord",
        "xcoordinate",
        "coordx",
        "coordinatex",
        "gridx",
        "umapx",
        "umap1",
        "umap01",
        "tsnex",
        "tsne1",
        "tsne01",
        "pc1",
        "pca1",
        "pca01",
        "dim1",
        "dimension1",
        "component1",
        "component01",
        "coord1",
        "coordinate1",
    }
    _Y_COLUMN_KEYS = {
        "y",
        "y1",
        "ycoord",
        "ycoordinate",
        "coordy",
        "coordinatey",
        "gridy",
        "umapy",
        "umap2",
        "umap02",
        "tsney",
        "tsne2",
        "tsne02",
        "pc2",
        "pca2",
        "pca02",
        "dim2",
        "dimension2",
        "component2",
        "component02",
        "coord2",
        "coordinate2",
    }

    @classmethod
    def _select_import_column(
        cls,
        combo: QtWidgets.QComboBox,
        preferred_keys: set[str],
        *,
        excluded_columns: set[str] | None = None,
        fallback_columns: list[str] | tuple[str, ...] = (),
    ) -> None:
        excluded = {cls._normalized_import_column_key(column) for column in (excluded_columns or set())}
        for index in range(combo.count()):
            text = combo.itemText(index)
            key = cls._normalized_import_column_key(text)
            if key in excluded:
                continue
            if key in preferred_keys:
                combo.setCurrentIndex(index)
                return
        for column in fallback_columns:
            key = cls._normalized_import_column_key(column)
            if key in excluded:
                continue
            index = combo.findText(column, QtCore.Qt.MatchFlag.MatchFixedString)
            if index >= 0:
                combo.setCurrentIndex(index)
                return

    @staticmethod
    def _numeric_import_columns(preview: DiscoveryImportPreview) -> list[str]:
        numeric_columns: list[str] = []
        for column in preview.columns:
            values = [row.get(column) for row in preview.preview_rows if str(row.get(column, "")).strip()]
            if not values:
                continue
            try:
                for value in values:
                    float(value)
            except (TypeError, ValueError):
                continue
            numeric_columns.append(column)
        return numeric_columns

    @staticmethod
    def _normalized_import_column_key(column: str) -> str:
        return "".join(character for character in str(column or "").casefold() if character.isalnum())

    def _populate_parameter_table(
        self,
        combo_method: QtWidgets.QComboBox | None,
        table: QtWidgets.QTableWidget | None,
    ) -> None:
        if combo_method is None or table is None:
            return
        method_id = str(combo_method.currentData() or "")
        method = next((item for item in self._method_definitions if item.method_id == method_id), None)
        parameters = method.parameters if method is not None else {}
        visible_parameters = [
            (name, spec)
            for name, spec in parameters.items()
            if str(spec.get("ui") or "").strip().lower() != "hidden"
        ]
        table.setRowCount(len(visible_parameters))
        for row, (name, spec) in enumerate(visible_parameters):
            key_item = self._readonly_item(name)
            tooltip = str(spec.get("info") or "")
            key_item.setToolTip(tooltip)
            table.setItem(row, 0, key_item)
            table.setCellWidget(row, 1, self._parameter_editor(spec, tooltip))
        table.resizeColumnsToContents()

    @staticmethod
    def _parameter_editor(spec: dict[str, object], tooltip: str) -> QtWidgets.QWidget:
        parameter_type = str(spec.get("type") or "str").strip().lower()
        ui_element = str(spec.get("ui") or "").strip().lower()
        default = spec.get("default", "")
        choices = spec.get("choices")
        if ui_element == "check_box" or (not ui_element and parameter_type == "bool"):
            editor = QtWidgets.QCheckBox()
            editor.setChecked(default if isinstance(default, bool) else str(default).strip().lower() in {"1", "true", "yes", "on"})
        elif ui_element == "combo_box" or (not ui_element and isinstance(choices, list) and choices):
            editor = QtWidgets.QComboBox()
            for choice in choices if isinstance(choices, list) else []:
                editor.addItem(str(choice), choice)
            index = editor.findData(default)
            editor.setCurrentIndex(index if index >= 0 else 0)
        elif ui_element == "spin_box" or (not ui_element and parameter_type == "int"):
            editor = QtWidgets.QSpinBox()
            editor.setRange(int(spec.get("min", -2_147_483_648)), int(spec.get("max", 2_147_483_647)))
            editor.setValue(int(default))
        elif ui_element == "double_spin_box" or (not ui_element and parameter_type == "float"):
            editor = QtWidgets.QDoubleSpinBox()
            editor.setDecimals(max(0, min(12, int(spec.get("decimals", 6)))))
            editor.setRange(float(spec.get("min", -1_000_000_000_000)), float(spec.get("max", 1_000_000_000_000)))
            editor.setSingleStep(0.1)
            editor.setValue(float(default))
        else:  # line_edit and unconfigured parameters
            editor = QtWidgets.QLineEdit(str(default))
        editor.setToolTip(tooltip)
        return editor

    def _handle_discovery_method_changed(
        self,
        combo_method: QtWidgets.QComboBox | None,
        table: QtWidgets.QTableWidget | None,
        edit_name: QtWidgets.QLineEdit | None,
    ) -> None:
        previous_name = edit_name.text().strip() if edit_name is not None else ""
        self._populate_parameter_table(combo_method, table)
        if edit_name is None:
            return
        if not previous_name or self._is_generated_discovery_name(previous_name):
            combo_vectors = (
                self._dialog.findChild(QtWidgets.QComboBox, "combo_embeddings_source")
                if self._dialog is not None
                else None
            )
            combo_filter = (
                self._dialog.findChild(QtWidgets.QComboBox, "combo_filter_preset")
                if self._dialog is not None
                else None
            )
            edit_name.setText(self._default_discovery_name(combo_method, combo_vectors, combo_filter))

    def _handle_discovery_source_changed(
        self,
        combo_method: QtWidgets.QComboBox | None,
        combo_vectors: QtWidgets.QComboBox | None,
        edit_name: QtWidgets.QLineEdit | None,
    ) -> None:
        if edit_name is None:
            return
        previous_name = edit_name.text().strip()
        if not previous_name or self._is_generated_discovery_name(previous_name):
            combo_filter = (
                self._dialog.findChild(QtWidgets.QComboBox, "combo_filter_preset")
                if self._dialog is not None
                else None
            )
            edit_name.setText(self._default_discovery_name(combo_method, combo_vectors, combo_filter))

    def _handle_discovery_filter_changed(
        self,
        combo_method: QtWidgets.QComboBox | None,
        combo_vectors: QtWidgets.QComboBox | None,
        combo_filter: QtWidgets.QComboBox | None,
        edit_name: QtWidgets.QLineEdit | None,
    ) -> None:
        if edit_name is None:
            return
        previous_name = edit_name.text().strip()
        if not previous_name or self._is_generated_discovery_name(previous_name):
            edit_name.setText(self._default_discovery_name(combo_method, combo_vectors, combo_filter))

    @staticmethod
    def _parameter_values_from_table(table: QtWidgets.QTableWidget | None) -> dict[str, object]:
        if table is None:
            return {}
        values: dict[str, object] = {}
        for row in range(table.rowCount()):
            key_item = table.item(row, 0)
            if key_item is None:
                continue
            key = key_item.text().strip()
            editor = table.cellWidget(row, 1)
            if not key:
                continue
            if isinstance(editor, QtWidgets.QCheckBox):
                values[key] = editor.isChecked()
            elif isinstance(editor, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
                values[key] = editor.value()
            elif isinstance(editor, QtWidgets.QComboBox):
                values[key] = editor.currentData()
            elif isinstance(editor, QtWidgets.QLineEdit):
                values[key] = editor.text().strip()
            else:
                value_item = table.item(row, 1)
                if value_item is not None:
                    values[key] = value_item.text().strip()
        return values

    def _default_discovery_name(
        self,
        combo_method: QtWidgets.QComboBox | None = None,
        combo_vectors: QtWidgets.QComboBox | None = None,
        combo_filter: QtWidgets.QComboBox | None = None,
    ) -> str:
        method_label = self._current_discovery_method_label(combo_method)
        embeddings_label = self._short_embeddings_label(combo_vectors)
        filter_label = self._short_filter_label(combo_filter)
        filter_tag = f"[{filter_label}]" if filter_label else ""
        base_name = " ".join(part for part in (embeddings_label, filter_tag, method_label) if part)
        if not embeddings_label:
            base_name = f"{method_label} projection"
        return self._next_available_discovery_name(base_name, prefer_base=bool(embeddings_label))

    def _next_available_discovery_name(self, base_name: str, *, prefer_base: bool = False) -> str:
        reserved_names = self._existing_discovery_names | self.active_run_names()
        if prefer_base and base_name.casefold() not in reserved_names:
            return base_name
        index = 2 if prefer_base else 1
        while f"{base_name} {index}".casefold() in reserved_names:
            index += 1
        return f"{base_name} {index}"

    def _discovery_name_exists(self, name: str) -> bool:
        normalized = name.strip().casefold()
        return normalized in self._existing_discovery_names or normalized in self.active_run_names()

    def _current_discovery_method_label(self, combo_method: QtWidgets.QComboBox | None) -> str:
        if combo_method is not None and combo_method.currentText().strip():
            return combo_method.currentText().strip()
        if self._method_definitions:
            return self._method_definitions[0].name.strip() or "Projection"
        return "Projection"

    @staticmethod
    def _short_embeddings_label(combo_vectors: QtWidgets.QComboBox | None) -> str:
        if combo_vectors is None:
            return ""
        return DiscoveryPanelController._short_embeddings_label_for_text(combo_vectors.currentText())

    def _is_generated_discovery_name(self, name: str) -> bool:
        text = name.strip()
        if not text:
            return True
        method_names = [method.name.strip() for method in self._method_definitions if method.name.strip()]
        if not method_names:
            method_names = ["Projection"]
        source_names = []
        if self._dialog is not None:
            combo_vectors = self._dialog.findChild(QtWidgets.QComboBox, "combo_embeddings_source")
            if combo_vectors is not None:
                source_names = [
                    self._short_embeddings_label_for_text(combo_vectors.itemText(index))
                    for index in range(combo_vectors.count())
                ]
        return any(
            re.fullmatch(
                rf"(?:{re.escape(source_name)} )?(?:\[[^\]]+\] )?{re.escape(method_name)}(?: (?:projection|discovery))?(?: \d+)?",
                text,
                flags=re.IGNORECASE,
            )
            for method_name in method_names
            for source_name in [name for name in source_names if name] or [""]
        )

    @staticmethod
    def _short_embeddings_label_for_text(label: str) -> str:
        text = str(label or "").strip()
        if not text or text.casefold() == "no completed embeddings":
            return ""
        return re.sub(r"\s+embeddings(?:\s+\d+)?$", "", text, flags=re.IGNORECASE).strip() or text

    @staticmethod
    def _short_filter_label(combo_filter: QtWidgets.QComboBox | None) -> str:
        if combo_filter is None or not combo_filter.currentData():
            return ""
        text = re.sub(r"\s+", " ", combo_filter.currentText()).strip()
        text = re.sub(r"^(?:filter|preset)\s*[:_-]?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+(?:filter\s+)?preset$", "", text, flags=re.IGNORECASE).strip()
        text = text or "filtered"
        if len(text) > 18:
            text = f"{text[:17].rstrip()}…"
        return text

    @staticmethod
    def _safe_export_stem(label: str) -> str:
        text = "".join(character if character.isalnum() else "_" for character in str(label or "projection")).strip("_")
        return text or "projection"

    @staticmethod
    def _readonly_item(text: str) -> QtWidgets.QTableWidgetItem:
        item = QtWidgets.QTableWidgetItem(text)
        item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
        return item
