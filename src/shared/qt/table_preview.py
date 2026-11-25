"""Display-only continuation rows for bounded table previews."""

from PySide6 import QtCore, QtGui, QtWidgets


def set_preview_continuation(
    table: QtWidgets.QTableWidget | QtGui.QStandardItemModel,
    shown: int,
    total: int,
    identifier_column: int = 0,
) -> None:
    """Replace any previous marker without adding it to the underlying data."""
    table.setRowCount(shown)
    if total <= shown or table.columnCount() == 0:
        return
    table.setRowCount(shown + 1)
    item_type = QtWidgets.QTableWidgetItem if isinstance(table, QtWidgets.QTableWidget) else QtGui.QStandardItem
    for column in range(table.columnCount()):
        item = item_type("..." if column == identifier_column else "")
        item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
        table.setItem(shown, column, item)
    if isinstance(table, QtWidgets.QTableWidget):
        table.setVerticalHeaderItem(shown, QtWidgets.QTableWidgetItem(""))
