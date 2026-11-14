"""Select source records by their data without exposing internal indexes."""
from PySide6 import QtCore, QtWidgets
from MetadataArea.service import MetadataService
from MetadataArea.table_model import MetadataTableModel
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child
from shared.qt.dialog_windows import install_dialog_window_policy


def select_records(repository):
    dialog = load_ui('MetadataArea', 'ui/record_picker.ui', None)
    dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
    install_dialog_window_policy(QtWidgets.QApplication.instance())
    table = require_child(dialog, QtWidgets.QTableView, 'table_records')
    buttons = require_child(dialog, QtWidgets.QDialogButtonBox, 'buttons_records')
    model = MetadataTableModel(table)
    service = MetadataService(repository)
    model.set_source(service, service.list_columns(), service.count_rows())
    table.setModel(model)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    try:
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return []
        return [model.row_id_at(index.row()) for index in table.selectionModel().selectedRows()]
    finally:
        buttons.accepted.disconnect(dialog.accept)
        buttons.rejected.disconnect(dialog.reject)
