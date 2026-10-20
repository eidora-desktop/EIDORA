from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtGui, QtWidgets

from DiscoveryArea.advanced_query import AdvancedQueryError, validate_advanced_query
from DiscoveryArea.filters import (
    AdvancedQueryFilter,
    FilterMatching,
    FilterOperator,
    FilterRule,
    QueryFilter,
    RangeFilter,
    SelectionFilter,
    SliderFilter,
)
from DiscoveryArea.metadata_access import DiscoveryMetadataAccess
from DiscoveryArea.range_slider import RangeSlider, ThresholdSlider
from shared.qt.icons import apply_icon_color
from shared.qt.panel_controller import UiPanelController
from shared.qt.item_views import configure_panel_item_view, minimum_panel_row_size
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child

if TYPE_CHECKING:
    from project.domain import Project


class _DeleteModeDismissFilter(QtCore.QObject):
    def __init__(
        self,
        should_keep_delete_mode: Callable[[QtCore.QObject, QtCore.QEvent], bool],
        dismiss: Callable[[], None],
        parent: QtCore.QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._should_keep_delete_mode = should_keep_delete_mode
        self._dismiss = dismiss

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() != QtCore.QEvent.Type.MouseButtonPress:
            return False
        if self._should_keep_delete_mode(obj, event):
            return False
        self._dismiss()
        return False


class _QueryTokenDragFilter(QtCore.QObject):
    def __init__(
        self,
        token_provider: Callable[[QtWidgets.QAbstractButton], str],
        parent: QtCore.QObject,
    ) -> None:
        super().__init__(parent)
        self._token_provider = token_provider
        self._press_position: QtCore.QPoint | None = None

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if not isinstance(obj, QtWidgets.QAbstractButton):
            return False
        if event.type() == QtCore.QEvent.Type.MouseButtonPress and isinstance(event, QtGui.QMouseEvent):
            if event.button() == QtCore.Qt.MouseButton.LeftButton:
                self._press_position = event.position().toPoint()
            return False
        if event.type() != QtCore.QEvent.Type.MouseMove or not isinstance(event, QtGui.QMouseEvent):
            return False
        if not event.buttons() & QtCore.Qt.MouseButton.LeftButton or self._press_position is None:
            return False
        distance = (event.position().toPoint() - self._press_position).manhattanLength()
        if distance < QtWidgets.QApplication.startDragDistance():
            return False
        token = self._token_provider(obj)
        if not token:
            return False
        mime_data = QtCore.QMimeData()
        mime_data.setText(token)
        drag = QtGui.QDrag(obj)
        drag.setMimeData(mime_data)
        drag.exec(QtCore.Qt.DropAction.CopyAction)
        self._press_position = None
        return True


class _EditorCornerButtonPositioner(QtCore.QObject):
    """Keep a code-owned button anchored inside an editor corner."""

    _MARGIN = 4

    def __init__(
        self,
        editor: QtWidgets.QPlainTextEdit,
        button: QtWidgets.QPushButton,
    ) -> None:
        super().__init__(editor)
        self._editor = editor
        self._button = button
        editor.installEventFilter(self)
        self.position_button()

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if obj is self._editor and event.type() == QtCore.QEvent.Type.Resize:
            self.position_button()
        return False

    def position_button(self) -> None:
        self._button.adjustSize()
        self._button.move(
            max(self._editor.width() - self._button.width() - self._MARGIN, 0),
            max(self._editor.height() - self._button.height() - self._MARGIN, 0),
        )
        self._button.raise_()


class FiltersPanelController(UiPanelController):
    package = "DiscoveryArea"
    ui_path = "ui/filters_panel.ui"
    _ADD_RULE_ITEM_ROLE = int(QtCore.Qt.ItemDataRole.UserRole) + 1
    _SLIDER_POSITION_STEPS = 1_000
    _QUERY_VALUE_BUTTON_NAMES = {
        "button_query_equals",
        "button_query_not_equals",
        "button_query_less",
        "button_query_less_equal",
        "button_query_greater",
        "button_query_greater_equal",
        "button_query_contains",
        "button_query_starts_with",
        "button_query_ends_with",
    }
    _UNQUOTED_METADATA_TYPES = {
        "bool",
        "boolean",
        "decimal",
        "float",
        "int",
        "integer",
        "number",
        "numeric",
    }
    _NUMERIC_METADATA_TYPES = {
        "decimal",
        "double",
        "float",
        "int",
        "integer",
        "number",
        "numeric",
        "percentage",
        "real",
    }
    _NON_NUMERIC_METADATA_TYPES = {
        "bool",
        "boolean",
        "categorical",
        "date",
        "datetime",
        "list",
    }

    def _wire(self) -> None:
        self._list = require_child(self._widget, QtWidgets.QListWidget, "list_filter_rules")
        configure_panel_item_view(self._list)
        self._list.setUniformItemSizes(False)
        self._options_button = require_child(self._widget, QtWidgets.QToolButton, "button_filter_options")
        self._matching_combo = require_child(self._widget, QtWidgets.QComboBox, "combo_matching")
        self._filtered_count_label = require_child(
            self._widget,
            QtWidgets.QLabel,
            "label_filtered",
        )
        self._add_rule_button: QtWidgets.QPushButton | None = None
        self._save_dialog: QtWidgets.QWidget | None = None
        self._delete_mode_dismiss_filter: _DeleteModeDismissFilter | None = None
        self._filtered_opacity_changed_callback = None
        self._hide_filtered_tooltips_changed_callback: Callable[[bool], None] | None = None
        self._filters_changed_callback: Callable[[], None] | None = None
        self._metadata_columns: list[str] = []
        self._metadata_group_columns: set[str] = set()
        self._metadata_field_labels: dict[str, str] = {}
        self._metadata_value_types: dict[str, str] = {}
        self._metadata_distinct_values: dict[str, list[str]] = {}
        self._metadata_numeric_ranges: dict[str, tuple[float, float, bool] | None] = {}
        self._metadata_access: DiscoveryMetadataAccess | None = None
        self._filter_controls_available = True
        self._applied_advanced_query = ""
        self._advanced_query_active = False
        self._paused_simple_apply_buttons: set[QtWidgets.QPushButton] = set()
        self._wire_filter_mode_pages()
        self._wire_advanced_query()
        self._configure_options_menu()
        self._matching_combo.currentIndexChanged.connect(
            lambda _index: self._emit_filters_changed()
        )
        self._hide_filtered_tooltips_button = require_child(
            self._widget,
            QtWidgets.QToolButton,
            "button_hide_tooltip",
        )
        self._hide_filtered_tooltips_button.toggled.connect(
            self._emit_hide_filtered_tooltips_changed
        )
        self._ensure_add_rule_button()
        self._list.currentRowChanged.connect(self._on_current_rule_changed)
        self._filtered_opacity_spins = (
            require_child(
                self._widget,
                QtWidgets.QSpinBox,
                "spin_filtered_point_opacity",
            ),
            require_child(
                self._widget,
                QtWidgets.QSpinBox,
                "spin_filtered_point_opacity_2",
            ),
        )
        for opacity_spin in self._filtered_opacity_spins:
            opacity_spin.valueChanged.connect(
                lambda value, source=opacity_spin: self._sync_filtered_point_opacity(
                    value,
                    source,
                )
            )
        self._sync_filtered_point_opacity(
            self._filtered_opacity_spins[0].value(),
            self._filtered_opacity_spins[0],
            emit_change=False,
        )
        self._disable_rich_marker_filter_controls()
        self._save_preset_callback: Callable[[str, dict], bool] | None = None
        self._load_preset_callback: Callable[[str], dict | None] | None = None
        self._preset_labels: list[str] = []

    def _disable_rich_marker_filter_controls(self) -> None:
        for name in ("panel_filtered_point_size", "panel_filtered_border_width"):
            panel = self._widget.findChild(QtWidgets.QWidget, name)
            if panel is not None:
                panel.setEnabled(False)
                panel.setVisible(False)

    def set_project(self, project) -> None:
        super().set_project(project)
        self._filter_controls_available = project is not None
        self._options_button.setEnabled(project is not None)
        if self._add_rule_button is not None:
            self._add_rule_button.setEnabled(project is not None)
        self._set_filter_controls_enabled(project is not None)
        self._load_metadata_filter_options(project)
        self._refresh_rule_option_widgets()
        self._refresh_advanced_query_fields()
        self._update_advanced_query_validation()

    def set_metadata_access(
        self,
        metadata_access: DiscoveryMetadataAccess | None,
    ) -> None:
        self._metadata_access = metadata_access

    def shutdown(self) -> None:
        self._uninstall_delete_mode_dismiss_filter()
        if self._save_dialog is not None:
            self._save_dialog.close()

    def _wire_filter_mode_pages(self) -> None:
        self._filter_mode_stack = require_child(
            self._widget,
            QtWidgets.QStackedWidget,
            "stack_filter_modes",
        )
        self._simple_filter_page = require_child(
            self._widget,
            QtWidgets.QWidget,
            "page_filter_simple",
        )
        self._advanced_filter_page = require_child(
            self._widget,
            QtWidgets.QWidget,
            "page_filter_advanced",
        )
        self._simple_filter_button = require_child(
            self._widget,
            QtWidgets.QAbstractButton,
            "button_filter_simple",
        )
        self._advanced_filter_button = require_child(
            self._widget,
            QtWidgets.QAbstractButton,
            "button_filter_advanced",
        )
        self._filter_mode_button_group = QtWidgets.QButtonGroup(self._widget)
        self._filter_mode_button_group.setExclusive(True)
        self._filter_mode_button_group.addButton(self._simple_filter_button)
        self._filter_mode_button_group.addButton(self._advanced_filter_button)
        self._simple_filter_button.clicked.connect(
            lambda _checked=False: self._set_filter_mode("simple")
        )
        self._advanced_filter_button.clicked.connect(
            lambda _checked=False: self._set_filter_mode("advanced")
        )
        self._set_filter_mode("simple", emit_change=False)

    def _wire_advanced_query(self) -> None:
        self._advanced_query_editor = require_child(
            self._widget,
            QtWidgets.QPlainTextEdit,
            "edit_advanced_query",
        )
        self._advanced_clear_button = QtWidgets.QPushButton(
            "clear",
            self._advanced_query_editor,
        )
        self._advanced_clear_button.setObjectName("button_clear_advanced_query")
        self._advanced_clear_button_positioner = _EditorCornerButtonPositioner(
            self._advanced_query_editor,
            self._advanced_clear_button,
        )
        self._advanced_query_status = self._widget.findChild(
            QtWidgets.QLabel,
            "label_advanced_query_status",
        )
        self._advanced_field_combo = require_child(
            self._widget,
            QtWidgets.QComboBox,
            "combo_query_field",
        )
        self._advanced_field_button = require_child(
            self._widget,
            QtWidgets.QPushButton,
            "button_query_insert_field",
        )
        self._advanced_value_combo = require_child(
            self._widget,
            QtWidgets.QComboBox,
            "combo_query_value",
        )
        self._advanced_value_button = require_child(
            self._widget,
            QtWidgets.QPushButton,
            "button_query_insert_value",
        )
        self._advanced_value_combo.setEnabled(False)
        self._advanced_value_button.setEnabled(False)
        self._advanced_apply_button = require_child(
            self._widget,
            QtWidgets.QPushButton,
            "button_apply_advanced_query",
        )
        self._query_token_buttons = [
            button
            for button in self._advanced_filter_page.findChildren(QtWidgets.QPushButton)
            if button.property("queryToken") is not None
        ]
        for button in self._query_token_buttons:
            button.setToolTip("Click to insert, or drag into the query editor")
            button.clicked.connect(
                lambda _checked=False, source=button: self._insert_query_token(
                    self._query_token_for_button(source)
                )
            )
        self._advanced_field_button.clicked.connect(
            lambda _checked=False: self._insert_query_token(
                self._query_token_for_button(self._advanced_field_button)
            )
        )
        self._advanced_field_combo.currentTextChanged.connect(
            lambda _text: self._refresh_advanced_query_values()
        )
        self._advanced_value_button.clicked.connect(self._insert_advanced_query_value)
        self._query_token_drag_filter = _QueryTokenDragFilter(
            self._query_token_for_button,
            self._widget,
        )
        for button in [*self._query_token_buttons, self._advanced_field_button]:
            button.installEventFilter(self._query_token_drag_filter)
        self._advanced_query_editor.textChanged.connect(
            self._update_advanced_query_validation
        )
        self._advanced_query_editor.textChanged.connect(
            self._update_advanced_query_value_controls
        )
        self._advanced_query_editor.cursorPositionChanged.connect(
            self._update_advanced_query_value_controls
        )
        self._advanced_apply_button.clicked.connect(self._apply_advanced_query)
        self._advanced_clear_button.clicked.connect(self._clear_advanced_query)
        self._refresh_advanced_query_fields()
        self._refresh_advanced_query_values()
        self._update_advanced_query_validation()

    def filter_mode(self) -> str:
        return (
            "advanced"
            if self._filter_mode_stack.currentWidget() is self._advanced_filter_page
            else "simple"
        )

    def _set_filter_mode(self, mode: str, *, emit_change: bool = True) -> None:
        advanced = str(mode).strip().casefold() == "advanced"
        page = self._advanced_filter_page if advanced else self._simple_filter_page
        button = self._advanced_filter_button if advanced else self._simple_filter_button
        changed = self._filter_mode_stack.currentWidget() is not page
        if changed:
            self._deactivate_filter_mode(self.filter_mode())
        self._filter_mode_stack.setCurrentWidget(page)
        button.setChecked(True)
        if changed:
            self._reactivate_filter_mode(mode)
        if changed and emit_change:
            self._emit_filters_changed()

    def _deactivate_filter_mode(self, mode: str) -> None:
        if mode == "advanced":
            self._advanced_query_active = False
            return
        self._paused_simple_apply_buttons.clear()
        for widget in self._iter_rule_widgets():
            apply_button = widget.findChild(QtWidgets.QPushButton, "button_apply_element")
            if apply_button is not None and apply_button.isChecked():
                self._paused_simple_apply_buttons.add(apply_button)
                apply_button.setChecked(False)
                self._update_rule_apply_button_icon(apply_button)

    def _reactivate_filter_mode(self, mode: str) -> None:
        if mode == "advanced":
            self._advanced_query_active = bool(self._applied_advanced_query)
            self._update_advanced_query_validation()
            return
        for apply_button in self._paused_simple_apply_buttons:
            if apply_button.parentWidget() is not None:
                apply_button.setChecked(True)
                self._update_rule_apply_button_icon(apply_button)
        self._paused_simple_apply_buttons.clear()

    def _query_token_for_button(self, button: QtWidgets.QAbstractButton) -> str:
        if button is self._advanced_field_button:
            field = self._advanced_field_combo.currentText().strip()
            return f"[{field}]" if field else ""
        token = str(button.property("queryToken") or "")
        button_name = button.objectName()
        if button_name == "button_query_parenthesis":
            return "()"
        if button_name == "button_query_in":
            return ' IN ("")' if self._query_field_uses_text_values() else " IN ()"
        if button_name in self._QUERY_VALUE_BUTTON_NAMES and self._query_field_uses_text_values():
            return f'{token}""'
        return token

    def _query_field_uses_text_values(self) -> bool:
        field = self._advanced_field_combo.currentText().strip()
        if not field:
            return False
        value_type = self._metadata_value_types.get(field, "").strip().casefold()
        if value_type in self._UNQUOTED_METADATA_TYPES:
            return False
        values = self._metadata_values_for_column(field)
        if values:
            return self._numeric_range_for_column(field, values) is None
        return True

    def _insert_query_token(self, token: str) -> None:
        if not token:
            return
        cursor = self._advanced_query_editor.textCursor()
        cursor.insertText(token)
        empty_text_position = token.rfind('""')
        if empty_text_position >= 0:
            cursor.movePosition(
                QtGui.QTextCursor.MoveOperation.Left,
                QtGui.QTextCursor.MoveMode.MoveAnchor,
                len(token) - empty_text_position - 1,
            )
        elif token.endswith("()"):
            cursor.movePosition(QtGui.QTextCursor.MoveOperation.Left)
        self._advanced_query_editor.setTextCursor(cursor)
        self._advanced_query_editor.setFocus()

    def _refresh_advanced_query_fields(self) -> None:
        current = str(
            self._advanced_field_combo.currentData()
            or self._advanced_field_combo.currentText()
            or ""
        )
        with QtCore.QSignalBlocker(self._advanced_field_combo):
            self._advanced_field_combo.clear()
            self._add_metadata_field_options(self._advanced_field_combo)
            index = self._advanced_field_combo.findData(current)
            if index >= 0:
                self._advanced_field_combo.setCurrentIndex(index)
        self._advanced_field_button.setEnabled(
            self._filter_controls_available and self._advanced_field_combo.count() > 0
        )
        self._refresh_advanced_query_values()

    def _refresh_advanced_query_values(self) -> None:
        current = self._advanced_value_combo.currentText()
        field = self._advanced_field_combo.currentText().strip()
        values = (
            [value for value in self._metadata_values_for_column(field) if value != ""]
            if field and self._query_field_uses_text_values()
            else []
        )
        with QtCore.QSignalBlocker(self._advanced_value_combo):
            self._advanced_value_combo.clear()
            self._advanced_value_combo.addItems(values)
            index = self._advanced_value_combo.findText(current)
            if index >= 0:
                self._advanced_value_combo.setCurrentIndex(index)
        self._update_advanced_query_value_controls()

    def _update_advanced_query_value_controls(self) -> None:
        enabled = (
            self._filter_controls_available
            and self._advanced_value_combo.count() > 0
            and self._advanced_query_accepts_value_at_cursor()
        )
        self._advanced_value_combo.setEnabled(enabled)
        self._advanced_value_button.setEnabled(enabled)

    def _advanced_query_accepts_value_at_cursor(self) -> bool:
        if not self._query_field_uses_text_values():
            return False
        if self._quoted_query_value_bounds() is not None:
            return True
        cursor = self._advanced_query_editor.textCursor()
        before_cursor = self._advanced_query_editor.toPlainText()[: cursor.selectionStart()]
        return bool(
            re.search(
                r"(?:==|!=|<=|>=|<|>|CONTAINS|STARTS\s+WITH|ENDS\s+WITH)\s*$",
                before_cursor,
                flags=re.IGNORECASE,
            )
            or re.search(r"\bIN\s*\(\s*$", before_cursor, flags=re.IGNORECASE)
        )

    def _quoted_query_value_bounds(self) -> tuple[int, int] | None:
        text = self._advanced_query_editor.toPlainText()
        position = self._advanced_query_editor.textCursor().selectionStart()
        quote_start: int | None = None
        escaped = False
        for index, character in enumerate(text):
            if quote_start is None:
                if character == '"':
                    quote_start = index
                continue
            if character == '"' and not escaped:
                if quote_start < position <= index:
                    return quote_start, index + 1
                quote_start = None
            escaped = character == "\\" and not escaped
            if character != "\\":
                escaped = False
        return None

    def _insert_advanced_query_value(self) -> None:
        if not self._advanced_value_button.isEnabled():
            return
        value = self._advanced_value_combo.currentText()
        encoded = json.dumps(value, ensure_ascii=False)
        cursor = self._advanced_query_editor.textCursor()
        bounds = self._quoted_query_value_bounds()
        if bounds is not None:
            cursor.setPosition(bounds[0])
            cursor.setPosition(
                bounds[1],
                QtGui.QTextCursor.MoveMode.KeepAnchor,
            )
        cursor.insertText(encoded)
        self._advanced_query_editor.setTextCursor(cursor)
        self._advanced_query_editor.setFocus()
        self._update_advanced_query_value_controls()

    def _update_advanced_query_validation(self) -> None:
        expression = self._advanced_query_editor.toPlainText().strip()
        if not expression:
            self._set_advanced_query_status("Build a query, then apply it.")
            self._advanced_apply_button.setEnabled(False)
            return
        try:
            validate_advanced_query(expression)
        except AdvancedQueryError as exc:
            suffix = " The previously applied query remains active." if self._applied_advanced_query else ""
            self._set_advanced_query_status(
                f"Error at character {exc.position + 1}: {exc.message}{suffix}"
            )
            self._advanced_apply_button.setEnabled(False)
            return
        if expression == self._applied_advanced_query and self._advanced_query_active:
            self._set_advanced_query_status("Query is valid and applied.")
            self._advanced_apply_button.setEnabled(False)
            return
        self._set_advanced_query_status("Query is valid. Apply it to update the filter.")
        self._advanced_apply_button.setEnabled(self._filter_controls_available)

    def _set_advanced_query_status(self, message: str) -> None:
        self._advanced_query_editor.setToolTip(message)
        self._advanced_apply_button.setToolTip(message)
        if self._advanced_query_status is not None:
            self._advanced_query_status.setText(message)

    def _apply_advanced_query(self) -> None:
        expression = self._advanced_query_editor.toPlainText().strip()
        try:
            validate_advanced_query(expression)
        except AdvancedQueryError:
            self._update_advanced_query_validation()
            return
        self._applied_advanced_query = expression
        self._advanced_query_active = True
        self._set_filter_mode("advanced", emit_change=False)
        self._update_advanced_query_validation()
        self._emit_filters_changed()
        self._show_status("Advanced filter query applied.")

    def _clear_advanced_query(self) -> None:
        had_applied_query = bool(self._applied_advanced_query)
        self._applied_advanced_query = ""
        self._advanced_query_active = False
        self._advanced_query_editor.clear()
        self._update_advanced_query_validation()
        if had_applied_query and self.filter_mode() == "advanced":
            self._emit_filters_changed()
        self._show_status("Advanced filter query cleared.")

    def _show_methods_report(self) -> None:
        from DiscoveryArea.settings_provenance import settings_report
        from DiscoveryArea.snapshot_provenance import SnapshotMethodsService
        from shared.methods_report import combine_reports
        from shared.qt.methods_citations_dialog import MethodsCitationsDialogController
        state = self.filter_state_payload()
        reports = [settings_report("Filters", filters=state)]
        if self._project is not None:
            reports.extend(SnapshotMethodsService.clustering_reports(self._project, [state]))
        MethodsCitationsDialogController().exec(combine_reports("Filters", reports))

    def _configure_options_menu(self) -> None:
        menu = QtWidgets.QMenu(self._options_button)
        self._load_menu = menu.addMenu("Load Recent")
        menu.addAction("Save Preset…").triggered.connect(self._save_current_preset)
        menu.addAction("Citations…").triggered.connect(self._show_methods_report)
        menu.addSeparator()
        menu.addAction("Add Selection Filter").triggered.connect(lambda: self._add_filter_rule("selection"))
        menu.addAction("Add Slider Filter").triggered.connect(lambda: self._add_filter_rule("slider"))
        menu.addAction("Add Range Filter").triggered.connect(
            lambda: self._add_filter_rule("range")
        )
        menu.addSeparator()
        menu.addAction("Remove…").triggered.connect(self._toggle_delete_mode)
        self._options_button.setMenu(menu)
        self._options_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self._options_button.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
        self._filter_menu = menu
        self._delete_mode = False
        self.set_recent_preset_labels([])

    def _ensure_add_rule_button(self) -> None:
        existing_row = self._add_rule_button_row()
        if existing_row >= 0:
            if existing_row != self._list.count() - 1:
                item = self._list.takeItem(existing_row)
                if item is not None:
                    self._list.addItem(item)
            return

        row = QtWidgets.QWidget(self._list)
        row.setObjectName("row_add_filter_rule")
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        button = QtWidgets.QPushButton(row)
        button.setObjectName("button_add_filter_rule")
        button.setText("add filter")
        button.setToolTip("Add filter rule")
        button.setMenu(self._build_add_filter_menu(button))
        layout.addStretch(1)
        layout.addWidget(button)
        layout.addStretch(1)

        item = QtWidgets.QListWidgetItem()
        item.setData(self._ADD_RULE_ITEM_ROLE, True)
        item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
        item.setSizeHint(self._item_size_hint_for_widget(row))
        self._list.addItem(item)
        self._list.setItemWidget(item, row)
        self._add_rule_button = button

    def _build_add_filter_menu(self, parent: QtWidgets.QWidget) -> QtWidgets.QMenu:
        menu = QtWidgets.QMenu(parent)
        menu.addAction("Add Selection Filter").triggered.connect(
            lambda: self._add_filter_rule("selection")
        )
        menu.addAction("Add Slider Filter").triggered.connect(
            lambda: self._add_filter_rule("slider")
        )
        menu.addAction("Add Range Filter").triggered.connect(
            lambda: self._add_filter_rule("range")
        )
        menu.addSeparator()
        menu.addAction("Remove…").triggered.connect(self._toggle_delete_mode)
        return menu

    def _add_rule_button_row(self) -> int:
        for row in range(self._list.count()):
            if self._is_add_rule_item(self._list.item(row)):
                return row
        return -1

    def _is_add_rule_item(self, item: QtWidgets.QListWidgetItem | None) -> bool:
        return item is not None and bool(item.data(self._ADD_RULE_ITEM_ROLE))

    def _filter_rule_count(self) -> int:
        return sum(1 for row in range(self._list.count()) if not self._is_add_rule_item(self._list.item(row)))

    def _iter_rule_widgets(self):
        for row in range(self._list.count()):
            item = self._list.item(row)
            if self._is_add_rule_item(item):
                continue
            widget = self._list.itemWidget(item)
            if widget is not None:
                yield widget

    def _clear_filter_rules(self) -> None:
        self._list.clear()
        self._add_rule_button = None
        self._ensure_add_rule_button()

    def _item_size_hint_for_widget(self, widget: QtWidgets.QWidget) -> QtCore.QSize:
        return minimum_panel_row_size(
            widget.sizeHint().expandedTo(widget.minimumSizeHint()).expandedTo(widget.minimumSize())
        )

    def _add_filter_rule(self, kind: str, *, switch_to_simple: bool = True) -> QtWidgets.QWidget:
        if switch_to_simple:
            self._set_filter_mode("simple")
        paths = {
            "selection": "ui/filter_rule_selection.ui",
            "query": "ui/filter_rule_query.ui",
            "slider": "ui/filter_rule_slider.ui",
            "range": "ui/filter_rule_range.ui",
        }
        path = paths[kind]
        rule = load_ui(self.package, path, self._widget)
        if kind == "slider":
            self._install_threshold_slider(rule)
        elif kind == "range":
            self._install_range_slider(rule)
        self._populate_rule(rule)
        apply_button = rule.findChild(QtWidgets.QPushButton, "button_apply_element")
        if apply_button is not None:
            apply_button.setCheckable(True)
            self._update_rule_apply_button_icon(apply_button)
            apply_button.clicked.connect(
                lambda _checked=False, button=apply_button, widget=rule: self._on_rule_button_clicked(button, widget)
            )
        item = QtWidgets.QListWidgetItem()
        item.setSizeHint(self._item_size_hint_for_widget(rule))
        insert_row = self._add_rule_button_row()
        if insert_row < 0:
            insert_row = self._list.count()
        self._list.insertItem(insert_row, item)
        self._list.setItemWidget(item, rule)
        self._ensure_add_rule_button()
        self._show_status(f"Added {kind} filter placeholder.")
        return rule

    @staticmethod
    def _install_threshold_slider(rule: QtWidgets.QWidget) -> None:
        placeholder = rule.findChild(QtWidgets.QSlider, "slider_filter_value")
        if placeholder is None or isinstance(placeholder, ThresholdSlider):
            return
        layout = placeholder.parentWidget().layout() if placeholder.parentWidget() else None
        if layout is None:
            return
        slider = ThresholdSlider(placeholder.parentWidget())
        slider.setObjectName(placeholder.objectName())
        slider.setSizePolicy(placeholder.sizePolicy())
        slider.setMinimumSize(placeholder.minimumSize())
        slider.setMaximumSize(placeholder.maximumSize())
        slider.setTickPosition(placeholder.tickPosition())
        slider.setTickInterval(placeholder.tickInterval())
        slider.setSingleStep(placeholder.singleStep())
        slider.setPageStep(placeholder.pageStep())
        layout.replaceWidget(placeholder, slider)
        placeholder.setObjectName("")
        placeholder.deleteLater()

    @staticmethod
    def _install_range_slider(rule: QtWidgets.QWidget) -> None:
        placeholder = rule.findChild(QtWidgets.QSlider, "slider_filter_value_range")
        if placeholder is None or isinstance(placeholder, RangeSlider):
            return
        layout = placeholder.parentWidget().layout() if placeholder.parentWidget() else None
        if layout is None:
            return
        slider = RangeSlider(placeholder.parentWidget())
        slider.setObjectName(placeholder.objectName())
        slider.setSizePolicy(placeholder.sizePolicy())
        slider.setMinimumSize(placeholder.minimumSize())
        slider.setMaximumSize(placeholder.maximumSize())
        slider.setTickPosition(placeholder.tickPosition())
        slider.setTickInterval(placeholder.tickInterval())
        slider.setSingleStep(placeholder.singleStep())
        slider.setPageStep(placeholder.pageStep())
        layout.replaceWidget(placeholder, slider)
        placeholder.setObjectName("")
        placeholder.deleteLater()

    def _populate_rule(self, rule: QtWidgets.QWidget) -> None:
        column_combo = rule.findChild(QtWidgets.QComboBox, "combo_column")
        if column_combo is not None:
            column_combo.clear()
            column_combo.addItem("None", "")
            self._add_metadata_field_options(
                column_combo,
                numeric_only=rule.objectName() in {
                    "row_filter_slider",
                    "row_filter_range",
                },
            )
            column_combo.currentTextChanged.connect(
                lambda _text, widget=rule: self._update_rule_value_options(
                    widget,
                    reset_slider_value=True,
                )
            )
            column_combo.currentTextChanged.connect(lambda _text, widget=rule: self._emit_filters_changed_for_active_rule(widget))
        operator_combo = rule.findChild(QtWidgets.QComboBox, "combo_operator")
        if operator_combo is not None:
            operator_combo.clear()
            operator_combo.addItems(["is", "is not", "contains"])
            operator_combo.currentTextChanged.connect(lambda _text, widget=rule: self._emit_filters_changed_for_active_rule(widget))
        value_combo = rule.findChild(QtWidgets.QComboBox, "combo_value_selection")
        if value_combo is not None:
            value_combo.clear()
            value_combo.setEnabled(False)
            value_combo.currentTextChanged.connect(lambda _text, widget=rule: self._emit_filters_changed_for_active_rule(widget))
        query_edit = rule.findChild(QtWidgets.QLineEdit, "edit_query_text")
        if query_edit is not None:
            query_edit.textChanged.connect(lambda _text, widget=rule: self._emit_filters_changed_for_active_rule(widget))
        min_max_combo = rule.findChild(QtWidgets.QComboBox, "combo_min_max")
        if min_max_combo is not None and min_max_combo.count() == 0:
            min_max_combo.addItems(["min", "max", "exact"])
        if min_max_combo is not None:
            min_max_combo.currentTextChanged.connect(
                lambda _text, widget=rule: self._update_slider_filter_appearance(
                    widget,
                )
            )
            min_max_combo.currentTextChanged.connect(lambda _text, widget=rule: self._emit_filters_changed_for_active_rule(widget))
            self._update_slider_filter_appearance(rule)
        slider = rule.findChild(QtWidgets.QSlider, "slider_filter_value")
        integer_spin = rule.findChild(QtWidgets.QSpinBox, "spin_filter_value")
        double_spin = rule.findChild(
            QtWidgets.QDoubleSpinBox,
            "spin_filter_value_double",
        )
        if slider is not None and integer_spin is not None and double_spin is not None:
            slider.valueChanged.connect(
                lambda value, widget=rule: self._set_slider_filter_value_from_position(
                    widget,
                    value,
                )
            )
            integer_spin.valueChanged.connect(
                lambda value, widget=rule: self._set_slider_filter_position_from_value(
                    widget,
                    value,
                )
            )
            double_spin.valueChanged.connect(
                lambda value, widget=rule: self._set_slider_filter_position_from_value(
                    widget,
                    value,
                )
            )
        range_slider = rule.findChild(RangeSlider, "slider_filter_value_range")
        range_integer_min = rule.findChild(QtWidgets.QSpinBox, "spin_filter_value_min")
        range_integer_max = rule.findChild(QtWidgets.QSpinBox, "spin_filter_value_max")
        range_double_min = rule.findChild(
            QtWidgets.QDoubleSpinBox,
            "spin_filter_range_min_double",
        )
        range_double_max = rule.findChild(
            QtWidgets.QDoubleSpinBox,
            "spin_filter_range_max_double",
        )
        if (
            range_slider is not None
            and range_integer_min is not None
            and range_integer_max is not None
            and range_double_min is not None
            and range_double_max is not None
        ):
            range_slider.valuesChanged.connect(
                lambda lower, upper, widget=rule: self._set_range_filter_values_from_positions(
                    widget,
                    lower,
                    upper,
                )
            )
            for spin, endpoint in (
                (range_integer_min, "lower"),
                (range_integer_max, "upper"),
                (range_double_min, "lower"),
                (range_double_max, "upper"),
            ):
                spin.valueChanged.connect(
                    lambda value, side=endpoint, widget=rule: self._set_range_filter_position_from_value(
                        widget,
                        side,
                        value,
                    )
                )
        self._update_rule_value_options(rule)

    def _on_current_rule_changed(self, row: int) -> None:
        if row >= 0:
            self._show_status(f"Selected filter row {row + 1}.")

    def _set_filter_controls_enabled(self, enabled: bool) -> None:
        self._matching_combo.setEnabled(enabled)
        self._advanced_query_editor.setEnabled(enabled)
        self._advanced_field_combo.setEnabled(enabled)
        self._advanced_field_button.setEnabled(enabled)
        self._advanced_clear_button.setEnabled(enabled)
        for button in self._query_token_buttons:
            button.setEnabled(enabled)
        for widget_name in (
            "spin_filtered_point_size",
            "spin_border_width",
            "spin_filtered_point_opacity",
            "spin_filtered_point_opacity_2",
        ):
            widget = self._widget.findChild(QtWidgets.QSpinBox, widget_name)
            if widget is not None:
                widget.setEnabled(enabled)

    def _save_current_preset(self) -> None:
        if self._save_preset_callback is None:
            return
        label, ok = QtWidgets.QInputDialog.getText(self._widget, "Save Filter Preset", "Preset name:")
        if not ok:
            return
        name = str(label).strip()
        if not name:
            return
        payload = self.filter_state_payload()
        if self._save_preset_callback(name, payload):
            self._show_status(f"Saved filter preset: {name}")

    def _toggle_delete_mode(self) -> None:
        if self._filter_rule_count() == 0:
            self._show_status("No filter elements available to delete.")
            return
        self._set_delete_mode(not self._delete_mode)
        self._show_status("Filter delete mode enabled." if self._delete_mode else "Filter delete mode disabled.")

    def _set_delete_mode(self, enabled: bool) -> None:
        if self._delete_mode == enabled:
            return
        self._delete_mode = enabled
        for widget in self._iter_rule_widgets():
            button = widget.findChild(QtWidgets.QPushButton, "button_apply_element") if widget is not None else None
            if button is not None:
                self._update_rule_apply_button_icon(button)
        if self._delete_mode:
            self._install_delete_mode_dismiss_filter()
        else:
            self._uninstall_delete_mode_dismiss_filter()

    def _install_delete_mode_dismiss_filter(self) -> None:
        app = QtWidgets.QApplication.instance()
        if app is None or self._delete_mode_dismiss_filter is not None:
            return
        self._delete_mode_dismiss_filter = _DeleteModeDismissFilter(
            self._is_delete_mode_remove_button_target,
            self._dismiss_delete_mode_from_outside_click,
            self._widget,
        )
        app.installEventFilter(self._delete_mode_dismiss_filter)

    def _uninstall_delete_mode_dismiss_filter(self) -> None:
        app = QtWidgets.QApplication.instance()
        if app is not None and self._delete_mode_dismiss_filter is not None:
            app.removeEventFilter(self._delete_mode_dismiss_filter)
        self._delete_mode_dismiss_filter = None

    def _dismiss_delete_mode_from_outside_click(self) -> None:
        if not self._delete_mode:
            return
        self._set_delete_mode(False)
        self._show_status("Filter delete mode disabled.")

    def _is_delete_mode_remove_button_target(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if isinstance(obj, QtWidgets.QWidget) and self._is_remove_button_or_child(obj):
            return True
        if isinstance(event, QtGui.QMouseEvent):
            if isinstance(obj, QtWidgets.QWidget):
                position = event.position().toPoint()
                if self._is_remove_button_at_widget_position(obj, position):
                    return True
            global_position = event.globalPosition().toPoint()
            widget_at_position = QtWidgets.QApplication.widgetAt(global_position)
            if widget_at_position is not None and self._is_remove_button_or_child(widget_at_position):
                return True
        return False

    def _is_remove_button_at_widget_position(self, widget: QtWidgets.QWidget, position: QtCore.QPoint) -> bool:
        if widget is self._list.viewport():
            item = self._list.itemAt(position)
            rule = self._list.itemWidget(item) if item is not None else None
            if rule is None:
                return False
            rule_position = rule.mapFrom(self._list.viewport(), position)
            child = rule.childAt(rule_position)
            return child is not None and self._is_remove_button_or_child(child)

        child = widget.childAt(position)
        return child is not None and self._is_remove_button_or_child(child)

    def _is_remove_button_or_child(self, widget: QtWidgets.QWidget) -> bool:
        current: QtWidgets.QWidget | None = widget
        remove_buttons = {
            rule.findChild(QtWidgets.QPushButton, "button_apply_element")
            for rule in self._iter_rule_widgets()
        }
        remove_buttons.discard(None)
        while current is not None:
            if current in remove_buttons:
                return True
            current = current.parentWidget()
        return False

    def _on_rule_button_clicked(self, button: QtWidgets.QPushButton, widget: QtWidgets.QWidget) -> None:
        if self._delete_mode:
            for row in range(self._list.count()):
                item = self._list.item(row)
                if self._list.itemWidget(item) is widget:
                    self._list.takeItem(row)
                    self._show_status("Deleted filter row.")
                    break
            if self._filter_rule_count() == 0:
                self._set_delete_mode(False)
            else:
                for item_widget in self._iter_rule_widgets():
                    item_button = (
                        item_widget.findChild(QtWidgets.QPushButton, "button_apply_element") if item_widget is not None else None
                    )
                    if item_button is not None:
                        self._update_rule_apply_button_icon(item_button)
            self._emit_filters_changed()
            return
        self._update_rule_apply_button_icon(button)
        self._show_status("Filter rule applied." if button.isChecked() else "Filter rule disabled.")
        self._emit_filters_changed()

    def _update_rule_apply_button_icon(self, button: QtWidgets.QPushButton) -> None:
        icon_name = "close" if self._delete_mode else ("animation_pause" if button.isChecked() else "animation_play")
        button.setText("")
        button.setProperty("iconName", icon_name)
        if self._delete_mode:
            button.setProperty("iconColorOverride", "#ffffff")
            button.setStyleSheet(
                "QPushButton {"
                " background-color: #c62828;"
                " border-color: #8e0000;"
                " color: #ffffff;"
                "}"
                "QPushButton:hover { background-color: #b71c1c; }"
                "QPushButton:pressed { background-color: #8e0000; }"
            )
            apply_icon_color(button, "#ffffff")
        else:
            button.setProperty("iconColorOverride", None)
            button.setStyleSheet("")
            apply_icon_color(button)

    def set_filtered_opacity_changed_callback(self, callback) -> None:
        self._filtered_opacity_changed_callback = callback

    def set_hide_filtered_tooltips_changed_callback(
        self,
        callback: Callable[[bool], None] | None,
    ) -> None:
        self._hide_filtered_tooltips_changed_callback = callback

    def hide_filtered_tooltips(self) -> bool:
        return self._hide_filtered_tooltips_button.isChecked()

    def _emit_hide_filtered_tooltips_changed(self, checked: bool) -> None:
        if self._hide_filtered_tooltips_changed_callback is not None:
            self._hide_filtered_tooltips_changed_callback(bool(checked))

    def filtered_opacity(self) -> float:
        return max(0.0, min(1.0, float(self._filtered_opacity_spins[0].value()) / 100.0))

    def filtered_point_size_factor(self) -> float:
        return 1.0

    def filtered_border_width_factor(self) -> float:
        return 1.0

    def _emit_filtered_opacity_changed(self) -> None:
        if self._filtered_opacity_changed_callback is not None:
            self._filtered_opacity_changed_callback(self.filtered_opacity())
        self._emit_filters_changed()

    def _sync_filtered_point_opacity(
        self,
        value: int,
        source: QtWidgets.QSpinBox,
        *,
        emit_change: bool = True,
    ) -> None:
        for opacity_spin in self._filtered_opacity_spins:
            if opacity_spin is source:
                continue
            with QtCore.QSignalBlocker(opacity_spin):
                opacity_spin.setValue(value)
        if emit_change:
            self._emit_filtered_opacity_changed()

    def set_filters_changed_callback(self, callback: Callable[[], None] | None) -> None:
        self._filters_changed_callback = callback

    def set_filtered_item_counts(self, visible: int, total: int) -> None:
        total_count = max(0, int(total))
        visible_count = min(max(0, int(visible)), total_count)
        self._filtered_count_label.setText(f"{visible_count}/{total_count}")

    def active_filters(self) -> list[FilterRule]:
        if self.filter_mode() == "advanced":
            return (
                [AdvancedQueryFilter(self._applied_advanced_query)]
                if self._advanced_query_active and self._applied_advanced_query
                else []
            )
        filters: list[FilterRule] = []
        for widget in self._iter_rule_widgets():
            apply_button = widget.findChild(QtWidgets.QPushButton, "button_apply_element")
            if apply_button is None or not apply_button.isChecked():
                continue
            kind = widget.objectName()
            column_combo = widget.findChild(QtWidgets.QComboBox, "combo_column")
            if column_combo is None:
                continue
            column = str(column_combo.currentData() or column_combo.currentText() or "").strip()
            if not column or column.lower() == "none":
                continue
            if kind == "row_filter_selection":
                operator = self._operator_from_combo(widget.findChild(QtWidgets.QComboBox, "combo_operator"))
                value_combo = widget.findChild(QtWidgets.QComboBox, "combo_value_selection")
                value = str(value_combo.currentText() if value_combo is not None else "")
                filters.append(SelectionFilter(column=column, operator=operator, value=value))
            elif kind == "row_filter_query":
                operator = self._operator_from_combo(widget.findChild(QtWidgets.QComboBox, "combo_operator"))
                edit = widget.findChild(QtWidgets.QLineEdit, "edit_query_text")
                query = str(edit.text() if edit is not None else "")
                filters.append(QueryFilter(column=column, operator=operator, query=query))
            elif kind == "row_filter_slider":
                mode_combo = widget.findChild(QtWidgets.QComboBox, "combo_min_max")
                mode = str(mode_combo.currentText() if mode_combo is not None else "max").strip().lower()
                if mode == "min":
                    operator = FilterOperator.GREATER_OR_EQUAL
                elif mode == "exact":
                    operator = FilterOperator.EQUALS
                else:
                    operator = FilterOperator.LESS_OR_EQUAL
                spin = self._active_slider_filter_spin(widget)
                threshold = float(spin.value() if spin is not None else 0.0)
                filters.append(SliderFilter(column=column, operator=operator, threshold=threshold))
            elif kind == "row_filter_range":
                spins = self._active_range_filter_spins(widget)
                if spins is None:
                    continue
                lower_spin, upper_spin = spins
                filters.append(
                    RangeFilter(
                        column=column,
                        lower=float(lower_spin.value()),
                        upper=float(upper_spin.value()),
                    )
                )
        return filters

    def filter_matching(self) -> FilterMatching:
        return (
            FilterMatching.ANY
            if self._matching_combo.currentText().strip().casefold() == "match any"
            else FilterMatching.ALL
        )

    def filter_state_payload(self) -> dict:
        return {
            "enabled": True,
            "mode": self.filter_mode(),
            "advanced_query": self._applied_advanced_query,
            "advanced_query_active": self._advanced_query_active,
            "matching": self.filter_matching().value,
            "rules": [self._rule_payload_for_widget(widget) for widget in self._iter_rule_widgets()],
            "filtered_opacity": self.filtered_opacity(),
            "hide_filtered_tooltips": self.hide_filtered_tooltips(),
            "filtered_point_size_factor": self.filtered_point_size_factor(),
            "filtered_border_width_factor": self.filtered_border_width_factor(),
        }

    def apply_filter_state_payload(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        rules = payload.get("rules")
        mode = "advanced" if str(payload.get("mode") or "simple").strip().casefold() == "advanced" else "simple"
        advanced_query = str(payload.get("advanced_query") or "").strip()
        try:
            validate_advanced_query(advanced_query)
        except AdvancedQueryError:
            advanced_query = ""
        self._applied_advanced_query = advanced_query
        self._advanced_query_active = bool(
            payload.get("advanced_query_active", bool(advanced_query))
        )
        with QtCore.QSignalBlocker(self._advanced_query_editor):
            self._advanced_query_editor.setPlainText(advanced_query)
        self._set_filter_mode(mode, emit_change=False)
        self._update_advanced_query_validation()
        matching = str(payload.get("matching") or "all").strip().casefold()
        with QtCore.QSignalBlocker(self._matching_combo):
            matching_index = self._matching_combo.findText(
                "match any" if matching in {"any", "match any"} else "match all",
                QtCore.Qt.MatchFlag.MatchFixedString,
            )
            self._matching_combo.setCurrentIndex(max(0, matching_index))
        self._clear_filter_rules()
        if isinstance(rules, list):
            for rule_payload in rules:
                if not isinstance(rule_payload, dict):
                    continue
                kind = str(rule_payload.get("kind") or "").strip().lower()
                if kind not in {"selection", "query", "slider", "range"}:
                    continue
                widget = self._add_filter_rule(kind, switch_to_simple=False)
                self._apply_rule_payload(widget, rule_payload)
        opacity_spin = self._widget.findChild(QtWidgets.QSpinBox, "spin_filtered_point_opacity")
        if opacity_spin is not None:
            opacity_spin.setValue(int(max(0.0, min(1.0, float(payload.get("filtered_opacity", 1.0)))) * 100))
        self._hide_filtered_tooltips_button.setChecked(
            bool(payload.get("hide_filtered_tooltips", True))
        )
        size_spin = self._widget.findChild(QtWidgets.QSpinBox, "spin_filtered_point_size")
        if size_spin is not None:
            size_spin.setValue(int(float(payload.get("filtered_point_size_factor", 1.0)) * 10.0))
        border_spin = self._widget.findChild(QtWidgets.QSpinBox, "spin_border_width")
        if border_spin is not None:
            border_spin.setValue(int(float(payload.get("filtered_border_width_factor", 1.0)) * 10.0))
        self._emit_filters_changed()

    def set_save_preset_callback(self, callback: Callable[[str, dict], bool] | None) -> None:
        self._save_preset_callback = callback

    def set_load_preset_callback(self, callback: Callable[[str], dict | None] | None) -> None:
        self._load_preset_callback = callback

    def set_recent_preset_labels(self, labels: list[str]) -> None:
        self._preset_labels = list(labels)
        self._load_menu.clear()
        if not self._preset_labels:
            action = self._load_menu.addAction("No Presets Saved")
            action.setEnabled(False)
            return
        for label in self._preset_labels[:10]:
            action = self._load_menu.addAction(label)
            action.triggered.connect(lambda _checked=False, value=label: self._load_preset_by_label(value))

    def _load_preset_by_label(self, label: str) -> None:
        if self._load_preset_callback is None:
            return
        payload = self._load_preset_callback(label)
        if isinstance(payload, dict):
            self.apply_filter_state_payload(payload)
            self._show_status(f"Loaded filter preset: {label}")

    def _rule_payload_for_widget(self, widget: QtWidgets.QWidget | None) -> dict:
        if widget is None:
            return {}
        kind_map = {
            "row_filter_selection": "selection",
            "row_filter_query": "query",
            "row_filter_slider": "slider",
            "row_filter_range": "range",
        }
        kind = kind_map.get(widget.objectName() or "", "")
        payload: dict[str, object] = {"kind": kind}
        column_combo = widget.findChild(QtWidgets.QComboBox, "combo_column")
        payload["column"] = str(
            (column_combo.currentData() or column_combo.currentText())
            if column_combo is not None else ""
        )
        apply_button = widget.findChild(QtWidgets.QPushButton, "button_apply_element")
        payload["active"] = bool(apply_button.isChecked()) if apply_button is not None else False
        if kind == "selection":
            operator = widget.findChild(QtWidgets.QComboBox, "combo_operator")
            value_combo = widget.findChild(QtWidgets.QComboBox, "combo_value_selection")
            payload["operator"] = str(operator.currentText() if operator is not None else "is")
            payload["value"] = str(value_combo.currentText() if value_combo is not None else "")
        elif kind == "query":
            operator = widget.findChild(QtWidgets.QComboBox, "combo_operator")
            edit = widget.findChild(QtWidgets.QLineEdit, "edit_query_text")
            payload["operator"] = str(operator.currentText() if operator is not None else "contains")
            payload["query"] = str(edit.text() if edit is not None else "")
        elif kind == "slider":
            mode_combo = widget.findChild(QtWidgets.QComboBox, "combo_min_max")
            spin = self._active_slider_filter_spin(widget)
            payload["mode"] = str(mode_combo.currentText() if mode_combo is not None else "max")
            payload["threshold"] = float(spin.value() if spin is not None else 0.0)
        elif kind == "range":
            spins = self._active_range_filter_spins(widget)
            payload["lower"] = float(spins[0].value()) if spins is not None else 0.0
            payload["upper"] = float(spins[1].value()) if spins is not None else 0.0
        return payload

    def _apply_rule_payload(self, widget: QtWidgets.QWidget | None, payload: dict) -> None:
        if widget is None:
            return
        column_combo = widget.findChild(QtWidgets.QComboBox, "combo_column")
        missing_column = False
        if column_combo is not None:
            requested_column = str(payload.get("column") or "")
            idx = column_combo.findData(requested_column)
            if idx < 0:
                idx = column_combo.findText(requested_column)
            if idx < 0 and requested_column:
                column_combo.addItem(f"Missing: {requested_column}", requested_column)
                idx = column_combo.count() - 1
                missing_column = True
            column_combo.setCurrentIndex(idx if idx >= 0 else 0)
        operator = widget.findChild(QtWidgets.QComboBox, "combo_operator")
        if operator is not None:
            idx = operator.findText(str(payload.get("operator") or ""))
            if idx >= 0:
                operator.setCurrentIndex(idx)
        value_combo = widget.findChild(QtWidgets.QComboBox, "combo_value_selection")
        if value_combo is not None:
            idx = value_combo.findText(str(payload.get("value") or ""))
            if idx >= 0:
                value_combo.setCurrentIndex(idx)
        edit = widget.findChild(QtWidgets.QLineEdit, "edit_query_text")
        if edit is not None:
            edit.setText(str(payload.get("query") or ""))
        mode_combo = widget.findChild(QtWidgets.QComboBox, "combo_min_max")
        if mode_combo is not None:
            idx = mode_combo.findText(str(payload.get("mode") or ""))
            if idx >= 0:
                mode_combo.setCurrentIndex(idx)
        spin = self._active_slider_filter_spin(widget)
        if spin is not None:
            try:
                threshold = float(payload.get("threshold") or 0.0)
                spin.setValue(
                    threshold
                    if isinstance(spin, QtWidgets.QDoubleSpinBox)
                    else int(round(threshold))
                )
            except (TypeError, ValueError):
                pass
        range_spins = self._active_range_filter_spins(widget)
        if range_spins is not None:
            try:
                lower = float(payload.get("lower", range_spins[0].minimum()))
                upper = float(payload.get("upper", range_spins[1].maximum()))
                self._set_range_filter_thresholds(widget, lower, upper)
            except (TypeError, ValueError):
                pass
        apply_button = widget.findChild(QtWidgets.QPushButton, "button_apply_element")
        if apply_button is not None:
            apply_button.setChecked(bool(payload.get("active", False)) and not missing_column)
            apply_button.setToolTip(
                "This field no longer exists; the rule is inactive." if missing_column else ""
            )
            self._update_rule_apply_button_icon(apply_button)

    def _emit_filters_changed(self) -> None:
        if self._filters_changed_callback is not None:
            self._filters_changed_callback()

    def _emit_filters_changed_for_active_rule(self, widget: QtWidgets.QWidget) -> None:
        apply_button = widget.findChild(QtWidgets.QPushButton, "button_apply_element")
        if apply_button is not None and apply_button.isChecked():
            self._emit_filters_changed()

    def _load_metadata_filter_options(self, project: "Project" | None) -> None:
        self._metadata_columns = []
        self._metadata_group_columns = set()
        self._metadata_field_labels = {}
        self._metadata_value_types = {}
        self._metadata_distinct_values = {}
        self._metadata_numeric_ranges = {}
        if project is None:
            return
        access = self._metadata_access or DiscoveryMetadataAccess(project)
        self._metadata_access = access
        try:
            columns = access.columns()
        except Exception:
            return
        editable = [column for column in columns if column.name != "id"]
        self._metadata_group_columns = {
            column.name
            for column in editable
            if bool(getattr(access, "is_group_field", lambda _name: False)(column.name))
        }
        self._metadata_columns = [
            column.name for column in editable if column.name in self._metadata_group_columns
        ] + [column.name for column in editable if column.name not in self._metadata_group_columns]
        self._metadata_field_labels = {
            column.name: getattr(column, "display_name", None) or column.name
            for column in columns
            if column.name != "id"
        }
        self._metadata_value_types = {
            column.name: str(column.value_type or "")
            for column in columns
            if column.name != "id"
        }

    def _refresh_rule_option_widgets(self) -> None:
        for widget in self._iter_rule_widgets():
            column_combo = widget.findChild(QtWidgets.QComboBox, "combo_column")
            if column_combo is not None:
                current = str(column_combo.currentData() or column_combo.currentText() or "")
                column_combo.blockSignals(True)
                column_combo.clear()
                column_combo.addItem("None", "")
                self._add_metadata_field_options(
                    column_combo,
                    numeric_only=widget.objectName() in {
                        "row_filter_slider",
                        "row_filter_range",
                    },
                )
                index = column_combo.findData(current)
                missing_column = index < 0 and bool(current)
                if index < 0 and current:
                    column_combo.addItem(f"Missing: {current}", current)
                    index = column_combo.count() - 1
                column_combo.setCurrentIndex(index if index >= 0 else 0)
                column_combo.blockSignals(False)
                if missing_column:
                    apply_button = widget.findChild(QtWidgets.QPushButton, "button_apply_element")
                    if apply_button is not None:
                        apply_button.setChecked(False)
                        apply_button.setToolTip(
                            "This field no longer exists; the rule is inactive."
                        )
            self._update_rule_value_options(widget)

    def _add_metadata_field_options(
        self,
        combo: QtWidgets.QComboBox,
        *,
        numeric_only: bool = False,
    ) -> None:
        columns = (
            [name for name in self._metadata_columns if self._is_numeric_filter_column(name)]
            if numeric_only
            else self._metadata_columns
        )
        groups = [name for name in columns if name in self._metadata_group_columns]
        metadata = [name for name in columns if name not in self._metadata_group_columns]
        for name in groups:
            combo.addItem(self._metadata_field_labels.get(name, name), name)
        if groups and metadata:
            combo.insertSeparator(combo.count())
        for name in metadata:
            combo.addItem(self._metadata_field_labels.get(name, name), name)

    def _is_numeric_filter_column(self, column: str) -> bool:
        value_type = self._metadata_value_types.get(column, "").strip().casefold()
        if value_type in self._NUMERIC_METADATA_TYPES:
            return True
        if value_type in self._NON_NUMERIC_METADATA_TYPES:
            return False
        values = self._metadata_values_for_column(column)
        return self._numeric_range_for_column(column, values) is not None

    def _update_rule_value_options(
        self,
        widget: QtWidgets.QWidget,
        *,
        reset_slider_value: bool = False,
    ) -> None:
        column_combo = widget.findChild(QtWidgets.QComboBox, "combo_column")
        if column_combo is None:
            return
        value_combo = widget.findChild(QtWidgets.QComboBox, "combo_value_selection")
        slider = widget.findChild(QtWidgets.QSlider, "slider_filter_value")
        integer_spin = widget.findChild(QtWidgets.QSpinBox, "spin_filter_value")
        double_spin = widget.findChild(
            QtWidgets.QDoubleSpinBox,
            "spin_filter_value_double",
        )
        range_slider = widget.findChild(RangeSlider, "slider_filter_value_range")
        range_integer_min = widget.findChild(QtWidgets.QSpinBox, "spin_filter_value_min")
        range_integer_max = widget.findChild(QtWidgets.QSpinBox, "spin_filter_value_max")
        range_double_min = widget.findChild(
            QtWidgets.QDoubleSpinBox,
            "spin_filter_range_min_double",
        )
        range_double_max = widget.findChild(
            QtWidgets.QDoubleSpinBox,
            "spin_filter_range_max_double",
        )
        has_slider_filter = (
            slider is not None and integer_spin is not None and double_spin is not None
        )
        has_range_filter = (
            range_slider is not None
            and range_integer_min is not None
            and range_integer_max is not None
            and range_double_min is not None
            and range_double_max is not None
        )
        if value_combo is None and not has_slider_filter and not has_range_filter:
            return
        column = str(column_combo.currentData() or column_combo.currentText() or "").strip()
        values = self._metadata_values_for_column(column)

        if value_combo is not None:
            current = value_combo.currentText()
            value_combo.blockSignals(True)
            value_combo.clear()
            value_combo.addItems(values)
            index = value_combo.findText(current)
            if index >= 0:
                value_combo.setCurrentIndex(index)
            value_combo.setEnabled(bool(values))
            value_combo.blockSignals(False)

        numeric_range = self._numeric_range_for_column(column, values)
        if has_slider_filter:
            self._set_slider_filter_range(
                slider,
                integer_spin,
                double_spin,
                numeric_range,
                reset_value=reset_slider_value,
            )
        if has_range_filter:
            self._set_range_filter_range(
                range_slider,
                range_integer_min,
                range_integer_max,
                range_double_min,
                range_double_max,
                numeric_range,
                reset_value=reset_slider_value,
            )

    def _metadata_values_for_column(self, column: str) -> list[str]:
        if (
            column
            and column.lower() != "none"
            and column not in self._metadata_distinct_values
            and self._metadata_access is not None
        ):
            try:
                self._metadata_distinct_values[column] = list(
                    self._metadata_access.distinct_values(column)
                )
            except Exception:
                self._metadata_distinct_values[column] = []
        return self._metadata_distinct_values.get(column, [])

    def _numeric_range_for_column(
        self,
        column: str,
        values: list[str],
    ) -> tuple[float, float, bool] | None:
        if not column or column.lower() == "none":
            return None
        if column in self._metadata_numeric_ranges:
            return self._metadata_numeric_ranges[column]
        numeric_values: list[float] = []
        use_double_spin = False
        for raw_value in values:
            text = str(raw_value).strip()
            if not text:
                continue
            try:
                value = float(text)
            except (TypeError, ValueError):
                self._metadata_numeric_ranges[column] = None
                return None
            if math.isfinite(value):
                numeric_values.append(value)
                try:
                    int(text)
                except ValueError:
                    use_double_spin = True
        numeric_range = (
            (min(numeric_values), max(numeric_values), use_double_spin)
            if numeric_values
            else None
        )
        self._metadata_numeric_ranges[column] = numeric_range
        return numeric_range

    def _set_slider_filter_range(
        self,
        slider: QtWidgets.QSlider,
        integer_spin: QtWidgets.QSpinBox,
        double_spin: QtWidgets.QDoubleSpinBox,
        numeric_range: tuple[float, float, bool] | None,
        *,
        reset_value: bool,
    ) -> None:
        with (
            QtCore.QSignalBlocker(slider),
            QtCore.QSignalBlocker(integer_spin),
            QtCore.QSignalBlocker(double_spin),
        ):
            if numeric_range is None:
                slider.setRange(0, 0)
                integer_spin.setRange(0, 0)
                integer_spin.setValue(0)
                integer_spin.setVisible(True)
                integer_spin.setEnabled(False)
                double_spin.setRange(0.0, 0.0)
                double_spin.setValue(0.0)
                double_spin.setVisible(False)
                double_spin.setEnabled(False)
                slider.setEnabled(False)
                return
            minimum, maximum, use_double_spin = numeric_range
            previous_spin = double_spin if not double_spin.isHidden() else integer_spin
            current = float(previous_spin.value())
            value = minimum if reset_value else max(minimum, min(maximum, current))
            if use_double_spin:
                double_spin.setRange(minimum, maximum)
                double_spin.setSingleStep(
                    max(
                        10.0 ** (-double_spin.decimals()),
                        (maximum - minimum) / 100.0,
                    )
                )
                double_spin.setValue(value)
                double_spin.setVisible(True)
                double_spin.setEnabled(True)
                integer_spin.setVisible(False)
                integer_spin.setEnabled(False)
                slider.setRange(
                    0,
                    self._SLIDER_POSITION_STEPS if maximum > minimum else 0,
                )
                slider.setValue(
                    self._slider_position_for_value(value, minimum, maximum)
                )
            else:
                integer_minimum = int(minimum)
                integer_maximum = int(maximum)
                integer_value = int(round(value))
                integer_spin.setRange(integer_minimum, integer_maximum)
                integer_spin.setSingleStep(1)
                integer_spin.setValue(integer_value)
                integer_spin.setVisible(True)
                integer_spin.setEnabled(True)
                double_spin.setVisible(False)
                double_spin.setEnabled(False)
                slider.setRange(integer_minimum, integer_maximum)
                slider.setValue(integer_value)
            slider.setEnabled(maximum > minimum)

    def _set_range_filter_range(
        self,
        slider: RangeSlider,
        integer_min: QtWidgets.QSpinBox,
        integer_max: QtWidgets.QSpinBox,
        double_min: QtWidgets.QDoubleSpinBox,
        double_max: QtWidgets.QDoubleSpinBox,
        numeric_range: tuple[float, float, bool] | None,
        *,
        reset_value: bool,
    ) -> None:
        with (
            QtCore.QSignalBlocker(slider),
            QtCore.QSignalBlocker(integer_min),
            QtCore.QSignalBlocker(integer_max),
            QtCore.QSignalBlocker(double_min),
            QtCore.QSignalBlocker(double_max),
        ):
            if numeric_range is None:
                slider.setRange(0, 0)
                slider.setValues(0, 0)
                for spin in (integer_min, integer_max):
                    spin.setRange(0, 0)
                    spin.setValue(0)
                    spin.setVisible(True)
                    spin.setEnabled(False)
                for spin in (double_min, double_max):
                    spin.setRange(0.0, 0.0)
                    spin.setValue(0.0)
                    spin.setVisible(False)
                    spin.setEnabled(False)
                slider.setEnabled(False)
                return

            minimum, maximum, use_double_spin = numeric_range
            previous = (
                (double_min, double_max)
                if not double_min.isHidden()
                else (integer_min, integer_max)
            )
            lower = minimum if reset_value else max(minimum, min(maximum, float(previous[0].value())))
            upper = maximum if reset_value else max(minimum, min(maximum, float(previous[1].value())))
            if lower > upper:
                lower, upper = upper, lower

            if use_double_spin:
                step = max(
                    10.0 ** (-double_min.decimals()),
                    (maximum - minimum) / 100.0,
                )
                for spin, value in ((double_min, lower), (double_max, upper)):
                    spin.setRange(minimum, maximum)
                    spin.setSingleStep(step)
                    spin.setValue(value)
                    spin.setVisible(True)
                    spin.setEnabled(True)
                for spin in (integer_min, integer_max):
                    spin.setVisible(False)
                    spin.setEnabled(False)
                slider.setRange(
                    0,
                    self._SLIDER_POSITION_STEPS if maximum > minimum else 0,
                )
                slider.setValues(
                    self._slider_position_for_value(lower, minimum, maximum),
                    self._slider_position_for_value(upper, minimum, maximum),
                )
            else:
                integer_minimum = int(minimum)
                integer_maximum = int(maximum)
                lower_int = int(round(lower))
                upper_int = int(round(upper))
                for spin, value in ((integer_min, lower_int), (integer_max, upper_int)):
                    spin.setRange(integer_minimum, integer_maximum)
                    spin.setSingleStep(1)
                    spin.setValue(value)
                    spin.setVisible(True)
                    spin.setEnabled(True)
                for spin in (double_min, double_max):
                    spin.setVisible(False)
                    spin.setEnabled(False)
                slider.setRange(integer_minimum, integer_maximum)
                slider.setValues(lower_int, upper_int)
            slider.setEnabled(maximum > minimum)

    def _set_range_filter_values_from_positions(
        self,
        widget: QtWidgets.QWidget,
        lower_position: int,
        upper_position: int,
    ) -> None:
        spins = self._active_range_filter_spins(widget)
        if spins is None or not spins[0].isEnabled():
            return
        lower_spin, upper_spin = spins
        if isinstance(lower_spin, QtWidgets.QDoubleSpinBox):
            minimum = float(lower_spin.minimum())
            maximum = float(lower_spin.maximum())
            lower = minimum + (
                (maximum - minimum)
                * float(lower_position)
                / float(max(1, self._SLIDER_POSITION_STEPS))
            )
            upper = minimum + (
                (maximum - minimum)
                * float(upper_position)
                / float(max(1, self._SLIDER_POSITION_STEPS))
            )
        else:
            lower = int(lower_position)
            upper = int(upper_position)
        with (
            QtCore.QSignalBlocker(lower_spin),
            QtCore.QSignalBlocker(upper_spin),
        ):
            lower_spin.setValue(lower)
            upper_spin.setValue(upper)
        self._emit_filters_changed_for_active_rule(widget)

    def _set_range_filter_position_from_value(
        self,
        widget: QtWidgets.QWidget,
        endpoint: str,
        _value: float,
    ) -> None:
        slider = widget.findChild(RangeSlider, "slider_filter_value_range")
        spins = self._active_range_filter_spins(widget)
        if slider is None or spins is None or not spins[0].isEnabled():
            return
        lower_spin, upper_spin = spins
        lower = float(lower_spin.value())
        upper = float(upper_spin.value())
        if endpoint == "lower" and lower > upper:
            upper = lower
            with QtCore.QSignalBlocker(upper_spin):
                upper_spin.setValue(upper)
        elif endpoint == "upper" and upper < lower:
            lower = upper
            with QtCore.QSignalBlocker(lower_spin):
                lower_spin.setValue(lower)
        if isinstance(lower_spin, QtWidgets.QDoubleSpinBox):
            minimum = float(lower_spin.minimum())
            maximum = float(lower_spin.maximum())
            positions = (
                self._slider_position_for_value(lower, minimum, maximum),
                self._slider_position_for_value(upper, minimum, maximum),
            )
        else:
            positions = (int(round(lower)), int(round(upper)))
        with QtCore.QSignalBlocker(slider):
            slider.setValues(*positions)
        self._emit_filters_changed_for_active_rule(widget)

    def _set_range_filter_thresholds(
        self,
        widget: QtWidgets.QWidget,
        lower: float,
        upper: float,
    ) -> None:
        slider = widget.findChild(RangeSlider, "slider_filter_value_range")
        spins = self._active_range_filter_spins(widget)
        if slider is None or spins is None:
            return
        lower_spin, upper_spin = spins
        bounded_lower = max(float(lower_spin.minimum()), min(float(lower_spin.maximum()), lower))
        bounded_upper = max(float(upper_spin.minimum()), min(float(upper_spin.maximum()), upper))
        if bounded_lower > bounded_upper:
            bounded_lower, bounded_upper = bounded_upper, bounded_lower
        with (
            QtCore.QSignalBlocker(lower_spin),
            QtCore.QSignalBlocker(upper_spin),
            QtCore.QSignalBlocker(slider),
        ):
            lower_spin.setValue(bounded_lower)
            upper_spin.setValue(bounded_upper)
            if isinstance(lower_spin, QtWidgets.QDoubleSpinBox):
                slider.setValues(
                    self._slider_position_for_value(
                        bounded_lower,
                        float(lower_spin.minimum()),
                        float(lower_spin.maximum()),
                    ),
                    self._slider_position_for_value(
                        bounded_upper,
                        float(upper_spin.minimum()),
                        float(upper_spin.maximum()),
                    ),
                )
            else:
                slider.setValues(int(round(bounded_lower)), int(round(bounded_upper)))

    def _set_slider_filter_value_from_position(
        self,
        widget: QtWidgets.QWidget,
        position: int,
    ) -> None:
        spin = self._active_slider_filter_spin(widget)
        if spin is None or not spin.isEnabled():
            return
        if isinstance(spin, QtWidgets.QDoubleSpinBox):
            minimum = float(spin.minimum())
            maximum = float(spin.maximum())
            value: float | int = minimum + (
                (maximum - minimum)
                * float(position)
                / float(max(1, self._SLIDER_POSITION_STEPS))
            )
        else:
            value = int(position)
        with QtCore.QSignalBlocker(spin):
            spin.setValue(value)
        self._emit_filters_changed_for_active_rule(widget)

    @staticmethod
    def _update_slider_filter_appearance(widget: QtWidgets.QWidget) -> None:
        mode_combo = widget.findChild(QtWidgets.QComboBox, "combo_min_max")
        slider = widget.findChild(QtWidgets.QSlider, "slider_filter_value")
        if mode_combo is None or slider is None:
            return
        mode = mode_combo.currentText().strip().casefold()
        slider.setInvertedAppearance(mode == "max")
        if isinstance(slider, ThresholdSlider):
            slider.setHighlightVisible(mode != "exact")

    def _set_slider_filter_position_from_value(
        self,
        widget: QtWidgets.QWidget,
        value: float,
    ) -> None:
        slider = widget.findChild(QtWidgets.QSlider, "slider_filter_value")
        spin = self._active_slider_filter_spin(widget)
        if slider is None or spin is None or not spin.isEnabled():
            return
        position = (
            self._slider_position_for_value(
                float(value),
                float(spin.minimum()),
                float(spin.maximum()),
            )
            if isinstance(spin, QtWidgets.QDoubleSpinBox)
            else int(round(value))
        )
        with QtCore.QSignalBlocker(slider):
            slider.setValue(position)
        self._emit_filters_changed_for_active_rule(widget)

    @staticmethod
    def _active_slider_filter_spin(
        widget: QtWidgets.QWidget,
    ) -> QtWidgets.QSpinBox | QtWidgets.QDoubleSpinBox | None:
        double_spin = widget.findChild(
            QtWidgets.QDoubleSpinBox,
            "spin_filter_value_double",
        )
        if double_spin is not None and not double_spin.isHidden():
            return double_spin
        return widget.findChild(QtWidgets.QSpinBox, "spin_filter_value")

    @staticmethod
    def _active_range_filter_spins(
        widget: QtWidgets.QWidget,
    ) -> tuple[
        QtWidgets.QSpinBox | QtWidgets.QDoubleSpinBox,
        QtWidgets.QSpinBox | QtWidgets.QDoubleSpinBox,
    ] | None:
        double_min = widget.findChild(
            QtWidgets.QDoubleSpinBox,
            "spin_filter_range_min_double",
        )
        double_max = widget.findChild(
            QtWidgets.QDoubleSpinBox,
            "spin_filter_range_max_double",
        )
        if (
            double_min is not None
            and double_max is not None
            and not double_min.isHidden()
        ):
            return double_min, double_max
        integer_min = widget.findChild(QtWidgets.QSpinBox, "spin_filter_value_min")
        integer_max = widget.findChild(QtWidgets.QSpinBox, "spin_filter_value_max")
        if integer_min is None or integer_max is None:
            return None
        return integer_min, integer_max

    @classmethod
    def _slider_position_for_value(
        cls,
        value: float,
        minimum: float,
        maximum: float,
    ) -> int:
        if maximum <= minimum:
            return 0
        fraction = (float(value) - minimum) / (maximum - minimum)
        return int(round(max(0.0, min(1.0, fraction)) * cls._SLIDER_POSITION_STEPS))

    @staticmethod
    def _operator_from_combo(combo: QtWidgets.QComboBox | None) -> FilterOperator:
        text = str(combo.currentText() if combo is not None else "").strip().lower()
        if text == "is not":
            return FilterOperator.NOT_EQUALS
        if text == "contains":
            return FilterOperator.CONTAINS
        if text == "does not contain":
            return FilterOperator.NOT_CONTAINS
        return FilterOperator.EQUALS
