from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, fields, replace
from pathlib import Path
from typing import Protocol

from PySide6 import QtCore, QtWidgets

from FiguresArea.domain import FigurePlotSettings
from shared.qt.panel_controller import UiPanelController


_FIGURE_EXPORT_OPTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Portable Document Format", ("pdf",)),
    ("Portable Network Graphics", ("png",)),
    ("JPEG Image", ("jpeg", "jpg")),
    ("WebP Image", ("webp",)),
    ("Scalable Vector Graphics", ("svg",)),
    ("Tagged Image File Format", ("tif", "tiff")),
)


class FigureSettingsTarget(Protocol):
    """Small interface shared by static and animation figure tabs."""

    @property
    def widget(self) -> QtWidgets.QWidget: ...

    def figure_settings(self) -> FigurePlotSettings: ...

    def apply_figure_settings(
        self,
        settings: FigurePlotSettings,
        *,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> None: ...

    def export_figure(
        self,
        path: Path,
        *,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> None: ...


class FigureSettingsPanelController(UiPanelController):
    package = "FiguresArea"
    ui_path = "ui/plot_settings_panel.ui"

    def _wire(self) -> None:
        self._active_plot_tab: FigureSettingsTarget | None = None
        self._figure_setting_widgets: dict[str, QtWidgets.QWidget] = {}
        self._save_preset_callback: Callable[[str, dict], bool] | None = None
        self._load_preset_callback: Callable[[str], dict | None] | None = None
        self._options_menu: QtWidgets.QMenu | None = None
        self._menu_load_recent: QtWidgets.QMenu | None = None
        self._settings_unapplied = False
        self._operation: str | None = None
        self._wire_page_buttons()
        self._populate_figure_settings_tree()
        self._configure_options_menu()
        apply_button = self._widget.findChild(QtWidgets.QPushButton, "button_apply_figure_settings")
        if apply_button is not None:
            apply_button.setProperty("hasRealAction", True)
            apply_button.clicked.connect(self._apply_figure_settings)
        export_button = self._widget.findChild(QtWidgets.QPushButton, "button_export_figure")
        if export_button is not None:
            export_button.setProperty("hasRealAction", True)
            export_button.clicked.connect(self._export_figure)
        self._wire_dynamic_setting_states()
        self._wire_parameter_change_tracking()
        self._update_dynamic_setting_states()

    def _configure_options_menu(self) -> None:
        button = self._widget.findChild(QtWidgets.QToolButton, "button_figure_settings_options")
        if button is None:
            return
        button.setProperty("hasRealAction", True)
        menu = QtWidgets.QMenu(button)
        self._menu_load_recent = QtWidgets.QMenu("Load Recent", menu)
        menu.addMenu(self._menu_load_recent)
        menu.addAction("Save Preset…").triggered.connect(self._save_current_preset)
        button.setMenu(menu)
        button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
        self._options_menu = menu
        self.set_recent_preset_labels([])

    def _wire_page_buttons(self) -> None:
        pages = {
            "button_figure_page": "panel_figure_page",
            "button_title_page": "panel_title_page",
            "button_plot_page": "panel_plot_page",
            "button_legend_page": "panel_legend_page",
            "button_caption_page": "panel_caption_page",
        }
        for button_name, page_name in pages.items():
            button = self._widget.findChild(QtWidgets.QPushButton, button_name)
            page = self._widget.findChild(QtWidgets.QWidget, page_name)
            if button is None or page is None:
                continue
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, name=page_name: self._show_settings_page(name))
        self._show_settings_page("panel_figure_page")

    def set_active_plot_tab(self, plot_tab: FigureSettingsTarget | None) -> None:
        self._active_plot_tab = plot_tab
        self._sync_figure_settings_from_active_tab()
        trees = [
            self._widget.findChild(QtWidgets.QTreeWidget, "tree_figure_parameters"),
            self._widget.findChild(QtWidgets.QTreeWidget, "tree_title_parameters"),
            self._widget.findChild(QtWidgets.QTreeWidget, "tree_legend_parameters"),
            self._widget.findChild(QtWidgets.QTreeWidget, "tree_caption_parameters"),
        ]
        apply_button = self._widget.findChild(QtWidgets.QPushButton, "button_apply_figure_settings")
        enabled = plot_tab is not None
        for tree in trees:
            if tree is not None:
                tree.setEnabled(enabled)
        self._settings_unapplied = False
        self._refresh_action_buttons()

    def set_save_preset_callback(self, callback: Callable[[str, dict], bool] | None) -> None:
        self._save_preset_callback = callback

    def set_load_preset_callback(self, callback: Callable[[str], dict | None] | None) -> None:
        self._load_preset_callback = callback

    def set_recent_preset_labels(self, labels: list[str]) -> None:
        if self._menu_load_recent is None:
            return
        self._menu_load_recent.clear()
        if not labels:
            action = self._menu_load_recent.addAction("No Presets Saved")
            action.setEnabled(False)
            return
        for label in labels[:10]:
            action = self._menu_load_recent.addAction(label)
            action.triggered.connect(lambda _checked=False, value=label: self._load_preset_label(value))

    def show_page_for_selection(self, selection_kind: str) -> None:
        page_names = {
            "main_title": "panel_title_page",
            "plot": "panel_plot_page",
            "plot_title": "panel_title_page",
            "legend": "panel_legend_page",
            "caption": "panel_caption_page",
        }
        button_names = {
            "panel_figure_page": "button_figure_page",
            "panel_title_page": "button_title_page",
            "panel_plot_page": "button_plot_page",
            "panel_legend_page": "button_legend_page",
            "panel_caption_page": "button_caption_page",
        }
        page_name = page_names.get(selection_kind, "panel_figure_page")
        self._show_settings_page(page_name)

    def _show_settings_page(self, page_name: str) -> None:
        stack = self._widget.findChild(QtWidgets.QStackedWidget, "stack_figure_settings_pages")
        page = self._widget.findChild(QtWidgets.QWidget, page_name)
        if stack is not None and page is not None:
            stack.setCurrentWidget(page)
        button_names = {
            "panel_figure_page": "button_figure_page",
            "panel_title_page": "button_title_page",
            "panel_plot_page": "button_plot_page",
            "panel_legend_page": "button_legend_page",
            "panel_caption_page": "button_caption_page",
        }
        for button_name in button_names.values():
            button = self._widget.findChild(QtWidgets.QPushButton, button_name)
            if button is not None:
                button.setChecked(button_name == button_names[page_name])

    def _populate_figure_settings_tree(self) -> None:
        tree = self._widget.findChild(QtWidgets.QTreeWidget, "tree_figure_parameters")
        if tree is None:
            return
        tree.clear()
        tree.setColumnCount(2)
        tree.setHeaderLabels(["Parameter", "Value"])
        self._figure_setting_widgets.clear()

        figure_item = self._add_group_item(tree, "Main Figure")
        self._add_double_spin(tree, figure_item, "width_mm", "width", suffix=" mm", minimum=1.0, maximum=1000.0)
        self._add_double_spin(tree, figure_item, "height_mm", "height", suffix=" mm", minimum=1.0, maximum=1000.0)
        self._add_double_spin(tree, figure_item, "margin_mm", "margin", suffix=" mm", minimum=0.0, maximum=1000.0, step=0.1)
        self._add_spin(tree, figure_item, "export_dpi", "resolution", suffix=" dpi", minimum=36, maximum=1200)

        subplot_item = self._add_group_item(tree, "Subplots")
        self._add_spin(tree, subplot_item, "rows", "rows", minimum=1, maximum=24)
        self._add_spin(tree, subplot_item, "columns", "columns", minimum=1, maximum=24)
        self._add_double_spin(tree, subplot_item, "subplot_margin_mm", "spacing", suffix=" mm", minimum=0.0, maximum=1000.0, step=0.1)

        plot_item = self._add_group_item(tree, "Axes")
        self._add_double_spin(tree, plot_item, "axis_box_line_width", "box width", minimum=0.0, maximum=1000.0, step=0.1)
        self._add_combo_box(
            tree,
            plot_item,
            "grid_mode",
            "grid mode",
            [("off", "none"), ("lines", "guidelines"), ("map", "map")],
        )
        self._add_double_spin(tree, plot_item, "grid_line_width", "grid width", minimum=0.0, maximum=1000.0, step=0.1)
        self._add_double_spin(tree, plot_item, "grid_resolution", "grid resolution", minimum=0.1, maximum=1000.0, step=0.1)
        self._add_spin(tree, plot_item, "tick_label_size", "label size", minimum=1, maximum=200)
        self._add_double_spin(tree, plot_item, "tick_label_margin_pt", "label margin", suffix=" pt", minimum=0.0, maximum=200.0, step=0.1)

        appearance_item = self._add_group_item(tree, "Appearance")
        self._add_double_spin(
            tree,
            appearance_item,
            "point_size_factor_percent",
            "point scale",
            suffix=" %",
            minimum=-95.0,
            maximum=500.0,
            step=5.0,
        )

        self._populate_title_settings_tree()
        self._populate_legend_settings_tree()
        self._populate_caption_settings_tree()

        tree.expandAll()
        tree.resizeColumnToContents(0)

    def _populate_legend_settings_tree(self) -> None:
        tree = self._widget.findChild(QtWidgets.QTreeWidget, "tree_legend_parameters")
        if tree is None:
            return
        tree.clear()
        tree.setColumnCount(2)
        tree.setHeaderLabels(["Parameter", "Value"])
        legend_item = self._add_group_item(tree, "Legend")
        self._add_check_box(tree, legend_item, "legend_show", "show")
        self._add_combo_box(
            tree,
            legend_item,
            "legend_scope",
            "scope",
            [("per_plot", "individual"), ("global", "global")],
        )
        self._add_combo_box(
            tree,
            legend_item,
            "legend_location",
            "location",
            [
                ("upper right", "upper right"),
                ("upper left", "upper left"),
                ("lower right", "lower right"),
                ("lower left", "lower left"),
                ("center right", "center right"),
                ("center left", "center left"),
                ("upper center", "upper center"),
                ("lower center", "lower center"),
            ],
        )
        self._add_check_box(tree, legend_item, "legend_include_point_color", "point color")
        self._add_check_box(tree, legend_item, "legend_include_border_color", "border color")
        self._add_check_box(tree, legend_item, "legend_include_shape", "shape")
        self._add_spin(tree, legend_item, "legend_font_size", "font size", minimum=1, maximum=200)
        self._add_double_spin(tree, legend_item, "legend_marker_size", "marker size", minimum=0.1, maximum=200.0, step=0.5)
        self._add_spin(tree, legend_item, "legend_columns", "columns", minimum=1, maximum=24)
        self._add_check_box(tree, legend_item, "legend_frame", "frame")
        self._add_double_spin(tree, legend_item, "legend_margin_mm", "margin", suffix=" mm", minimum=0.0, maximum=1000.0, step=0.1)
        pins_item = self._add_group_item(tree, "Pins")
        self._add_check_box(tree, pins_item, "pins_visible", "show")
        self._add_double_spin(
            tree,
            pins_item,
            "pin_size_factor_percent",
            "size",
            suffix=" %",
            minimum=-95.0,
            maximum=500.0,
            step=5.0,
        )
        self._add_check_box(tree, pins_item, "pin_thumbnail_visible", "thumbnail")
        self._add_double_spin(tree, pins_item, "pin_line_width", "line width", minimum=0.0, maximum=100.0, step=0.1)
        tree.expandAll()
        tree.resizeColumnToContents(0)

    def _populate_caption_settings_tree(self) -> None:
        tree = self._widget.findChild(QtWidgets.QTreeWidget, "tree_caption_parameters")
        if tree is None:
            return
        tree.clear()
        tree.setColumnCount(2)
        tree.setHeaderLabels(["Parameter", "Value"])
        main_caption_item = self._add_group_item(tree, "Main Caption")
        self._add_check_box(tree, main_caption_item, "main_caption_show", "show")
        self._add_combo_box(
            tree,
            main_caption_item,
            "main_caption_font",
            "font",
            self._font_items(),
        )
        self._add_spin(tree, main_caption_item, "main_caption_size", "size", minimum=1, maximum=200)
        self._add_check_box(tree, main_caption_item, "main_caption_justify", "justify")
        self._add_double_spin(tree, main_caption_item, "main_caption_line_spacing", "line spacing", minimum=0.1, maximum=10.0, step=0.1)

        subplot_caption_item = self._add_group_item(tree, "Subplot Captions")
        self._add_check_box(tree, subplot_caption_item, "subplot_caption_show", "show")
        self._add_combo_box(
            tree,
            subplot_caption_item,
            "subplot_caption_alignment",
            "alignment",
            [("left", "left"), ("center", "center"), ("right", "right"), ("justify", "justify")],
        )
        self._add_combo_box(
            tree,
            subplot_caption_item,
            "subplot_caption_font",
            "font",
            self._font_items(),
        )
        self._add_spin(tree, subplot_caption_item, "subplot_caption_size", "size", minimum=1, maximum=200)
        self._add_double_spin(tree, subplot_caption_item, "subplot_caption_line_spacing", "line spacing", minimum=0.1, maximum=10.0, step=0.1)
        self._add_double_spin(tree, subplot_caption_item, "caption_margin_mm", "margin", suffix=" mm", minimum=0.0, maximum=1000.0, step=0.1)
        tree.expandAll()
        tree.resizeColumnToContents(0)
        self._sync_figure_settings_from_active_tab()

    def _populate_title_settings_tree(self) -> None:
        tree = self._widget.findChild(QtWidgets.QTreeWidget, "tree_title_parameters")
        if tree is None:
            return
        tree.clear()
        tree.setColumnCount(2)
        tree.setHeaderLabels(["Parameter", "Value"])

        main_title_item = self._add_group_item(tree, "Main Title")
        self._add_check_box(tree, main_title_item, "main_title_show", "show")
        self._add_combo_box(
            tree,
            main_title_item,
            "main_title_location",
            "location",
            [("top", "top"), ("bottom", "bottom"), ("left", "left"), ("right", "right")],
        )
        self._add_combo_box(
            tree,
            main_title_item,
            "main_title_alignment",
            "alignment",
            [("left", "left"), ("center", "center"), ("right", "right")],
        )
        self._add_double_spin(tree, main_title_item, "main_title_margin_mm", "margin", suffix=" mm", minimum=-1000.0, maximum=1000.0, step=0.1)
        self._add_combo_box(
            tree,
            main_title_item,
            "main_title_font",
            "font",
            self._font_items(),
        )
        self._add_spin(tree, main_title_item, "main_title_size", "size", minimum=1, maximum=200)

        subtitle_item = self._add_group_item(tree, "Subplot Titles")
        self._add_check_box(tree, subtitle_item, "subtitle_show", "show")
        self._add_combo_box(
            tree,
            subtitle_item,
            "subtitle_location",
            "location",
            [("top", "top"), ("bottom", "bottom"), ("left", "left"), ("right", "right")],
        )
        self._add_combo_box(
            tree,
            subtitle_item,
            "subtitle_alignment",
            "alignment",
            [("left", "left"), ("center", "center"), ("right", "right")],
        )
        self._add_double_spin(tree, subtitle_item, "subtitle_margin_mm", "margin", suffix=" mm", minimum=-1000.0, maximum=1000.0, step=0.1)
        self._add_combo_box(
            tree,
            subtitle_item,
            "subtitle_font",
            "font",
            self._font_items(),
        )
        self._add_spin(tree, subtitle_item, "subtitle_size", "size", minimum=1, maximum=200)

        tree.expandAll()
        tree.resizeColumnToContents(0)

    @staticmethod
    def _add_group_item(tree: QtWidgets.QTreeWidget, label: str) -> QtWidgets.QTreeWidgetItem:
        item = QtWidgets.QTreeWidgetItem(tree, [label, ""])
        item.setFirstColumnSpanned(True)
        item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        return item

    def _add_double_spin(
        self,
        tree: QtWidgets.QTreeWidget,
        parent: QtWidgets.QTreeWidgetItem,
        key: str,
        label: str,
        *,
        suffix: str = "",
        minimum: float,
        maximum: float,
        step: float = 1.0,
    ) -> None:
        item = QtWidgets.QTreeWidgetItem(parent, [label, ""])
        spin = QtWidgets.QDoubleSpinBox(tree)
        spin.setObjectName(f"spin_figure_{key}")
        spin.setRange(minimum, maximum)
        spin.setDecimals(1)
        spin.setSingleStep(step)
        spin.setSuffix(suffix)
        tree.setItemWidget(item, 1, spin)
        self._figure_setting_widgets[key] = spin

    def _add_spin(
        self,
        tree: QtWidgets.QTreeWidget,
        parent: QtWidgets.QTreeWidgetItem,
        key: str,
        label: str,
        *,
        suffix: str = "",
        minimum: int,
        maximum: int,
    ) -> None:
        item = QtWidgets.QTreeWidgetItem(parent, [label, ""])
        spin = QtWidgets.QSpinBox(tree)
        spin.setObjectName(f"spin_figure_{key}")
        spin.setRange(minimum, maximum)
        spin.setSingleStep(1)
        spin.setSuffix(suffix)
        tree.setItemWidget(item, 1, spin)
        self._figure_setting_widgets[key] = spin

    def _add_check_box(
        self,
        tree: QtWidgets.QTreeWidget,
        parent: QtWidgets.QTreeWidgetItem,
        key: str,
        label: str,
    ) -> None:
        item = QtWidgets.QTreeWidgetItem(parent, [label, ""])
        checkbox = QtWidgets.QCheckBox(tree)
        checkbox.setObjectName(f"checkbox_figure_{key}")
        tree.setItemWidget(item, 1, checkbox)
        self._figure_setting_widgets[key] = checkbox

    def _add_combo_box(
        self,
        tree: QtWidgets.QTreeWidget,
        parent: QtWidgets.QTreeWidgetItem,
        key: str,
        label: str,
        items: list[tuple[str, str]],
    ) -> None:
        item = QtWidgets.QTreeWidgetItem(parent, [label, ""])
        combo = QtWidgets.QComboBox(tree)
        combo.setObjectName(f"combo_figure_{key}")
        for value, text in items:
            combo.addItem(text, value)
        tree.setItemWidget(item, 1, combo)
        self._figure_setting_widgets[key] = combo

    def _sync_figure_settings_from_active_tab(self) -> None:
        if self._active_plot_tab is None:
            settings = FigurePlotSettings()
        else:
            settings = self._active_plot_tab.figure_settings()
        self._set_widget_value("width_mm", settings.width_mm)
        self._set_widget_value("height_mm", settings.height_mm)
        self._set_widget_value("rows", settings.rows)
        self._set_widget_value("columns", settings.columns)
        self._set_widget_value("margin_mm", settings.margin_mm)
        self._set_widget_value("export_dpi", settings.export_dpi)
        self._set_widget_value("axis_box_line_width", settings.axis_box_line_width)
        self._set_widget_value("grid_mode", settings.grid_mode)
        self._set_widget_value("grid_line_width", settings.grid_line_width)
        self._set_widget_value("subplot_margin_mm", settings.subplot_margin_mm)
        self._set_widget_value("main_title_show", settings.main_title_show)
        self._set_widget_value("main_title_location", settings.main_title_location)
        self._set_widget_value("main_title_alignment", settings.main_title_alignment)
        self._set_widget_value("main_title_margin_mm", settings.main_title_margin_mm)
        self._set_widget_value("main_title_font", settings.main_title_font)
        self._set_widget_value("main_title_size", settings.main_title_size)
        self._set_widget_value("subtitle_show", settings.subtitle_show)
        self._set_widget_value("subtitle_location", settings.subtitle_location)
        self._set_widget_value("subtitle_alignment", settings.subtitle_alignment)
        self._set_widget_value("subtitle_margin_mm", settings.subtitle_margin_mm)
        self._set_widget_value("subtitle_font", settings.subtitle_font)
        self._set_widget_value("subtitle_size", settings.subtitle_size)
        self._set_widget_value("legend_show", settings.legend_show)
        self._set_widget_value("legend_scope", settings.legend_scope)
        self._set_widget_value("legend_location", settings.legend_location)
        self._set_widget_value("legend_include_point_color", settings.legend_include_point_color)
        self._set_widget_value("legend_include_border_color", settings.legend_include_border_color)
        self._set_widget_value("legend_include_shape", settings.legend_include_shape)
        self._set_widget_value("legend_font_size", settings.legend_font_size)
        self._set_widget_value("legend_marker_size", settings.legend_marker_size)
        self._set_widget_value("legend_columns", settings.legend_columns)
        self._set_widget_value("legend_frame", settings.legend_frame)
        self._set_widget_value("legend_margin_mm", settings.legend_margin_mm)
        self._set_widget_value("pins_visible", settings.pins_visible)
        self._set_widget_value("pin_size_factor_percent", settings.pin_size_factor_percent)
        self._set_widget_value("pin_thumbnail_visible", settings.pin_thumbnail_visible)
        self._set_widget_value("pin_line_width", settings.pin_line_width)
        self._set_widget_value("main_caption_show", settings.main_caption_show)
        self._set_widget_value("main_caption_font", settings.main_caption_font)
        self._set_widget_value("main_caption_size", settings.main_caption_size)
        self._set_widget_value("main_caption_justify", settings.main_caption_justify)
        self._set_widget_value("main_caption_line_spacing", settings.main_caption_line_spacing)
        self._set_widget_value("subplot_caption_show", settings.subplot_caption_show)
        self._set_widget_value("subplot_caption_alignment", settings.subplot_caption_alignment)
        self._set_widget_value("subplot_caption_font", settings.subplot_caption_font)
        self._set_widget_value("subplot_caption_size", settings.subplot_caption_size)
        self._set_widget_value("subplot_caption_line_spacing", settings.subplot_caption_line_spacing)
        self._set_widget_value("caption_margin_mm", settings.caption_margin_mm)
        self._set_widget_value("grid_resolution", settings.grid_resolution)
        self._set_widget_value("tick_label_size", settings.tick_label_size)
        self._set_widget_value("tick_label_margin_pt", settings.tick_label_margin_pt)
        self._set_widget_value("point_size_factor_percent", settings.point_size_factor_percent)
        self._update_dynamic_setting_states()
        self._settings_unapplied = False
        self._refresh_action_buttons()

    def _set_widget_value(self, key: str, value: object) -> None:
        widget = self._figure_setting_widgets.get(key)
        if widget is None:
            return
        previous = widget.blockSignals(True)
        try:
            if isinstance(widget, QtWidgets.QDoubleSpinBox):
                widget.setValue(float(value))
            elif isinstance(widget, QtWidgets.QSpinBox):
                widget.setValue(int(value))
            elif isinstance(widget, QtWidgets.QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QtWidgets.QComboBox):
                index = widget.findData(value)
                widget.setCurrentIndex(index if index >= 0 else 0)
        finally:
            widget.blockSignals(previous)

    def _apply_figure_settings(self) -> bool:
        if self._active_plot_tab is None:
            return False
        self._operation = "apply"
        self._refresh_action_buttons()
        activity = (
            self._activity_manager.progress(
                "Applying figure settings…", value=0, total=100
            )
            if self._activity_manager is not None
            else None
        )

        def report_progress(message: str, value: int) -> None:
            if activity is not None:
                activity.update(message=message, value=value, total=100)

        try:
            settings = self._settings_from_widget_values()
            if activity is None:
                self._active_plot_tab.apply_figure_settings(settings)
            else:
                self._active_plot_tab.apply_figure_settings(
                    settings, progress_callback=report_progress
                )
            self._settings_unapplied = False
            return True
        finally:
            if activity is not None:
                activity.finish()
            self._operation = None
            self._refresh_action_buttons()

    def _settings_from_widget_values(self) -> FigurePlotSettings:
        current = self._active_plot_tab.figure_settings() if self._active_plot_tab is not None else FigurePlotSettings()
        return replace(
            current,
            width_mm=self._double_value("width_mm"),
            height_mm=self._double_value("height_mm"),
            rows=self._int_value("rows"),
            columns=self._int_value("columns"),
            margin_mm=self._double_value("margin_mm"),
            export_dpi=self._int_value("export_dpi"),
            axis_box_line_width=self._double_value("axis_box_line_width"),
            show_axis=self._double_value("axis_box_line_width") > 0.0,
            grid_mode=self._combo_value("grid_mode"),
            grid_line_width=self._double_value("grid_line_width"),
            subplot_margin_mm=self._double_value("subplot_margin_mm"),
            main_title_show=self._checked_value("main_title_show"),
            main_title_location=self._combo_value("main_title_location"),
            main_title_alignment=self._combo_value("main_title_alignment"),
            main_title_margin_mm=self._double_value("main_title_margin_mm"),
            main_title_font=self._combo_value("main_title_font"),
            main_title_size=self._int_value("main_title_size"),
            subtitle_show=self._checked_value("subtitle_show"),
            subtitle_location=self._combo_value("subtitle_location"),
            subtitle_alignment=self._combo_value("subtitle_alignment"),
            subtitle_margin_mm=self._double_value("subtitle_margin_mm"),
            subtitle_font=self._combo_value("subtitle_font"),
            subtitle_size=self._int_value("subtitle_size"),
            legend_show=self._checked_value("legend_show"),
            legend_scope=self._combo_value("legend_scope"),
            legend_location=self._combo_value("legend_location"),
            legend_include_point_color=self._checked_value("legend_include_point_color"),
            legend_include_border_color=self._checked_value("legend_include_border_color"),
            legend_include_shape=self._checked_value("legend_include_shape"),
            legend_font_size=self._int_value("legend_font_size"),
            legend_marker_size=self._double_value("legend_marker_size"),
            legend_columns=self._int_value("legend_columns"),
            legend_frame=self._checked_value("legend_frame"),
            legend_margin_mm=self._double_value("legend_margin_mm"),
            pins_visible=self._checked_value("pins_visible"),
            pin_size_factor_percent=self._double_value("pin_size_factor_percent"),
            pin_thumbnail_visible=self._checked_value("pin_thumbnail_visible"),
            pin_line_width=self._double_value("pin_line_width"),
            main_caption_show=self._checked_value("main_caption_show"),
            main_caption_font=self._combo_value("main_caption_font"),
            main_caption_size=self._int_value("main_caption_size"),
            main_caption_justify=self._checked_value("main_caption_justify"),
            main_caption_line_spacing=self._double_value("main_caption_line_spacing"),
            subplot_caption_show=self._checked_value("subplot_caption_show"),
            subplot_caption_alignment=self._combo_value("subplot_caption_alignment"),
            subplot_caption_font=self._combo_value("subplot_caption_font"),
            subplot_caption_size=self._int_value("subplot_caption_size"),
            subplot_caption_line_spacing=self._double_value("subplot_caption_line_spacing"),
            caption_margin_mm=self._double_value("caption_margin_mm"),
            grid_resolution=self._double_value("grid_resolution"),
            tick_label_size=self._int_value("tick_label_size"),
            tick_label_margin_pt=self._double_value("tick_label_margin_pt"),
            point_size_factor_percent=self._double_value("point_size_factor_percent"),
        )

    def figure_settings_preset_payload(self) -> dict:
        return {"plot_settings": asdict(self._settings_from_widget_values())}

    def apply_figure_settings_preset_payload(self, payload: dict) -> bool:
        if self._active_plot_tab is None or not isinstance(payload, dict):
            return False
        settings_payload = payload.get("plot_settings") if isinstance(payload.get("plot_settings"), dict) else payload
        if not isinstance(settings_payload, dict):
            return False
        field_names = {field.name for field in fields(FigurePlotSettings)}
        values = {key: value for key, value in settings_payload.items() if key in field_names}
        if not values:
            return False
        settings = replace(self._active_plot_tab.figure_settings(), **values)
        self._active_plot_tab.apply_figure_settings(settings)
        self._sync_figure_settings_from_active_tab()
        return True

    def _save_current_preset(self) -> None:
        if self._save_preset_callback is None or self._active_plot_tab is None:
            return
        label, ok = QtWidgets.QInputDialog.getText(self._widget, "Save Figure Settings Preset", "Preset name:")
        if not ok:
            return
        name = str(label).strip()
        if not name:
            return
        payload = self.figure_settings_preset_payload()
        payload["name"] = name
        if self._save_preset_callback(name, payload):
            self._show_status(f"Saved figure settings preset: {name}")

    def _load_preset_label(self, label: str) -> None:
        if self._load_preset_callback is None:
            return
        payload = self._load_preset_callback(label)
        if self.apply_figure_settings_preset_payload(payload or {}):
            self._show_status(f"Loaded figure settings preset: {label}")

    def _export_figure(self) -> None:
        if self._active_plot_tab is None:
            QtWidgets.QMessageBox.information(
                self._widget,
                "No active figure",
                "Select a figure tab before exporting.",
            )
            return
        interactive_export = getattr(
            self._active_plot_tab, "export_interactively", None
        )
        if callable(interactive_export):
            self._operation = "export"
            self._refresh_action_buttons()
            try:
                interactive_export()
            except Exception as exc:  # noqa: BLE001 - keep export failures in the UI
                QtWidgets.QMessageBox.warning(
                    self._widget,
                    "Export failed",
                    f"Could not open animation export:\n{exc}",
                )
            finally:
                self._operation = None
                self._refresh_action_buttons()
            return
        self._operation = "export"
        self._refresh_action_buttons()
        destination = self._ask_export_destination()
        if destination is None:
            self._operation = None
            self._refresh_action_buttons()
            return
        activity = (
            self._activity_manager.progress(
                "Preparing figure export…", value=0, total=100
            )
            if self._activity_manager is not None
            else None
        )

        def report_progress(message: str, value: int) -> None:
            if activity is not None:
                activity.update(message=message, value=value, total=100)

        requires_apply = self._settings_unapplied
        try:
            if requires_apply:
                settings = self._settings_from_widget_values()
                if activity is None:
                    self._active_plot_tab.apply_figure_settings(settings)
                else:
                    self._active_plot_tab.apply_figure_settings(
                        settings,
                        progress_callback=lambda message, value: report_progress(
                            message, round(value * 0.45)
                        ),
                    )
                self._settings_unapplied = False
            if activity is None:
                self._active_plot_tab.export_figure(destination)
            else:
                export_progress = report_progress
                if requires_apply:
                    export_progress = lambda message, value: report_progress(
                        message, 45 + round(value * 0.55)
                    )
                self._active_plot_tab.export_figure(
                    destination,
                    progress_callback=export_progress,
                )
        except Exception as exc:
            if activity is not None:
                activity.retain_failed(f"Figure export failed: {exc}")
            QtWidgets.QMessageBox.warning(
                self._widget,
                "Export failed",
                f"Could not export figure:\n{exc}",
            )
            return
        else:
            self._show_status(f"Exported figure to {destination}")
        finally:
            if activity is not None:
                activity.finish()
            self._operation = None
            self._refresh_action_buttons()

    def _wire_parameter_change_tracking(self) -> None:
        for widget in self._figure_setting_widgets.values():
            if isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
                widget.valueChanged.connect(self._mark_settings_unapplied)
            elif isinstance(widget, QtWidgets.QCheckBox):
                widget.toggled.connect(self._mark_settings_unapplied)
            elif isinstance(widget, QtWidgets.QComboBox):
                widget.currentIndexChanged.connect(self._mark_settings_unapplied)

    def _mark_settings_unapplied(self, *_args: object) -> None:
        if self._active_plot_tab is not None:
            self._settings_unapplied = True
            self._refresh_action_buttons()

    def _refresh_action_buttons(self) -> None:
        has_plot = self._active_plot_tab is not None
        apply_button = self._widget.findChild(
            QtWidgets.QPushButton, "button_apply_figure_settings"
        )
        if apply_button is not None:
            apply_button.setText("apply")
            apply_button.setEnabled(
                has_plot and self._settings_unapplied and self._operation is None
            )
        export_button = self._widget.findChild(
            QtWidgets.QPushButton, "button_export_figure"
        )
        if export_button is not None:
            export_button.setText("export")
            export_button.setEnabled(has_plot and self._operation is None)

    def _ask_export_destination(self) -> Path | None:
        custom_options = getattr(self._active_plot_tab, "export_file_options", None)
        options = (
            custom_options()
            if callable(custom_options)
            else self._matplotlib_export_options()
        )
        if not options:
            QtWidgets.QMessageBox.warning(
                self._widget,
                "Export unavailable",
                "Matplotlib did not report any export formats.",
            )
            return None

        documents_dir = QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.StandardLocation.DocumentsLocation
        )
        if not documents_dir:
            documents_dir = QtCore.QDir.homePath()
        custom_default = getattr(self._active_plot_tab, "default_export_format", None)
        default_format = (
            str(custom_default()).lower().lstrip(".")
            if callable(custom_default)
            else self._default_export_format(options)
        )
        suggested_name = f"{self._suggested_export_stem()}.{default_format}"
        filter_string = ";;".join(label for label, _formats in options)
        default_filter = self._filter_for_format(options, default_format) or options[0][0]
        selected_path, selected_filter = QtWidgets.QFileDialog.getSaveFileName(
            self._widget,
            "Export Figure",
            str(Path(documents_dir) / suggested_name),
            filter_string,
            default_filter,
        )
        if not selected_path:
            return None

        target = Path(selected_path)
        if not target.suffix:
            selected_formats = self._formats_for_filter(options, selected_filter)
            target = target.with_suffix(f".{selected_formats[0] if selected_formats else default_format}")
        return target

    @staticmethod
    def _matplotlib_export_options() -> list[tuple[str, list[str]]]:
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas

        supported = {
            str(file_format).lower().lstrip(".")
            for file_format in FigureCanvas.get_supported_filetypes()
        }
        options: list[tuple[str, list[str]]] = []
        for description, formats in _FIGURE_EXPORT_OPTIONS:
            available = [file_format for file_format in formats if file_format in supported]
            if not available:
                continue
            patterns = " ".join(f"*.{file_format}" for file_format in available)
            options.append((f"{description} ({patterns})", available))
        return options

    @staticmethod
    def _default_export_format(options: list[tuple[str, list[str]]]) -> str:
        if any("pdf" in formats for _label, formats in options):
            return "pdf"
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas

        default = str(FigureCanvas.get_default_filetype() or "").lower().lstrip(".")
        if any(default in formats for _label, formats in options):
            return default
        return options[0][1][0]

    @staticmethod
    def _filter_for_format(options: list[tuple[str, list[str]]], file_format: str) -> str | None:
        return next((label for label, formats in options if file_format in formats), None)

    @staticmethod
    def _formats_for_filter(options: list[tuple[str, list[str]]], selected_filter: str) -> list[str]:
        return next((formats for label, formats in options if label == selected_filter), [])

    def _suggested_export_stem(self) -> str:
        if self._active_plot_tab is None:
            return "figure_export"
        title = str(self._active_plot_tab.widget.property("figureTitle") or "").strip()
        if not title:
            return "figure_export"
        for character in '<>:"/\\|?*':
            title = title.replace(character, "_")
        return title or "figure_export"

    def _double_value(self, key: str) -> float:
        widget = self._figure_setting_widgets[key]
        return float(widget.value()) if isinstance(widget, QtWidgets.QDoubleSpinBox) else 0.0

    def _int_value(self, key: str) -> int:
        widget = self._figure_setting_widgets[key]
        return int(widget.value()) if isinstance(widget, QtWidgets.QSpinBox) else 0

    def _checked_value(self, key: str) -> bool:
        widget = self._figure_setting_widgets[key]
        return bool(widget.isChecked()) if isinstance(widget, QtWidgets.QCheckBox) else False

    def _combo_value(self, key: str) -> str:
        widget = self._figure_setting_widgets[key]
        return str(widget.currentData()) if isinstance(widget, QtWidgets.QComboBox) else ""

    def _wire_dynamic_setting_states(self) -> None:
        for key in ("rows", "columns"):
            widget = self._figure_setting_widgets.get(key)
            if isinstance(widget, QtWidgets.QSpinBox):
                widget.valueChanged.connect(self._update_dynamic_setting_states)
        for key in ("main_title_show", "subtitle_show", "legend_show", "main_caption_show", "subplot_caption_show"):
            widget = self._figure_setting_widgets.get(key)
            if isinstance(widget, QtWidgets.QCheckBox):
                widget.toggled.connect(self._update_dynamic_setting_states)
        widget = self._figure_setting_widgets.get("grid_mode")
        if isinstance(widget, QtWidgets.QComboBox):
            widget.currentIndexChanged.connect(self._update_dynamic_setting_states)

    def _update_dynamic_setting_states(self) -> None:
        rows = self._int_value_or_default("rows", 1)
        columns = self._int_value_or_default("columns", 1)
        self._set_setting_enabled("subplot_margin_mm", rows > 1 or columns > 1)
        grid_mode = self._combo_value_or_default("grid_mode", "off")
        grid_enabled = grid_mode in {"lines", "map"}
        map_enabled = grid_mode == "map"
        for key in ("grid_line_width", "grid_resolution"):
            self._set_setting_enabled(key, grid_enabled)
        for key in ("tick_label_size", "tick_label_margin_pt"):
            self._set_setting_enabled(key, map_enabled)
        main_title_enabled = self._checked_value_or_default("main_title_show", False)
        for key in ("main_title_location", "main_title_alignment", "main_title_margin_mm", "main_title_font", "main_title_size"):
            self._set_setting_enabled(key, main_title_enabled)
        subtitle_enabled = self._checked_value_or_default("subtitle_show", False)
        for key in ("subtitle_location", "subtitle_alignment", "subtitle_margin_mm", "subtitle_font", "subtitle_size"):
            self._set_setting_enabled(key, subtitle_enabled)
        legend_enabled = self._checked_value_or_default("legend_show", False)
        for key in (
            "legend_scope",
            "legend_location",
            "legend_include_point_color",
            "legend_include_border_color",
            "legend_include_shape",
            "legend_font_size",
            "legend_marker_size",
            "legend_columns",
            "legend_frame",
            "legend_margin_mm",
        ):
            self._set_setting_enabled(key, legend_enabled)
        main_caption_enabled = self._checked_value_or_default("main_caption_show", False)
        for key in ("main_caption_font", "main_caption_size", "main_caption_justify", "main_caption_line_spacing"):
            self._set_setting_enabled(key, main_caption_enabled)
        subplot_caption_enabled = self._checked_value_or_default("subplot_caption_show", False)
        for key in ("subplot_caption_alignment", "subplot_caption_font", "subplot_caption_size", "subplot_caption_line_spacing", "caption_margin_mm"):
            self._set_setting_enabled(key, subplot_caption_enabled)

    def _set_setting_enabled(self, key: str, enabled: bool) -> None:
        widget = self._figure_setting_widgets.get(key)
        if widget is not None:
            widget.setEnabled(enabled)

    def _int_value_or_default(self, key: str, default: int) -> int:
        widget = self._figure_setting_widgets.get(key)
        return int(widget.value()) if isinstance(widget, QtWidgets.QSpinBox) else default

    def _checked_value_or_default(self, key: str, default: bool) -> bool:
        widget = self._figure_setting_widgets.get(key)
        return bool(widget.isChecked()) if isinstance(widget, QtWidgets.QCheckBox) else default

    def _combo_value_or_default(self, key: str, default: str) -> str:
        widget = self._figure_setting_widgets.get(key)
        return str(widget.currentData()) if isinstance(widget, QtWidgets.QComboBox) else default

    @staticmethod
    def _font_items() -> list[tuple[str, str]]:
        return [
            ("DejaVu Sans", "DejaVu Sans"),
            ("Arial", "Arial"),
            ("Helvetica", "Helvetica"),
            ("Times New Roman", "Times New Roman"),
            ("Courier New", "Courier New"),
        ]
