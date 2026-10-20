"""Shared sizing and behavior for panel tables, lists, and trees."""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets


PANEL_ITEM_ROW_HEIGHT = 36


class _MinimumRowHeightDelegate(QtWidgets.QStyledItemDelegate):
    def __init__(self, row_height: int, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self._row_height = max(1, int(row_height))

    def sizeHint(
        self,
        option: QtWidgets.QStyleOptionViewItem,
        index: QtCore.QModelIndex,
    ) -> QtCore.QSize:
        size = super().sizeHint(option, index)
        size.setHeight(max(size.height(), self._row_height))
        return size


def configure_panel_item_view(
    view: QtWidgets.QAbstractItemView,
    *,
    row_height: int = PANEL_ITEM_ROW_HEIGHT,
) -> None:
    """Apply the common minimum row height without changing view styling."""
    height = max(1, int(row_height))
    view.setProperty("panelRowHeight", height)
    delegate = getattr(view, "_eidora_row_height_delegate", None)
    if not isinstance(delegate, _MinimumRowHeightDelegate) or delegate._row_height != height:
        delegate = _MinimumRowHeightDelegate(height, view)
        view.setItemDelegate(delegate)
        view._eidora_row_height_delegate = delegate

    if isinstance(view, QtWidgets.QTableView):
        header = view.verticalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Fixed)
        header.setMinimumSectionSize(height)
        header.setDefaultSectionSize(height)


def minimum_panel_row_size(size: QtCore.QSize) -> QtCore.QSize:
    """Return a row hint that never falls below the shared panel height."""
    return QtCore.QSize(size.width(), max(size.height(), PANEL_ITEM_ROW_HEIGHT))
