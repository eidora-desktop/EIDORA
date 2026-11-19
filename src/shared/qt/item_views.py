"""Shared sizing and behavior for panel tables, lists, and trees."""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from shared.qt.widgets import delete_removed_widgets


PANEL_ITEM_ROW_HEIGHT = 36


def configure_checkable_item_view(view: QtWidgets.QAbstractItemView) -> None:
    """Keep checkbox painting and hit-testing aligned, including on macOS."""
    # The native macOS item-view style can omit indicators or paint them at
    # the viewport origin. Use one view-local style for drawing and geometry.
    style = QtWidgets.QStyleFactory.create("Fusion")
    style.setParent(view)
    view.setStyle(style)
    # A view created lazily can retain the previous native palette when its
    # style changes. Reapply the active theme after installing the local style.
    from shared.qt.theme import prepare_surface_widgets

    prepare_surface_widgets(view)


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


def clear_list_items(view: QtWidgets.QListWidget) -> None:
    """Keep item wrappers alive until Qt finishes deleting the native rows."""
    # PySide detaches each wrapper before calling QListWidget::clear(). Losing
    # the last reference there can delete a row while the binding walks the list.
    items = [view.item(row) for row in range(view.count())]
    widgets = [widget for item in items if (widget := view.itemWidget(item)) is not None]
    view.clear()
    delete_removed_widgets(view, widgets)
    del items


def minimum_panel_row_size(size: QtCore.QSize) -> QtCore.QSize:
    """Return a row hint that never falls below the shared panel height."""
    return QtCore.QSize(size.width(), max(size.height(), PANEL_ITEM_ROW_HEIGHT))
