from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6 import QtCore, QtGui, QtWidgets

from DiscoveryArea.domain import DiscoveryPinDisplaySettings
from DiscoveryArea.palettes import palette_options
from DiscoveryArea.thumbnails.repository import ThumbnailVariant
from DiscoveryArea.thumbnails.repository import ThumbnailVariantRepository
from shared.qt.icons import apply_icon_color
from shared.qt.panel_controller import UiPanelController
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import add_widget_to_container, require_child


class _SwatchClickFilter(QtCore.QObject):
    def __init__(self, click_handler) -> None:
        super().__init__()
        self._click_handler = click_handler

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() == QtCore.QEvent.Type.MouseButtonRelease and isinstance(obj, QtWidgets.QWidget):
            return bool(self._click_handler(obj))
        return False


class _TooltipThumbnailSizeSliderFilter(QtCore.QObject):
    def __init__(self, owner: "LayersSettingPanelController") -> None:
        super().__init__(owner.widget)
        self._owner = owner

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() == QtCore.QEvent.Type.Enter:
            self._owner._handle_tooltip_thumbnail_size_slider_entered()
        elif event.type() == QtCore.QEvent.Type.Leave:
            self._owner._handle_tooltip_thumbnail_size_slider_left()
        return False


class _MarkedGroupSwatchDelegate(QtWidgets.QStyledItemDelegate):
    def paint(
        self,
        painter: QtGui.QPainter,
        option: QtWidgets.QStyleOptionViewItem,
        index: QtCore.QModelIndex,
    ) -> None:
        super().paint(painter, option, index)
        raw = index.data(QtCore.Qt.ItemDataRole.UserRole)
        color = QtGui.QColor(str(raw or "").strip())
        if not color.isValid():
            return
        painter.save()
        rect = option.rect.adjusted(4, 4, -4, -4)
        painter.setPen(QtGui.QPen(QtGui.QColor("#666666"), 1))
        painter.setBrush(QtGui.QBrush(color))
        painter.drawRect(rect)
        painter.restore()


@dataclass(frozen=True)
class PreviewRenderSettings:
    render_value: str
    thumbnail_size_px: int
    thumbnail_limit: int


class LayersSettingPanelController(UiPanelController):
    package = "DiscoveryArea"
    _SETTINGS_UI_PATHS = {
        "tooltip": "ui/tooltip_settings.ui",
        "pins": "ui/pins_settings.ui",
        "previews": "ui/previews_settings.ui",
        "areas": "ui/areas_settings.ui",
    }
    _PREVIEW_THUMBNAIL_SIZE_MIN = 16
    _PREVIEW_THUMBNAIL_SIZE_MAX = 128
    _PREVIEW_THUMBNAIL_SIZE_STEP = 16
    _PREVIEW_THUMBNAIL_SLIDER_MIN = 1
    _PREVIEW_THUMBNAIL_SLIDER_MAX = 8
    _PREVIEW_THUMBNAIL_LIMITS = (25, 50, 75, 125, 250, 650, 1_250, 2_500, 5_000)
    _THUMBNAIL_DENSITY_WARNING_THRESHOLD = 2_500
    _ALL_AREAS_ID = "__all_areas__"

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        status_callback=None,
        activity_manager=None,
    ) -> None:
        self._widget = QtWidgets.QWidget(parent)
        self._widget.setObjectName("layer_settings_state_owner")
        self._project = None
        self._status_callback = status_callback
        self._activity_manager = activity_manager
        self._settings_views = {
            layer: load_ui(self.package, path, self._widget)
            for layer, path in self._SETTINGS_UI_PATHS.items()
        }
        for view in self._settings_views.values():
            view.hide()
        self._wire()

    def layer_settings_view(self, layer: str) -> QtWidgets.QWidget | None:
        return self._settings_views.get(str(layer))

    def _wire(self) -> None:
        self._selection_changed_callback = None
        self._delete_area_callback = None
        self._area_toggle_callback = None
        self._points_toggle_callback = None
        self._all_area_toggle_callback = None
        self._all_points_toggle_callback = None
        self._rename_callback = None
        self._color_changed_callback = None
        self._export_area_requested_callback = None
        self._write_areas_to_metadata_requested_callback = None
        self._marked_area_rows: list[dict[str, object]] = []
        self._marked_area_style_changed_callback: Callable[[float], None] | None = None
        self._pin_display_changed_callback = None
        self._pin_style_requested_callback: Callable[[], None] | None = None
        self._tooltip_thumbnail_size_changed_callback = None
        self._tooltip_thumbnail_size_preview_hidden_callback = None
        self._create_thumbnails_requested_callback: Callable[[], None] | None = None
        self._swatch_filter = _SwatchClickFilter(self._handle_swatch_clicked)
        self._pin_settings = DiscoveryPinDisplaySettings()
        self._tooltip_settings_panel = self._settings_views["tooltip"]
        self._tooltip_fields: QtWidgets.QWidget | None = None
        self._tooltip_thumbnail_size_slider = require_child(
            self._tooltip_settings_panel,
            QtWidgets.QSlider,
            "slider_tooltip_thumbnail_size",
        )
        self._tooltip_thumbnail_size_slider.setRange(25, 500)
        self._tooltip_thumbnail_size_slider.valueChanged.connect(self._handle_tooltip_thumbnail_size_changed)
        self._tooltip_thumbnail_alignment = "bottom"
        self._tooltip_info_field_group = require_child(
            self._tooltip_settings_panel,
            QtWidgets.QGroupBox,
            "info_field_group",
        )
        self._tooltip_thumbnail_group = require_child(
            self._tooltip_settings_panel,
            QtWidgets.QGroupBox,
            "thumbnail_group",
        )
        self._button_hide_tooltip_label: QtWidgets.QAbstractButton | None = None
        self._pin_settings_panel = self._settings_views["pins"]
        self._pin_info_field_group = require_child(
            self._pin_settings_panel, QtWidgets.QGroupBox, "group_info_field"
        )
        self._pin_thumbnail_group = require_child(
            self._pin_settings_panel, QtWidgets.QGroupBox, "group_thumbnail"
        )
        self._pin_thumbnail_field = self._pin_settings.pin_thumbnail_variant_id
        self._tooltip_thumbnail_field = self._pin_settings.tooltip_thumbnail_variant_id
        self._tooltip_thumbnail_size_slider_hovered = False
        self._tooltip_thumbnail_size_slider_filter = _TooltipThumbnailSizeSliderFilter(self)
        self._tooltip_thumbnail_size_slider.installEventFilter(self._tooltip_thumbnail_size_slider_filter)
        self._available_metadata_fields: list[str] = ["id", "label", "category"]
        self._last_thumbnail_selection: dict[str, str] = {"pins": "none", "tooltip": "none"}
        self._active_thumbnail_variants: list[tuple[str, str]] = []
        self._thumbnail_defaults_applied = False
        self._suspend_pin_display_changed = False
        self._render_combo = require_child(self._widget, QtWidgets.QComboBox, "combo_render_options")
        self._render_combo.currentIndexChanged.connect(self._on_render_combo_changed)
        self._thumbnail_size_slider = require_child(
            self._widget,
            QtWidgets.QSlider,
            "slider_preview_thumbnail_size",
        )
        self._thumbnail_size_slider.setRange(
            self._PREVIEW_THUMBNAIL_SLIDER_MIN,
            self._PREVIEW_THUMBNAIL_SLIDER_MAX,
        )
        self._thumbnail_size_slider.setSingleStep(1)
        self._thumbnail_size_slider.setPageStep(1)
        self._thumbnail_size_slider.setTickInterval(1)
        self._thumbnail_size_slider.setTracking(True)
        self._thumbnail_size_slider.valueChanged.connect(self._on_thumbnail_size_slider_value_changed)
        self._thumbnail_density_slider = require_child(
            self._widget,
            QtWidgets.QSlider,
            "slider_preview_thumbnail_density",
        )
        self._thumbnail_density_slider.setRange(
            0,
            len(self._PREVIEW_THUMBNAIL_LIMITS) - 1,
        )
        self._thumbnail_density_slider.setSingleStep(1)
        self._thumbnail_density_slider.setPageStep(1)
        self._thumbnail_density_slider.setTickInterval(1)
        self._thumbnail_density_slider.setTracking(False)
        self._thumbnail_density_slider.valueChanged.connect(
            self._on_thumbnail_density_slider_value_changed
        )
        self._thumbnail_density_warning = require_child(
            self._widget,
            QtWidgets.QLabel,
            "label_warning",
        )
        self._thumbnail_density_warning_effect = QtWidgets.QGraphicsOpacityEffect(
            self._thumbnail_density_warning
        )
        self._thumbnail_density_warning.setGraphicsEffect(
            self._thumbnail_density_warning_effect
        )
        self._thumbnail_density_warning_effect.setOpacity(0.0)
        self._button_auto_density = require_child(
            self._widget,
            QtWidgets.QAbstractButton,
            "button_auto_density",
        )
        self._button_auto_density.toggled.connect(
            self._on_thumbnail_density_auto_toggled
        )
        self._marked_area_opacity_spin = require_child(
            self._widget,
            QtWidgets.QSpinBox,
            "spin_marked_area_opacity",
        )
        self._marked_areas_tree = require_child(
            self._widget,
            QtWidgets.QTreeWidget,
            "tree_marked_areas",
        )
        self._marked_area_opacity_spin.valueChanged.connect(self._on_marked_area_style_changed)
        self._render_selection_changed_callback: Callable[[str], None] | None = None
        self._thumbnail_size_changed_callback: Callable[[int], None] | None = None
        self._thumbnail_density_changed_callback: Callable[[int], None] | None = None
        self._thumbnail_density_auto_requested_callback: Callable[[], None] | None = None
        self._render_variants: list[ThumbnailVariant] = []
        self._last_render_selection = "points_fast"
        self._preview_settings = PreviewRenderSettings(
            render_value="none",
            thumbnail_size_px=64,
            thumbnail_limit=650,
        )
        self._previews_enabled = False
        self._thumbnail_density_automatic = True
        self._thumbnail_rendered_count = 0
        self._thumbnail_potential_count = 0
        self._suspend_render_changed = False
        self._suspend_thumbnail_size_changed = False
        self._suspend_thumbnail_density_changed = False
        with QtCore.QSignalBlocker(self._button_auto_density):
            self._button_auto_density.setChecked(True)
        self._tooltip_fields = self._bind_fields_selection(self._tooltip_settings_panel, mode="tooltip")
        self._pin_fields = self._bind_fields_selection(self._pin_settings_panel, mode="pins")
        self._tooltip_info_field_group.toggled.connect(self._handle_tooltip_info_field_toggled)
        self._tooltip_thumbnail_group.toggled.connect(self._handle_tooltip_thumbnail_toggled)
        with QtCore.QSignalBlocker(self._tooltip_thumbnail_size_slider):
            self._tooltip_thumbnail_size_slider.setValue(int(self._pin_settings.tooltip_thumbnail_size))
        self._wire_pin_thumbnail_size_control(self._pin_settings_panel)
        self._wire_tooltip_alignment_options()
        self._wire_pin_alignment_options()
        self._wire_pin_settings_controls(self._pin_settings_panel)
        self._apply_pin_style_state_to_panel(self._pin_settings_panel)
        self._populate_marked_areas()
        self.set_thumbnail_size_px(64)
        self.set_thumbnail_limit(650)
        self.set_render_options([], selected="none")
        self._sync_tooltip_thumbnail_size_enabled()
        self._sync_pin_thumbnail_size_enabled()
        apply_icon_color(self._widget)

    def shutdown(self) -> None:
        pass

    def set_project(self, project) -> None:
        if project is not self._project:
            self._thumbnail_defaults_applied = False
        super().set_project(project)
        self.refresh_thumbnail_variants()

    def refresh_thumbnail_variants(self) -> None:
        render_value = self._preview_settings.render_value
        pin_value = str(self._pin_thumbnail_field or "none")
        tooltip_value = str(self._tooltip_thumbnail_field or "none")
        variants = self._thumbnail_variants()
        if variants and not self._thumbnail_defaults_applied:
            default_variant_id = str(variants[0].variant_id)
            if self._normalize_thumbnail_selection(pin_value) == "none":
                pin_value = default_variant_id
            if self._normalize_thumbnail_selection(tooltip_value) == "none":
                tooltip_value = default_variant_id
            self._thumbnail_defaults_applied = True
        self.set_render_options(variants, selected=render_value)
        self._suspend_pin_display_changed = True
        try:
            self._pin_thumbnail_field = pin_value
            self._tooltip_thumbnail_field = tooltip_value
            self._refresh_pin_thumbnail_combo(pin_value)
            self._refresh_tooltip_thumbnail_combo(tooltip_value)
            self._last_thumbnail_selection["pins"] = self._pin_thumbnail_field
            self._last_thumbnail_selection["tooltip"] = self._tooltip_thumbnail_field
            self._pin_settings = DiscoveryPinDisplaySettings.from_payload(
                {
                    **self._pin_settings.to_payload(),
                    "thumbnail_variant_id": self._pin_thumbnail_field,
                    "tooltip_thumbnail_variant_id": self._tooltip_thumbnail_field,
                }
            )
        finally:
            self._suspend_pin_display_changed = False

    def _populate_marked_areas(
        self,
        *,
        selected_area_id: str | None = None,
        all_areas_share: str = "0.0%",
    ) -> None:
        tree = self._tree()
        if tree is None:
            return
        self._configure_marked_areas_tree(tree)
        tree.itemSelectionChanged.connect(self._on_tree_selection_changed)
        tree.itemChanged.connect(self._on_tree_item_changed)
        tree.itemClicked.connect(self._on_tree_item_clicked)
        tree.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self.set_marked_areas(
            self._marked_area_rows,
            selected_area_id=selected_area_id,
            all_areas_share=all_areas_share,
        )

    def _configure_marked_areas_tree(self, tree: QtWidgets.QTreeWidget) -> None:
        tree.setColumnCount(5)
        tree.setHeaderLabels(["Label", "Share", "Selection", "Polygon", "Color"])
        header = tree.header()
        if header is not None:
            header.setStretchLastSection(False)
            header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Fixed)
            header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Fixed)
            header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.Fixed)
            header.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Fixed)
            header.setDefaultAlignment(
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
        tree.setColumnWidth(1, 58)
        tree.setColumnWidth(2, 22)
        tree.setColumnWidth(3, 22)
        tree.setColumnWidth(4, 22)
        tree.setItemDelegateForColumn(4, _MarkedGroupSwatchDelegate(tree))
        self._set_marked_areas_header_icons(tree)

    def _set_marked_areas_header_icons(self, tree: QtWidgets.QTreeWidget) -> None:
        header_item = tree.headerItem()
        if header_item is None:
            return
        header_item.setText(2, "")
        header_item.setText(3, "")
        header_item.setText(4, "")
        self._install_marked_header_icon_buttons(tree)

    def _install_marked_header_icon_buttons(self, tree: QtWidgets.QTreeWidget) -> None:
        header = tree.header()
        if header is None:
            return
        viewport = header.viewport()
        specs = [
            (2, "filter", "button_marked_header_selection_icon"),
            (3, "layers", "button_marked_header_polygon_icon"),
            (4, "color_swatch", "button_marked_header_color_icon"),
        ]
        buttons: list[QtWidgets.QToolButton] = []
        for section, icon_name, object_name in specs:
            button = viewport.findChild(QtWidgets.QToolButton, object_name)
            if button is None:
                button = QtWidgets.QToolButton(viewport)
                button.setObjectName(object_name)
                button.setAutoRaise(True)
                button.setEnabled(True)
                button.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
                button.setProperty("iconName", icon_name)
                button.setIconSize(QtCore.QSize(14, 14))
                button.setFixedSize(18, 18)
            button.setAttribute(
                QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                section != 4,
            )
            if section == 4:
                button.setToolTip("Set area color palette")
                button.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
                menu = QtWidgets.QMenu(button)
                for label, palette in palette_options():
                    action = menu.addAction(label)
                    action.triggered.connect(
                        lambda _checked=False, palette=palette: self._apply_palette_to_marked_areas(list(palette))
                    )
                button.setMenu(menu)
                button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
            buttons.append(button)
            self._position_marked_header_icon_button(button, header, section=section)
        for button in buttons:
            apply_icon_color(button)
            button.show()
            button.raise_()
        if not bool(header.property("_marked_header_icons_wired")):
            header.sectionResized.connect(lambda *_: self._position_marked_header_icons(tree))
            header.sectionMoved.connect(lambda *_: self._position_marked_header_icons(tree))
            header.geometriesChanged.connect(lambda: self._position_marked_header_icons(tree))
            header.setProperty("_marked_header_icons_wired", True)

    def _position_marked_header_icons(self, tree: QtWidgets.QTreeWidget) -> None:
        header = tree.header()
        if header is None:
            return
        mapping = {
            "button_marked_header_selection_icon": 2,
            "button_marked_header_polygon_icon": 3,
            "button_marked_header_color_icon": 4,
        }
        viewport = header.viewport()
        for object_name, section in mapping.items():
            button = viewport.findChild(QtWidgets.QToolButton, object_name)
            if button is None:
                continue
            self._position_marked_header_icon_button(button, header, section=section)

    @staticmethod
    def _position_marked_header_icon_button(
        button: QtWidgets.QToolButton,
        header: QtWidgets.QHeaderView,
        *,
        section: int,
    ) -> None:
        if header.isSectionHidden(section):
            button.hide()
            return
        section_width = header.sectionSize(section)
        if section_width <= 0:
            button.hide()
            return
        x = header.sectionViewportPosition(section) + max((section_width - 18) // 2, 0)
        y = 1
        button.move(x, y)

    def set_render_selection_changed_callback(self, callback: Callable[[str], None] | None) -> None:
        self._render_selection_changed_callback = callback

    def set_thumbnail_size_changed_callback(self, callback: Callable[[int], None] | None) -> None:
        self._thumbnail_size_changed_callback = callback

    def set_thumbnail_density_changed_callback(self, callback: Callable[[int], None] | None) -> None:
        self._thumbnail_density_changed_callback = callback

    def set_thumbnail_limit_changed_callback(
        self,
        callback: Callable[[int], None] | None,
    ) -> None:
        self.set_thumbnail_density_changed_callback(callback)

    def set_thumbnail_density_auto_requested_callback(
        self,
        callback: Callable[[], None] | None,
    ) -> None:
        self._thumbnail_density_auto_requested_callback = callback

    def set_marked_area_style_changed_callback(
        self,
        callback: Callable[[float], None] | None,
    ) -> None:
        self._marked_area_style_changed_callback = callback

    def _on_marked_area_style_changed(self, _value: int | float) -> None:
        if callable(self._marked_area_style_changed_callback):
            self._marked_area_style_changed_callback(float(self._marked_area_opacity_spin.value()) / 100.0)

    def selected_thumbnail_size_px(self) -> int:
        return int(self._preview_settings.thumbnail_size_px)

    def selected_thumbnail_limit(self) -> int:
        return int(self._preview_settings.thumbnail_limit)

    def thumbnail_limit_bounds(self) -> tuple[int, int]:
        return (self._PREVIEW_THUMBNAIL_LIMITS[0], self._PREVIEW_THUMBNAIL_LIMITS[-1])

    def thumbnail_density_is_automatic(self) -> bool:
        return bool(self._thumbnail_density_automatic)

    def selected_render_value(self) -> str:
        return str(self._preview_settings.render_value or "none")

    def _settings_with(
        self,
        *,
        render_value: str | None = None,
        thumbnail_size_px: int | None = None,
        thumbnail_limit: int | None = None,
    ) -> PreviewRenderSettings:
        return PreviewRenderSettings(
            render_value=str(
                self._preview_settings.render_value if render_value is None else render_value
            ),
            thumbnail_size_px=int(
                self._preview_settings.thumbnail_size_px
                if thumbnail_size_px is None
                else thumbnail_size_px
            ),
            thumbnail_limit=int(
                self._preview_settings.thumbnail_limit
                if thumbnail_limit is None
                else thumbnail_limit
            ),
        )

    def set_render_options(self, variants: list[ThumbnailVariant], selected: str | None = None) -> None:
        self._render_variants = list(variants)
        self._suspend_render_changed = True
        try:
            self._render_combo.clear()
            for variant in self._render_variants[:100]:
                label = str(variant.label or variant.variant_id)
                self._render_combo.addItem(label, str(variant.variant_id))
            if self._render_variants:
                self._render_combo.insertSeparator(self._render_combo.count())
            else:
                self._render_combo.addItem("no thumbnails available", "none")
                self._render_combo.insertSeparator(self._render_combo.count())
            self._render_combo.addItem("create new thumbnails", "create")
            target = str(selected or self._preview_settings.render_value or "none")
            if target == "create":
                target = "none"
            idx = self._render_combo.findData(target)
            if idx < 0:
                idx = 0 if self._render_combo.count() > 0 else -1
            if idx >= 0:
                self._render_combo.setCurrentIndex(idx)
                render_value = str(self._render_combo.currentData() or "none")
            else:
                render_value = "none"
            self._preview_settings = self._settings_with(render_value=render_value)
            self._last_render_selection = self._preview_settings.render_value
        finally:
            self._suspend_render_changed = False
        self._sync_thumbnail_controls()

    def set_thumbnail_size_px(self, value: int) -> None:
        normalized = self._normalize_thumbnail_size(value)
        self._preview_settings = self._settings_with(thumbnail_size_px=normalized)
        self._suspend_thumbnail_size_changed = True
        try:
            self._thumbnail_size_slider.setValue(self._slider_position_for_thumbnail_size(normalized))
        finally:
            self._suspend_thumbnail_size_changed = False

    def set_thumbnail_limit(
        self,
        value: int,
        *,
        automatic: bool | None = None,
    ) -> None:
        normalized = self.normalize_thumbnail_limit(value)
        self._preview_settings = self._settings_with(
            thumbnail_limit=normalized
        )
        self._thumbnail_rendered_count = min(
            self._thumbnail_rendered_count,
            normalized,
        )
        if automatic is not None:
            self._thumbnail_density_automatic = bool(automatic)
        self._suspend_thumbnail_density_changed = True
        try:
            self._thumbnail_density_slider.setValue(
                self._slider_position_for_thumbnail_limit(normalized)
            )
            with QtCore.QSignalBlocker(self._button_auto_density):
                self._button_auto_density.setChecked(
                    self._thumbnail_density_automatic
                )
        finally:
            self._suspend_thumbnail_density_changed = False
        self._sync_thumbnail_density_warning()

    @classmethod
    def normalize_thumbnail_limit(cls, value: int) -> int:
        """Return the available preview limit nearest to ``value``."""

        return min(
            cls._PREVIEW_THUMBNAIL_LIMITS,
            key=lambda limit: (abs(limit - int(value)), limit),
        )

    @classmethod
    def _slider_position_for_thumbnail_limit(cls, value: int) -> int:
        return cls._PREVIEW_THUMBNAIL_LIMITS.index(cls.normalize_thumbnail_limit(value))

    @classmethod
    def _thumbnail_limit_for_slider_position(cls, position: int) -> int:
        bounded_position = max(0, min(len(cls._PREVIEW_THUMBNAIL_LIMITS) - 1, int(position)))
        return cls._PREVIEW_THUMBNAIL_LIMITS[bounded_position]

    @classmethod
    def _normalize_thumbnail_size(cls, value: object) -> int:
        try:
            parsed = int(round(float(value)))
        except (TypeError, ValueError):
            parsed = 64
        clamped = max(cls._PREVIEW_THUMBNAIL_SIZE_MIN, min(cls._PREVIEW_THUMBNAIL_SIZE_MAX, parsed))
        offset = clamped - cls._PREVIEW_THUMBNAIL_SIZE_MIN
        snapped_offset = int(round(offset / cls._PREVIEW_THUMBNAIL_SIZE_STEP)) * cls._PREVIEW_THUMBNAIL_SIZE_STEP
        return cls._PREVIEW_THUMBNAIL_SIZE_MIN + snapped_offset

    @classmethod
    def _slider_position_for_thumbnail_size(cls, value: object) -> int:
        normalized = cls._normalize_thumbnail_size(value)
        return ((normalized - cls._PREVIEW_THUMBNAIL_SIZE_MIN) // cls._PREVIEW_THUMBNAIL_SIZE_STEP) + 1

    @classmethod
    def _thumbnail_size_for_slider_position(cls, value: object) -> int:
        try:
            position = int(round(float(value)))
        except (TypeError, ValueError):
            position = cls._slider_position_for_thumbnail_size(64)
        position = max(cls._PREVIEW_THUMBNAIL_SLIDER_MIN, min(cls._PREVIEW_THUMBNAIL_SLIDER_MAX, position))
        return cls._PREVIEW_THUMBNAIL_SIZE_MIN + ((position - 1) * cls._PREVIEW_THUMBNAIL_SIZE_STEP)

    def _sync_thumbnail_controls(self) -> None:
        self._render_combo.setEnabled(self._previews_enabled)
        enabled = self._previews_enabled and self._preview_settings.render_value not in {"", "none"}
        self._thumbnail_size_slider.setEnabled(enabled)
        self._thumbnail_density_slider.setEnabled(enabled)
        self._button_auto_density.setEnabled(enabled)
        self._sync_thumbnail_density_warning()

    def _sync_thumbnail_density_warning(self) -> None:
        visible = (
            self._preview_settings_active()
            and self._thumbnail_potential_count > self._THUMBNAIL_DENSITY_WARNING_THRESHOLD
            and self._preview_settings.thumbnail_limit >= self._THUMBNAIL_DENSITY_WARNING_THRESHOLD
        )
        self._thumbnail_density_warning.setVisible(True)
        self._thumbnail_density_warning_effect.setOpacity(1.0 if visible else 0.0)

    def set_thumbnail_rendered_count(self, count: int) -> None:
        self._thumbnail_rendered_count = max(0, int(count))

    def thumbnail_rendered_count(self) -> int:
        return self._thumbnail_rendered_count

    def set_thumbnail_potential_count(self, count: int) -> None:
        self._thumbnail_potential_count = max(0, int(count))
        self._sync_thumbnail_density_warning()

    def set_previews_enabled(self, enabled: bool) -> None:
        was_enabled = self._previews_enabled
        self._previews_enabled = bool(enabled)
        if self._previews_enabled and not was_enabled:
            self._select_first_render_variant_if_needed()
            self._emit_all_preview_settings_or_status()
            return
        self._sync_thumbnail_controls()

    def _select_first_render_variant_if_needed(self) -> None:
        if self._preview_settings.render_value not in {"", "none"} or not self._render_variants:
            return
        first_variant_id = str(self._render_variants[0].variant_id)
        index = self._render_combo.findData(first_variant_id)
        if index < 0:
            return
        self._suspend_render_changed = True
        try:
            self._render_combo.setCurrentIndex(index)
            self._preview_settings = self._settings_with(render_value=first_variant_id)
            self._last_render_selection = first_variant_id
        finally:
            self._suspend_render_changed = False

    def _on_render_combo_changed(self, _index: int) -> None:
        if self._suspend_render_changed:
            return
        value = str(self._render_combo.currentData() or "none")
        if value == "create":
            self._suspend_render_changed = True
            try:
                idx = self._render_combo.findData(self._last_render_selection)
                if idx >= 0:
                    self._render_combo.setCurrentIndex(idx)
            finally:
                self._suspend_render_changed = False
            QtCore.QTimer.singleShot(0, self._request_create_thumbnails)
            return
        self._preview_settings = self._settings_with(render_value=value)
        self._last_render_selection = value
        self._sync_thumbnail_controls()
        self._emit_preview_render_selection()

    def _on_thumbnail_size_slider_value_changed(self, slider_position: int) -> None:
        if self._suspend_thumbnail_size_changed:
            return
        normalized = self._thumbnail_size_for_slider_position(slider_position)
        self._preview_settings = self._settings_with(thumbnail_size_px=normalized)
        self._sync_thumbnail_controls()
        self._emit_preview_thumbnail_size()

    def _on_thumbnail_density_slider_value_changed(self, value: int) -> None:
        if self._suspend_thumbnail_density_changed:
            return
        normalized = self._thumbnail_limit_for_slider_position(value)
        self._preview_settings = self._settings_with(
            thumbnail_limit=normalized
        )
        self._thumbnail_rendered_count = min(
            self._thumbnail_rendered_count,
            normalized,
        )
        self._thumbnail_density_automatic = False
        with QtCore.QSignalBlocker(self._button_auto_density):
            self._button_auto_density.setChecked(False)
        self._sync_thumbnail_density_warning()
        self._sync_thumbnail_controls()
        self._emit_preview_thumbnail_density()

    def _on_thumbnail_density_auto_toggled(self, checked: bool) -> None:
        self._thumbnail_density_automatic = bool(checked)
        if checked and callable(self._thumbnail_density_auto_requested_callback):
            self._thumbnail_density_auto_requested_callback()
        elif not checked:
            self._emit_preview_thumbnail_density()
        self._sync_thumbnail_controls()

    def _preview_settings_active(self) -> bool:
        return self._previews_enabled and self._preview_settings.render_value not in {"", "none"}

    def _emit_all_preview_settings_or_status(self) -> None:
        self._sync_thumbnail_controls()
        if not self._preview_settings_active():
            self._show_status("Create thumbnails before enabling previews.")
            return
        self._emit_preview_thumbnail_size()
        self._emit_preview_thumbnail_density()
        self._emit_preview_render_selection()

    def _emit_preview_render_selection(self) -> None:
        if not self._preview_settings_active():
            return
        render_value = self._preview_settings.render_value
        if callable(self._render_selection_changed_callback):
            self._render_selection_changed_callback(render_value)
        self._show_preview_status()

    def _emit_preview_thumbnail_size(self) -> None:
        if not self._preview_settings_active():
            return
        if callable(self._thumbnail_size_changed_callback):
            self._thumbnail_size_changed_callback(int(self._preview_settings.thumbnail_size_px))
        self._show_preview_status()

    def _emit_preview_thumbnail_density(self) -> None:
        if not self._preview_settings_active():
            return
        if callable(self._thumbnail_density_changed_callback):
            self._thumbnail_density_changed_callback(
                int(self._preview_settings.thumbnail_limit)
            )
        self._show_preview_status()

    def _show_preview_status(self) -> None:
        settings = self._preview_settings
        render_value = settings.render_value
        if render_value in {"", "none"}:
            return
        self._show_status(
            f"Previews: {self._render_combo.currentText()}, "
            f"{int(settings.thumbnail_size_px)} px, "
            f"up to {int(settings.thumbnail_limit):,} visible previews"
            f"{' (auto)' if self._thumbnail_density_automatic else ''}."
        )

    def _bind_fields_selection(self, host: QtWidgets.QWidget | str, *, mode: str) -> QtWidgets.QWidget | None:
        if isinstance(host, str):
            host = self._widget.findChild(QtWidgets.QWidget, host)
        if host is None:
            return None
        fields = host
        if self._field_list(fields, mode) is None and self._thumbnail_combo(fields, mode) is None:
            fields = load_ui(self.package, "ui/fields_selection.ui", self._widget)
            add_widget_to_container(host, fields)
        combo = self._thumbnail_combo(fields, mode)
        if combo is not None:
            self._populate_thumbnail_combo(combo, include_none=False)
            combo.currentIndexChanged.connect(
                lambda _index, current_mode=mode, current_combo=combo: self._handle_thumbnail_combo_changed(
                    current_mode,
                    current_combo,
                )
            )
        add_thumbnails = self._find_child_any(
            fields,
            QtWidgets.QAbstractButton,
            (
                "button_add_new_thumbnails",
                "button_add_new_thumbnails_2",
                "button_add_new_thumbnails_tooltip",
                "button_create_new_thumbnails",
            ),
        )
        if add_thumbnails is not None:
            add_thumbnails.clicked.connect(self._request_create_thumbnails)
        selected = self._field_list(fields, mode)
        if selected is not None:
            selected.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
            checked = list(self._pin_settings.tooltip_fields if mode == "tooltip" else self._pin_settings.pin_fields)
            self._populate_field_list(selected, checked_fields=checked)
            selected.itemChanged.connect(lambda _item, current_mode=mode: self._handle_field_list_changed(current_mode))
        sync = self._field_button(fields, mode, "sync")
        if sync is not None:
            sync.clicked.connect(lambda _checked=False, source=mode: self._sync_fields_from_other_page(source))
        edit = self._field_button(fields, mode, "edit")
        if edit is not None and mode == "pins":
            edit.clicked.connect(self.show_pin_style_settings)
        self._wire_field_move_button(fields, mode, "up", -1)
        self._wire_field_move_button(fields, mode, "down", 1)
        return fields

    @staticmethod
    def _find_child_any(
        root: QtWidgets.QWidget,
        widget_type: type,
        names: tuple[str, ...],
    ):
        for name in names:
            child = root.findChild(widget_type, name)
            if child is not None:
                return child
        return None

    def _field_list(self, root: QtWidgets.QWidget, mode: str) -> QtWidgets.QListWidget | None:
        names = (
            ("list_pin_selected_metadata_fields", "list_selected_metadata_fields")
            if mode == "pins"
            else (
                "list_tooltip_selected_metadata_fields",
                "list_pin_selected_metadata_fields_tooltip",
                "list_pin_selected_metadata_fields",
                "list_selected_metadata_fields",
            )
        )
        return self._find_child_any(root, QtWidgets.QListWidget, names)

    def _thumbnail_combo(self, root: QtWidgets.QWidget, mode: str) -> QtWidgets.QComboBox | None:
        names = (
            ("combo_pin_thumbnail_field", "combo_pin_thumbnail_field_2", "combo_thumbnail_field")
            if mode == "pins"
            else ("combo_tooltip_thumbnail_field", "combo_pin_thumbnail_field", "combo_thumbnail_field")
        )
        return self._find_child_any(root, QtWidgets.QComboBox, names)

    def _field_button(
        self,
        root: QtWidgets.QWidget,
        mode: str,
        action: str,
    ) -> QtWidgets.QAbstractButton | None:
        names_by_action = {
            "up": (
                ("button_pin_move_up", "button_move_up")
                if mode == "pins"
                else ("button_tooltip_move_up", "button_pin_move_up_tooltip", "button_pin_move_up", "button_move_up")
            ),
            "down": (
                ("button_pin_move_down", "button_move_down")
                if mode == "pins"
                else ("button_tooltip_move_down", "button_pin_move_down_tooltip", "button_pin_move_down", "button_move_down")
            ),
            "sync": (
                ("button_pin_sync_metadata_fields", "button_sync_metadata_fields")
                if mode == "pins"
                else (
                    "button_tooltip_sync_metadata_fields",
                    "button_pin_sync_metadata_fields_tooltip",
                    "button_pin_sync_metadata_fields",
                    "button_sync_metadata_fields",
                )
            ),
            "edit": (
                ("button_edit_pin_layout", "button_edit_tooltip_layout")
                if mode == "pins"
                else ("button_edit_tooltip_layout",)
            ),
        }
        return self._find_child_any(root, QtWidgets.QAbstractButton, names_by_action[action])

    def _populate_field_list(self, list_widget: QtWidgets.QListWidget, *, checked_fields: list[str]) -> None:
        with QtCore.QSignalBlocker(list_widget):
            list_widget.clear()
            checked = {str(field).strip() for field in checked_fields if str(field).strip()}
            for field in self._available_metadata_fields:
                item = QtWidgets.QListWidgetItem(field)
                item.setFlags(
                    item.flags()
                    | QtCore.Qt.ItemFlag.ItemIsUserCheckable
                    | QtCore.Qt.ItemFlag.ItemIsEnabled
                    | QtCore.Qt.ItemFlag.ItemIsSelectable
                )
                item.setCheckState(
                    QtCore.Qt.CheckState.Checked if field in checked else QtCore.Qt.CheckState.Unchecked
                )
                list_widget.addItem(item)

    def set_metadata_fields(
        self,
        fields: list[str],
        *,
        tooltip_checked: list[str] | None = None,
        pin_checked: list[str] | None = None,
    ) -> None:
        cleaned = [str(field).strip() for field in fields if str(field).strip()]
        self._available_metadata_fields = cleaned or ["id", "label", "category"]
        self._suspend_pin_display_changed = True
        if self._pin_fields is not None:
            pin_list = self._field_list(self._pin_fields, "pins")
            if pin_list is not None:
                self._populate_field_list(
                    pin_list,
                    checked_fields=self._valid_fields_or_default(
                        list(self._pin_settings.pin_fields) if pin_checked is None else pin_checked,
                        allow_empty=pin_checked is not None,
                    ),
                )
            self._refresh_pin_thumbnail_combo(self._pin_thumbnail_field)
        tooltip_fields = self._valid_fields_or_default(
            list(self._pin_settings.tooltip_fields) if tooltip_checked is None else tooltip_checked,
            allow_empty=tooltip_checked is not None,
        )
        if self._tooltip_fields is not None:
            tooltip_list = self._field_list(self._tooltip_fields, "tooltip")
            if tooltip_list is not None:
                self._populate_field_list(tooltip_list, checked_fields=tooltip_fields)
            self._refresh_tooltip_thumbnail_combo(self._tooltip_thumbnail_field)
        self._pin_settings = DiscoveryPinDisplaySettings.from_payload(
            {
                **self._pin_settings.to_payload(),
                "pin_fields": self.selected_pin_fields(),
                "tooltip_fields": tooltip_fields,
            }
        )
        self._suspend_pin_display_changed = False

    def selected_pin_fields(self) -> list[str]:
        if self._pin_fields is None:
            return []
        selected = self._field_list(self._pin_fields, "pins")
        if selected is None:
            return []
        output: list[str] = []
        for row in range(selected.count()):
            item = selected.item(row)
            if item.checkState() == QtCore.Qt.CheckState.Checked:
                output.append(item.text())
        return output

    def selected_tooltip_fields(self) -> list[str]:
        if self._tooltip_fields is None:
            return list(self._pin_settings.tooltip_fields)
        selected = self._field_list(self._tooltip_fields, "tooltip")
        if selected is None:
            return []
        output: list[str] = []
        for row in range(selected.count()):
            item = selected.item(row)
            if item.checkState() == QtCore.Qt.CheckState.Checked:
                output.append(item.text())
        return output

    def _wire_field_move_button(self, root: QtWidgets.QWidget, mode: str, action: str, offset: int) -> None:
        button = self._field_button(root, mode, action)
        selected = self._field_list(root, mode)
        if button is None or selected is None:
            return
        button.clicked.connect(lambda: self._move_selected_field(selected, offset))

    def _move_selected_field(self, list_widget: QtWidgets.QListWidget, offset: int) -> None:
        row = list_widget.currentRow()
        target = row + offset
        if row < 0 or target < 0 or target >= list_widget.count():
            return
        item = list_widget.takeItem(row)
        list_widget.insertItem(target, item)
        list_widget.setCurrentRow(target)
        mode = "pins" if self._pin_fields is not None and list_widget is self._field_list(self._pin_fields, "pins") else "tooltip"
        self._handle_field_list_changed(mode)

    def _sync_fields_from_other_page(self, target_mode: str) -> None:
        source_root = self._pin_fields if target_mode == "tooltip" else self._tooltip_fields
        target_root = self._tooltip_fields if target_mode == "tooltip" else self._pin_fields
        source = (
            self._field_list(source_root, "pins" if target_mode == "tooltip" else "tooltip")
            if source_root is not None
            else None
        )
        target = (
            self._field_list(target_root, target_mode)
            if target_root is not None
            else None
        )
        if source is None or target is None:
            return
        checked = [
            source.item(row).text()
            for row in range(source.count())
            if source.item(row).checkState() == QtCore.Qt.CheckState.Checked
        ]
        self._populate_field_list(target, checked_fields=checked)
        self._show_status("Layers setting field selection synced.")
        self._handle_field_list_changed(target_mode)

    def _handle_field_list_changed(self, mode: str) -> None:
        if self._suspend_pin_display_changed:
            return
        payload = self._pin_settings.to_payload()
        if mode == "tooltip":
            payload["tooltip_fields"] = self.selected_tooltip_fields()
        else:
            payload["pin_fields"] = self.selected_pin_fields()
            payload["fields"] = payload["pin_fields"]
        self._pin_settings = DiscoveryPinDisplaySettings.from_payload(payload)
        self._emit_pin_display_changed()

    def _valid_fields_or_default(
        self,
        fields: list[str] | tuple[str, ...],
        *,
        allow_empty: bool = False,
    ) -> list[str]:
        available = set(self._available_metadata_fields)
        selected = [str(field).strip() for field in fields if str(field).strip() in available]
        if allow_empty:
            return selected
        return selected or ["id"]

    def _wire_tooltip_alignment_options(self) -> None:
        self._tooltip_alignment_button_group = QtWidgets.QButtonGroup(
            self._tooltip_settings_panel
        )
        self._tooltip_alignment_button_group.setExclusive(True)
        for alignment in ("left", "right", "top", "bottom"):
            button = require_child(
                self._tooltip_settings_panel,
                QtWidgets.QAbstractButton,
                f"button_align_{alignment}",
            )
            self._tooltip_alignment_button_group.addButton(button)
            button.toggled.connect(
                lambda checked, value=alignment: self._set_thumbnail_alignment(
                    "tooltip", value
                )
                if checked
                else None
            )
        self._sync_tooltip_alignment_options()

    def _sync_tooltip_alignment_options(self) -> None:
        for alignment in ("left", "right", "top", "bottom"):
            self._set_button_checked(
                self._tooltip_settings_panel,
                f"button_align_{alignment}",
                alignment == self._tooltip_thumbnail_alignment,
            )

    def _wire_pin_alignment_options(self) -> None:
        self._pin_alignment_button_group = QtWidgets.QButtonGroup(
            self._pin_settings_panel
        )
        self._pin_alignment_button_group.setExclusive(True)
        for alignment in ("left", "right", "top", "bottom"):
            button = require_child(
                self._pin_settings_panel,
                QtWidgets.QAbstractButton,
                f"button_align_{alignment}",
            )
            self._pin_alignment_button_group.addButton(button)
            button.toggled.connect(
                lambda checked, value=alignment: self._update_pin_style_setting(
                    "thumbnail_position", value
                )
                if checked
                else None
            )

    def _sync_pin_alignment_options(self, alignment: str) -> None:
        for value in ("left", "right", "top", "bottom"):
            self._set_button_checked(
                self._pin_settings_panel,
                f"button_align_{value}",
                value == alignment,
            )

    def _wire_thumbnail_alignment_button(
        self,
        mode: str,
        button: QtWidgets.QToolButton,
    ) -> None:
        button.clicked.connect(lambda _checked=False, current_mode=mode, current_button=button: self._show_thumbnail_alignment_menu(current_mode, current_button))

    def _show_thumbnail_alignment_menu(self, mode: str, button: QtWidgets.QToolButton) -> None:
        current = (
            str(self._pin_settings.style.get("thumbnail_position", "left"))
            if mode == "pins"
            else self._tooltip_thumbnail_alignment
        )
        menu = QtWidgets.QMenu(button)
        group = QtGui.QActionGroup(menu)
        group.setExclusive(True)
        for value in ("left", "right", "top", "bottom"):
            action = menu.addAction(f"Align {value.title()}")
            action.setCheckable(True)
            action.setChecked(value == current)
            group.addAction(action)
            action.triggered.connect(
                lambda checked=False, alignment=value, current_mode=mode: self._set_thumbnail_alignment(
                    current_mode, alignment
                )
            )
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def _set_thumbnail_alignment(self, mode: str, alignment: str) -> None:
        if mode == "pins":
            self._update_pin_style_setting("thumbnail_position", alignment)
            return
        self._tooltip_thumbnail_alignment = alignment
        self._sync_tooltip_alignment_options()
        payload = self._pin_settings.to_payload()
        payload["tooltip_thumbnail_alignment"] = alignment
        self._pin_settings = DiscoveryPinDisplaySettings.from_payload(payload)
        self._emit_pin_display_changed()

    def _wire_pin_thumbnail_size_control(self, panel: QtWidgets.QWidget) -> None:
        self._pin_thumbnail_size_slider = self._find_child_any(
            panel,
            QtWidgets.QSlider,
            ("slider_pin_thumbnail_size",),
        )
        if self._pin_thumbnail_size_slider is None:
            return
        self._pin_thumbnail_size_slider.setRange(25, 500)
        self._pin_thumbnail_size_slider.valueChanged.connect(
            lambda value: self._update_pin_style_setting("thumbnail_size", int(value))
        )

    def _wire_pin_settings_controls(self, panel: QtWidgets.QWidget) -> None:
        self._wire_color_swatch(panel, "swatch_pin_background_color", "#f0f0f0")
        self._wire_color_swatch(panel, "swatch_pin_border_color", "#666666")
        self._wire_color_swatch(panel, "swatch_pin_connector_color", "#333333")
        self._wire_color_swatch(panel, "swatch_connector_color", "#333333")
        self._wire_color_swatch(panel, "swatch_pin_text_color", "#222222")
        for name in ("group_thumbnail", "group_box", "group_connector", "group_info_field"):
            box = panel.findChild(QtWidgets.QGroupBox, name)
            if box is not None:
                state_key = {
                    "group_thumbnail": "thumbnail_visible",
                    "group_box": "box_visible",
                    "group_connector": "connector_visible",
                    "group_info_field": "text_visible",
                }[name]
                if name == "group_box":
                    box.toggled.connect(
                        lambda checked, current_panel=panel: self._handle_pin_box_toggled(
                            current_panel,
                            bool(checked),
                        )
                    )
                elif name == "group_thumbnail":
                    box.toggled.connect(self._handle_pin_thumbnail_toggled)
                else:
                    box.toggled.connect(
                        lambda checked, key=state_key: self._update_pin_style_setting(key, bool(checked))
                    )
        for name, key, transform in (
            ("spin_pin_opacity", "box_opacity", lambda value: float(value) / 100.0),
            ("spin_pin_outline_width", "border_width", float),
            ("spin_connector_width", "connector_width", float),
            ("spin_pin_font_size", "font_size", int),
        ):
            spin_int = panel.findChild(QtWidgets.QSpinBox, name)
            if spin_int is not None:
                spin_int.valueChanged.connect(
                    lambda value, current_key=key, current_transform=transform: self._update_pin_style_setting(
                        current_key,
                        current_transform(value),
                    )
                )
            spin_float = panel.findChild(QtWidgets.QDoubleSpinBox, name)
            if spin_float is not None:
                spin_float.valueChanged.connect(
                    lambda value, current_key=key, current_transform=transform: self._update_pin_style_setting(
                        current_key,
                        current_transform(value),
                    )
                )
        thumb_size_spin = panel.findChild(QtWidgets.QSpinBox, "spin_thumbnail_size")
        if thumb_size_spin is not None:
            thumb_size_spin.valueChanged.connect(lambda value: self._update_pin_style_setting("thumbnail_size", int(value)))
        thumb_size_slider = panel.findChild(QtWidgets.QSlider, "slider_thumbnail_size")
        if thumb_size_slider is not None and thumb_size_spin is not None:
            thumb_size_slider.valueChanged.connect(thumb_size_spin.setValue)
            thumb_size_spin.valueChanged.connect(thumb_size_slider.setValue)
        thumbnail_position_buttons = QtWidgets.QButtonGroup(panel)
        thumbnail_position_buttons.setExclusive(True)
        for name in (
            "button_thumbnail_left",
            "button_thumbnail_right",
            "button_thumbnail_top",
            "button_thumbnail_bottom",
            "button_thumbnail_left_2",
            "button_thumbnail_right_2",
            "button_thumbnail_top_2",
            "button_thumbnail_bottom_2",
        ):
            button = panel.findChild(QtWidgets.QAbstractButton, name)
            if button is not None:
                thumbnail_position_buttons.addButton(button)
        font_combo = panel.findChild(QtWidgets.QFontComboBox, "combo_pin_font")
        if font_combo is not None:
            font_combo.currentFontChanged.connect(
                lambda font: self._update_pin_style_setting("font_family", font.family())
            )
        for name, key, invert in (
            ("button_hide_pin_label", "show_label", True),
            ("button_bold_pin_label", "text_label_bold", False),
            ("button_italic_pin_label", "text_label_italic", False),
            ("button_underline_pin_label", "text_label_underline", False),
            ("button_bold_pin_value", "text_value_bold", False),
            ("button_italic_pin_value", "text_value_italic", False),
            ("button_underline_pin_value", "text_value_underline", False),
        ):
            button = panel.findChild(QtWidgets.QAbstractButton, name)
            if button is not None:
                button.toggled.connect(
                    lambda checked, current_key=key, current_invert=invert: self._update_pin_style_setting(
                        current_key,
                        not bool(checked) if current_invert else bool(checked),
                    )
                )
        for name, key, value in (
            ("radio_thumbnail_left", "thumbnail_position", "left"),
            ("radio_thumbnail_right", "thumbnail_position", "right"),
            ("radio_thumbnail_top", "thumbnail_position", "top"),
            ("radio_thumbnail_bottom", "thumbnail_position", "bottom"),
            ("button_thumbnail_left", "thumbnail_position", "left"),
            ("button_thumbnail_right", "thumbnail_position", "right"),
            ("button_thumbnail_top", "thumbnail_position", "top"),
            ("button_thumbnail_bottom", "thumbnail_position", "bottom"),
            ("button_thumbnail_left_2", "thumbnail_position", "left"),
            ("button_thumbnail_right_2", "thumbnail_position", "right"),
            ("button_thumbnail_top_2", "thumbnail_position", "top"),
            ("button_thumbnail_bottom_2", "thumbnail_position", "bottom"),
            ("radio_connector_to_center", "connector_target", "center"),
            ("radio_connector_to_thumbnail", "connector_target", "edge"),
            ("radio_connector_to_corner", "connector_target", "corner"),
        ):
            button = panel.findChild(QtWidgets.QAbstractButton, name)
            if button is not None:
                button.toggled.connect(
                    lambda checked, current_key=key, current_value=value: self._update_pin_style_setting(
                        current_key,
                        current_value,
                    )
                    if checked
                    else None
                )

    def _apply_pin_style_state_to_panel(self, panel: QtWidgets.QWidget) -> None:
        previous = self._suspend_pin_display_changed
        self._suspend_pin_display_changed = True
        try:
            style = self._pin_settings.style
            for key, widget_name in (
                ("thumbnail_visible", "group_thumbnail"),
                ("box_visible", "group_box"),
                ("connector_visible", "group_connector"),
                ("text_visible", "group_info_field"),
            ):
                box = panel.findChild(QtWidgets.QGroupBox, widget_name)
                if box is not None:
                    with QtCore.QSignalBlocker(box):
                        box.setChecked(bool(style.get(key, box.isChecked())))
            for key, widget_name, default in (
                ("box_color", "swatch_pin_background_color", "#f0f0f0"),
                ("border_color", "swatch_pin_border_color", "#666666"),
                ("connector_color", "swatch_pin_connector_color", "#333333"),
                ("connector_color", "swatch_connector_color", "#333333"),
                ("text_color", "swatch_pin_text_color", "#222222"),
            ):
                self._set_swatch_color(panel, widget_name, str(style.get(key, default)))
            spin_opacity = panel.findChild(QtWidgets.QSpinBox, "spin_pin_opacity")
            if spin_opacity is not None:
                with QtCore.QSignalBlocker(spin_opacity):
                    spin_opacity.setValue(int(float(style.get("box_opacity", 0.96)) * 100.0))
            spin_outline = panel.findChild(QtWidgets.QDoubleSpinBox, "spin_pin_outline_width")
            if spin_outline is not None:
                with QtCore.QSignalBlocker(spin_outline):
                    spin_outline.setValue(float(style.get("border_width", 1.0)))
            spin_connector = panel.findChild(QtWidgets.QDoubleSpinBox, "spin_connector_width")
            if spin_connector is not None:
                with QtCore.QSignalBlocker(spin_connector):
                    spin_connector.setValue(float(style.get("connector_width", 1.5)))
            spin_font = panel.findChild(QtWidgets.QSpinBox, "spin_pin_font_size")
            if spin_font is not None:
                with QtCore.QSignalBlocker(spin_font):
                    spin_font.setValue(int(style.get("font_size", 11)))
            font_combo = panel.findChild(QtWidgets.QFontComboBox, "combo_pin_font")
            if font_combo is not None:
                family = str(style.get("font_family") or "").strip()
                with QtCore.QSignalBlocker(font_combo):
                    if family:
                        font_combo.setCurrentFont(QtGui.QFont(family))
            for key, widget_name, invert in (
                ("show_label", "button_hide_pin_label", True),
                ("text_label_bold", "button_bold_pin_label", False),
                ("text_label_italic", "button_italic_pin_label", False),
                ("text_label_underline", "button_underline_pin_label", False),
                ("text_value_bold", "button_bold_pin_value", False),
                ("text_value_italic", "button_italic_pin_value", False),
                ("text_value_underline", "button_underline_pin_value", False),
            ):
                button = panel.findChild(QtWidgets.QAbstractButton, widget_name)
                if button is not None:
                    with QtCore.QSignalBlocker(button):
                        checked = bool(style.get(key, False))
                        button.setChecked(not checked if invert else checked)
            thumb_size = int(style.get("thumbnail_size", 96))
            thumb_size_spin = panel.findChild(QtWidgets.QSpinBox, "spin_thumbnail_size")
            if thumb_size_spin is not None:
                with QtCore.QSignalBlocker(thumb_size_spin):
                    thumb_size_spin.setValue(thumb_size)
            thumb_size_slider = self._find_child_any(
                panel,
                QtWidgets.QSlider,
                (
                    "slider_thumbnail_size",
                    "slider_pin_thumbnail_size",
                    "slider_tooltip_thumbnail_size",
                    "slider_preview_thumbnail_size",
                    "slider_tooltip_thumbnail_size_2",
                ),
            )
            if thumb_size_slider is not None:
                with QtCore.QSignalBlocker(thumb_size_slider):
                    thumb_size_slider.setValue(thumb_size)
            position = str(style.get("thumbnail_position", "left"))
            self._sync_pin_alignment_options(position)
            self._set_button_checked(panel, "radio_thumbnail_left", position == "left")
            self._set_button_checked(panel, "radio_thumbnail_right", position == "right")
            self._set_button_checked(panel, "radio_thumbnail_top", position == "top")
            self._set_button_checked(panel, "radio_thumbnail_bottom", position == "bottom")
            self._set_button_checked(panel, "button_thumbnail_left", position == "left")
            self._set_button_checked(panel, "button_thumbnail_right", position == "right")
            self._set_button_checked(panel, "button_thumbnail_top", position == "top")
            self._set_button_checked(panel, "button_thumbnail_bottom", position == "bottom")
            self._set_button_checked(panel, "button_thumbnail_left_2", position == "left")
            self._set_button_checked(panel, "button_thumbnail_right_2", position == "right")
            self._set_button_checked(panel, "button_thumbnail_top_2", position == "top")
            self._set_button_checked(panel, "button_thumbnail_bottom_2", position == "bottom")
            connector_target = str(style.get("connector_target", "center"))
            self._sync_connector_anchor_controls(
                panel,
                box_visible=bool(style.get("box_visible", True)),
                connector_target=connector_target,
            )
        finally:
            self._suspend_pin_display_changed = previous

    def _handle_pin_box_toggled(self, dialog: QtWidgets.QWidget, checked: bool) -> None:
        self._update_pin_style_setting("box_visible", bool(checked))
        self._sync_connector_anchor_controls(
            dialog,
            box_visible=bool(checked),
            connector_target=str(self._pin_settings.style.get("connector_target", "center")),
        )

    def _handle_pin_thumbnail_toggled(self, checked: bool) -> None:
        self._update_pin_style_setting("thumbnail_visible", bool(checked))
        self._sync_pin_thumbnail_size_enabled()

    def _sync_connector_anchor_controls(
        self,
        dialog: QtWidgets.QWidget,
        *,
        box_visible: bool,
        connector_target: str,
    ) -> None:
        normalized = str(connector_target or "center").strip().lower()
        normalized = {"box": "center", "thumbnail": "edge"}.get(normalized, normalized)
        self._set_radio_checked(dialog, "radio_connector_to_center", normalized == "center")
        self._set_radio_checked(dialog, "radio_connector_to_thumbnail", normalized == "edge")
        self._set_radio_checked(dialog, "radio_connector_to_corner", normalized == "corner")

    def _update_pin_style_setting(self, key: str, value: object) -> None:
        if self._suspend_pin_display_changed:
            return
        style = dict(self._pin_settings.style)
        style[str(key)] = value
        self._pin_settings = DiscoveryPinDisplaySettings.from_payload(
            {**self._pin_settings.to_payload(), "style": style}
        )
        self._emit_pin_display_changed()

    def _wire_color_swatch(self, root: QtWidgets.QWidget, object_name: str, default_color: str) -> None:
        swatch = root.findChild(QtWidgets.QWidget, object_name)
        if swatch is None:
            return
        color = str(swatch.property("color") or default_color)
        swatch.setProperty("color", color)
        swatch.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        swatch.installEventFilter(self._swatch_filter)
        swatch.setStyleSheet(f"background:{color}; border:1px solid #666666;")

    @staticmethod
    def _set_swatch_color(root: QtWidgets.QWidget, object_name: str, color: str) -> None:
        swatch = root.findChild(QtWidgets.QWidget, object_name)
        if swatch is None:
            return
        value = str(color or "#777777")
        swatch.setProperty("color", value)
        swatch.setStyleSheet(f"background:{value}; border:1px solid #666666;")

    def _handle_swatch_clicked(self, swatch: QtWidgets.QWidget) -> bool:
        name = swatch.objectName()
        if not name.startswith("swatch_"):
            return False
        current = QtGui.QColor(str(swatch.property("color") or "#777777"))
        chosen = QtWidgets.QColorDialog.getColor(current, swatch.window(), "Choose color")
        if chosen.isValid():
            swatch.setProperty("color", chosen.name())
            swatch.setStyleSheet(f"background:{chosen.name()}; border:1px solid #666666;")
            key = {
                "swatch_pin_background_color": "box_color",
                "swatch_pin_border_color": "border_color",
                "swatch_pin_connector_color": "connector_color",
                "swatch_connector_color": "connector_color",
                "swatch_pin_text_color": "text_color",
            }.get(name)
            if key is not None:
                self._update_pin_style_setting(key, chosen.name())
        return True

    def pin_display_style(self) -> dict[str, object]:
        return dict(self._pin_settings.style)

    @staticmethod
    def _set_radio_checked(root: QtWidgets.QWidget, object_name: str, checked: bool) -> None:
        radio = root.findChild(QtWidgets.QAbstractButton, object_name)
        if radio is not None:
            with QtCore.QSignalBlocker(radio):
                radio.setChecked(bool(checked))

    @staticmethod
    def _set_button_checked(root: QtWidgets.QWidget, object_name: str, checked: bool) -> None:
        button = root.findChild(QtWidgets.QAbstractButton, object_name)
        if button is not None:
            with QtCore.QSignalBlocker(button):
                button.setChecked(bool(checked))

    def pin_display_settings_payload(self) -> dict[str, object]:
        payload = self._pin_settings.to_payload()
        payload["pin_fields"] = self.selected_pin_fields()
        payload["tooltip_fields"] = self.selected_tooltip_fields()
        payload["fields"] = list(payload["pin_fields"])
        payload["thumbnail_variant_id"] = self._normalize_thumbnail_selection(self._pin_thumbnail_field)
        payload["tooltip_thumbnail_variant_id"] = self._normalize_thumbnail_selection(self._tooltip_thumbnail_field)
        payload["tooltip_thumbnail_size"] = self._current_tooltip_thumbnail_size()
        payload["tooltip_thumbnail_visible"] = self._tooltip_thumbnail_group.isChecked()
        payload["tooltip_info_field_visible"] = self._tooltip_info_field_group.isChecked()
        payload["tooltip_show_label"] = self._tooltip_show_label()
        self._pin_settings = DiscoveryPinDisplaySettings.from_payload(payload)
        return self._pin_settings.to_payload()

    def apply_pin_display_settings_payload(self, payload: dict[str, object]) -> None:
        if not isinstance(payload, dict):
            return
        self._thumbnail_defaults_applied = True
        settings = DiscoveryPinDisplaySettings.from_payload(payload)
        raw_pin_fields = payload.get("pin_fields")
        if raw_pin_fields is None:
            raw_pin_fields = payload.get("fields")
        allow_empty_pin_fields = isinstance(raw_pin_fields, list | tuple) and len(raw_pin_fields) == 0
        raw_tooltip_fields = payload.get("tooltip_fields")
        allow_empty_tooltip_fields = isinstance(raw_tooltip_fields, list | tuple) and len(raw_tooltip_fields) == 0
        pin_thumbnail = self._normalize_thumbnail_selection(settings.pin_thumbnail_variant_id)
        tooltip_thumbnail = self._normalize_thumbnail_selection(settings.tooltip_thumbnail_variant_id)
        tooltip_thumbnail_visible = bool(
            payload.get("tooltip_thumbnail_visible", tooltip_thumbnail != "none")
        )
        if tooltip_thumbnail == "none":
            tooltip_thumbnail = self.first_available_thumbnail_variant_id() or "none"
        settings = DiscoveryPinDisplaySettings.from_payload(
            {
                **settings.to_payload(),
                "pin_fields": self._valid_fields_or_default(
                    list(settings.pin_fields),
                    allow_empty=allow_empty_pin_fields,
                ),
                "tooltip_fields": self._valid_fields_or_default(
                    list(settings.tooltip_fields),
                    allow_empty=allow_empty_tooltip_fields,
                ),
                "thumbnail_variant_id": pin_thumbnail,
                "tooltip_thumbnail_variant_id": tooltip_thumbnail,
                "tooltip_thumbnail_size": settings.tooltip_thumbnail_size,
                "tooltip_thumbnail_visible": tooltip_thumbnail_visible,
                "tooltip_info_field_visible": settings.tooltip_info_field_visible,
                "tooltip_show_label": settings.tooltip_show_label,
            }
        )
        self._suspend_pin_display_changed = True
        try:
            self._pin_settings = settings
            self._pin_thumbnail_field = pin_thumbnail
            self._tooltip_thumbnail_field = tooltip_thumbnail
            self._tooltip_thumbnail_alignment = settings.tooltip_thumbnail_alignment
            with QtCore.QSignalBlocker(self._tooltip_thumbnail_group):
                self._tooltip_thumbnail_group.setChecked(settings.tooltip_thumbnail_visible)
            with QtCore.QSignalBlocker(self._tooltip_info_field_group):
                self._tooltip_info_field_group.setChecked(settings.tooltip_info_field_visible)
            self._sync_tooltip_alignment_options()
            with QtCore.QSignalBlocker(self._tooltip_thumbnail_size_slider):
                self._tooltip_thumbnail_size_slider.setValue(int(settings.tooltip_thumbnail_size))
            pin_list = self._field_list(self._pin_fields, "pins") if self._pin_fields is not None else None
            if pin_list is not None:
                self._populate_field_list(pin_list, checked_fields=list(settings.pin_fields))
            pin_combo = self._thumbnail_combo(self._pin_fields, "pins") if self._pin_fields is not None else None
            if pin_combo is not None:
                self._set_combo_data(pin_combo, pin_thumbnail)
            tooltip_list = self._field_list(self._tooltip_fields, "tooltip") if self._tooltip_fields is not None else None
            if tooltip_list is not None:
                self._populate_field_list(tooltip_list, checked_fields=list(settings.tooltip_fields))
            tooltip_combo = self._thumbnail_combo(self._tooltip_fields, "tooltip") if self._tooltip_fields is not None else None
            if tooltip_combo is not None:
                self._set_combo_data(tooltip_combo, tooltip_thumbnail)
            self._apply_pin_style_state_to_panel(self._pin_settings_panel)
        finally:
            self._suspend_pin_display_changed = False
        self._sync_tooltip_thumbnail_size_enabled()
        self._sync_pin_thumbnail_size_enabled()

    def _emit_pin_display_changed(self) -> None:
        if self._suspend_pin_display_changed:
            return
        payload = self._pin_settings.to_payload()
        payload["pin_fields"] = self.selected_pin_fields()
        payload["tooltip_fields"] = self.selected_tooltip_fields()
        payload["fields"] = list(payload["pin_fields"])
        payload["thumbnail_variant_id"] = self._current_pin_thumbnail_field()
        payload["tooltip_thumbnail_variant_id"] = self._current_tooltip_thumbnail_field()
        payload["tooltip_thumbnail_size"] = self._current_tooltip_thumbnail_size()
        payload["tooltip_thumbnail_visible"] = self._tooltip_thumbnail_group.isChecked()
        payload["tooltip_info_field_visible"] = self._tooltip_info_field_group.isChecked()
        payload["tooltip_show_label"] = self._tooltip_show_label()
        self._pin_settings = DiscoveryPinDisplaySettings.from_payload(payload)
        self._pin_thumbnail_field = self._pin_settings.pin_thumbnail_variant_id
        self._tooltip_thumbnail_field = self._pin_settings.tooltip_thumbnail_variant_id
        if callable(self._pin_display_changed_callback):
            self._pin_display_changed_callback()

    def _populate_thumbnail_combo(
        self,
        combo: QtWidgets.QComboBox,
        *,
        include_none: bool = True,
    ) -> None:
        combo.clear()
        if include_none:
            combo.addItem("none", "none")
        for variant_id, label in self._thumbnail_variant_entries():
            combo.addItem(label, variant_id)
        combo.insertSeparator(combo.count())
        combo.addItem("create new thumbnails", "create")

    def _set_combo_data(self, combo: QtWidgets.QComboBox, target: str) -> None:
        normalized = self._normalize_thumbnail_selection(target)
        with QtCore.QSignalBlocker(combo):
            for idx in range(combo.count()):
                if str(combo.itemData(idx) or "") == normalized:
                    combo.setCurrentIndex(idx)
                    return
            fallback = combo.findData("none")
            if fallback >= 0:
                combo.setCurrentIndex(fallback)
                return
            for idx in range(combo.count()):
                if str(combo.itemData(idx) or "") not in {"", "create"}:
                    combo.setCurrentIndex(idx)
                    return

    def _normalize_thumbnail_selection(self, value: str) -> str:
        target = str(value or "none").strip() or "none"
        if target in {"none", "create"}:
            return "none"
        available = {variant_id for variant_id, _label in self._thumbnail_variant_entries()}
        return target if target in available else "none"

    def _current_pin_thumbnail_field(self) -> str:
        if self._pin_fields is None:
            return self._pin_thumbnail_field
        combo = self._thumbnail_combo(self._pin_fields, "pins")
        if combo is None:
            return self._pin_thumbnail_field
        value = str(combo.currentData() or "").strip()
        return value or "none"

    def _current_tooltip_thumbnail_field(self) -> str:
        if self._tooltip_fields is None:
            return self._tooltip_thumbnail_field
        combo = self._thumbnail_combo(self._tooltip_fields, "tooltip")
        if combo is None:
            return self._tooltip_thumbnail_field
        value = str(combo.currentData() or "").strip()
        return value or "none"

    def _current_tooltip_thumbnail_size(self) -> int:
        if self._tooltip_thumbnail_size_slider is None:
            return int(self._pin_settings.tooltip_thumbnail_size)
        return max(25, min(500, int(self._tooltip_thumbnail_size_slider.value())))

    def _sync_tooltip_thumbnail_size_enabled(self) -> None:
        if self._tooltip_thumbnail_size_slider is None:
            return
        selected = str(self._tooltip_thumbnail_field or "none").strip() or "none"
        self._tooltip_thumbnail_size_slider.setEnabled(
            self._tooltip_thumbnail_group.isChecked() and selected != "none"
        )

    def _handle_tooltip_thumbnail_toggled(self, checked: bool) -> None:
        if self._suspend_pin_display_changed:
            return
        payload = self._pin_settings.to_payload()
        payload["tooltip_thumbnail_visible"] = bool(checked)
        self._pin_settings = DiscoveryPinDisplaySettings.from_payload(payload)
        self._sync_tooltip_thumbnail_size_enabled()
        self._emit_pin_display_changed()

    def _handle_tooltip_info_field_toggled(self, checked: bool) -> None:
        if self._suspend_pin_display_changed:
            return
        payload = self._pin_settings.to_payload()
        payload["tooltip_info_field_visible"] = bool(checked)
        self._pin_settings = DiscoveryPinDisplaySettings.from_payload(payload)
        self._emit_pin_display_changed()

    def _sync_pin_thumbnail_size_enabled(self) -> None:
        slider = getattr(self, "_pin_thumbnail_size_slider", None)
        if slider is None:
            return
        selected = str(self._pin_thumbnail_field or "none").strip() or "none"
        slider.setEnabled(
            self._pin_thumbnail_group.isChecked() and selected != "none"
        )

    def _tooltip_show_label(self) -> bool:
        if self._button_hide_tooltip_label is None:
            return bool(self._pin_settings.tooltip_show_label)
        return not bool(self._button_hide_tooltip_label.isChecked())

    def _handle_tooltip_thumbnail_size_changed(self, value: int) -> None:
        size = max(25, min(500, int(value)))
        if self._suspend_pin_display_changed:
            return
        payload = self._pin_settings.to_payload()
        payload["tooltip_thumbnail_size"] = size
        self._pin_settings = DiscoveryPinDisplaySettings.from_payload(payload)
        self._emit_pin_display_changed()
        if self._tooltip_thumbnail_size_slider_hovered and callable(self._tooltip_thumbnail_size_changed_callback):
            self._tooltip_thumbnail_size_changed_callback(size)

    def _handle_tooltip_thumbnail_size_slider_entered(self) -> None:
        self._tooltip_thumbnail_size_slider_hovered = True

    def _handle_tooltip_thumbnail_size_slider_left(self) -> None:
        self._tooltip_thumbnail_size_slider_hovered = False
        if callable(self._tooltip_thumbnail_size_preview_hidden_callback):
            self._tooltip_thumbnail_size_preview_hidden_callback()

    def _thumbnail_variant_entries(self) -> list[tuple[str, str]]:
        return [(variant.variant_id, variant.label) for variant in self._thumbnail_variants()]

    def _thumbnail_variants(self) -> list[ThumbnailVariant]:
        if self._project is None:
            return [
                ThumbnailVariant(
                    variant_id=variant_id,
                    label=label,
                    source_dir="",
                    identifier_column="",
                    sizes=(),
                    created_at="",
                )
                for variant_id, label in self._active_thumbnail_variants
            ]
        repo = ThumbnailVariantRepository(self._project.paths.root)
        variants = repo.list_variants()
        self._active_thumbnail_variants = [(variant.variant_id, variant.label) for variant in variants]
        return variants

    def _refresh_pin_thumbnail_combo(self, selected: str) -> None:
        if self._pin_fields is None:
            return
        combo = self._thumbnail_combo(self._pin_fields, "pins")
        if combo is None:
            return
        selected = self._normalize_thumbnail_selection(selected)
        if selected == "none":
            selected = self.first_available_thumbnail_variant_id() or "none"
        self._pin_thumbnail_field = selected
        self._populate_thumbnail_combo(combo, include_none=False)
        self._set_combo_data(combo, selected)
        self._sync_pin_thumbnail_size_enabled()

    def _refresh_tooltip_thumbnail_combo(self, selected: str) -> None:
        if self._tooltip_fields is None:
            return
        combo = self._thumbnail_combo(self._tooltip_fields, "tooltip")
        if combo is None:
            return
        selected = self._normalize_thumbnail_selection(selected)
        if selected == "none":
            selected = self.first_available_thumbnail_variant_id() or "none"
        self._tooltip_thumbnail_field = selected
        self._populate_thumbnail_combo(combo, include_none=False)
        self._set_combo_data(combo, selected)
        self._sync_tooltip_thumbnail_size_enabled()

    def first_available_thumbnail_variant_id(self) -> str | None:
        entries = self._thumbnail_variant_entries()
        if not entries:
            return None
        return str(entries[0][0] or "").strip() or None

    def _handle_thumbnail_combo_changed(self, mode: str, combo: QtWidgets.QComboBox) -> None:
        if self._suspend_pin_display_changed:
            return
        value = str(combo.currentData() or "none").strip() or "none"
        if value == "create":
            previous = self._last_thumbnail_selection.get(mode, "none")
            self._set_combo_data(combo, previous)
            if str(combo.currentData() or "") == "create":
                with QtCore.QSignalBlocker(combo):
                    combo.setCurrentIndex(-1)
            QtCore.QTimer.singleShot(0, self._request_create_thumbnails)
            return
        self._last_thumbnail_selection[mode] = value
        if mode == "pins":
            self._pin_thumbnail_field = value
            self._pin_settings = DiscoveryPinDisplaySettings.from_payload(
                {
                    **self._pin_settings.to_payload(),
                    "thumbnail_variant_id": value,
                }
            )
            self._sync_pin_thumbnail_size_enabled()
        else:
            self._tooltip_thumbnail_field = value
            self._pin_settings = DiscoveryPinDisplaySettings.from_payload(
                {
                    **self._pin_settings.to_payload(),
                    "tooltip_thumbnail_variant_id": value,
                }
            )
            self._sync_tooltip_thumbnail_size_enabled()
        self._emit_pin_display_changed()

    def _request_create_thumbnails(self) -> None:
        if self._project is None:
            self._show_status("Load a project before creating thumbnails.")
            return
        if self._create_thumbnails_requested_callback is None:
            self._show_status("Open Datasets Area to create thumbnails.")
            return
        self._create_thumbnails_requested_callback()

    def set_create_thumbnails_requested_callback(
        self,
        callback: Callable[[], None] | None,
    ) -> None:
        self._create_thumbnails_requested_callback = callback

    def set_pin_display_changed_callback(self, callback) -> None:
        self._pin_display_changed_callback = callback

    def set_pin_style_requested_callback(self, callback: Callable[[], None] | None) -> None:
        self._pin_style_requested_callback = callback

    def wire_pin_style_panel(self, panel: QtWidgets.QWidget) -> None:
        """Bind the Appearance panel's moved pin-style controls to this pin state."""
        self._wire_pin_settings_controls(panel)
        self._apply_pin_style_state_to_panel(panel)

    def show_pin_style_settings(self) -> None:
        if callable(self._pin_style_requested_callback):
            self._pin_style_requested_callback()

    def set_tooltip_thumbnail_size_changed_callback(self, callback) -> None:
        self._tooltip_thumbnail_size_changed_callback = callback

    def set_tooltip_thumbnail_size_preview_hidden_callback(self, callback) -> None:
        self._tooltip_thumbnail_size_preview_hidden_callback = callback

    def set_marked_areas(
        self,
        rows: list[dict[str, object]],
        *,
        selected_area_id: str | None = None,
        all_areas_share: str = "0.0%",
    ) -> None:
        tree = self._tree()
        if tree is None:
            return
        self._marked_area_rows = [dict(row) for row in rows]
        tree.blockSignals(True)
        try:
            tree.clear()
            all_item = QtWidgets.QTreeWidgetItem(["all areas", str(all_areas_share), "", "", ""])
            all_item.setData(0, QtCore.Qt.ItemDataRole.UserRole, self._ALL_AREAS_ID)
            all_item.setFlags(
                (all_item.flags() | QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                & ~QtCore.Qt.ItemFlag.ItemIsEditable
                & ~QtCore.Qt.ItemFlag.ItemIsSelectable
            )
            all_item.setCheckState(2, self._aggregate_marked_area_check_state(rows, "points_visible"))
            all_item.setCheckState(3, self._aggregate_marked_area_check_state(rows, "area_visible"))
            all_item.setToolTip(2, "Show or hide point coloring for all areas")
            all_item.setToolTip(3, "Show or hide all polygons")
            background = tree.palette().color(QtGui.QPalette.ColorRole.AlternateBase)
            for column in range(tree.columnCount()):
                font = all_item.font(column)
                font.setBold(True)
                all_item.setFont(column, font)
                all_item.setBackground(column, background)
                all_item.setSizeHint(column, QtCore.QSize(0, 26))
            tree.addTopLevelItem(all_item)
            selected_item: QtWidgets.QTreeWidgetItem | None = None
            for row in rows:
                item = QtWidgets.QTreeWidgetItem(
                    [
                        str(row.get("label") or ""),
                        str(row.get("share") or ""),
                        "",
                        "",
                        "",
                    ]
                )
                item.setData(0, QtCore.Qt.ItemDataRole.UserRole, str(row.get("area_id") or ""))
                item.setData(4, QtCore.Qt.ItemDataRole.UserRole, str(row.get("color_hex") or "#3b82f6"))
                item.setToolTip(4, str(row.get("color_hex") or "#3b82f6"))
                item.setFlags(
                    item.flags()
                    | QtCore.Qt.ItemFlag.ItemIsEditable
                    | QtCore.Qt.ItemFlag.ItemIsUserCheckable
                    | QtCore.Qt.ItemFlag.ItemIsSelectable
                    | QtCore.Qt.ItemFlag.ItemIsEnabled
                )
                item.setCheckState(2, QtCore.Qt.CheckState.Checked if bool(row.get("points_visible", True)) else QtCore.Qt.CheckState.Unchecked)
                item.setCheckState(3, QtCore.Qt.CheckState.Checked if bool(row.get("area_visible", True)) else QtCore.Qt.CheckState.Unchecked)
                item.setTextAlignment(1, int(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter))
                tree.addTopLevelItem(item)
                if selected_area_id and str(row.get("area_id") or "") == str(selected_area_id):
                    selected_item = item
            if selected_item is not None:
                tree.setCurrentItem(selected_item)
            elif tree.topLevelItemCount() > 1:
                tree.setCurrentItem(tree.topLevelItem(1))
        finally:
            tree.blockSignals(False)
        self._sync_marked_area_opacity_enabled()

    @staticmethod
    def _aggregate_marked_area_check_state(
        rows: list[dict[str, object]],
        key: str,
    ) -> QtCore.Qt.CheckState:
        enabled_count = sum(1 for row in rows if bool(row.get(key, True)))
        if rows and enabled_count == len(rows):
            return QtCore.Qt.CheckState.Checked
        if enabled_count:
            return QtCore.Qt.CheckState.PartiallyChecked
        return QtCore.Qt.CheckState.Unchecked

    def set_selection_changed_callback(self, callback) -> None:
        self._selection_changed_callback = callback

    def set_delete_area_callback(self, callback) -> None:
        self._delete_area_callback = callback

    def set_area_visibility_callback(self, callback) -> None:
        self._area_toggle_callback = callback

    def set_points_visibility_callback(self, callback) -> None:
        self._points_toggle_callback = callback

    def set_all_area_visibility_callback(self, callback) -> None:
        self._all_area_toggle_callback = callback

    def set_all_points_visibility_callback(self, callback) -> None:
        self._all_points_toggle_callback = callback

    def set_rename_callback(self, callback) -> None:
        self._rename_callback = callback

    def set_color_changed_callback(self, callback) -> None:
        self._color_changed_callback = callback

    def set_export_area_requested_callback(self, callback) -> None:
        self._export_area_requested_callback = callback

    def set_write_areas_to_metadata_requested_callback(self, callback) -> None:
        self._write_areas_to_metadata_requested_callback = callback

    def _tree(self) -> QtWidgets.QTreeWidget | None:
        return self._marked_areas_tree

    def _on_tree_selection_changed(self) -> None:
        tree = self._tree()
        if tree is None:
            return
        item = tree.currentItem()
        if item is None:
            return
        area_id = str(item.data(0, QtCore.Qt.ItemDataRole.UserRole) or "")
        if not area_id or area_id == self._ALL_AREAS_ID:
            return
        if callable(self._selection_changed_callback):
            self._selection_changed_callback(area_id)

    def _on_tree_item_changed(self, item: QtWidgets.QTreeWidgetItem, column: int) -> None:
        area_id = str(item.data(0, QtCore.Qt.ItemDataRole.UserRole) or "")
        if not area_id:
            return
        if area_id == self._ALL_AREAS_ID:
            checked = item.checkState(column) == QtCore.Qt.CheckState.Checked
            if column == 2:
                self._set_all_marked_area_child_states(column, checked)
                if callable(self._all_points_toggle_callback):
                    self._all_points_toggle_callback(checked)
            elif column == 3:
                self._set_all_marked_area_child_states(column, checked)
                self._sync_marked_area_opacity_enabled()
                if callable(self._all_area_toggle_callback):
                    self._all_area_toggle_callback(checked)
            return
        if column == 0 and callable(self._rename_callback):
            self._rename_callback(area_id, item.text(0).strip())
            return
        if column == 2 and callable(self._points_toggle_callback):
            self._sync_all_areas_check_state(column)
            self._points_toggle_callback(area_id, item.checkState(2) == QtCore.Qt.CheckState.Checked)
            return
        if column == 3:
            self._sync_all_areas_check_state(column)
            self._sync_marked_area_opacity_enabled()
            if callable(self._area_toggle_callback):
                self._area_toggle_callback(area_id, item.checkState(3) == QtCore.Qt.CheckState.Checked)

    def _set_all_marked_area_child_states(self, column: int, checked: bool) -> None:
        tree = self._tree()
        if tree is None:
            return
        state = QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked
        tree.blockSignals(True)
        try:
            for row in range(1, tree.topLevelItemCount()):
                tree.topLevelItem(row).setCheckState(column, state)
        finally:
            tree.blockSignals(False)

    def _sync_all_areas_check_state(self, column: int) -> None:
        tree = self._tree()
        if tree is None or tree.topLevelItemCount() == 0:
            return
        states = [tree.topLevelItem(row).checkState(column) for row in range(1, tree.topLevelItemCount())]
        if states and all(state == QtCore.Qt.CheckState.Checked for state in states):
            aggregate = QtCore.Qt.CheckState.Checked
        elif any(state == QtCore.Qt.CheckState.Checked for state in states):
            aggregate = QtCore.Qt.CheckState.PartiallyChecked
        else:
            aggregate = QtCore.Qt.CheckState.Unchecked
        tree.blockSignals(True)
        try:
            tree.topLevelItem(0).setCheckState(column, aggregate)
        finally:
            tree.blockSignals(False)

    def _sync_marked_area_opacity_enabled(self) -> None:
        tree = self._tree()
        if tree is None:
            return
        has_visible_area = any(
            tree.topLevelItem(row).checkState(3) != QtCore.Qt.CheckState.Unchecked
            for row in range(1, tree.topLevelItemCount())
        )
        self._marked_area_opacity_spin.setEnabled(has_visible_area)

    def _on_tree_item_clicked(self, item: QtWidgets.QTreeWidgetItem, column: int) -> None:
        if column != 4:
            return
        area_id = str(item.data(0, QtCore.Qt.ItemDataRole.UserRole) or "")
        if not area_id or area_id == self._ALL_AREAS_ID:
            return
        current_hex = str(item.data(4, QtCore.Qt.ItemDataRole.UserRole) or "#3b82f6")
        current = QtGui.QColor(current_hex)
        if not current.isValid():
            current = QtGui.QColor("#3b82f6")
        tree = self._tree()
        parent = tree.window() if tree is not None else self._widget.window()
        color = QtWidgets.QColorDialog.getColor(current, parent, "Select area color")
        if not color.isValid():
            return
        color_hex = color.name(QtGui.QColor.NameFormat.HexRgb).lower()
        item.setData(4, QtCore.Qt.ItemDataRole.UserRole, color_hex)
        item.setToolTip(4, color_hex)
        tree = self._tree()
        if tree is not None:
            tree.viewport().update()
        if callable(self._color_changed_callback):
            self._color_changed_callback(area_id, color_hex)

    def _apply_palette_to_marked_areas(self, palette: list[str]) -> None:
        tree = self._tree()
        if tree is None or not palette:
            return
        changes: list[tuple[str, str]] = []
        for row in range(tree.topLevelItemCount()):
            item = tree.topLevelItem(row)
            if item is None:
                continue
            area_id = str(item.data(0, QtCore.Qt.ItemDataRole.UserRole) or "")
            if not area_id or area_id == self._ALL_AREAS_ID:
                continue
            color_hex = str(palette[len(changes) % len(palette)]).lower()
            item.setData(4, QtCore.Qt.ItemDataRole.UserRole, color_hex)
            item.setToolTip(4, color_hex)
            changes.append((area_id, color_hex))
        tree.viewport().update()
        if callable(self._color_changed_callback):
            for area_id, color_hex in changes:
                self._color_changed_callback(area_id, color_hex)

    def _on_tree_context_menu(self, pos: QtCore.QPoint) -> None:
        tree = self._tree()
        if tree is None:
            return
        item = tree.itemAt(pos)
        if item is None:
            return
        area_id = str(item.data(0, QtCore.Qt.ItemDataRole.UserRole) or "")
        if not area_id:
            return
        menu = QtWidgets.QMenu(tree)
        if area_id == self._ALL_AREAS_ID:
            return
        action_export = menu.addAction("Export Area…")
        menu.addSeparator()
        action_delete = menu.addAction("Remove Area…")
        chosen = menu.exec(tree.mapToGlobal(pos))
        if chosen == action_export:
            self._request_export_area(area_id)
        elif chosen == action_delete and callable(self._delete_area_callback):
            self._delete_area_callback(area_id)

    def _request_export_area(self, area_id: str) -> None:
        if area_id and callable(self._export_area_requested_callback):
            self._export_area_requested_callback(area_id)

    def _request_write_areas_to_metadata(self) -> None:
        if self._marked_area_rows and callable(self._write_areas_to_metadata_requested_callback):
            self._write_areas_to_metadata_requested_callback()
