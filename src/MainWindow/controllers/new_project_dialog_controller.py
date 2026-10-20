from pathlib import Path

from PySide6 import QtWidgets

from project.service import ProjectService
from shared.qt.ui_loader import load_ui


def choose_new_project(service: ProjectService) -> tuple[str, Path] | None:
    dialog = load_ui("MainWindow", "ui/new_project.ui", None)
    name = dialog.findChild(QtWidgets.QLineEdit, "edit_name")
    location = dialog.findChild(QtWidgets.QLineEdit, "edit_location")
    destination = dialog.findChild(QtWidgets.QLabel, "label_destination")
    browse = dialog.findChild(QtWidgets.QPushButton, "button_browse")
    buttons = dialog.findChild(QtWidgets.QDialogButtonBox, "buttons")
    buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("Create")
    location.setText(str(service.workspace_root))

    def refresh():
        path = service.available_project_path(name.text(), parent=Path(location.text()))
        destination.setText(f"Project folder: {path}")

    def choose():
        folder = QtWidgets.QFileDialog.getExistingDirectory(dialog, "Project Location", location.text())
        if folder:
            location.setText(folder)
            refresh()

    connections = [(name.textChanged, refresh), (browse.clicked, choose),
                   (buttons.accepted, dialog.accept), (buttons.rejected, dialog.reject)]
    for signal, slot in connections:
        signal.connect(slot)
    refresh()
    try:
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            return name.text(), Path(location.text())
        return None
    finally:
        for signal, slot in connections:
            signal.disconnect(slot)
