from __future__ import annotations

import numpy as np
from PySide6 import QtCore, QtWidgets

from DiscoveryArea.domain import DiscoveryDataset
from DiscoveryArea.service import DiscoveryService
from shared.qt.background_tasks import BackgroundTasks
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class SaveAreaDialogController:
    def exec(
        self,
        *,
        polygon_points: list[list[float]],
        dataset: DiscoveryDataset,
        visibility: np.ndarray | None,
    ) -> tuple[str, list[int]] | None:
        dialog = load_ui("DiscoveryArea", "ui/save_area_dialog.ui", None)
        dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        name = require_child(dialog, QtWidgets.QLineEdit, "edit_area_name")
        count = require_child(dialog, QtWidgets.QLabel, "label_area_count")
        include = require_child(dialog, QtWidgets.QCheckBox, "checkbox_include_filtered")
        buttons = require_child(dialog, QtWidgets.QDialogButtonBox, "button_box_save_area")
        save = buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Save)
        selected: list[int] = []
        filtered: list[int] = []
        ready = False
        count.setText("Counting points…")
        include.setChecked(False)
        include.hide()
        save.setEnabled(False)

        # Freeze the inputs before entering the dialog's nested event loop.
        snapshot = DiscoveryDataset(dataset.coordinates.copy(), dataset.metadata_ids.copy())
        visibility = None if visibility is None else visibility.copy()
        service = DiscoveryService()

        def calculate(_progress):
            return service.polygon_selection_members(
                polygon_points=polygon_points, dataset=snapshot, visibility=visibility,
            )

        def update(*_args):
            if not ready:
                return
            selected_count = len(selected) + (len(filtered) if include.isChecked() else 0)
            count.setText(f"{selected_count:,} points")
            save.setEnabled(bool(name.text().strip()) and selected_count > 0)

        def completed(_revision, result):
            nonlocal selected, filtered, ready
            selected, filtered = result
            ready = True
            include.setVisible(bool(filtered))
            update()

        def failed(_revision, message):
            from shared.diagnostics.service import LOGGER

            LOGGER.warning("Save Area selection failed: %s", message)
            count.setText("Could not count points. Try again.")

        def accept():
            if save.isEnabled():
                dialog.accept()

        runner = BackgroundTasks(dialog)
        connections = [
            (runner.finished, completed), (runner.failed, failed),
            (name.textChanged, update), (include.toggled, update),
            (buttons.accepted, accept), (buttons.rejected, dialog.reject),
        ]
        for signal, slot in connections:
            signal.connect(slot)
        runner.submit(0, calculate)
        name.setFocus()
        try:
            if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
                return None
            members = selected + filtered if include.isChecked() else selected
            return name.text().strip(), members
        finally:
            runner.shutdown()
            for signal, slot in connections:
                signal.disconnect(slot)
