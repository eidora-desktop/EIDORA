"""Choose a shared source; persistence belongs to the assignment service."""
from PySide6 import QtWidgets
from AssetsArea.record_link_service import assign_source


def assign_source_dialog(parent, project, record_ids):
    if not record_ids:
        raise ValueError('Select records first.')
    path, _ = QtWidgets.QFileDialog.getOpenFileName(parent, f'Assign source to {len(record_ids):,} records')
    if not path:
        return False
    from MetadataArea.controllers.operation_dialog_controller import run_operation
    run_operation(lambda: assign_source(project, record_ids, path), "Assigning the shared source…")
    return True
