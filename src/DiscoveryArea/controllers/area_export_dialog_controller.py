from __future__ import annotations

import re
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from DiscoveryArea.domain import PolygonArea
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class AreaExportDialogController:
    _FORMATS = (
        ("CSV", "csv", "CSV files (*.csv)"),
        ("TSV", "tsv", "TSV files (*.tsv)"),
        ("JSON", "json", "JSON files (*.json)"),
    )

    def exec(self, area: PolygonArea) -> dict[str, object] | None:
        dialog = load_ui("DiscoveryArea", "ui/export_area_dialog.ui", None)
        if not isinstance(dialog, QtWidgets.QDialog):
            return None
        dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        combo_format = require_child(dialog, QtWidgets.QComboBox, "combo_area_export_format")
        checkbox_metadata = require_child(dialog, QtWidgets.QCheckBox, "checkbox_area_export_metadata")
        edit_file = require_child(dialog, QtWidgets.QLineEdit, "edit_area_export_file")
        button_browse = require_child(dialog, QtWidgets.QPushButton, "button_browse_area_export_file")
        summary = require_child(dialog, QtWidgets.QTextBrowser, "text_area_export_summary")
        button_box = require_child(dialog, QtWidgets.QDialogButtonBox, "button_box_area_export")
        for label, value, _file_filter in self._FORMATS:
            combo_format.addItem(label, value)
        edit_file.setText(str(Path.home() / f"{self._safe_stem(area.label)}.csv"))

        def selected_format() -> tuple[str, str]:
            value = str(combo_format.currentData() or "csv")
            selected = next((item for item in self._FORMATS if item[1] == value), self._FORMATS[0])
            return selected[1], selected[2]

        def apply_extension() -> None:
            text = edit_file.text().strip()
            if text:
                export_format, _file_filter = selected_format()
                edit_file.setText(str(Path(text).expanduser().with_suffix(f".{export_format}")))

        def update_summary() -> None:
            detail = "ID plus all metadata columns" if checkbox_metadata.isChecked() else "ID only"
            summary.setPlainText(f"Exports {len(area.metadata_ids)} members from {area.label}.\n{detail}.")

        def browse() -> None:
            export_format, file_filter = selected_format()
            path, _selected_filter = QtWidgets.QFileDialog.getSaveFileName(
                dialog,
                "Export Area Members",
                edit_file.text().strip() or str(Path.home() / f"area.{export_format}"),
                f"{file_filter};;All Files (*)",
            )
            if path:
                edit_file.setText(path)
                apply_extension()

        accepted_payload: dict[str, object] = {}

        def accept() -> None:
            if not edit_file.text().strip():
                summary.setPlainText("Choose an output file.")
                return
            apply_extension()
            accepted_payload.update(
                {
                    "area_id": area.area_id,
                    "destination": edit_file.text().strip(),
                    "export_format": str(combo_format.currentData() or "csv"),
                    "include_metadata": checkbox_metadata.isChecked(),
                }
            )
            dialog.accept()

        combo_format.currentIndexChanged.connect(lambda _index: apply_extension())
        checkbox_metadata.toggled.connect(lambda _checked: update_summary())
        button_browse.clicked.connect(browse)
        button_box.accepted.connect(accept)
        button_box.rejected.connect(dialog.reject)
        update_summary()
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return None
        return dict(accepted_payload)

    @staticmethod
    def _safe_stem(label: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]+", "_", str(label)).strip("_") or "area"


class AreasToMetadataDialogController:
    def exec(
        self,
        areas: list[PolygonArea],
        *,
        existing_columns: list[str],
        overlap_count: int,
        binary_columns: list[tuple[str, str]],
    ) -> dict[str, object] | None:
        dialog = load_ui("DiscoveryArea", "ui/areas_to_metadata_dialog.ui", None)
        if not isinstance(dialog, QtWidgets.QDialog):
            return None
        dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        edit_column = require_child(dialog, QtWidgets.QLineEdit, "edit_areas_metadata_column")
        label_column = require_child(dialog, QtWidgets.QLabel, "label_areas_metadata_column")
        combo_mode = require_child(dialog, QtWidgets.QComboBox, "combo_areas_metadata_mode")
        binary_panel = require_child(dialog, QtWidgets.QWidget, "panel_areas_binary_columns")
        binary_list = require_child(dialog, QtWidgets.QListWidget, "list_areas_binary_columns")
        priority_panel = require_child(dialog, QtWidgets.QWidget, "panel_area_priority")
        priority_list = require_child(dialog, QtWidgets.QListWidget, "list_areas_metadata_priority")
        button_up = require_child(dialog, QtWidgets.QPushButton, "button_area_priority_up")
        button_down = require_child(dialog, QtWidgets.QPushButton, "button_area_priority_down")
        summary = require_child(dialog, QtWidgets.QTextBrowser, "text_areas_metadata_summary")
        button_box = require_child(dialog, QtWidgets.QDialogButtonBox, "button_box_areas_metadata")
        edit_column.setText("area_groups")
        combo_mode.addItem("Single-label", "single_label")
        combo_mode.addItem("Non-mutually exclusive", "non_mutually_exclusive")
        combo_mode.addItem("Multi-label", "multi_label")
        labels_by_id = {area.area_id: area.label for area in areas}
        for area_id, column in binary_columns:
            binary_list.addItem(f"{labels_by_id.get(area_id, area_id)}  →  {column}")
        for area in areas:
            item = QtWidgets.QListWidgetItem(area.label)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, area.area_id)
            priority_list.addItem(item)
        if priority_list.count():
            priority_list.setCurrentRow(0)

        def move(delta: int) -> None:
            row = priority_list.currentRow()
            target = row + delta
            if row < 0 or target < 0 or target >= priority_list.count():
                return
            item = priority_list.takeItem(row)
            priority_list.insertItem(target, item)
            priority_list.setCurrentRow(target)
            update_summary()

        def update_summary() -> None:
            mode = str(combo_mode.currentData() or "single_label")
            is_binary = mode == "non_mutually_exclusive"
            show_priority = mode == "single_label" and overlap_count > 0
            label_column.setVisible(not is_binary)
            edit_column.setVisible(not is_binary)
            binary_panel.setVisible(is_binary)
            priority_panel.setVisible(show_priority)
            if mode == "single_label":
                detail = "Each item receives at most one area label."
                if show_priority:
                    detail += " For overlaps, the first matching area above wins."
            elif is_binary:
                detail = "Each area is written as an independent 1/0 metadata column."
            else:
                detail = 'All matching labels are stored with "; " between them.'
            summary.setPlainText(
                f"Areas: {len(areas)}\nOverlapping metadata rows: {overlap_count}\n{detail}"
            )

        accepted_payload: dict[str, object] = {}

        def accept() -> None:
            mode = str(combo_mode.currentData() or "single_label")
            column = edit_column.text().strip()
            if mode != "non_mutually_exclusive" and (
                not column or column.casefold() == "id"
            ):
                summary.setPlainText("Choose an editable metadata column name.")
                return
            candidates = (
                [resolved for _area_id, resolved in binary_columns]
                if mode == "non_mutually_exclusive"
                else [column]
            )
            existing_by_key = {name.casefold(): name for name in existing_columns}
            conflicts = [
                existing_by_key[name.casefold()]
                for name in candidates
                if name.casefold() in existing_by_key
            ]
            if conflicts:
                listed_columns = "\n".join(f"• {name}" for name in conflicts)
                choice = QtWidgets.QMessageBox.question(
                    dialog,
                    "Overwrite Metadata Columns",
                    "Replace all values in the following metadata column(s)?\n\n"
                    f"{listed_columns}",
                    QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                    QtWidgets.QMessageBox.StandardButton.No,
                )
                if choice != QtWidgets.QMessageBox.StandardButton.Yes:
                    return
            accepted_payload.update(
                {
                    "column_name": "" if mode == "non_mutually_exclusive" else column,
                    "mode": mode,
                    "priority_area_ids": [
                        str(priority_list.item(index).data(QtCore.Qt.ItemDataRole.UserRole) or "")
                        for index in range(priority_list.count())
                    ] if mode == "single_label" and overlap_count > 0 else [],
                }
            )
            dialog.accept()

        button_up.clicked.connect(lambda: move(-1))
        button_down.clicked.connect(lambda: move(1))
        combo_mode.currentIndexChanged.connect(lambda _index: update_summary())
        button_box.accepted.connect(accept)
        button_box.rejected.connect(dialog.reject)
        update_summary()
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return None
        return dict(accepted_payload)
