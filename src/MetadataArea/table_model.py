from __future__ import annotations

import html
import re

from PySide6 import QtCore, QtGui, QtWidgets

from MetadataArea.domain import MetadataColumn, MetadataRow, MetadataSearchMatch
from MetadataArea.service import MetadataService


class MetadataTableModel(QtCore.QAbstractTableModel):
    """Read-only table model that pages metadata rows from the project store."""

    sort_order_changed = QtCore.Signal()

    PAGE_SIZE = 200
    MATCH_RANGES_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1
    MARKER_BACKGROUND = "#fff176"
    MARKER_FOREGROUND = "#000000"

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._service: MetadataService | None = None
        self._columns: list[MetadataColumn] = []
        self._row_count = 0
        self._order_by = "id"
        self._descending = False
        self._cache: dict[int, list[MetadataRow]] = {}
        self._match_cells_by_row: dict[int, tuple[int, ...]] = {}
        self._active_match: MetadataSearchMatch | None = None
        self._search_query = ""
        self._search_regex = False
        self._search_match_case = False
        self._search_match_word = False
        self._publication_active = False
        self._pending_page = False

    def set_publication_active(self, active: bool) -> None:
        self._publication_active = active
        if not active and self._pending_page:
            self._pending_page = False
            self._emit_all_data_changed()

    @property
    def order_by(self) -> str:
        return self._order_by

    @property
    def descending(self) -> bool:
        return self._descending

    def set_source(
        self,
        service: MetadataService,
        columns: list[MetadataColumn],
        row_count: int,
    ) -> None:
        self.beginResetModel()
        self._service = service
        self._columns = columns
        self._row_count = row_count
        self._order_by = "id"
        self._descending = False
        self._cache.clear()
        self._match_cells_by_row.clear()
        self._active_match = None
        self._search_query = ""
        self._search_regex = False
        self._search_match_case = False
        self._search_match_word = False
        self.endResetModel()

    def clear(self) -> None:
        self.beginResetModel()
        self._service = None
        self._columns = []
        self._row_count = 0
        self._order_by = "id"
        self._descending = False
        self._cache.clear()
        self._match_cells_by_row.clear()
        self._active_match = None
        self._search_query = ""
        self._search_regex = False
        self._search_match_case = False
        self._search_match_word = False
        self.endResetModel()

    def rowCount(self, parent: QtCore.QModelIndex | QtCore.QPersistentModelIndex = QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else self._row_count

    def columnCount(self, parent: QtCore.QModelIndex | QtCore.QPersistentModelIndex = QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._columns)

    def data(
        self,
        index: QtCore.QModelIndex | QtCore.QPersistentModelIndex,
        role: int = QtCore.Qt.ItemDataRole.DisplayRole,
    ):
        if not index.isValid():
            return None
        if role == self.MATCH_RANGES_ROLE:
            return self._match_ranges_for_index(index)
        if role == QtCore.Qt.ItemDataRole.ToolTipRole:
            value = self._value_for_index(index)
            if value is None:
                return None
            return self._wrapped_tooltip(str(value))
        if role not in (
            QtCore.Qt.ItemDataRole.DisplayRole,
            QtCore.Qt.ItemDataRole.EditRole,
        ):
            return None
        return self._value_for_index(index)

    def headerData(
        self,
        section: int,
        orientation: QtCore.Qt.Orientation,
        role: int = QtCore.Qt.ItemDataRole.DisplayRole,
    ):
        if (
            role == QtCore.Qt.ItemDataRole.TextAlignmentRole
            and orientation == QtCore.Qt.Orientation.Horizontal
        ):
            return QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
        if role != QtCore.Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == QtCore.Qt.Orientation.Horizontal and 0 <= section < len(self._columns):
            if self._columns[section].name == "id":
                return "ID"
            return self._columns[section].name
        if orientation == QtCore.Qt.Orientation.Vertical:
            return str(section + 1)
        return None

    def flags(self, index: QtCore.QModelIndex | QtCore.QPersistentModelIndex) -> QtCore.Qt.ItemFlag:
        return super().flags(index) & ~QtCore.Qt.ItemFlag.ItemIsEditable

    def sort(self, column: int, order: QtCore.Qt.SortOrder = QtCore.Qt.SortOrder.AscendingOrder) -> None:
        if not 0 <= column < len(self._columns):
            return
        self.layoutAboutToBeChanged.emit()
        self._order_by = self._columns[column].name
        self._descending = order == QtCore.Qt.SortOrder.DescendingOrder
        self._cache.clear()
        self.clear_search_matches()
        self.layoutChanged.emit()
        # Rebuild row-offset search results after the view has handled the layout.
        self.sort_order_changed.emit()

    def row_id_at(self, row_index: int) -> int | None:
        row = self._row_at(row_index)
        return None if row is None else row.id

    def column_name_at(self, column_index: int) -> str | None:
        if not 0 <= column_index < len(self._columns):
            return None
        return self._columns[column_index].name

    def refresh_row(self, row_index: int) -> None:
        if row_index < 0 or row_index >= self._row_count or not self._columns:
            return
        page_start = (row_index // self.PAGE_SIZE) * self.PAGE_SIZE
        self._cache.pop(page_start, None)
        self.dataChanged.emit(
            self.index(row_index, 0),
            self.index(row_index, len(self._columns) - 1),
            [
                QtCore.Qt.ItemDataRole.DisplayRole,
                QtCore.Qt.ItemDataRole.EditRole,
                QtCore.Qt.ItemDataRole.ToolTipRole,
                self.MATCH_RANGES_ROLE,
            ],
        )

    def refresh_current_order(self) -> None:
        self.layoutAboutToBeChanged.emit()
        self._cache.clear()
        self.layoutChanged.emit()
        self._emit_all_data_changed()

    def set_search_matches(
        self,
        matches: list[MetadataSearchMatch],
        active_match: MetadataSearchMatch | None,
        query: str,
        regex: bool,
        match_case: bool,
        match_word: bool,
    ) -> None:
        self._match_cells_by_row = {match.row_offset: match.column_indexes for match in matches}
        self._active_match = active_match
        self._search_query = query
        self._search_regex = regex
        self._search_match_case = match_case
        self._search_match_word = match_word
        self._emit_all_data_changed()

    def clear_search_matches(self) -> None:
        if not self._match_cells_by_row and self._active_match is None:
            return
        self._match_cells_by_row.clear()
        self._active_match = None
        self._search_query = ""
        self._search_regex = False
        self._search_match_case = False
        self._search_match_word = False
        self._emit_all_data_changed()

    def _row_at(self, row_index: int) -> MetadataRow | None:
        if self._service is None or row_index < 0 or row_index >= self._row_count:
            return None
        page_start = (row_index // self.PAGE_SIZE) * self.PAGE_SIZE
        if page_start not in self._cache:
            if self._publication_active:
                self._pending_page = True
                return None
            self._cache[page_start] = self._service.fetch_rows(
                order_by=self._order_by,
                descending=self._descending,
                limit=self.PAGE_SIZE,
                offset=page_start,
            )
        page = self._cache[page_start]
        page_index = row_index - page_start
        if page_index >= len(page):
            return None
        return page[page_index]

    def _value_for_index(self, index: QtCore.QModelIndex | QtCore.QPersistentModelIndex) -> str | None:
        row = self._row_at(index.row())
        if row is None or not 0 <= index.column() < len(self._columns):
            return None
        column = self._columns[index.column()]
        if column.name == "id":
            return str(row.id)
        return row.values.get(column.name, "")

    def _match_ranges_for_index(self, index: QtCore.QModelIndex | QtCore.QPersistentModelIndex):
        matching_columns = self._match_cells_by_row.get(index.row(), ())
        if index.column() not in matching_columns:
            return ()
        value = self.data(index, QtCore.Qt.ItemDataRole.DisplayRole)
        if value is None:
            return ()
        return self._match_ranges(str(value))

    def _match_ranges(self, value: str) -> tuple[tuple[int, int], ...]:
        if not self._search_query:
            return ()
        try:
            pattern = self._compile_search_pattern(
                self._search_query,
                regex=self._search_regex,
                match_case=self._search_match_case,
                match_word=self._search_match_word,
            )
        except re.error:
            return ()
        return tuple(
            (match.start(), match.end())
            for match in pattern.finditer(value)
            if match.start() != match.end()
        )

    def _emit_all_data_changed(self) -> None:
        if self._row_count <= 0 or not self._columns:
            return
        top_left = self.index(0, 0)
        bottom_right = self.index(self._row_count - 1, len(self._columns) - 1)
        self.dataChanged.emit(
            top_left,
            bottom_right,
            [self.MATCH_RANGES_ROLE],
        )

    @staticmethod
    def _compile_search_pattern(
        query: str,
        regex: bool,
        match_case: bool,
        match_word: bool,
    ) -> re.Pattern[str]:
        pattern_text = query if regex else re.escape(query)
        if match_word:
            pattern_text = rf"\b(?:{pattern_text})\b"
        flags = 0 if match_case else re.IGNORECASE
        return re.compile(pattern_text, flags)

    @staticmethod
    def _wrapped_tooltip(value: str) -> str:
        escaped = html.escape(value).replace("\n", "<br>")
        return f'<div style="width: 420px; white-space: normal; word-wrap: break-word;">{escaped}</div>'


class MetadataHighlightDelegate(QtWidgets.QStyledItemDelegate):
    """Paints exact search match characters without coloring the whole cell."""

    def paint(
        self,
        painter: QtGui.QPainter,
        option: QtWidgets.QStyleOptionViewItem,
        index: QtCore.QModelIndex | QtCore.QPersistentModelIndex,
    ) -> None:
        ranges = index.data(MetadataTableModel.MATCH_RANGES_ROLE) or ()
        if not ranges:
            super().paint(painter, option, index)
            return

        styled_option = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(styled_option, index)
        text = styled_option.text
        styled_option.text = ""

        style = styled_option.widget.style() if styled_option.widget is not None else QtWidgets.QApplication.style()
        style.drawControl(QtWidgets.QStyle.ControlElement.CE_ItemViewItem, styled_option, painter, styled_option.widget)

        text_rect = style.subElementRect(
            QtWidgets.QStyle.SubElement.SE_ItemViewItemText,
            styled_option,
            styled_option.widget,
        )
        visible_text, visible_ranges, prefix, suffix = self._visible_text_segment(
            text,
            ranges,
            styled_option.fontMetrics,
            text_rect.width(),
        )

        document = QtGui.QTextDocument()
        document.setDefaultFont(styled_option.font)
        document.setDocumentMargin(0)
        foreground = index.data(QtCore.Qt.ItemDataRole.ForegroundRole)
        text_color = (
            foreground.color()
            if isinstance(foreground, QtGui.QBrush)
            else styled_option.palette.color(QtGui.QPalette.ColorRole.Text)
        )
        document.setHtml(
            self._highlighted_html(
                visible_text,
                visible_ranges,
                text_color,
                prefix=prefix,
                suffix=suffix,
            )
        )
        document.setTextWidth(text_rect.width())
        painter.save()
        painter.translate(text_rect.topLeft())
        clip = QtCore.QRectF(0, 0, text_rect.width(), text_rect.height())
        document.drawContents(painter, clip)
        painter.restore()

    @classmethod
    def _highlighted_html(
        cls,
        text: str,
        ranges: tuple[tuple[int, int], ...],
        text_color: QtGui.QColor,
        prefix: str = "",
        suffix: str = "",
    ) -> str:
        fragments: list[str] = []
        cursor = 0
        for start, end in ranges:
            fragments.append(html.escape(text[cursor:start]))
            fragments.append(
                '<span style="'
                f"background-color: {MetadataTableModel.MARKER_BACKGROUND}; "
                f"color: {MetadataTableModel.MARKER_FOREGROUND};"
                '">'
                f"{html.escape(text[start:end])}"
                "</span>"
            )
            cursor = end
        fragments.append(html.escape(text[cursor:]))
        body = f"{html.escape(prefix)}{''.join(fragments)}{html.escape(suffix)}"
        return f'<span style="color: {text_color.name()};">{body}</span>'

    @staticmethod
    def _visible_text_segment(
        text: str,
        ranges: tuple[tuple[int, int], ...],
        metrics: QtGui.QFontMetrics,
        available_width: int,
    ) -> tuple[str, tuple[tuple[int, int], ...], str, str]:
        if not text or not ranges or available_width <= 0:
            return text, ranges, "", ""
        if metrics.horizontalAdvance(text) <= available_width:
            return text, ranges, "", ""

        first_start, first_end = ranges[0]
        ellipsis = "…"
        ellipsis_width = metrics.horizontalAdvance(ellipsis)

        # Shift window toward the first match so highlighted characters stay visible.
        left_budget = max(0, int(available_width * 0.25))
        start_index = first_start
        consumed = 0
        while start_index > 0:
            char_width = metrics.horizontalAdvance(text[start_index - 1])
            if consumed + char_width > left_budget:
                break
            consumed += char_width
            start_index -= 1

        prefix = ellipsis if start_index > 0 else ""
        budget = max(0, available_width - (ellipsis_width if prefix else 0))
        end_index = start_index
        consumed = 0
        while end_index < len(text):
            char_width = metrics.horizontalAdvance(text[end_index])
            if consumed + char_width > budget:
                break
            consumed += char_width
            end_index += 1

        # Ensure the first match always fits in the visible segment.
        if end_index < first_end:
            end_index = first_end

        suffix = ellipsis if end_index < len(text) else ""
        segment = text[start_index:end_index]
        adjusted_ranges = tuple(
            (max(0, start - start_index), min(end_index - start_index, end - start_index))
            for start, end in ranges
            if end > start_index and start < end_index
        )
        return segment, adjusted_ranges, prefix, suffix
