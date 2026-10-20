from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import re
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtGui, QtWidgets

from MetadataArea.controllers.metadata_groups_panel_controller import MetadataGroupsPanelController
from MetadataArea.controllers.metadata_import_dialog_controller import MetadataImportDialogController
from MetadataArea.domain import MetadataColumn, MetadataSearchMatch
from MetadataArea.importing import MetadataImportPreset, MetadataImportService
from MetadataArea.importing.readers import IMPORT_FILE_FILTER
from MetadataArea.repository import MetadataRepository
from MetadataArea.service import MetadataService
from MetadataArea.table_model import MetadataHighlightDelegate, MetadataTableModel
from shared.qt.message_dialogs import show_error
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import add_widget_to_container, require_child

if TYPE_CHECKING:
    from project.domain import Project
    from shared.qt.activity import ActivityManager


class MetadataAreaController:
    """UI binder for the metadata area."""

    INITIAL_ROW_LIMIT = 1000
    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        status_callback: Callable[[str], None] | None = None,
        activity_manager: ActivityManager | None = None,
    ) -> None:
        self._widget = load_ui("MetadataArea", "ui/metadata_area.ui", parent)
        self._project: Project | None = None
        self._status_callback = status_callback
        self._activity_manager = activity_manager
        self._groups_changed_callback: Callable[[], None] | None = None
        self._metadata_changed_callback: Callable[[], None] | None = None
        self._columns: list[MetadataColumn] = []
        self._row_count = 0
        self._media_publication_active = False
        self._refresh_after_media = False
        self._search_matches: list[MetadataSearchMatch] = []
        self._search_position = -1
        self._table_panel = self._mount_panel(
            host_name="panel_metadata_table",
            package="MetadataArea",
            ui_path="ui/metadata_table.ui",
        )
        self._metadata_groups_panel = MetadataGroupsPanelController(
            parent=self._widget,
            status_callback=self._show_status,
            activity_manager=activity_manager,
        )
        self._metadata_groups_panel.set_changed_callback(self._on_groups_changed)
        self._metadata_groups_panel.set_metadata_changed_callback(
            self._on_group_metadata_changed
        )
        groups_host = require_child(self._widget, QtWidgets.QWidget, "panel_metadata_groups")
        add_widget_to_container(groups_host, self._metadata_groups_panel.widget)
        splitter = require_child(self._widget, QtWidgets.QSplitter, "splitter_metadata")
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        groups_host.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Maximum,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        groups_host.setMaximumWidth(groups_host.sizeHint().width())
        self._table_model = MetadataTableModel(self._widget)

        self._table = require_child(self._table_panel, QtWidgets.QTableView, "table_metadata")
        self._metadata_groups_panel.set_row_height(
            self._table.verticalHeader().defaultSectionSize()
        )
        self._table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setItemDelegate(MetadataHighlightDelegate(self._table))
        self._table.horizontalHeader().setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.horizontalHeader().customContextMenuRequested.connect(self._on_header_context_menu)
        self._import_button = require_child(self._table_panel, QtWidgets.QPushButton, "button_import_metadata")
        self._export_button = require_child(self._table_panel, QtWidgets.QAbstractButton, "button_export_metadata")
        self._add_columns_button = require_child(
            self._table_panel,
            QtWidgets.QPushButton,
            "button_add_columns",
        )
        self._add_rows_button = require_child(
            self._table_panel,
            QtWidgets.QPushButton,
            "button_add_rows",
        )
        self._search_edit = require_child(self._table_panel, QtWidgets.QLineEdit, "edit_metadata_search")
        self._search_column_combo = require_child(self._table_panel, QtWidgets.QComboBox, "combo_search_column")
        self._match_case_button = require_child(
            self._table_panel,
            QtWidgets.QAbstractButton,
            "button_metadata_match_case",
        )
        self._match_word_button = require_child(
            self._table_panel,
            QtWidgets.QAbstractButton,
            "button_metadata_match_word",
        )
        self._regex_button = require_child(
            self._table_panel,
            QtWidgets.QAbstractButton,
            "button_metadata_search_regex",
        )
        self._matches_label = require_child(self._table_panel, QtWidgets.QLabel, "label_metadata_search_matches")
        self._previous_button = require_child(
            self._table_panel,
            QtWidgets.QAbstractButton,
            "button_metadata_search_previous",
        )
        self._next_button = require_child(self._table_panel, QtWidgets.QAbstractButton, "button_metadata_search_next")
        self._clear_button = require_child(self._table_panel, QtWidgets.QAbstractButton, "button_metadata_search_clear")

        self._wire_search()
        self._table.doubleClicked.connect(self._edit_cell)
        self._import_button.clicked.connect(self._on_import_metadata)
        self._export_button.clicked.connect(self._on_export_metadata)
        self._add_columns_button.clicked.connect(
            lambda: self._open_metadata_import(MetadataImportPreset.UPDATE)
        )
        self._add_rows_button.clicked.connect(
            lambda: self._open_metadata_import(MetadataImportPreset.APPEND)
        )
        self._render_empty_state("No project loaded")

    @property
    def widget(self) -> QtWidgets.QWidget:
        return self._widget

    def show_group_set(self, group_set_id):
        self._metadata_groups_panel.select_group_set(group_set_id)

    def set_groups_changed_callback(self, callback: Callable[[], None] | None) -> None:
        self._groups_changed_callback = callback

    def set_metadata_changed_callback(self, callback: Callable[[], None] | None) -> None:
        self._metadata_changed_callback = callback

    def set_project(self, project: Project | None) -> None:
        self._project = project
        self._metadata_groups_panel.set_project(project)
        self.refresh()

    def shutdown(self) -> None:
        return

    def refresh(self) -> None:
        if self._media_publication_active:
            self._refresh_after_media = True
            return
        if self._activity_manager is not None:
            with self._activity_manager.busy("Refreshing metadata…"):
                self._refresh()
            return
        self._refresh()

    def set_media_publication_active(self, active: bool) -> None:
        self._media_publication_active = active
        self._table_model.set_publication_active(active)
        for control in (self._import_button, self._add_columns_button, self._add_rows_button,
                        self._export_button, self._search_edit, self._search_column_combo):
            control.setEnabled(not active)
        self._metadata_groups_panel.widget.setEnabled(not active)
        if not active:
            if self._refresh_after_media:
                self._refresh_after_media = False
                self.refresh()
            self._update_import_button()

    def select_metadata_row(self, metadata_id: int) -> bool:
        """Select and center one canonical metadata row in the current table order."""

        if self._media_publication_active or self._project is None or self._table.model() is not self._table_model:
            return False
        row_offset = self._metadata_service().row_offset_for_id(
            int(metadata_id),
            order_by=self._table_model.order_by,
            descending=self._table_model.descending,
        )
        if row_offset is None:
            return False
        index = self._table_model.index(row_offset, 0)
        if not index.isValid():
            return False
        self._table.setCurrentIndex(index)
        self._table.selectRow(row_offset)
        self._table.setCurrentIndex(index)
        self._table.scrollTo(
            index,
            QtWidgets.QAbstractItemView.ScrollHint.PositionAtCenter,
        )
        self._table.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)
        return True

    def _refresh(self) -> None:
        self._metadata_groups_panel.refresh()
        if self._project is None:
            self._render_empty_state("No project loaded")
            return

        try:
            service = self._metadata_service()
            self._columns = service.list_columns()
            self._row_count = service.count_rows()
        except Exception as exc:
            self._columns = []
            self._row_count = 0
            self._render_empty_state("Metadata could not be loaded")
            self._show_status(f"Metadata load failed: {exc}")
            return

        if self._is_empty_metadata_project():
            self._render_empty_state("No metadata imported")
            self._show_status("No metadata imported")
            return

        self._render_table()
        self._update_search()
        self._show_status(f"Loaded {self._row_count} metadata rows")

    def _on_groups_changed(self) -> None:
        if self._groups_changed_callback is not None:
            self._groups_changed_callback()

    def _on_group_metadata_changed(self) -> None:
        self.refresh()
        self._notify_metadata_changed()

    def _mount_panel(self, *, host_name: str, package: str, ui_path: str) -> QtWidgets.QWidget:
        host = require_child(self._widget, QtWidgets.QWidget, host_name)
        panel = load_ui(package, ui_path, self._widget)
        add_widget_to_container(host, panel)
        return panel

    def _on_import_metadata(self) -> None:
        if self._project is None:
            show_error(self._widget, "Import Metadata", "Open or create a project before importing metadata.")
            self._show_status("No project loaded")
            return
        preset = (
            MetadataImportPreset.REPLACE_TABLE
            if self._has_metadata_rows()
            else MetadataImportPreset.INITIAL
        )
        self._open_metadata_import(preset)

    def _on_export_metadata(self) -> None:
        if self._project is None or not self._has_metadata_rows():
            return
        path, selected_filter = QtWidgets.QFileDialog.getSaveFileName(
            self._widget,
            "Export Metadata",
            str(Path.home() / "metadata.csv"),
            "CSV files (*.csv);;TSV files (*.tsv);;JSON files (*.json)",
        )
        if not path:
            return
        export_format = "json" if "JSON" in selected_filter else "tsv" if "TSV" in selected_filter else "csv"
        activity = (
            self._activity_manager.progress(
                "Preparing metadata export…", value=0, total=100
            )
            if self._activity_manager is not None
            else None
        )
        try:
            if activity is not None:
                activity.update(message="Writing metadata export…", value=25)
            destination = self._metadata_service().export_metadata(
                Path(path), export_format
            )
            if activity is not None:
                activity.update(message="Metadata export complete", value=100)
                activity.retain_completed()
        except Exception as exc:
            if activity is not None:
                activity.retain_failed(f"Metadata export failed: {exc}")
            show_error(self._widget, "Export Metadata Failed", str(exc))
            if activity is None:
                self._show_status(f"Metadata export failed: {exc}")
            return
        finally:
            if activity is not None:
                activity.finish()
        if activity is None:
            self._show_status(f"Exported metadata to {destination}")

    def _open_metadata_import(self, preset: MetadataImportPreset) -> None:
        if self._project is None:
            return
        operation_name = preset.label
        source_path, _selected_filter = QtWidgets.QFileDialog.getOpenFileName(
            self._widget, operation_name, "", IMPORT_FILE_FILTER,
        )
        if not source_path:
            self._show_status("Metadata import cancelled")
            return
        repository = MetadataRepository(
            self._project.paths.metadata_database,
            self._project.paths.metadata_columns,
        )
        dialog = MetadataImportDialogController(
            MetadataImportService(self._project, repository),
            Path(source_path),
            preset,
            process_manager=self._activity_manager.processes if self._activity_manager else None,
        )
        result = dialog.exec()
        if result is None:
            self._show_status("Metadata import cancelled")
            return
        self._clear_search()
        self.refresh()
        if result.impact.affected_groups:
            self._on_groups_changed()
        self._notify_metadata_changed()
        action = {
            MetadataImportPreset.INITIAL: "Imported metadata",
            MetadataImportPreset.UPDATE: "Updated metadata",
            MetadataImportPreset.APPEND: "Appended items",
            MetadataImportPreset.REPLACE_TABLE: "Replaced metadata",
        }.get(result.preset, "Updated metadata")
        message = f"{action} from {Path(result.source_path).name}"
        self._show_status(message)

    def _wire_search(self) -> None:
        self._table_model.sort_order_changed.connect(self._update_search)
        self._search_edit.textChanged.connect(self._update_search)
        self._search_column_combo.currentIndexChanged.connect(self._update_search)
        self._match_case_button.toggled.connect(self._update_search)
        self._match_word_button.toggled.connect(self._update_search)
        self._regex_button.toggled.connect(self._update_search)
        self._next_button.clicked.connect(self._select_next_match)
        self._previous_button.clicked.connect(self._select_previous_match)
        self._clear_button.clicked.connect(self._clear_search)

    def _render_empty_state(self, message: str) -> None:
        self._columns = []
        self._row_count = 0
        self._table.setSortingEnabled(False)
        self._table_model.clear()
        empty_model = QtGui.QStandardItemModel(1, 1, self._table)
        empty_model.setHorizontalHeaderLabels(["Metadata"])
        item = QtGui.QStandardItem(message)
        item.setEditable(False)
        empty_model.setItem(0, 0, item)
        self._table.setModel(empty_model)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._populate_search_columns([])
        self._set_search_enabled(False)
        self._update_import_button()
        self._update_search_controls()

    def _render_table(self) -> None:
        self._set_search_enabled(self._row_count > 0)
        self._populate_search_columns(self._columns)
        self._table.setSortingEnabled(False)
        self._table_model.set_source(self._metadata_service(), self._columns, self._row_count)
        self._table.setModel(self._table_model)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setSortingEnabled(True)
        self._table.sortByColumn(0, QtCore.Qt.SortOrder.AscendingOrder)
        self._update_import_button()

    def _is_empty_metadata_project(self) -> bool:
        return self._row_count == 0 and all(column.name == "id" for column in self._columns)

    def _has_metadata_rows(self) -> bool:
        return self._row_count > 0

    def _update_import_button(self) -> None:
        self._import_button.setText("Replace…" if self._has_metadata_rows() else "Import…")
        self._export_button.setEnabled(self._has_metadata_rows())
        self._add_columns_button.setEnabled(self._has_metadata_rows())
        self._add_rows_button.setEnabled(self._has_metadata_rows())

    def _update_search(self) -> None:
        if self._media_publication_active:
            return
        query = self._search_edit.text().strip()
        self._search_matches = []
        self._search_position = -1
        if not query or self._row_count == 0:
            self._table_model.clear_search_matches()
            self._update_search_controls()
            return

        try:
            self._search_matches = self._metadata_service().search_matches(
                query,
                regex=self._regex_button.isChecked(),
                match_case=self._match_case_button.isChecked(),
                match_word=self._match_word_button.isChecked(),
                order_by=self._table_model.order_by,
                descending=self._table_model.descending,
                search_column=self._selected_search_column(),
            )
        except re.error as exc:
            self._table_model.clear_search_matches()
            self._matches_label.setText("invalid")
            self._previous_button.setEnabled(False)
            self._next_button.setEnabled(False)
            self._clear_button.setEnabled(True)
            self._show_status(f"Invalid regular expression: {exc}")
            return

        if self._search_matches:
            self._search_position = 0
            self._select_match_at_position()
        else:
            self._table_model.clear_search_matches()
        self._update_search_controls()

    def _select_next_match(self) -> None:
        if not self._search_matches:
            return
        self._search_position = (self._search_position + 1) % len(self._search_matches)
        self._select_match_at_position()
        self._update_search_controls()

    def _select_previous_match(self) -> None:
        if not self._search_matches:
            return
        self._search_position = (self._search_position - 1) % len(self._search_matches)
        self._select_match_at_position()
        self._update_search_controls()

    def _select_match_at_position(self) -> None:
        if self._search_position < 0:
            return
        match = self._search_matches[self._search_position]
        active_column = match.column_indexes[0] if match.column_indexes else 0
        self._table_model.set_search_matches(
            self._search_matches,
            match,
            query=self._search_edit.text().strip(),
            regex=self._regex_button.isChecked(),
            match_case=self._match_case_button.isChecked(),
            match_word=self._match_word_button.isChecked(),
        )
        index = self._table_model.index(match.row_offset, active_column)
        if index.isValid():
            self._table.setCurrentIndex(index)
            self._table.selectRow(match.row_offset)
            self._table.setCurrentIndex(index)
            self._table.scrollTo(index, QtWidgets.QAbstractItemView.ScrollHint.PositionAtCenter)

    def _clear_search(self) -> None:
        self._search_edit.clear()
        self._search_matches = []
        self._search_position = -1
        self._table_model.clear_search_matches()
        self._table.clearSelection()
        self._update_search_controls()

    def _edit_cell(self, index: QtCore.QModelIndex | QtCore.QPersistentModelIndex) -> None:
        if self._media_publication_active:
            return
        if not index.isValid() or index.model() is not self._table_model:
            return

        column_name = self._table_model.column_name_at(index.column())
        row_id = self._table_model.row_id_at(index.row())
        if column_name is None or row_id is None:
            return
        if column_name == "id":
            self._show_status("Metadata ID values cannot be edited")
            return

        editor = QtWidgets.QPlainTextEdit()
        editor.setObjectName("edit_metadata_cell_value")
        editor.setPlainText(str(index.data(QtCore.Qt.ItemDataRole.DisplayRole) or ""))
        editor.selectAll()

        dialog = QtWidgets.QDialog(self._widget)
        dialog.setObjectName("dialog_metadata_cell_editor")
        dialog.setWindowTitle("Edit Metadata Cell")
        dialog.resize(480, 260)
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.addWidget(editor)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.setObjectName("button_box_metadata_cell_editor")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return

        self._save_cell_edit(index, row_id, column_name, editor.toPlainText())

    def _save_cell_edit(
        self,
        index: QtCore.QModelIndex | QtCore.QPersistentModelIndex,
        row_id: int,
        column_name: str,
        value: str,
    ) -> None:
        current_value = str(index.data(QtCore.Qt.ItemDataRole.DisplayRole) or "")
        if value == current_value:
            return

        try:
            self._metadata_service().update_cell(row_id, column_name, value)
        except Exception as exc:
            show_error(self._widget, "Edit Metadata Failed", str(exc))
            self._show_status("Metadata edit failed")
            return

        if column_name == self._table_model.order_by:
            self._table_model.refresh_current_order()
        else:
            self._table_model.refresh_row(index.row())
        if self._search_edit.text().strip():
            self._update_search()
        self._notify_metadata_changed()
        self._show_status("Updated metadata cell")

    def _on_header_context_menu(self, position: QtCore.QPoint) -> None:
        if self._media_publication_active:
            return
        if self._project is None or self._row_count == 0 or self._table.model() is not self._table_model:
            return

        header = self._table.horizontalHeader()
        section = header.logicalIndexAt(position)
        column_name = self._table_model.column_name_at(section)
        if column_name is None:
            return
        if column_name == "id":
            self._show_status("Metadata ID column cannot be renamed or deleted")
            return

        menu = QtWidgets.QMenu(header)
        rename_action = menu.addAction("Rename Column…")
        menu.addSeparator()
        delete_action = menu.addAction("Delete Column…")
        selected_action = menu.exec(header.mapToGlobal(position))
        if selected_action == rename_action:
            self._rename_metadata_column(column_name)
        elif selected_action == delete_action:
            self._delete_metadata_column(column_name)

    def _rename_metadata_column(self, column_name: str) -> None:
        new_name, accepted = self._ask_text("Rename Metadata Column", "New column name:", text=column_name)
        if not accepted:
            self._show_status("Rename metadata column cancelled")
            return

        try:
            self._metadata_service().rename_column(column_name, new_name)
        except Exception as exc:
            show_error(self._widget, "Rename Metadata Column Failed", str(exc))
            self._show_status("Rename metadata column failed")
            return

        self._clear_search()
        self.refresh()
        self._notify_metadata_changed()
        self._show_status(f"Renamed metadata column {column_name} to {new_name.strip()}")

    def _delete_metadata_column(self, column_name: str) -> None:
        if not self._confirm_delete_column(column_name):
            self._show_status("Delete metadata column cancelled")
            return

        try:
            self._metadata_service().delete_column(column_name)
        except Exception as exc:
            show_error(self._widget, "Delete Metadata Column Failed", str(exc))
            self._show_status("Delete metadata column failed")
            return

        self._clear_search()
        self.refresh()
        self._notify_metadata_changed()
        self._show_status(f"Deleted metadata column {column_name}")

    def _confirm_delete_column(self, column_name: str) -> bool:
        result = QtWidgets.QMessageBox.question(
            self._widget,
            "Delete Metadata Column",
            f"Delete metadata column '{column_name}'? This cannot be undone.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        return result == QtWidgets.QMessageBox.StandardButton.Yes

    def _ask_text(self, title: str, label: str, text: str = "") -> tuple[str, bool]:
        return QtWidgets.QInputDialog.getText(self._widget, title, label, text=text)

    def _update_search_controls(self) -> None:
        total = len(self._search_matches)
        current = self._search_position + 1 if total and self._search_position >= 0 else 0
        self._matches_label.setText(f"{current}/{total}")
        self._previous_button.setEnabled(total > 0)
        self._next_button.setEnabled(total > 0)
        self._clear_button.setEnabled(self._search_edit.isEnabled() and bool(self._search_edit.text()))

    def _set_search_enabled(self, enabled: bool) -> None:
        if not enabled and self._search_edit.text():
            self._search_edit.clear()
        self._search_edit.setEnabled(enabled)
        self._search_column_combo.setEnabled(enabled)
        self._match_case_button.setEnabled(enabled)
        self._match_word_button.setEnabled(enabled)
        self._regex_button.setEnabled(enabled)

    def _populate_search_columns(self, columns: list[MetadataColumn]) -> None:
        previous_column = self._selected_search_column()
        self._search_column_combo.blockSignals(True)
        self._search_column_combo.clear()
        self._search_column_combo.addItem("all columns", None)
        for column in columns:
            self._search_column_combo.addItem(self._column_label(column), column.name)
        if previous_column is not None:
            index = self._search_column_combo.findData(previous_column)
            if index >= 0:
                self._search_column_combo.setCurrentIndex(index)
        self._search_column_combo.blockSignals(False)

    def _selected_search_column(self) -> str | None:
        data = self._search_column_combo.currentData()
        return str(data) if data else None

    @staticmethod
    def _column_label(column: MetadataColumn) -> str:
        if column.name == "id":
            return "ID"
        return column.name

    def _metadata_service(self) -> MetadataService:
        if self._project is None:
            raise RuntimeError("No project is loaded")
        paths = self._project.paths
        return MetadataService(MetadataRepository(paths.metadata_database, paths.metadata_columns))

    def _notify_metadata_changed(self) -> None:
        if self._metadata_changed_callback is not None:
            self._metadata_changed_callback()

    def _show_status(self, message: str) -> None:
        if self._status_callback is not None:
            self._status_callback(message)
