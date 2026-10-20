from __future__ import annotations

import math
from collections.abc import Callable

from PySide6 import QtCore, QtWidgets

from DiscoveryArea.canvas.viewport import VIEWPORT_ASPECT_PRESETS
from DiscoveryArea.controllers.transform_element_controllers import (
    TransformGridElementController,
    TransformOverlapElementController,
)
from DiscoveryArea.transforms import TransformSettings
from shared.qt.item_views import configure_panel_item_view, minimum_panel_row_size
from shared.qt.icons import apply_icon_color
from shared.qt.panel_controller import UiPanelController
from shared.qt.widgets import require_child


class TransformPanelController(UiPanelController):
    package = "DiscoveryArea"
    ui_path = "ui/transform_panel.ui"
    SPACING_DISPLAY_SCALE = 10.0

    def _wire(self) -> None:
        self._list = require_child(
            self._widget, QtWidgets.QListWidget, "list_transform_elements"
        )
        configure_panel_item_view(self._list)
        self._grid_element = TransformGridElementController(self._list)
        self._overlap_element = TransformOverlapElementController(self._list)
        self._add_element(self._grid_element.widget)
        self._add_element(self._overlap_element.widget)
        self._fit_list_to_elements()

        self._grid_spin = self._grid_element.percentage
        self._grid_format = self._grid_element.format
        self._grid_format.clear()
        for preset in VIEWPORT_ASPECT_PRESETS:
            self._grid_format.addItem(preset.label, preset.key)
        self._spacing_spin = self._overlap_element.minimum_spacing
        spacing_tooltip = (
            "Minimum center-to-center spacing. A value of 0.10 corresponds to 0.01 "
            "normalized projection units. Independent of zoom, point size, and "
            "thumbnail size. Zero disables spreading."
        )
        self._spacing_spin.setRange(0.0, 1.0)
        self._spacing_spin.setDecimals(3)
        self._spacing_spin.setSingleStep(0.05)
        self._spacing_spin.setToolTip(spacing_tooltip)
        self._grid_button = self._grid_element.apply_button
        self._overlap_button = self._overlap_element.apply_button
        self._overlap_progress = self._overlap_element.progress
        self._overlap_progress.setRange(0, 100)
        self._overlap_progress.setValue(0)
        self._overlap_progress.show()
        self._options_button = require_child(
            self._widget, QtWidgets.QToolButton, "button_transform_options"
        )
        self._apply_callback: Callable[[TransformSettings], None] | None = None
        self._cancel_callback: Callable[[], None] | None = None
        self._save_preset_callback: Callable[[str, dict], bool] | None = None
        self._load_preset_callback: Callable[[str], dict | None] | None = None
        self._applied = TransformSettings()
        self._busy = False
        self._overlap_progress_active = False
        self._pending_apply_kind: str | None = None
        self._has_projection = False
        self._transformable = True
        self._grid_spin.valueChanged.connect(self._settings_edited)
        self._grid_format.currentIndexChanged.connect(self._settings_edited)
        self._spacing_spin.valueChanged.connect(self._settings_edited)
        self._grid_button.clicked.connect(
            lambda checked=False: self._apply_clicked("grid", checked=checked)
        )
        self._overlap_button.clicked.connect(
            lambda checked=False: self._apply_clicked("overlap", checked=checked)
        )
        self._configure_options_menu()
        self._sync()

    def _add_element(self, widget: QtWidgets.QWidget) -> None:
        item = QtWidgets.QListWidgetItem()
        item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
        size = widget.sizeHint().expandedTo(widget.minimumSizeHint())
        item.setSizeHint(minimum_panel_row_size(size))
        self._list.addItem(item)
        self._list.setItemWidget(item, widget)

    def _fit_list_to_elements(self) -> None:
        content_height = sum(
            self._list.item(row).sizeHint().height()
            for row in range(self._list.count())
        )
        frame_height = self._list.frameWidth() * 2
        self._list.setFixedHeight(content_height + frame_height)

    def set_apply_callback(self, callback: Callable[[TransformSettings], None] | None) -> None:
        self._apply_callback = callback

    def set_cancel_callback(self, callback: Callable[[], None] | None) -> None:
        self._cancel_callback = callback

    def set_save_preset_callback(self, callback: Callable[[str, dict], bool] | None) -> None:
        self._save_preset_callback = callback

    def set_load_preset_callback(self, callback: Callable[[str], dict | None] | None) -> None:
        self._load_preset_callback = callback

    def set_recent_preset_labels(self, labels: list[str]) -> None:
        self._load_recent_menu.clear()
        if not labels:
            action = self._load_recent_menu.addAction("No Presets Saved")
            action.setEnabled(False)
            return
        for label in labels[:10]:
            action = self._load_recent_menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, value=label: self._load_preset_label(value)
            )

    def applied_preset_payload(self, name: str = "") -> dict[str, object]:
        return {
            "name": str(name).strip(),
            "preset_version": 2,
            "transform": self._applied.to_preset_dict(),
        }

    def applied_settings(self) -> TransformSettings:
        return self._applied

    def settings(self) -> TransformSettings:
        return TransformSettings(
            snap_percent=self._grid_spin.value(),
            grid_aspect_preset=str(self._grid_format.currentData() or "free"),
            minimum_spacing=self._minimum_spacing_from_display(
                self._spacing_spin.value()
            ),
        ).normalized()

    def set_projection(
        self,
        settings: TransformSettings | None,
        *,
        populated: bool,
        transformable: bool = True,
    ) -> None:
        self._busy = False
        self._overlap_progress_active = False
        self._pending_apply_kind = None
        self._has_projection = bool(populated)
        self._transformable = bool(transformable)
        self._widget.setEnabled(self._transformable)
        self._widget.setToolTip(
            ""
            if self._transformable
            else "Transformations are unavailable for coordinate-truthful projections."
        )
        self._applied = self._visible_settings(settings or TransformSettings())
        with QtCore.QSignalBlocker(self._grid_format):
            index = self._grid_format.findData(self._applied.grid_aspect_preset)
            self._grid_format.setCurrentIndex(max(0, index))
        self._overlap_progress.setValue(
            100 if self._applied.minimum_spacing > 0.0 else 0
        )
        self._sync()

    def set_processing(self, *, calculate_overlap: bool = False) -> None:
        if not self._transformable:
            return
        self._busy = True
        self._overlap_progress_active = bool(calculate_overlap)
        if calculate_overlap:
            self._overlap_progress.setValue(0)
        self._sync()

    def set_calculation_progress(self, value: int) -> None:
        if self._busy and self._overlap_progress_active:
            self._overlap_progress.setValue(max(0, min(100, int(value))))

    def set_applied(self, settings: TransformSettings) -> None:
        completed_overlap = self._pending_apply_kind in {"overlap", "preset"}
        self._busy = False
        self._overlap_progress_active = False
        self._pending_apply_kind = None
        self._applied = self._visible_settings(settings)
        if completed_overlap:
            self._overlap_progress.setValue(
                100 if self._applied.minimum_spacing > 0.0 else 0
            )
        self._sync()

    def set_failure(self, message: str) -> None:
        self._busy = False
        self._overlap_progress_active = False
        self._pending_apply_kind = None
        self._overlap_progress.setValue(0)
        self._show_status(str(message))
        self._sync_controls()

    def set_project(self, project) -> None:
        super().set_project(project)
        if project is None:
            self.set_projection(None, populated=False, transformable=False)

    def _settings_edited(self, *_args) -> None:
        self._sync()

    def _apply_clicked(self, kind: str, *, checked: bool) -> None:
        button = self._grid_button if kind == "grid" else self._overlap_button
        self._update_apply_button_icon(button)
        if self._busy:
            if (
                self._pending_apply_kind == "preset" or kind == self._pending_apply_kind
            ) and self._cancel_callback is not None:
                self._cancel_callback()
            return
        if kind == "overlap" and not checked:
            self._overlap_progress.setValue(0)
        if kind == "grid" and not checked:
            with QtCore.QSignalBlocker(self._overlap_button):
                self._overlap_button.setChecked(False)
            self._update_apply_button_icon(self._overlap_button)
            self._overlap_progress.setValue(0)
        self._pending_apply_kind = kind
        if self._apply_callback is not None:
            self._apply_callback(self._settings_for_apply(kind, enabled=checked))

    def _settings_for_apply(self, kind: str, *, enabled: bool = True) -> TransformSettings:
        staged = self.settings()
        if kind == "grid":
            return TransformSettings(
                snap_percent=staged.snap_percent if enabled else 0,
                grid_aspect_preset=staged.grid_aspect_preset,
                minimum_spacing=self._applied.minimum_spacing if enabled else 0.0,
            ).normalized()
        return TransformSettings(
            snap_percent=self._applied.snap_percent,
            grid_aspect_preset=self._applied.grid_aspect_preset,
            minimum_spacing=staged.minimum_spacing if enabled else 0.0,
        ).normalized()

    def _show_methods_report(self) -> None:
        from DiscoveryArea.settings_provenance import settings_report
        from shared.qt.methods_citations_dialog import MethodsCitationsDialogController
        MethodsCitationsDialogController().exec(settings_report("Applied Transforms", transform=self.applied_settings().to_dict()))

    def _configure_options_menu(self) -> None:
        menu = QtWidgets.QMenu(self._widget)
        self._options_menu = menu
        self._load_recent_menu = menu.addMenu("Load Recent")
        self._save_preset_action = menu.addAction("Save Preset…")
        self._save_preset_action.triggered.connect(self._save_current_preset)
        menu.addAction("Citations…").triggered.connect(self._show_methods_report)
        self._options_button.setMenu(menu)
        self._options_button.setPopupMode(
            QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self._options_button.setStyleSheet(
            "QToolButton::menu-indicator { image: none; width: 0px; }"
        )
        self.set_recent_preset_labels([])

    def _save_current_preset(self) -> None:
        if self._save_preset_callback is None or not self._has_projection or self._busy:
            return
        label, ok = QtWidgets.QInputDialog.getText(
            self._widget, "Save Transform Preset", "Preset name:"
        )
        name = str(label).strip()
        if not ok or not name:
            return
        if self._save_preset_callback(name, self.applied_preset_payload(name)):
            self._show_status(f"Saved transform preset: {name}")

    def _load_preset_label(self, label: str) -> None:
        if self._load_preset_callback is None or not self._has_projection or self._busy:
            return
        payload = self._load_preset_callback(label)
        if not isinstance(payload, dict):
            self._show_status(f"Could not load transform preset: {label}")
            return
        try:
            preset_version = int(payload.get("preset_version", 1))
        except (TypeError, ValueError):
            preset_version = -1
        if preset_version not in {1, 2}:
            self._show_status(f"Unsupported transform preset: {label}")
            return
        recipe = payload.get("transform")
        try:
            settings = TransformSettings.from_preset_dict(recipe)
        except (TypeError, ValueError, OverflowError):
            self._show_status(f"Invalid transform preset: {label}")
            return
        settings = TransformSettings(
            snap_percent=settings.snap_percent,
            grid_aspect_preset=settings.grid_aspect_preset,
            minimum_spacing=settings.minimum_spacing,
        ).normalized()
        with (
            QtCore.QSignalBlocker(self._grid_spin),
            QtCore.QSignalBlocker(self._grid_format),
            QtCore.QSignalBlocker(self._spacing_spin),
        ):
            self._grid_spin.setValue(settings.snap_percent)
            index = self._grid_format.findData(settings.grid_aspect_preset)
            self._grid_format.setCurrentIndex(max(0, index))
            self._spacing_spin.setValue(
                self._minimum_spacing_to_display(settings.minimum_spacing)
            )
        settings = TransformSettings(
            snap_percent=settings.snap_percent,
            grid_aspect_preset=settings.grid_aspect_preset,
            minimum_spacing=self._minimum_spacing_from_display(
                self._spacing_spin.value()
            ),
        ).normalized()
        self._pending_apply_kind = "preset"
        self._sync()
        self._show_status(f"Applying transform preset: {label}")
        if self._apply_callback is not None:
            self._apply_callback(settings)

    def _sync(self) -> None:
        self._sync_controls()

    @classmethod
    def _minimum_spacing_from_display(cls, value: float) -> float:
        return float(value) / cls.SPACING_DISPLAY_SCALE

    @classmethod
    def _minimum_spacing_to_display(cls, value: float) -> float:
        return float(value) * cls.SPACING_DISPLAY_SCALE

    def _sync_controls(self) -> None:
        enabled = self._has_projection and self._transformable and not self._busy
        self._grid_spin.setEnabled(enabled)
        self._grid_format.setEnabled(enabled)
        self._spacing_spin.setEnabled(enabled)
        staged = self.settings()
        grid_changed = (
            staged.snap_percent != self._applied.snap_percent
            or (
                staged.snap_percent > 0
                and staged.grid_aspect_preset != self._applied.grid_aspect_preset
            )
        )
        overlap_changed = not math.isclose(
            staged.minimum_spacing,
            self._applied.minimum_spacing,
            abs_tol=0.000_001,
        )
        if overlap_changed and not self._busy:
            self._overlap_progress.setValue(0)
        if not self._busy:
            with (
                QtCore.QSignalBlocker(self._grid_button),
                QtCore.QSignalBlocker(self._overlap_button),
            ):
                self._grid_button.setChecked(
                    self._applied.snap_percent > 0 and not grid_changed
                )
                self._overlap_button.setChecked(
                    self._applied.minimum_spacing > 0.0 and not overlap_changed
                )
        self._update_apply_button_icon(self._grid_button)
        self._update_apply_button_icon(self._overlap_button)
        self._grid_button.setEnabled(
            self._has_projection
            and self._transformable
            and (
                self._busy and self._pending_apply_kind in {"grid", "preset"}
                or not self._busy
            )
        )
        self._overlap_button.setEnabled(
            self._has_projection
            and self._transformable
            and (
                self._busy and self._pending_apply_kind in {"overlap", "preset"}
                or not self._busy
            )
        )
        self._save_preset_action.setEnabled(enabled)
        self._options_button.setEnabled(enabled)

    @staticmethod
    def _update_apply_button_icon(button: QtWidgets.QPushButton) -> None:
        button.setText("")
        button.setProperty(
            "iconName", "animation_pause" if button.isChecked() else "animation_play"
        )
        apply_icon_color(button)

    @staticmethod
    def _visible_settings(settings: TransformSettings) -> TransformSettings:
        value = settings.normalized()
        return TransformSettings(
            snap_percent=value.snap_percent,
            grid_aspect_preset=value.grid_aspect_preset,
            minimum_spacing=value.minimum_spacing,
        ).normalized()
