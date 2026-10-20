from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtGui, QtWidgets

from DiscoveryArea.metadata_access import DiscoveryMetadataAccess
from DiscoveryArea.color_rules import analyze_rule_overlaps, ordered_rules, overlap_key, overlap_labels
from DiscoveryArea.palettes import MAX_CATEGORICAL_VALUES, distinct_palette, next_unused_color, palette_options
from shared.qt.panel_controller import UiPanelController
from shared.qt.icons import apply_icon_color
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child
from shared.qt.widgets import wire_two_option_stack

if TYPE_CHECKING:
    from project.domain import Project


class _ConditionalValuesModel(QtCore.QStringListModel):
    """Supply tooltips on demand without one item mutation per metadata value."""

    def data(self, index, role=QtCore.Qt.ItemDataRole.DisplayRole):
        if role == QtCore.Qt.ItemDataRole.ToolTipRole:
            role = QtCore.Qt.ItemDataRole.DisplayRole
        return super().data(index, role)


class AppearancePanelController(UiPanelController):
    package = "DiscoveryArea"
    ui_path = "ui/appearance_panel.ui"

    _POINT_ROWS = [
        ("#4c78a8", "45.0%", "A"),
        ("#f58518", "35.0%", "B"),
        ("#54a24b", "20.0%", "C"),
    ]
    _GLOBAL_POINT_ROWS = [("#000000", "100.0%", "Global")]
    _GLOBAL_BORDER_ROWS = [("#111111", "100.0%", "Global")]
    _BORDER_ROWS = [("#111111", "100.0%", "Global")]
    _MAX_CATEGORICAL_UNIQUE = MAX_CATEGORICAL_VALUES
    _GLOBAL_LABEL = "Global"
    _NO_VALID_FIELDS_LABEL = "no valid fields"
    _DEFAULT_POINT_GRADIENT = ("#3b82f6", "#ef4444")
    _DEFAULT_BORDER_GRADIENT = ("#3b82f6", "#ef4444")
    _DEFAULT_PREVIEW_FILL_SOURCE = "none"
    _DEFAULT_PREVIEW_BORDER_SOURCE = "point"
    _DEFAULT_PREVIEW_FILL_OPACITY = 0.7
    _DEFAULT_PREVIEW_BODY_OPACITY = 1.0
    _DEFAULT_PREVIEW_FRAME_WIDTH = 4.0
    _DEFAULT_PREVIEW_FRAME_OPACITY = 1.0
    _COLOR_TABLE_NAMES = (
        "table_point_colors",
        "table_border_colors",
        "table_fill_color_conditions",
        "table_border_color_conditions",
    )
    _DYNAMIC_COMBO_NAMES = (
        "combo_point_size_column",
        "combo_point_color_column",
        "combo_fill_color_condition_field",
        "combo_fill_color_condition_value",
        "combo_border_width_column",
        "combo_border_color_column",
        "combo_border_color_condition_field",
        "combo_border_color_condition_value",
    )
    _DISTRIBUTION_TOTAL = "total"
    _DISTRIBUTION_VISIBLE = "visible"
    _TOTAL_SHARE_ROLE = int(QtCore.Qt.ItemDataRole.UserRole) + 1
    _VISIBLE_SHARE_ROLE = int(QtCore.Qt.ItemDataRole.UserRole) + 2
    _CONDITION_KEY_ROLE = int(QtCore.Qt.ItemDataRole.UserRole) + 3

    def _wire(self) -> None:
        self._style_changed_callback = None
        self._save_project_preset_callback: Callable[[str, dict], bool] | None = None
        self._load_project_preset_callback: Callable[[str], dict | None] | None = None
        self._save_global_template_callback: Callable[[str, dict], bool] | None = None
        self._load_global_template_callback: Callable[[str], dict | None] | None = None
        self._project_preset_labels: list[str] = []
        self._global_template_labels: list[str] = []
        self._project: Project | None = None
        self._metadata_access: DiscoveryMetadataAccess | None = None
        self._metadata_column_names: set[str] = set()
        self._metadata_column_values: dict[str, list[str]] = {}
        self._metadata_column_counts: dict[str, list[tuple[str, int]]] = {}
        self._metadata_numeric_columns: dict[str, bool] = {}
        self._metadata_unique_counts: dict[str, int] = {}
        self._metadata_color_column_order: list[str] = []
        self._metadata_group_columns: set[str] = set()
        self._metadata_field_labels: dict[str, str] = {}
        self._categorical_color_columns: list[str] = []
        self._continuous_color_columns: list[str] = []
        self._point_color_mappings: dict[str, dict[str, str]] = {}
        self._border_color_mappings: dict[str, dict[str, str]] = {}
        self._point_color_column_current = self._GLOBAL_LABEL
        self._border_color_column_current = self._GLOBAL_LABEL
        self._point_gradient_start = self._DEFAULT_POINT_GRADIENT[0]
        self._point_gradient_end = self._DEFAULT_POINT_GRADIENT[1]
        self._border_gradient_start = self._DEFAULT_BORDER_GRADIENT[0]
        self._border_gradient_end = self._DEFAULT_BORDER_GRADIENT[1]
        self._point_opacity = 1.0
        self._border_opacity = 1.0
        self._preview_fill_color_source = self._DEFAULT_PREVIEW_FILL_SOURCE
        self._preview_border_color_source = self._DEFAULT_PREVIEW_BORDER_SOURCE
        self._preview_fill_opacity = self._DEFAULT_PREVIEW_FILL_OPACITY
        self._preview_body_opacity = self._DEFAULT_PREVIEW_BODY_OPACITY
        self._preview_frame_width = self._DEFAULT_PREVIEW_FRAME_WIDTH
        self._preview_frame_opacity = self._DEFAULT_PREVIEW_FRAME_OPACITY
        self._conditional_rules: dict[str, list[tuple[str, str]]] = {
            "point": [],
            "border": [],
        }
        self._conditional_mappings: dict[str, dict[str, str]] = {
            "point": {},
            "border": {},
        }
        self._conflict_warnings = {"point": {}, "border": {}}
        self._overlap_groups: dict[str, list[dict]] = {"point": [], "border": []}
        self._conflict_resolutions: dict[str, dict[str, dict[str, str]]] = {
            "point": {}, "border": {},
        }
        self._dismissed_conflicts: dict[str, set[str]] = {"point": set(), "border": set()}
        self._conflict_dialog_active = False
        self._conflict_choice_menu: QtWidgets.QMenu | None = None
        self._conflict_jobs = {}
        self._conflict_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="color-conflicts")
        executor = self._conflict_executor
        self._widget.destroyed.connect(lambda: executor.shutdown(wait=False, cancel_futures=True))
        self._conflict_timer = QtCore.QTimer(self._widget)
        self._conflict_timer.setInterval(50)
        self._conflict_timer.timeout.connect(self._poll_conflict_warnings)
        self._conditional_menu = None
        self._conditional_names = {"point": "Conditional", "border": "Conditional"}
        self._conditional_value_columns: dict[str, str] = {}
        self._property_button_groups: list[QtWidgets.QButtonGroup] = []
        self._distribution_scope = self._DISTRIBUTION_TOTAL
        self._distribution_menu: QtWidgets.QMenu | None = None
        self._wire_dynamic_combo_tooltips()
        self._wire_appearance_pages()
        self._wire_preview_controls()
        self._wire_property_modes()
        self._wire_compact_color_mode_stack(
            "stack_point_color_mode",
            "radio_points_categorical",
            "radio_points_continuous",
        )
        self._wire_compact_color_mode_stack(
            "stack_border_color_mode",
            "radio_borders_categorical",
            "radio_borders_continuous",
        )
        self._setup_page_button_state()
        self._setup_preset_menu()
        self._repopulate_color_combo("point")
        self._repopulate_color_combo("border")
        self._populate_numeric_mapping_combos()
        self._setup_color_table("table_point_colors", [])
        self._setup_color_table("table_border_colors", [])
        self._setup_color_table("table_fill_color_conditions", [])
        self._setup_color_table("table_border_color_conditions", [])
        self._wire_gradient_buttons()
        self._wire_style_controls()
        self._wire_color_controls()
        self._wire_border_controls()
        self._wire_uniform_color_controls()
        self._wire_conditional_controls()
        self._populate_conditional_fields()
        self._enforce_color_mode_constraints("point")
        self._enforce_color_mode_constraints("border")
        self._set_gradient_preview("frame_point_gradient_preview", self._point_gradient_start, self._point_gradient_end)
        self._set_gradient_preview("frame_border_gradient_preview", self._border_gradient_start, self._border_gradient_end)
        self._set_uniform_swatch("point", "#000000")
        self._set_uniform_swatch("border", "#111111")
        apply_icon_color(self._widget)
        self._lock_horizontal_content_minimum()

    def _lock_horizontal_content_minimum(self) -> None:
        """Protect the content minimum without reserving its preferred width."""
        scroll = require_child(
            self._widget,
            QtWidgets.QScrollArea,
            "scroll_appearance_controls",
        )
        stack = require_child(
            self._widget,
            QtWidgets.QStackedWidget,
            "stack_appearance_pages",
        )
        self._widget.ensurePolished()
        # Hidden nested layouts can retain the larger Designer SVG fallback
        # sizes after semantic icons are applied. Refresh from the inside out
        # before turning their measured width into a persistent minimum.
        for layout in reversed(stack.findChildren(QtWidgets.QLayout)):
            layout.invalidate()
            layout.activate()
        scroll.setMinimumWidth(stack.minimumSizeHint().width())

    def _wire_dynamic_combo_tooltips(self) -> None:
        for name in self._DYNAMIC_COMBO_NAMES:
            combo = self._widget.findChild(QtWidgets.QComboBox, name)
            if combo is None:
                continue
            combo.currentIndexChanged.connect(
                lambda _index, combo=combo: self._update_dynamic_combo_tooltip(combo)
            )
            self._refresh_dynamic_combo_tooltips(combo)

    @staticmethod
    def _update_dynamic_combo_tooltip(combo: QtWidgets.QComboBox) -> None:
        combo.setToolTip(combo.currentText())

    def _refresh_dynamic_combo_tooltips(self, combo: QtWidgets.QComboBox) -> None:
        for index in range(combo.count()):
            combo.setItemData(
                index,
                combo.itemText(index),
                QtCore.Qt.ItemDataRole.ToolTipRole,
            )
        self._update_dynamic_combo_tooltip(combo)

    def set_project(self, project) -> None:
        super().set_project(project)
        self._project = project
        enabled = project is not None
        for name in (
            "combo_point_color_column",
            "combo_border_color_column",
            "combo_point_size_column",
            "combo_border_width_column",
            "combo_fill_color_condition_field",
            "combo_fill_color_condition_value",
            "combo_border_color_condition_field",
            "combo_border_color_condition_value",
            "button_add_fill_color_condition",
            "button_add_border_color_condition",
            "button_appearance_options",
        ):
            widget = self._widget.findChild(QtWidgets.QWidget, name)
            if widget is not None:
                widget.setEnabled(enabled)
        self._load_metadata_columns(project)
        for target in ("point", "border"):
            self._queue_conflict_warnings(target)

    def _wire_property_modes(self) -> None:
        groups = (
            ("stack_point_size", ("button_point_size_uniform", "button_point_size_mapped")),
            (
                "stack_fill_color",
                (
                    "button_fill_color_uniform",
                    "button_fill_color_mapped",
                    "button_fill_color_conditional",
                ),
            ),
            ("stack_border_width", ("button_border_width_uniform", "button_border_width_mapped")),
            (
                "stack_border_color",
                (
                    "button_border_color_uniform",
                    "button_border_color_mapped",
                    "button_border_color_conditional",
                ),
            ),
        )
        for stack_name, button_names in groups:
            stack = require_child(self._widget, QtWidgets.QStackedWidget, stack_name)
            group = QtWidgets.QButtonGroup(self._widget)
            group.setExclusive(True)
            self._property_button_groups.append(group)
            for index, button_name in enumerate(button_names):
                button = require_child(self._widget, QtWidgets.QAbstractButton, button_name)
                button.setCheckable(True)
                group.addButton(button, index)
                button.clicked.connect(
                    lambda _checked=False, stack=stack, index=index: self._select_property_page(stack, index)
                )
            checked = next((group.id(button) for button in group.buttons() if button.isChecked()), 0)
            self._select_property_page(stack, max(0, checked), emit=False)

    def _wire_compact_color_mode_stack(
        self,
        stack_name: str,
        categorical_name: str,
        continuous_name: str,
    ) -> None:
        wire_two_option_stack(
            self._widget,
            stack_name,
            categorical_name,
            continuous_name,
        )
        stack = require_child(self._widget, QtWidgets.QStackedWidget, stack_name)
        stack.currentChanged.connect(
            lambda _index,
            stack_name=stack_name,
            categorical_name=categorical_name,
            continuous_name=continuous_name: self._sync_mode_stack_from_radios(
                stack_name=stack_name,
                categorical_name=categorical_name,
                continuous_name=continuous_name,
            )
        )
        self._sync_mode_stack_from_radios(
            stack_name=stack_name,
            categorical_name=categorical_name,
            continuous_name=continuous_name,
        )

    def _wire_appearance_pages(self) -> None:
        stack = require_child(self._widget, QtWidgets.QStackedWidget, "stack_appearance_pages")
        self._appearance_page_group = QtWidgets.QButtonGroup(self._widget)
        self._appearance_page_group.setExclusive(True)
        if self._widget.findChild(QtWidgets.QAbstractButton, "button_rules_page") is not None:
            page_buttons = ("button_points_page", "button_borders_page", "button_rules_page")
        elif self._widget.findChild(QtWidgets.QAbstractButton, "button_size_page") is not None:
            page_buttons = ("button_size_page", "button_fill_page", "button_borders_page")
        else:
            page_buttons = ("button_points_page", "button_borders_page")
        if self._widget.findChild(QtWidgets.QAbstractButton, "button_previews_page") is not None:
            page_buttons = (*page_buttons, "button_previews_page")
        for index, name in enumerate(page_buttons):
            button = require_child(self._widget, QtWidgets.QAbstractButton, name)
            button.setCheckable(True)
            self._appearance_page_group.addButton(button, index)
            button.clicked.connect(
                lambda _checked=False, index=index: self._select_appearance_page(index)
            )
        selected = next(
            (
                self._appearance_page_group.id(button)
                for button in self._appearance_page_group.buttons()
                if button.isChecked()
            ),
            0,
        )
        self._select_appearance_page(max(0, selected))

    def _select_appearance_page(self, index: int) -> None:
        stack = self._widget.findChild(QtWidgets.QStackedWidget, "stack_appearance_pages")
        if stack is None or not 0 <= index < stack.count():
            return
        stack.setCurrentIndex(index)
        for page_index in range(stack.count()):
            page = stack.widget(page_index)
            policy = page.sizePolicy()
            policy.setVerticalPolicy(
                QtWidgets.QSizePolicy.Policy.Preferred
                if page_index == index
                else QtWidgets.QSizePolicy.Policy.Ignored
            )
            page.setSizePolicy(policy)
        button = self._appearance_page_group.button(index)
        if button is not None:
            button.setChecked(True)
        scroll = self._widget.findChild(QtWidgets.QScrollArea, "scroll_appearance_controls")
        if scroll is not None:
            scroll.verticalScrollBar().setValue(0)
        stack.updateGeometry()
        QtCore.QTimer.singleShot(0, self._sync_scroll_content_height_deferred)

    def _select_property_page(
        self,
        stack: QtWidgets.QStackedWidget,
        index: int,
        *,
        emit: bool = True,
    ) -> None:
        if not 0 <= index < stack.count():
            return
        stack.setCurrentIndex(index)
        for page_index in range(stack.count()):
            page = stack.widget(page_index)
            policy = page.sizePolicy()
            policy.setVerticalPolicy(
                QtWidgets.QSizePolicy.Policy.Preferred
                if page_index == index
                else QtWidgets.QSizePolicy.Policy.Ignored
            )
            page.setSizePolicy(policy)
        stack.updateGeometry()
        QtCore.QTimer.singleShot(0, self._sync_scroll_content_height_deferred)
        if stack.objectName() == "stack_border_width":
            self._update_border_color_enabled()
        if index == 2 and stack.objectName() in {"stack_fill_color", "stack_border_color"}:
            self._populate_conditional_values("fill" if stack.objectName() == "stack_fill_color" else "border")
        if emit:
            self._emit_style_changed()

    def _sync_scroll_content_height_deferred(self) -> None:
        self._sync_scroll_content_height()
        QtCore.QTimer.singleShot(0, self._sync_scroll_content_height)

    def _sync_scroll_content_height(self) -> None:
        scroll = self._widget.findChild(QtWidgets.QScrollArea, "scroll_appearance_controls")
        content = scroll.widget() if scroll is not None else None
        if content is None or content.layout() is None:
            return
        content.setMinimumHeight(0)
        content.layout().invalidate()
        content.layout().activate()
        left, top, right, bottom = content.layout().getContentsMargins()
        del left, right
        item_heights: list[int] = []
        for index in range(content.layout().count()):
            widget = content.layout().itemAt(index).widget()
            if widget is None:
                continue
            if isinstance(widget, QtWidgets.QStackedWidget) and widget.objectName() == "stack_appearance_pages":
                current = widget.currentWidget()
                height = current.sizeHint().height() if current is not None else 0
                widget.setMinimumHeight(height)
                item_heights.append(height)
            else:
                item_heights.append(widget.sizeHint().height())
        spacing = max(0, content.layout().spacing()) * max(0, len(item_heights) - 1)
        content.setMinimumHeight(top + bottom + spacing + sum(item_heights))
        content.updateGeometry()

    def set_metadata_access(
        self,
        metadata_access: DiscoveryMetadataAccess | None,
    ) -> None:
        self._metadata_access = metadata_access
        for target in ("point", "border"):
            self._queue_conflict_warnings(target)

    def _load_metadata_columns(self, project: "Project" | None) -> None:
        conditional_styles = {
            target: self._conditional_color_style(target)
            for target in ("point", "border")
            if self._conditional_rules[target]
        }
        self._metadata_column_names = set()
        self._metadata_column_values = {}
        self._metadata_column_counts = {}
        self._metadata_numeric_columns = {}
        self._metadata_unique_counts = {}
        self._metadata_color_column_order = []
        self._metadata_group_columns = set()
        self._metadata_field_labels = {}
        self._categorical_color_columns = []
        self._continuous_color_columns = []
        if project is not None:
            access = self._metadata_access or DiscoveryMetadataAccess(project)
            self._metadata_access = access
            try:
                columns = access.columns()
                editable = [column for column in columns if column.name != "id"]
                self._metadata_column_names = {column.name for column in editable}
                self._metadata_group_columns = {
                    column.name
                    for column in editable
                    if bool(getattr(access, "is_group_field", lambda _name: False)(column.name))
                }
                self._metadata_color_column_order = [
                    column.name for column in editable if column.name in self._metadata_group_columns
                ] + [
                    column.name for column in editable if column.name not in self._metadata_group_columns
                ]
                self._metadata_field_labels = {
                    column.name: getattr(column, "display_name", None) or column.name
                    for column in editable
                }
                for column in editable:
                    name = column.name
                    is_group_field = bool(
                        getattr(access, "is_group_field", lambda _name: False)(name)
                    )
                    value_type = str(column.value_type or "").strip().lower()
                    is_numeric = value_type in {
                        "int",
                        "integer",
                        "float",
                        "number",
                        "numeric",
                        "real",
                    }
                    is_categorical = is_numeric or value_type in {
                        "text",
                        "string",
                        "categorical",
                    }
                    if column.unique_values is None:
                        # Hand-authored and older projects may not have column
                        # summaries yet. Aggregate just this column in SQLite
                        # rather than materializing complete metadata rows.
                        counts = [
                            (value if value else "(missing)", int(count))
                            for value, count in access.value_counts(name)
                        ]
                        self._metadata_column_counts[name] = counts
                        unique_count = len(counts)
                        is_numeric = self._is_numeric_column(
                            [value for value, _count in counts if value != "(missing)"]
                        )
                    else:
                        unique_count = max(0, int(column.unique_values))
                    self._metadata_numeric_columns[name] = is_numeric
                    self._metadata_unique_counts[name] = unique_count
                    if is_numeric and not is_group_field:
                        self._continuous_color_columns.append(name)
                    if is_categorical and (is_group_field or unique_count <= self._MAX_CATEGORICAL_UNIQUE):
                        self._categorical_color_columns.append(name)
            except Exception:
                pass
        self._repopulate_color_combo("point")
        self._repopulate_color_combo("border")
        self._populate_numeric_mapping_combos()
        self._populate_conditional_fields()
        for target, style in conditional_styles.items():
            # Stable field IDs survive renames; migrate displayed rule labels
            # and their colors together using the same path as saved styles.
            self._restore_conditional_style(target, style)
        self._point_color_column_current = self._current_combo_text("combo_point_color_column")
        self._border_color_column_current = self._current_combo_text("combo_border_color_column")
        self._refresh_point_color_table_from_selection()
        self._refresh_border_color_table_from_selection()
        self._enforce_color_mode_constraints("point")
        self._enforce_color_mode_constraints("border")
        self._emit_style_changed()

    def _wire_gradient_buttons(self) -> None:
        mappings = [
            ("button_point_gradient_start_color", "point", "start"),
            ("button_point_gradient_end_color", "point", "end"),
            ("button_border_gradient_start_color", "border", "start"),
            ("button_border_gradient_end_color", "border", "end"),
        ]
        for button_name, target, side in mappings:
            button = self._widget.findChild(QtWidgets.QToolButton, button_name)
            if button is None:
                continue
            button.clicked.connect(
                lambda _checked=False, target=target, side=side: self._pick_gradient_color(target, side)
            )

    def _wire_uniform_color_controls(self) -> None:
        color_validator = QtGui.QRegularExpressionValidator(
            QtCore.QRegularExpression(r"#[0-9A-Fa-f]{6}"),
            self._widget,
        )
        for target, swatch_name, line_name in (
            ("point", "swatch_uniform_point_color", "line_point_color_value"),
            ("border", "swatch_uniform_border_color", "line_border_color_value"),
        ):
            button = self._widget.findChild(QtWidgets.QToolButton, swatch_name)
            if button is not None:
                button.clicked.connect(
                    lambda _checked=False, target=target: self._pick_uniform_color(target)
                )
            line = self._widget.findChild(QtWidgets.QLineEdit, line_name)
            if line is not None:
                line.setValidator(color_validator)
                line.textEdited.connect(
                    lambda text, target=target: self._apply_uniform_color_text(target, text)
                )
                line.editingFinished.connect(
                    lambda target=target: self._finish_uniform_color_text(target)
                )

    def _set_uniform_swatch(self, target: str, color_hex: str) -> None:
        color = QtGui.QColor(str(color_hex))
        if not color.isValid():
            return
        value = color.name()
        swatch_name = "swatch_uniform_point_color" if target == "point" else "swatch_uniform_border_color"
        button = self._widget.findChild(QtWidgets.QToolButton, swatch_name)
        if button is not None:
            button.setProperty("colorHex", value)
            button.setStyleSheet(f"QToolButton {{ background: {value}; border: 1px solid palette(mid); }}")
        line_name = "line_point_color_value" if target == "point" else "line_border_color_value"
        line = self._widget.findChild(QtWidgets.QLineEdit, line_name)
        if line is not None and line.text() != value:
            with QtCore.QSignalBlocker(line):
                line.setText(value)

    def _apply_uniform_color_text(self, target: str, text: str) -> None:
        color = QtGui.QColor(str(text).strip())
        if not color.isValid() or len(str(text).strip()) != 7:
            return
        self._set_uniform_swatch(target, color.name())
        self._emit_style_changed()

    def _finish_uniform_color_text(self, target: str) -> None:
        line_name = "line_point_color_value" if target == "point" else "line_border_color_value"
        line = self._widget.findChild(QtWidgets.QLineEdit, line_name)
        if line is not None and not line.hasAcceptableInput():
            with QtCore.QSignalBlocker(line):
                line.setText(self._uniform_color(target))

    def _pick_uniform_color(self, target: str) -> None:
        selected = QtWidgets.QColorDialog.getColor(
            QtGui.QColor(self._uniform_color(target)),
            self._widget,
            "Select Color",
        )
        if not selected.isValid():
            return
        self._set_uniform_swatch(target, selected.name())
        self._emit_style_changed()

    def _populate_numeric_mapping_combos(self) -> None:
        controls = (
            (
                "combo_point_size_column",
                "button_point_size_mapped",
                "stack_point_size",
            ),
            (
                "combo_border_width_column",
                "button_border_width_mapped",
                "stack_border_width",
            ),
        )
        mapping_available = bool(self._continuous_color_columns) and self._project is not None
        for name, button_name, stack_name in controls:
            combo = self._widget.findChild(QtWidgets.QComboBox, name)
            if combo is None:
                continue
            current = str(combo.currentText() or "")
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(self._continuous_color_columns)
            if current in self._continuous_color_columns:
                combo.setCurrentText(current)
            combo.blockSignals(False)
            self._refresh_dynamic_combo_tooltips(combo)
            combo.setEnabled(mapping_available)
            self._set_property_page_available(stack_name, button_name, 1, mapping_available)

    def _set_property_page_available(
        self, stack_name: str, button_name: str, index: int, available: bool
    ) -> None:
        stack = require_child(self._widget, QtWidgets.QStackedWidget, stack_name)
        button = require_child(self._widget, QtWidgets.QAbstractButton, button_name)
        button.setEnabled(available)
        stack.widget(index).setEnabled(available)
        if not available and stack.currentIndex() == index:
            self._set_property_mode(stack_name, "uniform")

    def _wire_conditional_controls(self) -> None:
        for target in ("fill", "border"):
            field = self._widget.findChild(QtWidgets.QComboBox, f"combo_{target}_color_condition_field")
            values = self._widget.findChild(QtWidgets.QComboBox, f"combo_{target}_color_condition_value")
            if values is not None:
                values.setModel(_ConditionalValuesModel(values))
            add = self._widget.findChild(QtWidgets.QAbstractButton, f"button_add_{target}_color_condition")
            if field is not None:
                field.currentTextChanged.connect(
                    lambda _text, target=target: self._populate_conditional_values(target)
                )
            if add is not None:
                add.clicked.connect(
                    lambda _checked=False, target=target: self._add_conditional_rule(target)
                )

    def _populate_conditional_fields(self) -> None:
        for target in ("fill", "border"):
            self._set_property_page_available(
                f"stack_{target}_color",
                f"button_{target}_color_conditional",
                2,
                bool(self._metadata_color_column_order),
            )
            self._conditional_value_columns.pop(target, None)
            values = self._widget.findChild(QtWidgets.QComboBox, f"combo_{target}_color_condition_value")
            stack = self._widget.findChild(QtWidgets.QStackedWidget, f"stack_{target}_color")
            if stack.currentIndex() != 2:
                values.model().setStringList([])
            combo = self._widget.findChild(QtWidgets.QComboBox, f"combo_{target}_color_condition_field")
            if combo is None:
                continue
            current = str(combo.currentData() or combo.currentText() or "")
            combo.blockSignals(True)
            combo.clear()
            self._add_metadata_field_options(combo, self._metadata_color_column_order)
            if current in self._metadata_color_column_order:
                combo.setCurrentIndex(combo.findData(current))
            combo.blockSignals(False)
            self._refresh_dynamic_combo_tooltips(combo)
            self._populate_conditional_values(target)

    def _populate_conditional_values(self, ui_target: str) -> None:
        # Hidden uniform/mapped controls must not enumerate every filename just
        # to open Discovery. Populate the builder only when Conditional is used.
        stack = self._widget.findChild(QtWidgets.QStackedWidget, f"stack_{ui_target}_color")
        if stack is None or stack.currentIndex() != 2:
            return
        field = self._widget.findChild(QtWidgets.QComboBox, f"combo_{ui_target}_color_condition_field")
        values = self._widget.findChild(QtWidgets.QComboBox, f"combo_{ui_target}_color_condition_value")
        if field is None or values is None:
            return
        column = str(field.currentData() or field.currentText() or "").strip()
        if self._conditional_value_columns.get(ui_target) == column:
            return
        current = str(values.currentText() or "")
        options = [value for value, _count in self._metadata_value_counts(column)] if column else []
        values.blockSignals(True)
        values.model().setStringList(options)
        if current in options:
            values.setCurrentText(current)
        elif options:
            values.setCurrentIndex(0)
        values.blockSignals(False)
        self._conditional_value_columns[ui_target] = column
        self._update_dynamic_combo_tooltip(values)

    def _add_conditional_rule(self, ui_target: str) -> None:
        target = "point" if ui_target == "fill" else "border"
        field_combo = self._widget.findChild(QtWidgets.QComboBox, f"combo_{ui_target}_color_condition_field")
        value_combo = self._widget.findChild(QtWidgets.QComboBox, f"combo_{ui_target}_color_condition_value")
        if field_combo is None or value_combo is None:
            return
        field = str(field_combo.currentData() or field_combo.currentText() or "").strip()
        value = str(value_combo.currentText() or "").strip()
        if not field or not value:
            return
        rule = (field, value)
        if rule not in self._conditional_rules[target]:
            self._conditional_rules[target].append(rule)
            self._dismissed_conflicts[target].clear()
        mapping = self._conditional_mappings[target]
        label = self._conditional_rule_label(field, value)
        self._capture_conditional_table_colors(target)
        if label not in mapping:
            mapping[label] = next_unused_color(list(mapping.values()))
        if "all rest" not in mapping:
            mapping["all rest"] = next_unused_color(list(mapping.values()))
        self._refresh_conditional_table(target)
        self._emit_style_changed()

    def _refresh_conditional_table(
        self,
        target: str,
        *,
        check_conflicts: bool = True,
        capture_colors: bool = True,
    ) -> None:
        table_name = "table_fill_color_conditions" if target == "point" else "table_border_color_conditions"
        if capture_colors:
            self._capture_conditional_table_colors(target)
        mapping = self._conditional_mappings[target]
        table = self._conditional_table(target)
        shares = {
            str(table.item(row, 2).data(self._CONDITION_KEY_ROLE) or table.item(row, 2).text()): (
                str(table.item(row, 1).data(self._TOTAL_SHARE_ROLE) or "0.0%"),
                str(table.item(row, 1).data(self._VISIBLE_SHARE_ROLE) or "0.0%"),
            )
            for row in range(self._table_logical_row_count(table))
        }
        labels = [self._conditional_rule_label(field, value) for field, value in self._conditional_rules[target]]
        rows = [
            (mapping.get(label, "#000000"), *shares.get(label, ("0.0%", "0.0%")), label, label)
            for label in labels
        ]
        groups = {str(group["key"]): group for group in self._overlap_groups[target]}
        for key, resolution in self._conflict_resolutions[target].items():
            group = groups.get(key)
            if group is None or resolution.get("type") != "color":
                continue
            condition_key = f"overlap:{key}"
            rows.append((
                resolution.get("color", "#000000"),
                *shares.get(condition_key, ("0.0%", "0.0%")),
                resolution.get("condition") or group["label"],
                condition_key,
            ))
        if labels:
            rows.append((
                mapping.get("all rest", "#000000"),
                *shares.get("all rest", ("0.0%", "0.0%")),
                "all rest",
                "all rest",
            ))
        self._setup_color_table(table_name, rows)
        if check_conflicts:
            self._queue_conflict_warnings(target)
        else:
            self._apply_conflict_warnings(target)

    def _conditional_table(self, target: str) -> QtWidgets.QTableWidget:
        name = "table_fill_color_conditions" if target == "point" else "table_border_color_conditions"
        return require_child(self._widget, QtWidgets.QTableWidget, name)

    def _show_conditional_menu(self, target: str, position: QtCore.QPoint) -> None:
        table = self._conditional_table(target)
        row = table.rowAt(position.y())
        if row < 0 or row >= self._table_logical_row_count(table):
            return
        item = table.item(row, 2)
        label = item.text()
        condition_key = str(item.data(self._CONDITION_KEY_ROLE) or label)
        if label == "all rest":
            return
        if self._conditional_menu is not None:
            self._conditional_menu.deleteLater()
        menu = QtWidgets.QMenu(table)
        self._conditional_menu = menu
        if condition_key.startswith("overlap:"):
            key = condition_key.removeprefix("overlap:")
            menu.addAction("Change Conflict Decision…").triggered.connect(
                lambda _checked=False: self._reopen_conflict_resolution(target, key)
            )
        else:
            menu.addAction("Delete Rule").triggered.connect(
                lambda _checked=False: self._delete_conditional_rule(target, label)
            )
        menu.popup(table.viewport().mapToGlobal(position))

    def _delete_conditional_rule(self, target: str, label: str) -> None:
        self._capture_conditional_table_colors(target)
        self._conditional_rules[target] = [
            (field, value) for field, value in self._conditional_rules[target]
            if self._conditional_rule_label(field, value) != label
        ]
        self._conditional_mappings[target].pop(label, None)
        for key in tuple(self._conflict_resolutions[target]):
            if label in overlap_labels(key):
                del self._conflict_resolutions[target][key]
        self._dismissed_conflicts[target].clear()
        self._refresh_conditional_table(target, capture_colors=False)
        self._emit_style_changed()

    def _queue_conflict_warnings(self, target: str) -> None:
        previous = self._conflict_jobs.pop(target, None)
        if previous is not None:
            previous.cancel()
        self._conflict_warnings[target] = {}
        self._overlap_groups[target] = []
        self._apply_conflict_warnings(target)
        rules = self._conditional_color_style(target)["rules"]
        if len(rules) < 2 or self._metadata_access is None:
            return
        self._conflict_jobs[target] = self._conflict_executor.submit(
            analyze_rule_overlaps, rules, self._metadata_access,
        )
        self._conflict_timer.start()

    def _poll_conflict_warnings(self) -> None:
        for target, future in list(self._conflict_jobs.items()):
            if not future.done():
                continue
            del self._conflict_jobs[target]
            try:
                _warnings, groups = future.result()
                self._overlap_groups[target] = groups
            except Exception as error:
                self._show_status(f"Could not check color rule conflicts: {error}")
            self._rebuild_conflict_warnings(target)
            self._refresh_conditional_table(target, check_conflicts=False)
            QtCore.QTimer.singleShot(0, self._widget, self._prompt_next_conflict)
        if not self._conflict_jobs:
            self._conflict_timer.stop()

    def _apply_conflict_warnings(self, target: str) -> None:
        table = self._conditional_table(target)
        with QtCore.QSignalBlocker(table):
            for row in range(self._table_logical_row_count(table)):
                item = table.item(row, 2)
                warning = self._conflict_warnings[target].get(item.text())
                unresolved = any(
                    item.text() in group["labels"]
                    and str(group["key"]) not in self._conflict_resolutions[target]
                    for group in self._overlap_groups[target]
                )
                item.setForeground(
                    QtGui.QBrush(QtGui.QColor("#e53935")) if unresolved else QtGui.QBrush()
                )
                item.setToolTip(warning or item.text())
        self._install_conflict_decision_buttons(target)

    def _install_conflict_decision_buttons(self, target: str) -> None:
        table = self._conditional_table(target)
        groups = self._overlap_groups[target]
        for row in range(self._table_logical_row_count(table)):
            if table.cellWidget(row, 2) is not None:
                table.removeCellWidget(row, 2)
            item = table.item(row, 2)
            if item is None:
                continue
            condition_key = str(item.data(self._CONDITION_KEY_ROLE) or item.text())
            if condition_key.startswith("overlap:"):
                key = condition_key.removeprefix("overlap:")
                row_groups = [group for group in groups if str(group["key"]) == key]
            else:
                row_groups = [group for group in groups if item.text() in group["labels"]]
            if not row_groups:
                continue
            unresolved = any(
                str(group["key"]) not in self._conflict_resolutions[target]
                for group in row_groups
            )
            cell = QtWidgets.QWidget(table)
            palette = cell.palette()
            background_role = (
                QtGui.QPalette.ColorRole.AlternateBase
                if table.alternatingRowColors() and row % 2
                else QtGui.QPalette.ColorRole.Base
            )
            palette.setBrush(
                QtGui.QPalette.ColorRole.Window,
                table.palette().brush(background_role),
            )
            cell.setPalette(palette)
            cell.setAutoFillBackground(True)
            cell.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
            cell.customContextMenuRequested.connect(
                lambda position, cell=cell, target=target, table=table:
                self._show_conditional_menu(
                    target,
                    table.viewport().mapFromGlobal(cell.mapToGlobal(position)),
                )
            )
            layout = QtWidgets.QHBoxLayout(cell)
            layout.setContentsMargins(3, 0, 1, 0)
            layout.setSpacing(2)
            label = QtWidgets.QLabel(item.text(), cell)
            label.setObjectName("label_conflicting_condition")
            label.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            if unresolved:
                label.setStyleSheet("color: #e53935;")
            layout.addWidget(label, 1)
            button = QtWidgets.QToolButton(cell)
            button.setObjectName("button_resolve_color_conflict")
            button.setText("!")
            button.setAutoRaise(True)
            button.setFixedSize(12, 18)
            button.setStyleSheet("QToolButton { padding: 0; margin: 0; }")
            button.setToolTip("Set conflict rule")
            button.setAccessibleName(f"Change conflict decision for {item.text()}")
            button.clicked.connect(
                lambda _checked=False, target=target, groups=row_groups, button=button:
                self._show_conflict_choice(target, groups, button)
            )
            layout.addWidget(button)
            table.setCellWidget(row, 2, cell)

    def _show_conflict_choice(
        self, target: str, groups: list[dict], button: QtWidgets.QToolButton
    ) -> None:
        if len(groups) == 1:
            key = str(groups[0]["key"])
            QtCore.QTimer.singleShot(
                0, self._widget, lambda: self._reopen_conflict_resolution(target, key)
            )
            return
        if self._conflict_choice_menu is not None:
            self._conflict_choice_menu.deleteLater()
        menu = QtWidgets.QMenu(button)
        self._conflict_choice_menu = menu
        for group in groups:
            key = str(group["key"])
            action = menu.addAction(str(group["label"]))
            action.triggered.connect(
                lambda _checked=False, target=target, key=key: QtCore.QTimer.singleShot(
                    0, self._widget, lambda: self._reopen_conflict_resolution(target, key)
                )
            )
        menu.popup(button.mapToGlobal(QtCore.QPoint(0, button.height())))

    def _reopen_conflict_resolution(self, target: str, key: str) -> None:
        group = next(
            (group for group in self._overlap_groups[target] if str(group["key"]) == key),
            None,
        )
        if group is None:
            return
        self._capture_conditional_table_colors(target)
        resolution = self._show_conflict_resolution_dialog(target, group)
        if resolution is None:
            return
        self._conflict_resolutions[target][key] = resolution
        self._dismissed_conflicts[target].discard(key)
        self._rebuild_conflict_warnings(target)
        self._refresh_conditional_table(
            target, check_conflicts=False, capture_colors=False
        )
        self._emit_style_changed()

    def _rebuild_conflict_warnings(self, target: str) -> None:
        messages: dict[str, list[str]] = {}
        resolutions = self._conflict_resolutions[target]
        for group in self._overlap_groups[target]:
            resolution = resolutions.get(str(group["key"]))
            if resolution and resolution.get("type") == "color":
                outcome = f'uses separate color {resolution.get("color", "")}'
            elif resolution and resolution.get("type") == "rule":
                outcome = f'prioritizes {resolution.get("winner", group["winner"])}'
            else:
                outcome = f'currently uses {group["winner"]}; no explicit resolution was saved'
            message = (
                f'{group["count"]} item(s) match {group["label"]}; {outcome}.'
            )
            for label in group["labels"]:
                messages.setdefault(label, []).append(message)
        self._conflict_warnings[target] = {
            label: "\n".join(items) for label, items in messages.items()
        }

    def _prompt_next_conflict(self) -> None:
        if self._conflict_dialog_active:
            return
        for target in ("point", "border"):
            for group in self._overlap_groups[target]:
                key = str(group["key"])
                if (
                    key in self._conflict_resolutions[target]
                    or key in self._dismissed_conflicts[target]
                ):
                    continue
                self._conflict_dialog_active = True
                try:
                    resolution = self._show_conflict_resolution_dialog(target, group)
                finally:
                    self._conflict_dialog_active = False
                if resolution is None:
                    self._dismissed_conflicts[target].add(key)
                else:
                    self._capture_conditional_table_colors(target)
                    self._conflict_resolutions[target][key] = resolution
                    self._rebuild_conflict_warnings(target)
                    self._refresh_conditional_table(
                        target, check_conflicts=False, capture_colors=False
                    )
                    self._emit_style_changed()
                QtCore.QTimer.singleShot(0, self._widget, self._prompt_next_conflict)
                return

    def _show_conflict_resolution_dialog(self, target: str, group: dict) -> dict[str, str] | None:
        dialog = load_ui(self.package, "ui/color_rule_conflict_dialog.ui", None)
        if not isinstance(dialog, QtWidgets.QDialog):
            return None
        dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        dialog.setWindowTitle("Fill Color Conflict" if target == "point" else "Border Color Conflict")
        conflict_details = (
            f'{group["count"]} item(s) match all of these rules:\n'
            + "\n".join(f'• {label}' for label in group["labels"])
        )
        message = dialog.findChild(QtWidgets.QLabel, "label_conflict_message")
        if message is not None:
            message.setText(conflict_details)
        else:
            dialog.setToolTip(conflict_details)
        combo = require_child(dialog, QtWidgets.QComboBox, "combo_priority_rule")
        combo.addItems(list(group["labels"]))
        existing = self._conflict_resolutions[target].get(str(group["key"]), {})
        combo.setCurrentText(str(existing.get("winner") or group["winner"]))
        radio_rule = require_child(dialog, QtWidgets.QRadioButton, "radio_prioritize_rule")
        radio_color = require_child(dialog, QtWidgets.QRadioButton, "radio_overlap_color")
        swatch = require_child(dialog, QtWidgets.QToolButton, "button_overlap_color")
        condition = require_child(dialog, QtWidgets.QLineEdit, "line_new_condition")
        default_condition = str(
            group.get("label") or " && ".join(str(label) for label in group["labels"])
        )
        condition.setText(str(existing.get("condition") or default_condition))
        color = str(existing.get("color") or next_unused_color(
            list(self._conditional_mappings[target].values())
        ))
        if existing.get("type") == "color":
            radio_color.setChecked(True)

        def update_swatch() -> None:
            swatch.setProperty("colorHex", color)
            swatch.setStyleSheet(
                f"QToolButton {{ background: {color}; border: 1px solid palette(mid); }}"
            )

        def choose_color() -> None:
            nonlocal color
            selected = QtWidgets.QColorDialog.getColor(
                QtGui.QColor(color), dialog, "Select Overlap Color"
            )
            if selected.isValid():
                color = selected.name()
                radio_color.setChecked(True)
                update_swatch()

        update_swatch()
        def prioritize_selected_rule(_index: int) -> None:
            radio_rule.setChecked(True)

        def select_overlap_color(_text: str) -> None:
            radio_color.setChecked(True)

        swatch.clicked.connect(choose_color)
        combo.activated.connect(prioritize_selected_rule)
        condition.textEdited.connect(select_overlap_color)
        buttons = require_child(dialog, QtWidgets.QDialogButtonBox, "button_box")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        result = int(QtWidgets.QDialog.DialogCode.Rejected)
        event_loop = QtCore.QEventLoop()

        def dialog_finished(code: int) -> None:
            nonlocal result
            result = int(code)
            event_loop.quit()

        dialog.finished.connect(dialog_finished)
        try:
            # QDialog.exec() can enter an unsafe native lifetime path in PySide
            # after other transient windows have been created and destroyed.
            # Keep the dialog modal but own the nested loop explicitly.
            dialog.open()
            if dialog.isVisible():
                event_loop.exec()
        finally:
            dialog.finished.disconnect(dialog_finished)
            swatch.clicked.disconnect(choose_color)
            combo.activated.disconnect(prioritize_selected_rule)
            condition.textEdited.disconnect(select_overlap_color)
            buttons.accepted.disconnect(dialog.accept)
            buttons.rejected.disconnect(dialog.reject)
        if result != QtWidgets.QDialog.DialogCode.Accepted:
            return None
        if radio_color.isChecked():
            return {
                "type": "color",
                "color": color,
                "condition": condition.text().strip() or default_condition,
            }
        return {"type": "rule", "winner": combo.currentText()}

    def shutdown(self) -> None:
        self._conflict_timer.stop()
        self._conflict_jobs.clear()
        self._conflict_executor.shutdown(wait=False, cancel_futures=True)

    def set_point_color_view_counts(
        self,
        column: str,
        visible_counts: dict[str, int],
        visible_total: int,
    ) -> None:
        self.set_mapped_color_view_counts("point", column, visible_counts, visible_total)

    def set_mapped_color_view_counts(
        self,
        target: str,
        column: str,
        visible_counts: dict[str, int],
        visible_total: int,
    ) -> None:
        combo_name = "combo_point_color_column" if target == "point" else "combo_border_color_column"
        table_name = "table_point_colors" if target == "point" else "table_border_colors"
        if self._current_combo_text(combo_name) != str(column):
            return
        total_counts = dict(self._metadata_value_counts(str(column)))
        total = max(1, sum(total_counts.values()))
        self._set_table_percentages(
            table_name,
            total_counts,
            total,
            visible_counts,
            visible_total,
        )

    def set_conditional_color_counts(
        self,
        target: str,
        total_counts: dict[str, int],
        total: int,
        visible_counts: dict[str, int],
        visible_total: int,
    ) -> None:
        table_name = "table_fill_color_conditions" if target == "point" else "table_border_color_conditions"
        self._set_table_percentages(
            table_name,
            total_counts,
            total,
            visible_counts,
            visible_total,
        )

    def _set_table_percentages(
        self,
        table_name: str,
        total_counts: dict[str, int],
        total: int,
        visible_counts: dict[str, int],
        visible_total: int,
    ) -> None:
        table = self._widget.findChild(QtWidgets.QTableWidget, table_name)
        if table is None:
            return
        total_denominator = max(1, int(total))
        visible_denominator = max(1, int(visible_total))
        for row in range(self._table_logical_row_count(table)):
            label_item = table.item(row, 2)
            distribution_item = table.item(row, 1)
            if label_item is None or distribution_item is None:
                continue
            label = str(
                label_item.data(self._CONDITION_KEY_ROLE) or label_item.text()
            ).strip()
            distribution_item.setData(
                self._TOTAL_SHARE_ROLE,
                f"{100.0 * total_counts.get(label, 0) / total_denominator:.1f}%",
            )
            distribution_item.setData(
                self._VISIBLE_SHARE_ROLE,
                f"{100.0 * visible_counts.get(label, 0) / visible_denominator:.1f}%",
            )
        self._refresh_distribution_values(table)

    def _setup_page_button_state(self) -> None:
        buttons = [
            self._widget.findChild(QtWidgets.QAbstractButton, name)
            for name in (
                "button_size_page",
                "button_fill_page",
                "button_points_page",
                "button_borders_page",
                "button_rules_page",
                "button_previews_page",
            )
        ]
        for button in buttons:
            if button is not None:
                button.setCheckable(True)
                button.setAutoExclusive(True)
        for name in ("radio_points_continuous", "radio_borders_continuous"):
            radio = self._widget.findChild(QtWidgets.QRadioButton, name)
            if radio is not None:
                radio.setEnabled(True)

    @staticmethod
    def _normalize_preview_color_source(value: object) -> str:
        normalized = str(value or "none").strip().lower().replace(" ", "_")
        normalized = {"point_color": "point", "border_color": "border"}.get(
            normalized,
            normalized,
        )
        return normalized if normalized in {"none", "point", "border"} else "none"

    def _wire_preview_controls(self) -> None:
        fill_combo = require_child(
            self._widget,
            QtWidgets.QComboBox,
            "combo_preview_fill_color",
        )
        border_combo = require_child(
            self._widget,
            QtWidgets.QComboBox,
            "combo_preview_border_color",
        )
        fill_opacity = require_child(
            self._widget,
            QtWidgets.QSpinBox,
            "spin_preview_fill_opacity",
        )
        body_opacity = require_child(
            self._widget, QtWidgets.QSpinBox, "spin_body_opacity"
        )
        frame_width = require_child(
            self._widget, QtWidgets.QSpinBox, "spin_frame_width"
        )
        frame_opacity = require_child(
            self._widget, QtWidgets.QSpinBox, "spin_frame_opacity"
        )
        for combo, selected in (
            (fill_combo, self._preview_fill_color_source),
            (border_combo, self._preview_border_color_source),
        ):
            with QtCore.QSignalBlocker(combo):
                combo.clear()
                combo.addItem("none", "none")
                combo.addItem("point color", "point")
                combo.addItem("border color", "border")
                combo.setCurrentIndex(combo.findData(selected))
        with QtCore.QSignalBlocker(fill_opacity):
            fill_opacity.setValue(int(round(self._preview_fill_opacity * 100.0)))
        with QtCore.QSignalBlocker(body_opacity):
            body_opacity.setValue(int(round(self._preview_body_opacity * 100.0)))
        with QtCore.QSignalBlocker(frame_width):
            frame_width.setValue(int(round(self._preview_frame_width)))
        with QtCore.QSignalBlocker(frame_opacity):
            frame_opacity.setValue(int(round(self._preview_frame_opacity * 100.0)))
        fill_combo.currentIndexChanged.connect(self._on_preview_style_changed)
        border_combo.currentIndexChanged.connect(self._on_preview_style_changed)
        fill_opacity.valueChanged.connect(self._on_preview_style_changed)
        body_opacity.valueChanged.connect(self._on_preview_style_changed)
        frame_width.valueChanged.connect(self._on_preview_style_changed)
        frame_opacity.valueChanged.connect(self._on_preview_style_changed)
        self._sync_preview_controls()

    def _on_preview_style_changed(self, _value: object = None) -> None:
        fill_combo = require_child(
            self._widget, QtWidgets.QComboBox, "combo_preview_fill_color"
        )
        border_combo = require_child(
            self._widget, QtWidgets.QComboBox, "combo_preview_border_color"
        )
        fill_opacity = require_child(
            self._widget, QtWidgets.QSpinBox, "spin_preview_fill_opacity"
        )
        body_opacity = require_child(
            self._widget, QtWidgets.QSpinBox, "spin_body_opacity"
        )
        frame_width = require_child(
            self._widget, QtWidgets.QSpinBox, "spin_frame_width"
        )
        frame_opacity = require_child(
            self._widget, QtWidgets.QSpinBox, "spin_frame_opacity"
        )
        self._preview_fill_color_source = self._normalize_preview_color_source(
            fill_combo.currentData()
        )
        self._preview_border_color_source = self._normalize_preview_color_source(
            border_combo.currentData()
        )
        self._preview_fill_opacity = max(
            0.0,
            min(1.0, float(fill_opacity.value()) / 100.0),
        )
        self._preview_body_opacity = max(
            0.0, min(1.0, float(body_opacity.value()) / 100.0)
        )
        self._preview_frame_width = max(0.0, float(frame_width.value()))
        self._preview_frame_opacity = max(
            0.0, min(1.0, float(frame_opacity.value()) / 100.0)
        )
        self._sync_preview_controls()
        self._emit_style_changed()

    def _sync_preview_controls(self) -> None:
        spin = self._widget.findChild(QtWidgets.QSpinBox, "spin_preview_fill_opacity")
        if spin is not None:
            spin.setEnabled(self._preview_fill_color_source != "none")
        frame_enabled = self._preview_border_color_source != "none"
        for name in ("spin_frame_width", "spin_frame_opacity"):
            control = self._widget.findChild(QtWidgets.QSpinBox, name)
            if control is not None:
                control.setEnabled(frame_enabled)

    def preview_style(self) -> dict[str, object]:
        return {
            "fill_color_source": self._preview_fill_color_source,
            "border_color_source": self._preview_border_color_source,
            "fill_opacity": float(self._preview_fill_opacity),
            "body_opacity": float(self._preview_body_opacity),
            "frame_width": float(self._preview_frame_width),
            "frame_opacity": float(self._preview_frame_opacity),
        }

    def set_preview_style(
        self,
        *,
        fill_color_source: object,
        border_color_source: object,
        fill_opacity: object,
        body_opacity: object | None = None,
        frame_width: object | None = None,
        frame_opacity: object | None = None,
        emit: bool = True,
    ) -> None:
        self._preview_fill_color_source = self._normalize_preview_color_source(
            fill_color_source
        )
        self._preview_border_color_source = self._normalize_preview_color_source(
            border_color_source
        )
        try:
            opacity = float(fill_opacity)
        except (TypeError, ValueError):
            opacity = self._DEFAULT_PREVIEW_FILL_OPACITY
        self._preview_fill_opacity = max(0.0, min(1.0, opacity))
        numeric_values = (
            ("_preview_body_opacity", body_opacity, self._preview_body_opacity, 0.0, 1.0),
            ("_preview_frame_width", frame_width, self._preview_frame_width, 0.0, 30.0),
            ("_preview_frame_opacity", frame_opacity, self._preview_frame_opacity, 0.0, 1.0),
        )
        for attribute, raw_value, current, minimum, maximum in numeric_values:
            try:
                value = current if raw_value is None else float(raw_value)
            except (TypeError, ValueError):
                value = current
            setattr(self, attribute, max(minimum, min(maximum, value)))
        for name, value in (
            ("combo_preview_fill_color", self._preview_fill_color_source),
            ("combo_preview_border_color", self._preview_border_color_source),
        ):
            combo = require_child(self._widget, QtWidgets.QComboBox, name)
            with QtCore.QSignalBlocker(combo):
                combo.setCurrentIndex(combo.findData(value))
        spin = require_child(
            self._widget, QtWidgets.QSpinBox, "spin_preview_fill_opacity"
        )
        with QtCore.QSignalBlocker(spin):
            spin.setValue(int(round(self._preview_fill_opacity * 100.0)))
        for name, value in (
            ("spin_body_opacity", self._preview_body_opacity * 100.0),
            ("spin_frame_width", self._preview_frame_width),
            ("spin_frame_opacity", self._preview_frame_opacity * 100.0),
        ):
            control = require_child(self._widget, QtWidgets.QSpinBox, name)
            with QtCore.QSignalBlocker(control):
                control.setValue(int(round(value)))
        self._sync_preview_controls()
        if emit:
            self._emit_style_changed()

    def _setup_preset_menu(self) -> None:
        button = require_child(self._widget, QtWidgets.QToolButton, "button_appearance_options")
        button.setMenu(self._build_preset_menu(button))
        button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")

    def _build_preset_menu(self, parent: QtWidgets.QWidget) -> QtWidgets.QMenu:
        menu = QtWidgets.QMenu(parent)
        self._load_project_menu = menu.addMenu("Load Project Recent")
        self._load_global_menu = menu.addMenu("Load Global Template")
        menu.addAction("Save Project Preset…").triggered.connect(self._save_project_preset)
        menu.addAction("Save Global Template…").triggered.connect(self._save_global_template)
        self._refresh_preset_menus()
        return menu

    def _setup_color_table(self, name: str, rows: list[tuple[str, ...]]) -> None:
        table = self._widget.findChild(QtWidgets.QTableWidget, name)
        if table is None:
            return
        blocker = QtCore.QSignalBlocker(table)
        table.clearSelection()
        table.setCurrentIndex(QtCore.QModelIndex())
        table.setProperty("appearance_table_name", name)
        if not bool(table.property("appearance_cell_connected")):
            table.cellClicked.connect(
                lambda row, column, table_name=name: self._on_color_table_cell_clicked(table_name, row, column)
            )
            table.setProperty("appearance_cell_connected", True)
        if not bool(table.property("appearance_header_connected")):
            table.horizontalHeader().sectionClicked.connect(
                lambda column, table_name=name: self._on_color_table_header_clicked(table_name, column)
            )
            table.setProperty("appearance_header_connected", True)
        conditional_target = {"table_fill_color_conditions": "point", "table_border_color_conditions": "border"}.get(name)
        table.setColumnCount(3)
        last_header = "condition" if name in {
            "table_fill_color_conditions",
            "table_border_color_conditions",
        } else "label"
        table.setHorizontalHeaderLabels(["", self._distribution_header_label(), last_header])
        if conditional_target and not table.property("conditional_connected"):
            table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
            table.customContextMenuRequested.connect(
                lambda position, target=conditional_target: self._show_conditional_menu(target, position)
            )
            table.setProperty("conditional_connected", True)
        distribution_header = table.horizontalHeaderItem(1)
        if distribution_header is not None:
            distribution_header.setToolTip("Choose whether percentages use the entire dataset or visible filtered items")
        logical_row_count = len(rows)
        # Qt's macOS accessibility adapter can retain cell indexes after a
        # model resize. Never shrink this backing model; spare rows stay
        # hidden while appearance_row_count tracks the rows containing data.
        capacity = max(1, table.rowCount(), logical_row_count)
        table.clearContents()
        if table.rowCount() < capacity:
            table.setRowCount(capacity)
        table.setProperty("appearance_row_count", logical_row_count)
        for row in range(capacity):
            table.setRowHidden(row, row >= logical_row_count)
        table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSortingEnabled(False)
        table.horizontalHeader().setSectionsClickable(True)
        table.horizontalHeader().setSortIndicatorShown(False)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.setShowGrid(True)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Fixed)
        table.verticalHeader().setMinimumSectionSize(12)
        table.verticalHeader().setDefaultSectionSize(18)
        table.horizontalHeader().setVisible(True)
        table.horizontalHeader().setFixedHeight(20)
        table.horizontalHeader().setDefaultAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
        )

        for row, values in enumerate(rows):
            if len(values) == 3:
                color, share, label = values
                total_share = share
                condition_key = label
            else:
                color, total_share, share, label = values[:4]
                condition_key = values[4] if len(values) >= 5 else label
            color_item = self._readonly_item("")
            color_item.setData(QtCore.Qt.ItemDataRole.UserRole, color)
            color_item.setBackground(QtGui.QColor(color))
            color_item.setToolTip(color)
            table.setItem(row, 0, color_item)

            total_item = self._readonly_item(total_share)
            total_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            total_item.setData(self._TOTAL_SHARE_ROLE, total_share)
            total_item.setData(self._VISIBLE_SHARE_ROLE, share)
            total_item.setText(self._distribution_share(total_item))
            table.setItem(row, 1, total_item)

            label_item = self._readonly_item(label)
            label_item.setData(self._CONDITION_KEY_ROLE, condition_key)
            label_item.setToolTip(label)
            table.setItem(row, 2, label_item)
            if conditional_target:
                warning = self._conflict_warnings[conditional_target].get(label)
                if warning:
                    label_item.setForeground(QtGui.QColor("#e53935"))
                    label_item.setToolTip(warning)

        self._adjust_color_columns(table)
        self._install_palette_header_button(table, name)

    def _wire_style_controls(self) -> None:
        for name in (
            "spin_point_size",
            "spin_point_size_min",
            "spin_point_size_max",
            "spin_border_width",
            "spin_border_width_min",
            "spin_border_width_max",
            "spin_point_opacity",
            "spin_border_opacity",
        ):
            spin = self._widget.findChild(QtWidgets.QSpinBox, name)
            if spin is not None:
                spin.valueChanged.connect(lambda _value: self._emit_style_changed())
                if name in {"spin_border_width", "spin_border_width_max"}:
                    spin.valueChanged.connect(lambda _value: self._update_border_color_enabled())
        for name in ("combo_point_size_column", "combo_border_width_column"):
            combo = self._widget.findChild(QtWidgets.QComboBox, name)
            if combo is not None:
                combo.currentIndexChanged.connect(lambda _index: self._emit_style_changed())
        self._update_border_color_enabled()

    def _update_border_color_enabled(self) -> None:
        group = self._widget.findChild(QtWidgets.QFrame, "group_border_color")
        if group is None:
            return
        width_mapped = self._property_page_index("stack_border_width") == 1
        width = self._spin_value("spin_border_width_max" if width_mapped else "spin_border_width", 0.0)
        group.setEnabled(width > 0.0)

    def _wire_border_controls(self) -> None:
        for name in ("combo_border_color_column", "radio_borders_categorical", "radio_borders_continuous"):
            widget = self._widget.findChild(QtWidgets.QWidget, name)
            if widget is None:
                continue
            if isinstance(widget, QtWidgets.QComboBox):
                widget.currentIndexChanged.connect(lambda _index: self._on_border_color_column_changed())
            elif isinstance(widget, QtWidgets.QAbstractButton):
                widget.toggled.connect(lambda _checked: self._on_border_color_mode_changed())

    def set_style_changed_callback(self, callback) -> None:
        self._style_changed_callback = callback

    def point_style(self) -> dict[str, object]:
        size_spin = self._widget.findChild(QtWidgets.QSpinBox, "spin_point_size")
        size = float(size_spin.value()) if size_spin is not None else 5.0
        size_combo = self._widget.findChild(QtWidgets.QComboBox, "combo_point_size_column")
        size_column = self._combo_value(size_combo) if size_combo is not None else ""
        mapped = (
            self._property_page_index("stack_point_size") == 1
            and size_column in self._continuous_color_columns
        )
        return {
            "size": max(1.0, size),
            "opacity": self._spin_value("spin_point_opacity", 100.0) / 100.0,
            "size_mode": "mapped" if mapped else "uniform",
            "size_column": size_column if mapped else "",
            "size_min": self._spin_value("spin_point_size_min", 1.0),
            "size_max": self._spin_value("spin_point_size_max", 30.0),
        }

    def point_color_style(self) -> dict[str, object]:
        self._capture_current_point_color_mapping()
        appearance_mode = self._color_appearance_mode("point")
        if appearance_mode == "uniform":
            color = self._uniform_color("point")
            return {
                "appearance_mode": "uniform",
                "mode": "categorical",
                "column": self._GLOBAL_LABEL,
                "palette": [color],
                "mapping": {self._GLOBAL_LABEL: color},
                "gradient_start": self._point_gradient_start,
                "gradient_end": self._point_gradient_end,
            }
        if appearance_mode == "conditional":
            return self._conditional_color_style("point")
        combo = self._widget.findChild(QtWidgets.QComboBox, "combo_point_color_column")
        categorical = self._widget.findChild(QtWidgets.QRadioButton, "radio_points_categorical")
        mode = "categorical" if categorical is None or categorical.isChecked() else "continuous"
        column = self._combo_value(combo) if combo is not None else self._GLOBAL_LABEL
        palette = self._table_palette("table_point_colors")
        mapping = self._point_color_mappings.get(column, self._color_table_mapping("table_point_colors"))
        return {
            "appearance_mode": "mapped",
            "mode": mode,
            "column": column,
            "palette": palette,
            "mapping": dict(mapping),
            "gradient_start": self._point_gradient_start,
            "gradient_end": self._point_gradient_end,
        }

    def border_style(self) -> dict[str, object]:
        self._capture_current_border_color_mapping()
        width_spin = self._widget.findChild(QtWidgets.QSpinBox, "spin_border_width")
        width_combo = self._widget.findChild(QtWidgets.QComboBox, "combo_border_width_column")
        width_column = self._combo_value(width_combo) if width_combo is not None else ""
        width_mapped = (
            self._property_page_index("stack_border_width") == 1
            and width_column in self._continuous_color_columns
        )
        color_mode = self._color_appearance_mode("border")
        if color_mode == "uniform":
            color = self._uniform_color("border")
            color_payload: dict[str, object] = {
                "appearance_mode": "uniform",
                "mode": "categorical",
                "column": self._GLOBAL_LABEL,
                "palette": [color],
                "mapping": {self._GLOBAL_LABEL: color},
            }
        elif color_mode == "conditional":
            color_payload = self._conditional_color_style("border")
        else:
            color_payload = {"appearance_mode": "mapped"}
        combo = self._widget.findChild(QtWidgets.QComboBox, "combo_border_color_column")
        categorical = self._widget.findChild(QtWidgets.QRadioButton, "radio_borders_categorical")
        mode = "categorical" if categorical is None or categorical.isChecked() else "continuous"
        column = self._combo_value(combo) if combo is not None else self._GLOBAL_LABEL
        mapping = self._border_color_mappings.get(column, self._color_table_mapping("table_border_colors"))
        mapped_color_payload = {
            "width": float(width_spin.value()) if width_spin is not None else 0.0,
            "opacity": self._spin_value("spin_border_opacity", 100.0) / 100.0,
            "width_mode": "mapped" if width_mapped else "uniform",
            "width_column": width_column if width_mapped else "",
            "width_min": self._spin_value("spin_border_width_min", 0.0),
            "width_max": self._spin_value("spin_border_width_max", 10.0),
            "mode": mode,
            "column": column,
            "palette": self._table_palette("table_border_colors"),
            "mapping": dict(mapping),
            "gradient_start": self._border_gradient_start,
            "gradient_end": self._border_gradient_end,
        }
        if color_mode == "mapped":
            return mapped_color_payload
        mapped_color_payload.update(color_payload)
        mapped_color_payload["width"] = float(width_spin.value()) if width_spin is not None else 0.0
        return mapped_color_payload

    def _property_page_index(self, stack_name: str) -> int:
        stack = self._widget.findChild(QtWidgets.QStackedWidget, stack_name)
        return int(stack.currentIndex()) if stack is not None else 0

    def _color_appearance_mode(self, target: str) -> str:
        index = self._property_page_index("stack_fill_color" if target == "point" else "stack_border_color")
        return ("uniform", "mapped", "conditional")[max(0, min(2, index))]

    def _spin_value(self, name: str, fallback: float) -> float:
        spin = self._widget.findChild(QtWidgets.QSpinBox, name)
        return float(spin.value()) if spin is not None else float(fallback)

    def _uniform_color(self, target: str) -> str:
        button_name = "swatch_uniform_point_color" if target == "point" else "swatch_uniform_border_color"
        button = self._widget.findChild(QtWidgets.QToolButton, button_name)
        fallback = "#000000" if target == "point" else "#111111"
        return str(button.property("colorHex") or fallback) if button is not None else fallback

    def _conditional_color_style(self, target: str) -> dict[str, object]:
        self._capture_conditional_table_colors(target)
        mapping = dict(self._conditional_mappings[target])
        fallback = "#000000" if target == "point" else "#111111"
        if "all rest" not in mapping:
            mapping["all rest"] = fallback
        rules = [
            {"field": field, "value": value, "label": self._conditional_rule_label(field, value)}
            for field, value in self._conditional_rules[target]
        ]
        return {
            "appearance_mode": "conditional",
            "mode": "categorical",
            "column": self._conditional_names[target],
            "custom": True,
            "rules": rules,
            "mapping": mapping,
            "palette": list(mapping.values()),
            "conflict_resolutions": dict(self._conflict_resolutions[target]),
        }

    def appearance_state_payload(self) -> dict[str, object]:
        self._capture_current_point_color_mapping()
        self._capture_current_border_color_mapping()
        payload = {
            "point": self.point_style(),
            "point_colors": self.point_color_style(),
            "border": self.border_style(),
            "previews": self.preview_style(),
        }
        payload["custom_color_lists"] = {
            target: (
                [
                    {
                        "name": self._conditional_names[target],
                        "rules": self._conditional_color_style(target)["rules"],
                        "mapping": dict(self._conditional_mappings[target]),
                        "conflict_resolutions": dict(self._conflict_resolutions[target]),
                    }
                ]
                if self._conditional_rules[target]
                else []
            )
            for target in ("point", "border")
        }
        return payload

    def _emit_style_changed(self) -> None:
        if self._style_changed_callback is not None:
            self._style_changed_callback(self.appearance_state_payload())

    def _wire_color_controls(self) -> None:
        for name in ("combo_point_color_column", "radio_points_categorical", "radio_points_continuous"):
            widget = self._widget.findChild(QtWidgets.QWidget, name)
            if widget is None:
                continue
            if isinstance(widget, QtWidgets.QComboBox):
                widget.currentIndexChanged.connect(lambda _index: self._on_point_color_column_changed())
            elif isinstance(widget, QtWidgets.QAbstractButton):
                widget.toggled.connect(lambda _checked: self._on_point_color_mode_changed())

    def _on_point_color_column_changed(self) -> None:
        self._capture_current_point_color_mapping(self._point_color_column_current)
        self._select_default_color_mode_for_column("point")
        self._enforce_color_mode_constraints("point")
        self._refresh_point_color_table_from_selection()
        self._point_color_column_current = self._current_combo_text("combo_point_color_column")
        self._emit_style_changed()

    def _on_point_color_mode_changed(self) -> None:
        self._enforce_color_mode_constraints("point")
        self._refresh_point_color_table_from_selection()
        self._emit_style_changed()

    def _on_border_color_column_changed(self) -> None:
        self._capture_current_border_color_mapping(self._border_color_column_current)
        self._select_default_color_mode_for_column("border")
        self._enforce_color_mode_constraints("border")
        self._refresh_border_color_table_from_selection()
        self._border_color_column_current = self._current_combo_text("combo_border_color_column")
        self._emit_style_changed()

    def _on_border_color_mode_changed(self) -> None:
        self._enforce_color_mode_constraints("border")
        self._refresh_border_color_table_from_selection()
        self._emit_style_changed()

    def _refresh_point_color_table_from_selection(self) -> None:
        categorical = self._widget.findChild(QtWidgets.QRadioButton, "radio_points_categorical")
        if categorical is not None and not categorical.isChecked():
            return
        combo = self._widget.findChild(QtWidgets.QComboBox, "combo_point_color_column")
        if combo is None:
            return
        column = self._combo_value(combo)
        if not column or column == self._NO_VALID_FIELDS_LABEL:
            self._setup_color_table("table_point_colors", [])
            return
        counts = self._metadata_value_counts(column)
        if not counts:
            self._setup_color_table("table_point_colors", self._GLOBAL_POINT_ROWS)
            return
        limited = counts if column in self._metadata_group_columns else counts[: self._MAX_CATEGORICAL_UNIQUE]
        saved = self._point_color_mappings.setdefault(column, {})
        total = max(1, sum(count for _label, count in counts))
        rows: list[tuple[str, str, str]] = []
        for label, count in limited:
            if label not in saved:
                saved[label] = next_unused_color(list(saved.values()))
            color = saved[label]
            share = f"{(100.0 * count / total):.1f}%"
            rows.append((color, share, label))
        self._setup_color_table("table_point_colors", rows)

    def _refresh_border_color_table_from_selection(self) -> None:
        categorical = self._widget.findChild(QtWidgets.QRadioButton, "radio_borders_categorical")
        if categorical is not None and not categorical.isChecked():
            return
        combo = self._widget.findChild(QtWidgets.QComboBox, "combo_border_color_column")
        if combo is None:
            return
        column = self._combo_value(combo)
        if not column or column == self._NO_VALID_FIELDS_LABEL:
            self._setup_color_table("table_border_colors", [])
            return
        counts = self._metadata_value_counts(column)
        if not counts:
            self._setup_color_table("table_border_colors", self._GLOBAL_BORDER_ROWS)
            return
        limited = counts if column in self._metadata_group_columns else counts[: self._MAX_CATEGORICAL_UNIQUE]
        saved = self._border_color_mappings.setdefault(column, {})
        total = max(1, sum(count for _label, count in counts))
        rows: list[tuple[str, str, str]] = []
        for label, count in limited:
            if label not in saved:
                saved[label] = next_unused_color(list(saved.values()))
            color = saved[label]
            share = f"{(100.0 * count / total):.1f}%"
            rows.append((color, share, label))
        self._setup_color_table("table_border_colors", rows)

    def _metadata_value_counts(self, column: str) -> list[tuple[str, int]]:
        cached = self._metadata_column_counts.get(column)
        if cached is not None:
            return list(cached)
        legacy_values = self._metadata_column_values.get(column)
        if legacy_values is not None:
            counts: dict[str, int] = {}
            for value in legacy_values:
                key = value if value else "(missing)"
                counts[key] = counts.get(key, 0) + 1
            loaded = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        elif self._metadata_access is not None:
            try:
                loaded = [
                    (value if value else "(missing)", int(count))
                    for value, count in self._metadata_access.value_counts(column)
                ]
            except Exception:
                loaded = []
        else:
            loaded = []
        self._metadata_column_counts[column] = loaded
        return list(loaded)

    def _color_table_mapping(self, table_name: str) -> dict[str, str]:
        table = self._widget.findChild(QtWidgets.QTableWidget, table_name)
        if table is None:
            return {}
        mapping: dict[str, str] = {}
        for row in range(self._table_logical_row_count(table)):
            color_item = table.item(row, 0)
            label_item = table.item(row, 2)
            if color_item is None or label_item is None:
                continue
            label = label_item.text().strip()
            color = str(color_item.data(QtCore.Qt.ItemDataRole.UserRole) or "").strip()
            if label and color:
                mapping[label] = color
        return mapping

    def _capture_conditional_table_colors(self, target: str) -> None:
        table = self._conditional_table(target)
        for row in range(self._table_logical_row_count(table)):
            color_item = table.item(row, 0)
            label_item = table.item(row, 2)
            if color_item is None or label_item is None:
                continue
            key = str(label_item.data(self._CONDITION_KEY_ROLE) or label_item.text()).strip()
            color = str(color_item.data(QtCore.Qt.ItemDataRole.UserRole) or "").strip()
            if not key or not color:
                continue
            if key.startswith("overlap:"):
                group_key = key.removeprefix("overlap:")
                resolution = self._conflict_resolutions[target].get(group_key)
                if resolution and resolution.get("type") == "color":
                    resolution["color"] = color
            else:
                self._conditional_mappings[target][key] = color

    def _current_combo_text(self, name: str) -> str:
        combo = self._widget.findChild(QtWidgets.QComboBox, name)
        if combo is None:
            return self._GLOBAL_LABEL
        return self._combo_value(combo) or self._GLOBAL_LABEL

    def _conditional_rule_label(self, field: str, value: str) -> str:
        label = self._metadata_field_labels.get(str(field))
        if label is None:
            label = (
                f"Missing group field ({field})"
                if str(field).startswith(("group:", "group-set:"))
                else str(field)
            )
        return f"{label}: {value}"

    def _capture_current_point_color_mapping(self, column: str | None = None) -> None:
        key = str(column or self._current_combo_text("combo_point_color_column")).strip() or self._GLOBAL_LABEL
        mapping = self._color_table_mapping("table_point_colors")
        if mapping:
            self._point_color_mappings[key] = mapping

    def _capture_current_border_color_mapping(self, column: str | None = None) -> None:
        key = str(column or self._current_combo_text("combo_border_color_column")).strip() or self._GLOBAL_LABEL
        mapping = self._color_table_mapping("table_border_colors")
        if mapping:
            self._border_color_mappings[key] = mapping

    def _table_palette(self, table_name: str) -> list[str]:
        table = self._widget.findChild(QtWidgets.QTableWidget, table_name)
        if table is None:
            return ["#4c78a8", "#f58518", "#54a24b"]
        values: list[str] = []
        for row in range(self._table_logical_row_count(table)):
            item = table.item(row, 0)
            if item is None:
                continue
            color = str(item.data(QtCore.Qt.ItemDataRole.UserRole) or "").strip()
            if color:
                values.append(color)
        return values or ["#4c78a8", "#f58518", "#54a24b"]

    @staticmethod
    def _readonly_item(text: str) -> QtWidgets.QTableWidgetItem:
        item = QtWidgets.QTableWidgetItem(text)
        item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
        return item

    @staticmethod
    def _table_logical_row_count(table: QtWidgets.QTableWidget) -> int:
        value = table.property("appearance_row_count")
        return max(0, int(value)) if isinstance(value, int) else table.rowCount()

    def _on_table_cell_clicked(self, table_name: str, row: int, column: int) -> None:
        if column == 0 and table_name in {
            "table_point_colors",
            "table_border_colors",
            "table_fill_color_conditions",
            "table_border_color_conditions",
        }:
            self._pick_table_color(table_name, row)

    def _on_color_table_cell_clicked(self, table_name: str, row: int, column: int) -> None:
        self._on_table_cell_clicked(table_name, row, column)

    def _on_color_table_header_clicked(self, table_name: str, column: int) -> None:
        if column == 1:
            self._show_distribution_scope_menu(table_name)

    @staticmethod
    def _adjust_color_columns(table: QtWidgets.QTableWidget) -> None:
        square = table.verticalHeader().defaultSectionSize() or 18
        table.setColumnWidth(0, square)
        table.horizontalHeader().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)

    def _distribution_header_label(self) -> str:
        if self._distribution_scope == self._DISTRIBUTION_VISIBLE:
            return "vis"
        return "all"

    def _distribution_share(self, item: QtWidgets.QTableWidgetItem) -> str:
        role = (
            self._VISIBLE_SHARE_ROLE
            if self._distribution_scope == self._DISTRIBUTION_VISIBLE
            else self._TOTAL_SHARE_ROLE
        )
        return str(item.data(role) or "0.0%")

    def _refresh_distribution_values(self, table: QtWidgets.QTableWidget) -> None:
        header_item = table.horizontalHeaderItem(1)
        if header_item is not None:
            header_item.setText(self._distribution_header_label())
        for row in range(self._table_logical_row_count(table)):
            item = table.item(row, 1)
            if item is not None:
                item.setText(self._distribution_share(item))
        self._adjust_color_columns(table)

    def _set_distribution_scope(self, scope: str) -> None:
        if scope not in {self._DISTRIBUTION_TOTAL, self._DISTRIBUTION_VISIBLE}:
            return
        self._distribution_scope = scope
        for table_name in self._COLOR_TABLE_NAMES:
            table = self._widget.findChild(QtWidgets.QTableWidget, table_name)
            if table is not None:
                self._refresh_distribution_values(table)

    def _show_distribution_scope_menu(self, table_name: str) -> None:
        table = self._widget.findChild(QtWidgets.QTableWidget, table_name)
        if table is None:
            return
        if self._distribution_menu is not None:
            self._distribution_menu.deleteLater()
        menu = QtWidgets.QMenu(table.horizontalHeader())
        self._distribution_menu = menu
        group = QtGui.QActionGroup(menu)
        group.setExclusive(True)
        for label, scope in (
            ("Entire Distribution", self._DISTRIBUTION_TOTAL),
            ("Visible Distribution", self._DISTRIBUTION_VISIBLE),
        ):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(scope == self._distribution_scope)
            group.addAction(action)
            action.triggered.connect(
                lambda _checked=False, scope=scope: self._set_distribution_scope(scope)
            )
        header = table.horizontalHeader()
        position = QtCore.QPoint(header.sectionViewportPosition(1), header.height())
        menu.popup(header.mapToGlobal(position))

    def _install_palette_header_button(self, table: QtWidgets.QTableWidget, table_name: str) -> None:
        header = table.horizontalHeader()
        for existing in header.findChildren(QtWidgets.QToolButton, f"button_{table_name}_palette"):
            existing.deleteLater()
        button = QtWidgets.QToolButton(header)
        button.setObjectName(f"button_{table_name}_palette")
        button.setAutoRaise(True)
        button.setProperty("iconName", "color_swatch")
        button.setIconSize(QtCore.QSize(14, 14))
        button.setFixedSize(18, 18)
        button.setToolTip("Set color palette")
        button.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
        menu = QtWidgets.QMenu(button)
        for label, palette in palette_options():
            action = menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, table_name=table_name, palette=palette: self._apply_palette_to_table(table_name, list(palette))
            )
        button.setMenu(menu)
        button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        apply_icon_color(button)
        button.move(0, 1)
        button.show()

    def _pick_table_color(self, table_name: str, row: int) -> None:
        table = self._widget.findChild(QtWidgets.QTableWidget, table_name)
        if table is None or not (0 <= row < self._table_logical_row_count(table)):
            return
        current = table.item(row, 0)
        current_color = QtGui.QColor(str(current.data(QtCore.Qt.ItemDataRole.UserRole) or "#4c78a8")) if current else QtGui.QColor("#4c78a8")
        color = QtWidgets.QColorDialog.getColor(current_color, self._widget, "Select Color")
        if not color.isValid():
            return
        self._set_table_row_color(table, row, color.name())
        self._emit_style_changed()

    def _apply_palette_to_table(self, table_name: str, palette: list[str]) -> None:
        table = self._widget.findChild(QtWidgets.QTableWidget, table_name)
        if table is None or not palette:
            return
        row_count = self._table_logical_row_count(table)
        if row_count == 0:
            return
        colors = self._distinct_palette(row_count, fallback=palette)
        for row, color in enumerate(colors):
            self._set_table_row_color(table, row, color)
        if table_name == "table_point_colors":
            self._capture_current_point_color_mapping()
        elif table_name == "table_border_colors":
            self._capture_current_border_color_mapping()
        elif table_name == "table_fill_color_conditions":
            self._capture_conditional_table_colors("point")
        elif table_name == "table_border_color_conditions":
            self._capture_conditional_table_colors("border")
        self._emit_style_changed()

    def _set_table_row_color(self, table: QtWidgets.QTableWidget, row: int, color_hex: str) -> None:
        item = table.item(row, 0)
        if item is None:
            item = self._readonly_item("")
            table.setItem(row, 0, item)
        item.setData(QtCore.Qt.ItemDataRole.UserRole, color_hex)
        item.setBackground(QtGui.QColor(color_hex))
        item.setToolTip(color_hex)

    def _set_gradient_preview(self, name: str, start: str, end: str) -> None:
        frame = self._widget.findChild(QtWidgets.QFrame, name)
        if frame is None:
            return
        frame.setStyleSheet(
            "QFrame {"
            "border: 1px solid #444;"
            "border-radius: 4px;"
            "background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
            f" stop:0 {start}, stop:1 {end});"
            "}"
        )

    def _pick_gradient_color(self, target: str, side: str) -> None:
        if target == "point":
            initial = self._point_gradient_start if side == "start" else self._point_gradient_end
        else:
            initial = self._border_gradient_start if side == "start" else self._border_gradient_end
        selected = QtWidgets.QColorDialog.getColor(QtGui.QColor(initial), self._widget, "Select Color")
        if not selected.isValid():
            return
        chosen = selected.name()
        if target == "point":
            if side == "start":
                self._point_gradient_start = chosen
            else:
                self._point_gradient_end = chosen
            self._set_gradient_preview("frame_point_gradient_preview", self._point_gradient_start, self._point_gradient_end)
        else:
            if side == "start":
                self._border_gradient_start = chosen
            else:
                self._border_gradient_end = chosen
            self._set_gradient_preview("frame_border_gradient_preview", self._border_gradient_start, self._border_gradient_end)
        self._emit_style_changed()

    def set_save_project_preset_callback(self, callback: Callable[[str, dict], bool] | None) -> None:
        self._save_project_preset_callback = callback

    def set_load_project_preset_callback(self, callback: Callable[[str], dict | None] | None) -> None:
        self._load_project_preset_callback = callback

    def set_save_global_template_callback(self, callback: Callable[[str, dict], bool] | None) -> None:
        self._save_global_template_callback = callback

    def set_load_global_template_callback(self, callback: Callable[[str], dict | None] | None) -> None:
        self._load_global_template_callback = callback

    def set_recent_project_preset_labels(self, labels: list[str]) -> None:
        self._project_preset_labels = list(labels)
        self._refresh_preset_menus()

    def set_global_template_labels(self, labels: list[str]) -> None:
        self._global_template_labels = list(labels)
        self._refresh_preset_menus()

    def _refresh_preset_menus(self) -> None:
        project_menu = getattr(self, "_load_project_menu", None)
        if project_menu is not None:
            project_menu.clear()
            if not self._project_preset_labels:
                empty = project_menu.addAction("No Presets Saved")
                empty.setEnabled(False)
            else:
                for label in self._project_preset_labels[:10]:
                    action = project_menu.addAction(label)
                    action.triggered.connect(
                        lambda _checked=False, value=label: self._load_project_preset_by_label(value)
                    )
        global_menu = getattr(self, "_load_global_menu", None)
        if global_menu is not None:
            global_menu.clear()
            if not self._global_template_labels:
                empty = global_menu.addAction("No Templates Saved")
                empty.setEnabled(False)
            else:
                for label in self._global_template_labels[:10]:
                    action = global_menu.addAction(label)
                    action.triggered.connect(
                        lambda _checked=False, value=label: self._load_global_template_by_label(value)
                    )

    def _save_project_preset(self) -> None:
        if self._save_project_preset_callback is None:
            self._show_status("Open a project before saving appearance presets.")
            return
        name, ok = QtWidgets.QInputDialog.getText(self._widget, "Save Appearance Preset", "Preset name:")
        if not ok:
            return
        preset_name = name.strip()
        if not preset_name:
            self._show_status("Preset name cannot be empty.")
            return
        if self._save_project_preset_callback(preset_name, self.appearance_state_payload()):
            self._show_status(f"Saved appearance preset: {preset_name}")

    def _save_global_template(self) -> None:
        if self._save_global_template_callback is None:
            return
        name, ok = QtWidgets.QInputDialog.getText(self._widget, "Save Global Appearance Template", "Template name:")
        if not ok:
            return
        template_name = name.strip()
        if not template_name:
            self._show_status("Template name cannot be empty.")
            return
        if self._save_global_template_callback(template_name, self.appearance_state_payload()):
            self._show_status(f"Saved global appearance template: {template_name}")

    def _load_project_preset_by_label(self, label: str) -> None:
        if self._load_project_preset_callback is None:
            return
        payload = self._load_project_preset_callback(label)
        if isinstance(payload, dict):
            self.apply_preset_payload(payload)
            self._show_status(f"Loaded appearance preset: {label}")

    def _load_global_template_by_label(self, label: str) -> None:
        if self._load_global_template_callback is None:
            return
        payload = self._load_global_template_callback(label)
        if isinstance(payload, dict):
            skipped = self._apply_global_template_payload(payload)
            if skipped:
                self._show_status(
                    "Loaded global appearance template; skipped unavailable metadata columns: "
                    + ", ".join(skipped)
                )
            else:
                self._show_status(f"Loaded global appearance template: {label}")

    def load_preset_path(self, path: Path) -> bool:
        from DiscoveryArea.presets_repository import PresetRepository

        payload = PresetRepository(path.parent).load_payload_from_path(path)
        if not isinstance(payload, dict):
            self._show_status(f"Failed to load appearance preset: {path.name}")
            return False
        self.apply_preset_payload(payload)
        self._show_status(f"Loaded appearance preset: {path.stem}")
        return True

    def apply_preset_payload(self, payload: dict) -> bool:
        if not isinstance(payload, dict):
            return False
        self._apply_preset_payload(payload)
        return True

    def _apply_preset_payload(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        point = payload.get("point", {}) if isinstance(payload.get("point"), dict) else {}
        point_colors = payload.get("point_colors", {}) if isinstance(payload.get("point_colors"), dict) else {}
        border = payload.get("border", {}) if isinstance(payload.get("border"), dict) else {}
        previews = payload.get("previews", {}) if isinstance(payload.get("previews"), dict) else {}
        self._restore_mapping_cache(point_colors, border)
        try:
            self._point_opacity = max(0.0, min(1.0, float(point.get("opacity", 1.0))))
        except (TypeError, ValueError):
            self._point_opacity = 1.0
        try:
            self._border_opacity = max(0.0, min(1.0, float(border.get("opacity", 1.0))))
        except (TypeError, ValueError):
            self._border_opacity = 1.0
        self._set_spin_value("spin_point_size", point.get("size"))
        self._set_spin_value("spin_point_opacity", self._point_opacity * 100.0)
        self._set_spin_value("spin_border_width", border.get("width"))
        self._set_spin_value("spin_border_opacity", self._border_opacity * 100.0)
        self.set_preview_style(
            fill_color_source=previews.get(
                "fill_color_source", self._DEFAULT_PREVIEW_FILL_SOURCE
            ),
            border_color_source=previews.get(
                "border_color_source", self._DEFAULT_PREVIEW_BORDER_SOURCE
            ),
            fill_opacity=previews.get(
                "fill_opacity", self._DEFAULT_PREVIEW_FILL_OPACITY
            ),
            body_opacity=previews.get(
                "body_opacity", self._DEFAULT_PREVIEW_BODY_OPACITY
            ),
            frame_width=previews.get(
                "frame_width", self._DEFAULT_PREVIEW_FRAME_WIDTH
            ),
            frame_opacity=previews.get(
                "frame_opacity", self._DEFAULT_PREVIEW_FRAME_OPACITY
            ),
            emit=False,
        )
        self._set_spin_value("spin_point_size_min", point.get("size_min"))
        self._set_spin_value("spin_point_size_max", point.get("size_max"))
        self._set_spin_value("spin_border_width_min", border.get("width_min"))
        self._set_spin_value("spin_border_width_max", border.get("width_max"))
        self._set_combo_text("combo_point_size_column", str(point.get("size_column", "")))
        self._set_combo_text("combo_border_width_column", str(border.get("width_column", "")))
        self._set_combo_text("combo_point_color_column", str(point_colors.get("column", self._GLOBAL_LABEL)))
        self._set_combo_text("combo_border_color_column", str(border.get("column", self._GLOBAL_LABEL)))
        self._set_mode_button("radio_points_categorical", "radio_points_continuous", str(point_colors.get("mode", "categorical")))
        self._set_mode_button("radio_borders_categorical", "radio_borders_continuous", str(border.get("mode", "categorical")))
        self._set_property_mode("stack_point_size", str(point.get("size_mode") or "uniform"))
        self._set_property_mode("stack_border_width", str(border.get("width_mode") or "uniform"))
        self._set_property_mode(
            "stack_fill_color",
            str(
                point_colors.get("appearance_mode")
                or ("conditional" if bool(point_colors.get("custom")) else "mapped" if str(point_colors.get("column") or self._GLOBAL_LABEL) != self._GLOBAL_LABEL else "uniform")
            ),
        )
        self._set_property_mode(
            "stack_border_color",
            str(
                border.get("appearance_mode")
                or ("conditional" if bool(border.get("custom")) else "mapped" if str(border.get("column") or self._GLOBAL_LABEL) != self._GLOBAL_LABEL else "uniform")
            ),
        )
        point_mapping = point_colors.get("mapping") if isinstance(point_colors.get("mapping"), dict) else {}
        border_mapping = border.get("mapping") if isinstance(border.get("mapping"), dict) else {}
        self._set_uniform_swatch("point", str(point_mapping.get(self._GLOBAL_LABEL) or self._table_palette("table_point_colors")[0]))
        self._set_uniform_swatch("border", str(border_mapping.get(self._GLOBAL_LABEL) or self._table_palette("table_border_colors")[0]))
        self._restore_conditional_style("point", point_colors)
        self._restore_conditional_style("border", border)
        self._sync_mode_stack_from_radios(
            stack_name="stack_point_color_mode",
            categorical_name="radio_points_categorical",
            continuous_name="radio_points_continuous",
        )
        self._sync_mode_stack_from_radios(
            stack_name="stack_border_color_mode",
            categorical_name="radio_borders_categorical",
            continuous_name="radio_borders_continuous",
        )
        self._point_gradient_start = str(point_colors.get("gradient_start") or self._point_gradient_start)
        self._point_gradient_end = str(point_colors.get("gradient_end") or self._point_gradient_end)
        self._border_gradient_start = str(border.get("gradient_start") or self._border_gradient_start)
        self._border_gradient_end = str(border.get("gradient_end") or self._border_gradient_end)
        self._set_gradient_preview("frame_point_gradient_preview", self._point_gradient_start, self._point_gradient_end)
        self._set_gradient_preview("frame_border_gradient_preview", self._border_gradient_start, self._border_gradient_end)
        # Rebuild dependent UI explicitly because combo/radio setters use blocked signals.
        self._enforce_color_mode_constraints("point")
        self._enforce_color_mode_constraints("border")
        self._refresh_point_color_table_from_selection()
        self._refresh_border_color_table_from_selection()
        if isinstance(point_colors.get("palette"), list):
            self._apply_palette_to_table("table_point_colors", [str(item) for item in point_colors["palette"]])
        point_mapping = point_colors.get("mapping")
        if isinstance(point_mapping, dict):
            self._apply_color_mapping("table_point_colors", point_mapping)
        if isinstance(border.get("palette"), list):
            self._apply_palette_to_table("table_border_colors", [str(item) for item in border["palette"]])
        border_mapping = border.get("mapping")
        if isinstance(border_mapping, dict):
            self._apply_color_mapping("table_border_colors", border_mapping)
        self._emit_style_changed()

    def _set_property_mode(self, stack_name: str, mode: str) -> None:
        stack = self._widget.findChild(QtWidgets.QStackedWidget, stack_name)
        if stack is None:
            return
        options = ("uniform", "mapped", "conditional")
        try:
            index = options.index(str(mode).strip().lower())
        except ValueError:
            index = 0
        index = min(index, stack.count() - 1)
        prefixes = {
            "stack_point_size": "button_point_size_",
            "stack_fill_color": "button_fill_color_",
            "stack_border_width": "button_border_width_",
            "stack_border_color": "button_border_color_",
        }
        button = self._widget.findChild(
            QtWidgets.QAbstractButton,
            prefixes.get(stack_name, "") + options[index],
        )
        if not stack.widget(index).isEnabledTo(stack):
            index = 0
            button = self._widget.findChild(
                QtWidgets.QAbstractButton,
                prefixes.get(stack_name, "") + options[index],
            )
        self._select_property_page(stack, index, emit=False)
        if button is not None:
            button.setChecked(True)

    def _restore_conditional_style(self, target: str, payload: dict) -> None:
        raw_rules = payload.get("rules")
        if not isinstance(raw_rules, list):
            return
        name = str(payload.get("column") or "Conditional").strip()
        if name and name.casefold() not in {"global", "all items", "none"}:
            self._conditional_names[target] = name
        rules: list[tuple[str, str]] = []
        renamed_labels: dict[str, str] = {}
        for rule in ordered_rules(raw_rules):
            if not isinstance(rule, dict):
                continue
            field = str(rule.get("field") or "").strip()
            value = str(rule.get("value") or "").strip()
            if field and value:
                rules.append((field, value))
                stored_label = str(rule.get("label") or "").strip()
                current_label = self._conditional_rule_label(field, value)
                if stored_label and stored_label != current_label:
                    renamed_labels[stored_label] = current_label
        self._conditional_rules[target] = rules
        mapping = payload.get("mapping")
        self._conditional_mappings[target] = self._normalized_str_mapping(mapping) if isinstance(mapping, dict) else {}
        for old_label, new_label in renamed_labels.items():
            if old_label in self._conditional_mappings[target] and new_label not in self._conditional_mappings[target]:
                self._conditional_mappings[target][new_label] = self._conditional_mappings[target].pop(old_label)
        raw_resolutions = payload.get("conflict_resolutions")
        resolutions: dict[str, dict[str, str]] = {}
        if isinstance(raw_resolutions, dict):
            for key, resolution in raw_resolutions.items():
                if isinstance(resolution, dict) and resolution.get("type") in {"rule", "color"}:
                    labels = overlap_labels(str(key))
                    resolved_key = (
                        overlap_key([renamed_labels.get(label, label) for label in labels])
                        if labels
                        else str(key)
                    )
                    resolutions[resolved_key] = {
                        str(name): str(value) for name, value in resolution.items()
                    }
                    winner = resolutions[resolved_key].get("winner")
                    if winner in renamed_labels:
                        resolutions[resolved_key]["winner"] = renamed_labels[winner]
        # Migrate presets created by the brief overlap-table implementation.
        old_mapping = payload.get("overlap_mapping")
        if isinstance(old_mapping, dict):
            for key, color in old_mapping.items():
                resolutions.setdefault(str(key), {"type": "color", "color": str(color)})
        self._conflict_resolutions[target] = resolutions
        self._dismissed_conflicts[target].clear()
        self._refresh_conditional_table(target, capture_colors=False)

    def _restore_mapping_cache(self, point_colors: dict, border: dict) -> None:
        point_column = str(point_colors.get("column") or self._GLOBAL_LABEL).strip() or self._GLOBAL_LABEL
        point_mapping = point_colors.get("mapping")
        if isinstance(point_mapping, dict):
            self._point_color_mappings[point_column] = self._normalized_str_mapping(point_mapping)
        border_column = str(border.get("column") or self._GLOBAL_LABEL).strip() or self._GLOBAL_LABEL
        border_mapping = border.get("mapping")
        if isinstance(border_mapping, dict):
            self._border_color_mappings[border_column] = self._normalized_str_mapping(border_mapping)

    def _apply_global_template_payload(self, payload: dict) -> list[str]:
        if not isinstance(payload, dict):
            return []
        adjusted = dict(payload)
        skipped: list[str] = []
        for key in ("point_colors", "border"):
            section = payload.get(key)
            if not isinstance(section, dict):
                continue
            column = str(section.get("column") or self._GLOBAL_LABEL).strip() or self._GLOBAL_LABEL
            if (
                column in {self._GLOBAL_LABEL, "All items"}
                or column in self._metadata_column_names
                or column in self._metadata_column_values
            ):
                continue
            next_section = dict(section)
            next_section["column"] = self._GLOBAL_LABEL
            adjusted[key] = next_section
            skipped.append(column)
        self._apply_preset_payload(adjusted)
        return sorted(set(skipped))

    @staticmethod
    def _normalized_str_mapping(mapping: dict) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, value in mapping.items():
            label = str(key).strip()
            mapped = str(value).strip()
            if label and mapped:
                normalized[label] = mapped
        return normalized

    def _sync_mode_stack_from_radios(self, *, stack_name: str, categorical_name: str, continuous_name: str) -> None:
        stack = self._widget.findChild(QtWidgets.QStackedWidget, stack_name)
        categorical = self._widget.findChild(QtWidgets.QRadioButton, categorical_name)
        continuous = self._widget.findChild(QtWidgets.QRadioButton, continuous_name)
        if stack is None or categorical is None or continuous is None:
            return
        current_index = 1 if continuous.isChecked() and stack.count() > 1 else 0
        stack.setCurrentIndex(current_index)
        for page_index in range(stack.count()):
            page = stack.widget(page_index)
            policy = page.sizePolicy()
            policy.setVerticalPolicy(
                QtWidgets.QSizePolicy.Policy.Preferred
                if page_index == current_index
                else QtWidgets.QSizePolicy.Policy.Ignored
            )
            page.setSizePolicy(policy)
        stack.updateGeometry()
        QtCore.QTimer.singleShot(0, self._sync_scroll_content_height_deferred)

    def _apply_color_mapping(self, table_name: str, mapping: dict) -> None:
        table = self._widget.findChild(QtWidgets.QTableWidget, table_name)
        if table is None:
            return
        normalized: dict[str, str] = {}
        for key, value in mapping.items():
            label = str(key).strip()
            color = str(value).strip()
            if label and color:
                normalized[label] = color
        if not normalized:
            return
        for row in range(self._table_logical_row_count(table)):
            label_item = table.item(row, 2)
            if label_item is None:
                continue
            color = normalized.get(label_item.text().strip())
            if color:
                self._set_table_row_color(table, row, color)
        if table_name == "table_point_colors":
            self._capture_current_point_color_mapping()
        elif table_name == "table_border_colors":
            self._capture_current_border_color_mapping()

    def _set_spin_value(self, name: str, value: object) -> None:
        spin = self._widget.findChild(QtWidgets.QSpinBox, name)
        if spin is None:
            return
        try:
            numeric = int(round(float(value)))
        except Exception:
            return
        spin.blockSignals(True)
        spin.setValue(max(spin.minimum(), min(spin.maximum(), numeric)))
        spin.blockSignals(False)

    def _set_combo_text(self, name: str, text: str) -> None:
        combo = self._widget.findChild(QtWidgets.QComboBox, name)
        if combo is None:
            return
        index = combo.findData(text)
        if index < 0:
            index = combo.findText(text)
        if index < 0 and name in {"combo_point_size_column", "combo_border_width_column"}:
            return
        if index < 0:
            index = combo.findText(self._GLOBAL_LABEL)
        if index < 0:
            index = combo.findText("All items")
        if index < 0:
            return
        combo.blockSignals(True)
        combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _set_mode_button(self, categorical_name: str, continuous_name: str, mode: str) -> None:
        categorical = self._widget.findChild(QtWidgets.QRadioButton, categorical_name)
        continuous = self._widget.findChild(QtWidgets.QRadioButton, continuous_name)
        target = continuous if mode.lower() == "continuous" else categorical
        if target is None:
            return
        target.blockSignals(True)
        target.setChecked(True)
        target.blockSignals(False)

    @staticmethod
    def _is_numeric_column(values: list[str]) -> bool:
        saw_numeric = False
        for value in values:
            text = str(value).strip()
            if not text:
                continue
            try:
                float(text)
                saw_numeric = True
            except ValueError:
                return False
        return saw_numeric

    def _combo_value(self, combo: QtWidgets.QComboBox) -> str:
        value = str(combo.currentData() or combo.currentText() or "").strip()
        if value == self._NO_VALID_FIELDS_LABEL:
            return ""
        return value

    def _add_metadata_field_options(
        self, combo: QtWidgets.QComboBox, names: list[str]
    ) -> None:
        groups = [name for name in names if name in self._metadata_group_columns]
        metadata = [name for name in names if name not in self._metadata_group_columns]
        for name in groups:
            combo.addItem(self._metadata_field_labels.get(name, name), name)
        if groups and metadata:
            combo.insertSeparator(combo.count())
        for name in metadata:
            combo.addItem(self._metadata_field_labels.get(name, name), name)

    def _repopulate_color_combo(self, target: str) -> None:
        combo_name = "combo_point_color_column" if target == "point" else "combo_border_color_column"
        combo = self._widget.findChild(QtWidgets.QComboBox, combo_name)
        if combo is None:
            return
        current = self._combo_value(combo)
        eligible = set(self._categorical_color_columns) | set(self._continuous_color_columns)
        options = [
            name for name in self._metadata_color_column_order if name in eligible
        ]
        combo.blockSignals(True)
        combo.clear()
        if options:
            self._add_metadata_field_options(combo, options)
            selected = combo.findData(current if current in options else options[0])
            combo.setCurrentIndex(max(0, selected))
            combo.setEnabled(True)
        else:
            combo.addItem(self._NO_VALID_FIELDS_LABEL)
            combo.setEnabled(False)
        combo.blockSignals(False)
        self._refresh_dynamic_combo_tooltips(combo)
        ui_target = "fill" if target == "point" else "border"
        self._set_property_page_available(
            f"stack_{ui_target}_color", f"button_{ui_target}_color_mapped", 1, bool(options)
        )
        self._select_default_color_mode_for_column(target)
        self._enforce_color_mode_constraints(target)

    def _select_default_color_mode_for_column(self, target: str) -> None:
        if target == "point":
            combo_name = "combo_point_color_column"
            categorical_name = "radio_points_categorical"
            continuous_name = "radio_points_continuous"
            stack_name = "stack_point_color_mode"
        else:
            combo_name = "combo_border_color_column"
            categorical_name = "radio_borders_categorical"
            continuous_name = "radio_borders_continuous"
            stack_name = "stack_border_color_mode"
        combo = self._widget.findChild(QtWidgets.QComboBox, combo_name)
        categorical = self._widget.findChild(QtWidgets.QRadioButton, categorical_name)
        continuous = self._widget.findChild(QtWidgets.QRadioButton, continuous_name)
        if combo is None or categorical is None or continuous is None:
            return
        column = self._combo_value(combo)
        if column in self._categorical_color_columns:
            target_button = categorical
        elif column in self._continuous_color_columns:
            target_button = continuous
        else:
            return
        target_button.blockSignals(True)
        target_button.setChecked(True)
        target_button.blockSignals(False)
        self._sync_mode_stack_from_radios(
            stack_name=stack_name,
            categorical_name=categorical_name,
            continuous_name=continuous_name,
        )

    def _enforce_color_mode_constraints(self, target: str) -> None:
        if target == "point":
            combo_name = "combo_point_color_column"
            categorical_name = "radio_points_categorical"
            continuous_name = "radio_points_continuous"
        else:
            combo_name = "combo_border_color_column"
            categorical_name = "radio_borders_categorical"
            continuous_name = "radio_borders_continuous"
        combo = self._widget.findChild(QtWidgets.QComboBox, combo_name)
        categorical = self._widget.findChild(QtWidgets.QRadioButton, categorical_name)
        continuous = self._widget.findChild(QtWidgets.QRadioButton, continuous_name)
        if combo is None or categorical is None or continuous is None:
            return
        column = self._combo_value(combo)
        categorical_ok = column in self._categorical_color_columns
        continuous_ok = column in self._continuous_color_columns
        categorical.setEnabled(categorical_ok)
        continuous.setEnabled(continuous_ok)
        if categorical.isChecked() and not categorical_ok and continuous_ok:
            continuous.setChecked(True)
        elif continuous.isChecked() and not continuous_ok and categorical_ok:
            categorical.setChecked(True)

    def _distinct_palette(self, count: int, *, fallback: list[str] | None = None) -> list[str]:
        return distinct_palette(count, fallback=fallback)
