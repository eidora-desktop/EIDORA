from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtGui, QtWidgets

from MetadataArea.controllers.metadata_groups_export_selection_panel_controller import (
    MetadataGroupsExportSelectionPanelController,
)
from MetadataArea.domain import (
    MetadataGroupColumnConflictPolicy,
    MetadataGroupExportDestination,
    MetadataGroupExportLayout,
    MetadataGroupExportRequest,
    MetadataGroupExportResult,
    MetadataGroupExportStructure,
    MetadataGroupOverlapPolicy,
)
from MetadataArea.group_service import MetadataGroupService
from shared.qt.message_dialogs import show_error
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child

if TYPE_CHECKING:
    from shared.qt.activity import ActivityManager


class MetadataGroupsExportDialogController:
    """Bind the group-export dialog and delegate preview/export work to the service."""

    def __init__(
        self,
        parent: QtWidgets.QWidget | None,
        service: MetadataGroupService,
        *,
        activity_manager: ActivityManager | None = None,
    ) -> None:
        self._parent = parent
        self._service = service
        self._activity_manager = activity_manager
        self._dialog: QtWidgets.QDialog | None = None
        self._connections: list[tuple[object, object]] = []
        self._result: MetadataGroupExportResult | None = None
        self._preview_model: QtGui.QStandardItemModel | None = None
        self._selection_controller: MetadataGroupsExportSelectionPanelController | None = None

    def exec(self) -> MetadataGroupExportResult | None:
        dialog = load_ui("MetadataArea", "ui/metadata_groups_export_dialog.ui", None)
        if not isinstance(dialog, QtWidgets.QDialog):
            return None
        self._dialog = dialog
        self._selection_controller = MetadataGroupsExportSelectionPanelController(
            dialog, self._service
        )
        self._preview_model = QtGui.QStandardItemModel(dialog)
        preview_table = self._preview_table()
        preview_table.setModel(self._preview_model)
        preview_table.horizontalHeader().setDefaultAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        preview_table.horizontalHeader().setStretchLastSection(True)
        self._json_preview().setFont(
            QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
        )
        dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self._splitter().setSizes([280, 620])
        self._configure_identifier_combo()
        self._connect_signals()
        self._refresh()
        try:
            if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
                return None
            return self._result
        finally:
            self._disconnect_signals()
            if self._selection_controller is not None:
                self._selection_controller.dispose()
                self._selection_controller = None
            dialog.close()
            dialog.setParent(None)
            self._preview_model = None
            self._dialog = None

    def _configure_identifier_combo(self) -> None:
        identifier = self._identifier_combo()
        identifier.setItemData(0, "id")
        for column in self._service.list_metadata_columns():
            if column.name != "id":
                identifier.addItem(column.label, column.name)

    def _connect_signals(self) -> None:
        if self._selection_controller is not None:
            self._bind(self._selection_controller.selection_changed, self._refresh)
        self._bind(self._structure_combo().currentIndexChanged, self._refresh)
        self._bind(self._radio_memberships().toggled, self._refresh)
        self._bind(self._radio_group_columns().toggled, self._refresh)
        self._bind(self._radio_category().toggled, self._refresh)
        self._bind(self._identifier_combo().currentIndexChanged, self._refresh)
        self._bind(self._new_column_name_edit().textChanged, self._refresh)
        self._bind(self._cancel_button().clicked, self._reject)
        self._bind(self._export_file_button().clicked, self._export_to_file)
        self._bind(
            self._write_metadata_button().clicked,
            lambda: self._accept(MetadataGroupExportDestination.METADATA),
        )

    def _bind(self, signal, slot) -> None:
        signal.connect(slot)
        self._connections.append((signal, slot))

    def _disconnect_signals(self) -> None:
        for signal, slot in reversed(self._connections):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._connections.clear()

    def _refresh(self, *_args) -> None:
        if self._dialog is None:
            return
        structure = self._structure()
        table_output = structure == MetadataGroupExportStructure.TABLE
        self._table_options_widget().setVisible(table_output)
        show_new_column = table_output and (
            self._radio_category().isChecked() or self._radio_memberships().isChecked()
        )
        self._new_column_container().setVisible(show_new_column)
        self._new_column_container().setEnabled(show_new_column)
        json_output = structure in {
            MetadataGroupExportStructure.GROUPS_WITH_MEMBERS,
            MetadataGroupExportStructure.ITEMS_WITH_GROUPS,
        }
        self._preview_table().setVisible(not json_output)
        self._json_preview().setVisible(json_output)

        if not self._selected_group_ids():
            if self._preview_model is not None:
                self._preview_model.clear()
            self._json_preview().clear()
            self._preview_status().setText("Select at least one group to export.")
            self._export_file_button().setEnabled(False)
            self._write_metadata_button().setVisible(table_output)
            self._write_metadata_button().setEnabled(False)
            return

        suggested_name, _filters, default_format = self._file_dialog_options(structure)
        file_preview = self._service.preview_export(
            self._request(
                MetadataGroupExportDestination.FILE,
                destination_path=str(Path.home() / suggested_name),
                export_format=default_format,
            )
        )
        metadata_preview = (
            self._service.preview_export(
                self._request(MetadataGroupExportDestination.METADATA)
            )
            if table_output
            else None
        )
        if json_output:
            self._json_preview().setPlainText(file_preview.text)
        else:
            self._populate_preview_table(file_preview.output_columns, file_preview.rows)
        shown_items = (
            min(file_preview.output_row_count, 100)
            if json_output
            else len(file_preview.rows)
        )
        item_label = {
            MetadataGroupExportStructure.TABLE: "row(s)",
            MetadataGroupExportStructure.GROUPS_WITH_MEMBERS: "group(s)",
            MetadataGroupExportStructure.ITEMS_WITH_GROUPS: "membership record(s)",
            MetadataGroupExportStructure.GROUP_FILES: "group file(s)",
        }[structure]
        status = (
            f"{file_preview.selected_group_count} selected group(s). "
            f"Showing {shown_items} of {file_preview.output_row_count} {item_label}."
        )
        if structure == MetadataGroupExportStructure.GROUPS_WITH_MEMBERS:
            status += " Each group preview shows at most 100 identifiers."
        messages = list(file_preview.messages)
        if metadata_preview is not None:
            messages.extend(
                f"Write to Metadata: {message}"
                for message in metadata_preview.messages
                if message not in file_preview.messages
            )
        self._preview_status().setText("\n".join((status, *messages)))
        self._export_file_button().setEnabled(file_preview.valid)
        self._write_metadata_button().setVisible(table_output)
        self._write_metadata_button().setEnabled(
            metadata_preview is not None and metadata_preview.valid
        )

    def _populate_preview_table(
        self,
        columns: tuple[str, ...],
        rows: tuple[tuple[object, ...], ...],
    ) -> None:
        if self._preview_model is None:
            return
        self._preview_model.clear()
        self._preview_model.setColumnCount(len(columns))
        self._preview_model.setHorizontalHeaderLabels(list(columns))
        for row in rows:
            items = [QtGui.QStandardItem(str(value)) for value in row]
            for item in items:
                item.setEditable(False)
            self._preview_model.appendRow(items)
        self._preview_table().resizeColumnsToContents()

    def _request(
        self,
        destination: MetadataGroupExportDestination,
        *,
        destination_path: str = "",
        export_format: str = "csv",
    ) -> MetadataGroupExportRequest:
        if self._radio_category().isChecked():
            layout = MetadataGroupExportLayout.CATEGORY_COLUMN
        elif self._radio_group_columns().isChecked():
            layout = MetadataGroupExportLayout.GROUP_COLUMNS
        else:
            layout = MetadataGroupExportLayout.MEMBERSHIP_TABLE
        return MetadataGroupExportRequest(
            group_ids=self._selected_group_ids(),
            destination=destination,
            layout=layout,
            structure=self._structure(),
            destination_path=(
                destination_path
                if destination == MetadataGroupExportDestination.FILE
                else ""
            ),
            export_format=export_format,
            identifier_column=str(self._identifier_combo().currentData() or "id"),
            category_column_name=self._new_column_name_edit().text().strip(),
            overlap_policy=MetadataGroupOverlapPolicy.MULTIPLE,
            column_conflict_policy=MetadataGroupColumnConflictPolicy.UNIQUE,
        )

    def _accept(
        self,
        destination: MetadataGroupExportDestination,
        *,
        destination_path: str = "",
        export_format: str = "csv",
    ) -> None:
        if self._dialog is None:
            return
        label = (
            "Writing groups to metadata…"
            if destination == MetadataGroupExportDestination.METADATA
            else "Exporting metadata groups…"
        )
        activity = (
            self._activity_manager.progress(label, value=10, total=100)
            if self._activity_manager is not None
            else None
        )
        try:
            if activity is not None:
                activity.update(value=30)
            self._result = self._service.export_groups(
                self._request(
                    destination,
                    destination_path=destination_path,
                    export_format=export_format,
                )
            )
            if activity is not None:
                activity.update(message="Metadata group export complete", value=100)
                activity.retain_completed()
        except Exception as exc:
            if activity is not None:
                activity.retain_failed(f"Metadata group export failed: {exc}")
            show_error(self._dialog, "Export Groups Failed", str(exc))
            self._refresh()
            return
        finally:
            if activity is not None:
                activity.finish()
        self._dialog.accept()

    def _reject(self, *_args) -> None:
        if self._dialog is not None:
            self._dialog.reject()

    def _export_to_file(self, *_args) -> None:
        if self._dialog is None:
            return
        suggested_name, filters, _default_format = self._file_dialog_options(
            self._structure()
        )
        path, selected_filter = QtWidgets.QFileDialog.getSaveFileName(
            self._dialog,
            "Export Groups",
            str(Path.home() / suggested_name),
            filters,
        )
        if not path:
            return
        formats = {
            "CSV files (*.csv)": "csv",
            "TSV files (*.tsv)": "tsv",
            "JSON records (*.json)": "json",
            "JSON files (*.json)": "json",
            "ZIP archives (*.zip)": "zip",
        }
        output = Path(path)
        export_format = formats.get(selected_filter)
        if export_format is None:
            suffix_format = output.suffix.lower().lstrip(".")
            supported = {"csv", "tsv", "json", "zip"}
            export_format = suffix_format if suffix_format in supported else _default_format
        output = output.with_suffix(f".{export_format}")
        self._accept(
            MetadataGroupExportDestination.FILE,
            destination_path=str(output),
            export_format=export_format,
        )

    @staticmethod
    def _file_dialog_options(
        structure: MetadataGroupExportStructure,
    ) -> tuple[str, str, str]:
        if structure == MetadataGroupExportStructure.TABLE:
            return (
                "metadata_groups.csv",
                "CSV files (*.csv);;TSV files (*.tsv);;JSON records (*.json)",
                "csv",
            )
        if structure == MetadataGroupExportStructure.GROUPS_WITH_MEMBERS:
            return "metadata_groups_by_group.json", "JSON files (*.json)", "json"
        if structure == MetadataGroupExportStructure.ITEMS_WITH_GROUPS:
            return "metadata_group_memberships.json", "JSON files (*.json)", "json"
        return "metadata_group_lists.zip", "ZIP archives (*.zip)", "zip"

    def _structure(self) -> MetadataGroupExportStructure:
        structures = (
            MetadataGroupExportStructure.TABLE,
            MetadataGroupExportStructure.GROUPS_WITH_MEMBERS,
            MetadataGroupExportStructure.ITEMS_WITH_GROUPS,
            MetadataGroupExportStructure.GROUP_FILES,
        )
        index = self._structure_combo().currentIndex()
        return structures[index] if 0 <= index < len(structures) else structures[0]

    def _widget(self, widget_type: type, name: str):
        if self._dialog is None:
            raise RuntimeError("The group export dialog is not open.")
        return require_child(self._dialog, widget_type, name)

    def _selected_group_ids(self) -> tuple[str, ...]:
        if self._selection_controller is None:
            return ()
        return self._selection_controller.selected_group_ids

    def _splitter(self):
        return self._widget(QtWidgets.QSplitter, "splitter_export_groups")

    def _new_column_container(self):
        return self._widget(QtWidgets.QWidget, "new_column_container")

    def _table_options_widget(self):
        return self._widget(QtWidgets.QWidget, "table_options_widget")

    def _structure_combo(self):
        return self._widget(QtWidgets.QComboBox, "combo_export_structure")

    def _radio_memberships(self):
        return self._widget(QtWidgets.QRadioButton, "radio_export_layout_membership_table")

    def _radio_group_columns(self):
        return self._widget(QtWidgets.QRadioButton, "radio_export_layout_group_columns")

    def _radio_category(self):
        return self._widget(QtWidgets.QRadioButton, "radio_export_layout_category_column")

    def _identifier_combo(self):
        return self._widget(QtWidgets.QComboBox, "combo_export_group_identifier")

    def _new_column_name_edit(self):
        return self._widget(QtWidgets.QLineEdit, "line_new_column_name")

    def _preview_status(self):
        return self._widget(QtWidgets.QLabel, "label_export_group_preview_status")

    def _preview_table(self):
        return self._widget(QtWidgets.QTableView, "table_export_group_preview")

    def _json_preview(self):
        return self._widget(QtWidgets.QPlainTextEdit, "text_export_group_json_preview")

    def _cancel_button(self):
        return self._widget(QtWidgets.QPushButton, "button_cancel_group_export")

    def _export_file_button(self):
        return self._widget(QtWidgets.QPushButton, "button_export_file")

    def _write_metadata_button(self):
        return self._widget(QtWidgets.QPushButton, "button_write_metadata")
