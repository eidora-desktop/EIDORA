from __future__ import annotations

from collections.abc import Callable

from PySide6 import QtCore, QtGui, QtWidgets

from DiscoveryArea.metadata_access import DiscoveryMetadataAccess
from DiscoveryArea.controllers.metadata_options import MetadataOptions
from shared.qt.icons import apply_icon_color
from shared.qt.panel_controller import UiPanelController
from shared.qt.preset_menu import append_manage_presets_action
from shared.qt.widgets import require_child


class _SlicerInteractionFilter(QtCore.QObject):
    def __init__(self, parent, activate) -> None:
        super().__init__(parent)
        self._activate = activate

    def eventFilter(self, watched, event) -> bool:
        event_type = event.type()
        using_control = (
            event_type == QtCore.QEvent.Type.MouseButtonPress
            and event.button() == QtCore.Qt.MouseButton.LeftButton
        ) or event_type == QtCore.QEvent.Type.Wheel
        if event_type == QtCore.QEvent.Type.KeyPress:
            using_control = event.key() in (
                QtCore.Qt.Key.Key_Left, QtCore.Qt.Key.Key_Right,
                QtCore.Qt.Key.Key_Up, QtCore.Qt.Key.Key_Down,
                QtCore.Qt.Key.Key_PageUp, QtCore.Qt.Key.Key_PageDown,
                QtCore.Qt.Key.Key_Home, QtCore.Qt.Key.Key_End,
                QtCore.Qt.Key.Key_Space, QtCore.Qt.Key.Key_Return,
                QtCore.Qt.Key.Key_Enter, QtCore.Qt.Key.Key_Backspace,
                QtCore.Qt.Key.Key_Delete,
            ) or bool(event.text().strip())
        if using_control and watched.isEnabled():
            self._activate()
        return super().eventFilter(watched, event)


class SlicerPanelController(UiPanelController):
    package = "DiscoveryArea"
    ui_path = "ui/slicer_panel.ui"
    _navigation_speed_options = (100, 150, 200, 250, 300, 400, 500, 750, 1000, 2000)

    def _wire(self) -> None:
        self._metadata_options = MetadataOptions(self._widget, self._show_status)
        self._pending_state = None
        self._values_loading = False
        self._column_combo = require_child(self._widget, QtWidgets.QComboBox, "combo_slicer_column")
        self._scrollbar = require_child(self._widget, QtWidgets.QScrollBar, "scrollbar_slicer")
        self._backward_button = require_child(self._widget, QtWidgets.QToolButton, "button_backward")
        self._forward_button = require_child(self._widget, QtWidgets.QToolButton, "button_forward")
        self._apply_button = require_child(self._widget, QtWidgets.QPushButton, "button_apply_element")
        self._menu_button = require_child(self._widget, QtWidgets.QToolButton, "button_slicer_options")
        self._values_label = require_child(self._widget, QtWidgets.QLabel, "values_label")
        self._start_spin = require_child(self._widget, QtWidgets.QSpinBox, "spin_slicer_start")
        self._spread_spin = require_child(self._widget, QtWidgets.QSpinBox, "spin_slicer_spread")
        self._step_spin = require_child(self._widget, QtWidgets.QSpinBox, "spin_slicer_steps")
        self._spread_spin.setMinimum(1)
        self._step_spin.setMinimum(1)
        if self._step_spin.value() < 1:
            self._step_spin.setValue(1)
        self._add_field_indicator()
        self._start_spin.setRange(0, 0)
        self._start_spin.setSpecialValueText("")
        self._start_spin.setEnabled(False)
        self._scrollbar.setRange(0, 0)
        self._scrollbar.setEnabled(False)
        self._apply_button.setCheckable(True)
        self._apply_button.setChecked(False)
        self._apply_button.setEnabled(False)
        self._update_apply_button_icon()
        self._save_preset_callback: Callable[[str, dict], bool] | None = None
        self._load_preset_callback: Callable[[str], dict | None] | None = None
        self._create_animation_callback = None
        self._slicer_changed_callback: Callable[[], None] | None = None
        self._menu_load_recent: QtWidgets.QMenu | None = None
        self._distinct_values_by_column: dict[str, list[str]] = {}
        self._metadata_access: DiscoveryMetadataAccess | None = None
        self._column_values: list[str] = []
        self._numeric_values: list[int] | None = None
        self._start_index = 0
        self._active_values: list[str] = []
        self._slicer_enabled = False
        self._applying_state = False
        self._column_combo.currentIndexChanged.connect(self._on_column_changed)
        self._spread_spin.valueChanged.connect(self._on_spread_changed)
        self._step_spin.valueChanged.connect(self._on_step_changed)
        self._start_spin.valueChanged.connect(self._on_start_changed)
        self._scrollbar.valueChanged.connect(self._on_scrollbar_value_changed)
        self._navigation_repeat_timer = QtCore.QTimer(self._widget)
        self._navigation_repeat_timer.setInterval(300)
        self._navigation_repeat_timer.timeout.connect(self._repeat_navigation_move)
        self._navigation_repeat_interval_ms = 300
        self._navigation_direction = 0
        self._navigation_repeated = False
        self._backward_button.pressed.connect(lambda: self._start_navigation_repeat(-1))
        self._forward_button.pressed.connect(lambda: self._start_navigation_repeat(1))
        self._backward_button.released.connect(self._stop_navigation_repeat)
        self._forward_button.released.connect(self._stop_navigation_repeat)
        self._apply_button.clicked.connect(self._on_apply_button_clicked)
        self._configure_menu()
        self._interaction_filter = _SlicerInteractionFilter(self._widget, self._activate_from_interaction)
        for control in (self._scrollbar, self._backward_button, self._forward_button,
                        self._start_spin, self._spread_spin, self._step_spin):
            control.installEventFilter(self._interaction_filter)
            if isinstance(control, QtWidgets.QSpinBox):
                control.lineEdit().installEventFilter(self._interaction_filter)

    def set_project(self, project) -> None:
        self._metadata_options.reset()
        self._pending_state = None
        self._distinct_values_by_column = {}
        super().set_project(project)
        self._populate_columns(project)
        self._on_column_changed(self._column_combo.currentIndex())

    def _populate_columns(self, project) -> None:
        previous_column = self._current_column()
        with QtCore.QSignalBlocker(self._column_combo):
            self._column_combo.clear()
            self._add_field_indicator()
            if project is not None:
                access = self._metadata_access or DiscoveryMetadataAccess(project)
                self._metadata_access = access
                try:
                    columns = access.columns()
                except Exception:
                    columns = []
                for column in columns:
                    if column.name != "id":
                        self._column_combo.addItem(
                            getattr(column, "display_name", None) or column.name, column.name,
                        )
            self._column_combo.setEnabled(project is not None)
            index = self._column_combo.findData(previous_column) if previous_column else 0
            self._column_combo.setCurrentIndex(index if index >= 0 else 0)

    def refresh_metadata_groups(self, metadata_access: DiscoveryMetadataAccess) -> None:
        """Update group choices without reloading an unchanged physical slice."""
        state = self.slicer_state_payload()
        pending = self._values_loading
        self.set_metadata_access(metadata_access)
        self._distinct_values_by_column = {
            name: values for name, values in self._distinct_values_by_column.items()
            if not name.startswith(("group:", "group-set:"))
        }
        self._populate_columns(self._project)
        column = str(state.get("column") or "")
        if pending or column.startswith(("group:", "group-set:")) or column != self._current_column():
            self.apply_slicer_state_payload(state)

    def set_metadata_access(
        self,
        metadata_access: DiscoveryMetadataAccess | None,
    ) -> None:
        self._metadata_access = metadata_access
        self._metadata_options.reset()

    def shutdown(self) -> None:
        self._metadata_options.shutdown()
        self._navigation_repeat_timer.stop()

    def _values_for_column(self, column, ready):
        if column in self._distinct_values_by_column or not column or self._metadata_access is None:
            self._metadata_options.cancel(self._column_combo)
            return list(self._distinct_values_by_column.get(column, []))
        access = self._metadata_access
        return self._metadata_options.get(self._column_combo, (id(access), column),
            lambda: SlicerPanelController._sorted_slicer_values(set(access.distinct_values(column))), ready)

    def _on_column_changed(self, index: int) -> None:
        self._pending_state = None
        values = self._values_for_column(self._current_column(),
            lambda: self._on_column_changed(self._column_combo.currentIndex()))
        if values is None:
            self._pending_state = self.slicer_state_payload()
            self._pending_state.update(enabled=index > 0, position=0)
            self._pending_state.pop('start', None)
            self._set_values_loading(True)
            self._show_status("Loading slicer values…")
            return
        self._set_values_loading(False)
        self._column_values = values
        self._configure_start_spin(reset=True)
        self._slicer_enabled = index > 0
        self._apply_button.setChecked(self._slicer_enabled)
        self._configure_scrollbar(len(self._column_values))
        self._update_active_range()
        self._update_enabled_state()
        self._emit_slicer_changed()

    def _set_values_loading(self, loading: bool) -> None:
        self._values_loading = loading
        if loading:
            self._navigation_repeat_timer.stop()
        for control in (self._start_spin, self._spread_spin, self._step_spin,
                        self._scrollbar, self._backward_button, self._forward_button,
                        self._apply_button):
            control.setEnabled(not loading)

    def _on_spread_changed(self, value: int) -> None:
        if value < 1:
            self._set_spin_value(self._spread_spin, 1)
        self._configure_scrollbar(len(self._column_values))
        self._update_active_range()
        self._emit_slicer_changed()

    def _on_step_changed(self, value: int) -> None:
        if value < 1:
            self._set_spin_value(self._step_spin, 1)
        self._configure_scrollbar(len(self._column_values))
        self._update_active_range()
        self._emit_slicer_changed()

    def _on_start_changed(self, _value: int) -> None:
        self._start_index = self._start_index_for_value(self._start_spin.value())
        self._configure_scrollbar(len(self._column_values), reset_position=True)
        self._update_active_range()
        self._emit_slicer_changed()

    def _on_scrollbar_value_changed(self, _value: int) -> None:
        maximum = self._scrollbar.maximum()
        snapped = self._snap_start(self._scrollbar.value(), maximum, self._current_step())
        if snapped != self._scrollbar.value():
            self._scrollbar.blockSignals(True)
            self._scrollbar.setValue(snapped)
            self._scrollbar.blockSignals(False)
        self._update_active_range()
        self._emit_slicer_changed()

    def _activate_from_interaction(self) -> None:
        if (self._slicer_enabled or self._applying_state or self._values_loading
                or not self._current_column() or not self._column_values):
            return
        self._apply_button.setChecked(True)
        self._on_apply_button_clicked()

    def _start_navigation_repeat(self, direction: int) -> None:
        self._activate_from_interaction()
        self._navigation_direction = direction
        self._navigation_repeated = False
        self._navigation_repeat_timer.start()

    def _stop_navigation_repeat(self) -> None:
        self._navigation_repeat_timer.stop()
        if not self._navigation_repeated:
            self._move_scrollbar(self._navigation_direction)
        self._navigation_direction = 0

    def _repeat_navigation_move(self) -> None:
        self._navigation_repeated = True
        self._move_scrollbar(self._navigation_direction)

    def _move_scrollbar(self, direction: int) -> None:
        action = (
            QtWidgets.QAbstractSlider.SliderSingleStepAdd
            if direction > 0
            else QtWidgets.QAbstractSlider.SliderSingleStepSub
        )
        self._scrollbar.triggerAction(action)

    def _on_apply_button_clicked(self) -> None:
        if not self._current_column():
            self._apply_button.setChecked(False)
            return
        self._slicer_enabled = bool(self._apply_button.isChecked())
        self._update_enabled_state()
        self._update_active_range()
        self._emit_slicer_changed()

    def _configure_scrollbar(self, values_count: int, *, reset_position: bool = False) -> None:
        spread = self._current_spread()
        step = self._current_step()
        if self._numeric_values is not None:
            maximum = max(0, self._numeric_values[-1] - self._start_spin.value() - spread)
        else:
            maximum = max(0, int(values_count) - self._start_index - spread)
        self._scrollbar.blockSignals(True)
        self._scrollbar.setMinimum(0)
        self._scrollbar.setMaximum(maximum if values_count > 0 else 0)
        self._scrollbar.setPageStep(spread)
        self._scrollbar.setSingleStep(step)
        position = 0 if reset_position else self._scrollbar.value()
        self._scrollbar.setValue(self._snap_start(position, maximum, step))
        self._scrollbar.blockSignals(False)
        self._update_enabled_state()

    def _update_active_range(self) -> None:
        if not self._column_values:
            self._active_values = []
            self._update_range_display(None, None)
            return
        spread = self._current_spread()
        if self._numeric_values is not None:
            range_start = self._numeric_start()
            range_end = range_start + spread
            self._active_values = [
                value
                for value, numeric_value in zip(self._column_values, self._numeric_values, strict=True)
                if range_start <= numeric_value <= range_end
            ]
            self._update_range_display(str(range_start), str(range_end))
            return

        start = min(self._start_index + self._scrollbar.value(), len(self._column_values) - 1)
        end = min(start + spread, len(self._column_values))
        self._active_values = self._column_values[start:end]
        if not self._active_values:
            self._update_range_display(None, None)
            return
        self._update_range_display(self._active_values[0], self._active_values[-1])

    def _update_enabled_state(self) -> None:
        has_column = bool(self._current_column())
        has_values = bool(self._column_values)
        self._scrollbar.setEnabled(has_column and has_values and not self._values_loading)
        self._start_spin.setEnabled(has_column and has_values)
        navigation_enabled = self._scrollbar.isEnabled()
        self._backward_button.setEnabled(navigation_enabled)
        self._forward_button.setEnabled(navigation_enabled)
        self._apply_button.setEnabled(has_column)
        if self._apply_button.isChecked() != self._slicer_enabled:
            self._apply_button.blockSignals(True)
            self._apply_button.setChecked(self._slicer_enabled)
            self._apply_button.blockSignals(False)
        self._update_apply_button_icon()

    def _update_apply_button_icon(self) -> None:
        self._apply_button.setText("")
        self._apply_button.setProperty(
            "iconName",
            "animation_pause" if self._apply_button.isChecked() else "animation_play",
        )
        apply_icon_color(self._apply_button)

    def _update_range_display(self, start: str | None, end: str | None) -> None:
        if not self._slicer_enabled or start is None:
            self._values_label.setText("")
            return
        display_end = start if end is None else end
        self._values_label.setText(f"{start} - {display_end}")

    def _current_column(self) -> str:
        value = self._column_combo.currentData()
        return str(value).strip() if value is not None else ""

    def _add_field_indicator(self) -> None:
        self._column_combo.addItem("select field", None)
        indicator = self._column_combo.model().item(0)
        if indicator is None:
            return
        font = indicator.font()
        font.setItalic(True)
        indicator.setFont(font)
        indicator.setFlags(
            indicator.flags()
            & ~QtCore.Qt.ItemFlag.ItemIsEnabled
            & ~QtCore.Qt.ItemFlag.ItemIsSelectable
        )

    def _current_spread(self) -> int:
        return max(1, int(self._spread_spin.value()))

    def _current_step(self) -> int:
        return max(1, int(self._step_spin.value()))

    def _configure_start_spin(self, *, reset: bool) -> None:
        if not self._column_values:
            self._numeric_values = None
            self._start_index = 0
            self._start_spin.blockSignals(True)
            self._start_spin.setRange(0, 0)
            self._start_spin.setValue(0)
            self._start_spin.setSpecialValueText(" ")
            self._start_spin.blockSignals(False)
            return

        numeric_values = self._integer_values(self._column_values)
        self._numeric_values = numeric_values
        minimum = numeric_values[0] if numeric_values is not None else 0
        maximum = numeric_values[-1] if numeric_values is not None else len(self._column_values) - 1
        self._start_spin.blockSignals(True)
        self._start_spin.setRange(minimum, maximum)
        self._start_spin.setSingleStep(1)
        self._start_spin.setSpecialValueText(str(minimum))
        if reset:
            self._start_spin.setValue(minimum)
        self._start_spin.blockSignals(False)
        self._start_index = self._start_index_for_value(self._start_spin.value())

    def _start_index_for_value(self, value: int) -> int:
        if self._numeric_values is None:
            return min(max(int(value), 0), max(0, len(self._column_values) - 1))
        for index, numeric_value in enumerate(self._numeric_values):
            if numeric_value >= value:
                return index
        return max(0, len(self._numeric_values) - 1)

    def _numeric_start(self) -> int:
        return self._start_spin.value() + self._scrollbar.value()

    @staticmethod
    def _integer_values(values: list[str]) -> list[int] | None:
        numeric_values: list[int] = []
        for value in values:
            try:
                numeric_value = float(value)
            except ValueError:
                return None
            if not numeric_value.is_integer():
                return None
            numeric_values.append(int(numeric_value))
        return numeric_values

    @staticmethod
    def _set_spin_value(spin: QtWidgets.QSpinBox, value: int) -> None:
        spin.blockSignals(True)
        spin.setValue(value)
        spin.blockSignals(False)

    @staticmethod
    def _snap_start(value: int, maximum: int, step: int) -> int:
        step = max(1, int(step))
        maximum = max(0, int(maximum))
        clamped = min(max(int(value), 0), maximum)
        if clamped == maximum:
            return maximum
        return min(((clamped + step // 2) // step) * step, maximum)

    @staticmethod
    def _sorted_slicer_values(values: set[str]) -> list[str]:
        from DiscoveryArea.slicing import sorted_slicer_values
        return sorted_slicer_values(values)

    def _emit_slicer_changed(self) -> None:
        if self._applying_state:
            return
        if self._slicer_changed_callback is not None:
            self._slicer_changed_callback()

    def _configure_menu(self) -> None:
        menu = QtWidgets.QMenu(self._menu_button)
        animation_action = menu.addAction("Create Animation…")
        animation_action.triggered.connect(self._create_animation)
        menu.addSeparator()
        speed_menu = menu.addMenu("Set Speed")
        self._speed_actions: dict[int, QtGui.QAction] = {}
        speed_group = QtGui.QActionGroup(speed_menu)
        speed_group.setExclusive(True)
        for interval_ms in self._navigation_speed_options:
            action = speed_menu.addAction(f"{interval_ms}ms")
            action.setCheckable(True)
            action.triggered.connect(
                lambda checked=False, value=interval_ms: checked and self._set_navigation_repeat_interval(value)
            )
            speed_group.addAction(action)
            self._speed_actions[interval_ms] = action
        self._update_speed_menu_selection()
        menu.addSeparator()
        self._menu_load_recent = menu.addMenu("Apply Preset")
        save_action = menu.addAction("Save as Preset…")
        save_action.triggered.connect(self._save_current_preset)
        self._menu_button.setMenu(menu)
        self._menu_button.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        self._menu_button.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
        self.set_recent_preset_labels([])

    def set_create_animation_callback(self, callback) -> None:
        self._create_animation_callback = callback

    def _create_animation(self) -> None:
        if self._values_loading or not self._current_column() or not self._column_values:
            self._show_status("Select a Slicer field with values before creating an animation.")
            return
        if self._create_animation_callback is None:
            self._show_status("Animation creation is unavailable.")
            return
        payload = dict(self.slicer_state_payload())
        payload["enabled"] = True
        self._create_animation_callback(payload, list(self._column_values))

    def _set_navigation_repeat_interval(self, interval_ms: int) -> None:
        self._navigation_repeat_interval_ms = min(
            self._navigation_speed_options,
            key=lambda option: abs(option - int(interval_ms)),
        )
        self._navigation_repeat_timer.setInterval(self._navigation_repeat_interval_ms)
        self._update_speed_menu_selection()
        self._emit_slicer_changed()

    def _update_speed_menu_selection(self) -> None:
        action = self._speed_actions.get(self._navigation_repeat_interval_ms)
        if action is not None:
            action.setChecked(True)

    def slicer_state_payload(self) -> dict:
        if self._pending_state is not None:
            return self._pending_state
        return {
            "enabled": bool(self._slicer_enabled and self._current_column()),
            "column": self._current_column(),
            "spread": self._current_spread(),
            "step": self._current_step(),
            "start": int(self._start_spin.value()),
            "position": int(self._scrollbar.value()),
            "auto_roll_seconds": self._navigation_repeat_interval_ms / 1000,
        }

    def apply_slicer_state_payload(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        if self._metadata_access is not None:
            payload = self._metadata_access.canonical_field_payload(payload)
        slicer_payload = payload.get("slicer") if isinstance(payload.get("slicer"), dict) else payload
        column = str(slicer_payload.get("column") or "")
        self._pending_state = dict(slicer_payload)
        available = self._column_combo.findData(column) >= 0 or self._column_combo.findText(column) >= 0
        if available and self._values_for_column(column, lambda: self.apply_slicer_state_payload(payload)) is None:
            index = self._column_combo.findData(column)
            if index < 0:
                index = self._column_combo.findText(column)
            with QtCore.QSignalBlocker(self._column_combo):
                self._column_combo.setCurrentIndex(index)
            self._set_values_loading(True)
            return
        self._pending_state = None
        self._applying_state = True
        try:
            if "auto_roll_seconds" in slicer_payload:
                self._set_navigation_repeat_interval(
                    self._coerce_navigation_repeat_interval(slicer_payload.get("auto_roll_seconds"))
                )
            self._spread_spin.setValue(self._coerce_positive_int(slicer_payload.get("spread"), default=1))
            self._step_spin.setValue(self._coerce_positive_int(slicer_payload.get("step"), default=1))
            index = self._column_combo.findData(column)
            if index < 0:
                index = self._column_combo.findText(column)
            desired_enabled = bool(slicer_payload.get("enabled", bool(column and index >= 0))) and index >= 0
            self._column_combo.setCurrentIndex(index if index >= 0 else 0)
            if column and index < 0:
                self._show_status(f"Slicer column unavailable: {column}")
            self._on_column_changed(self._column_combo.currentIndex())
            self._slicer_enabled = desired_enabled
            self._apply_button.setChecked(desired_enabled)
            if "start" in slicer_payload and self._current_column():
                self._start_spin.setValue(self._coerce_start_value(slicer_payload.get("start")))
                self._start_index = self._start_index_for_value(self._start_spin.value())
            self._configure_scrollbar(len(self._column_values))
            position = self._coerce_nonnegative_int(slicer_payload.get("position"), default=0)
            self._scrollbar.setValue(max(self._scrollbar.minimum(), min(self._scrollbar.maximum(), position)))
            self._update_active_range()
            self._update_enabled_state()
        finally:
            self._applying_state = False
        self._emit_slicer_changed()

    @staticmethod
    def _coerce_positive_int(value: object, *, default: int) -> int:
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return int(default)

    @staticmethod
    def _coerce_nonnegative_int(value: object, *, default: int) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return int(default)

    def _coerce_start_value(self, value: object) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return self._start_spin.minimum()

    @staticmethod
    def _coerce_navigation_repeat_interval(value: object) -> int:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return 300
        return int(round(seconds * 1000))

    def set_slicer_changed_callback(self, callback: Callable[[], None] | None) -> None:
        self._slicer_changed_callback = callback

    def active_selection(self) -> dict[str, object]:
        column = self._current_column()
        enabled = bool(self._slicer_enabled and column and not self._values_loading)
        return {
            "enabled": enabled,
            "column": column if enabled else "",
            "values": list(self._active_values) if enabled else [],
        }

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
        for label in labels:
            action = self._menu_load_recent.addAction(label)
            action.triggered.connect(lambda _checked=False, value=label: self._load_preset_label(value))
        append_manage_presets_action(self._menu_load_recent, self._open_settings)

    def _save_current_preset(self) -> None:
        if self._save_preset_callback is None:
            return
        label, ok = QtWidgets.QInputDialog.getText(self._widget, "Save Slicer Preset", "Preset name:")
        if not ok:
            return
        name = str(label).strip()
        if not name:
            return
        if self._save_preset_callback(name, self.slicer_state_payload()):
            self._show_status(f"Saved slicer preset: {name}")

    def _load_preset_label(self, label: str) -> None:
        if self._load_preset_callback is None:
            return
        payload = self._load_preset_callback(label)
        if isinstance(payload, dict):
            self.apply_slicer_state_payload(payload)
            self._show_status(f"Loaded slicer preset: {label}")
