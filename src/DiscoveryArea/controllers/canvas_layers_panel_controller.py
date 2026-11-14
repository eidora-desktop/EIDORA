from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from DiscoveryArea.canvas.viewport import VIEWPORT_ASPECT_PRESETS, normalize_viewport_aspect_preset
from DiscoveryArea.presets_repository import PresetEntry
from shared.qt.dialog_windows import mark_ephemeral_window
from shared.qt.panel_controller import UiPanelController
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class _LayerSettingsKeyFilter(QtCore.QObject):
    def __init__(self, owner: "CanvasLayersPanelController", layer: str) -> None:
        super().__init__(owner.widget)
        self._owner = owner
        self._layer = layer

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() != QtCore.QEvent.Type.KeyPress or (
            event.modifiers() & ~QtCore.Qt.KeyboardModifier.KeypadModifier
        ):
            return False
        enter_keys = (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter)
        if obj is self._owner._layer_settings_checkboxes[self._layer] and event.key() in enter_keys:
            self._owner._open_layer_settings(self._layer)
            return True
        if obj is self._owner._layer_settings_buttons[self._layer]:
            if event.key() in (*enter_keys, QtCore.Qt.Key.Key_Space):
                if not event.isAutoRepeat():
                    self._owner._toggle_layer_settings(self._layer, focus_settings=True)
                return True
            if (
                event.key() == QtCore.Qt.Key.Key_Escape
                and self._owner._active_layer_settings_layer == self._layer
            ):
                self._owner._dismiss_layer_settings(self._layer)
                return True
        return False


class _LayerSettingsClickFilter(QtCore.QObject):
    def __init__(self, owner: "CanvasLayersPanelController") -> None:
        super().__init__(owner.widget)
        self._owner = owner

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() in (QtCore.QEvent.Type.MouseButtonPress, QtCore.QEvent.Type.MouseButtonDblClick):
            self._owner._close_layer_settings_if_clicked_outside(event)
        return False


class CanvasLayersPanelController(UiPanelController):
    package = "DiscoveryArea"
    ui_path = "ui/canvas_layers.ui"
    _NEW_CAMERA_DATA = "__new_camera__"
    _CENTER_FIT_CAMERA_DATA = "__center_fit_camera__"
    _CENTER_FIT_CAMERA_LABEL = "center fit"
    _LAYER_SETTINGS = {
        "tooltip": ("checkbox_show_tooltips", "ui/tooltip_settings.ui"),
        "pins": ("checkbox_show_pins", "ui/pins_settings.ui"),
        "areas": ("checkbox_show_marked_areas", "ui/areas_settings.ui"),
        "previews": ("checkbox_show_thumbnails", "ui/previews_settings.ui"),
    }

    def _wire(self) -> None:
        self._areas_visibility_callback = None
        self._pins_visibility_callback = None
        self._tooltips_visibility_callback = None
        self._layer_settings_view_provider: Callable[[str], QtWidgets.QWidget | None] | None = None
        self._layer_settings_popups: dict[str, QtWidgets.QWidget] = {}
        self._layer_settings_checkboxes: dict[str, QtWidgets.QCheckBox] = {}
        self._layer_settings_buttons: dict[str, QtWidgets.QPushButton] = {}
        self._layer_settings_click_filter = _LayerSettingsClickFilter(self)
        self._tooltip_settings_popup: QtWidgets.QWidget | None = None
        self._active_layer_settings_layer: str | None = None
        self._layer_settings_show_generation = 0
        self._checkboxes = [
            require_child(self._widget, QtWidgets.QCheckBox, "checkbox_show_tooltips"),
            require_child(self._widget, QtWidgets.QCheckBox, "checkbox_show_pins"),
            require_child(self._widget, QtWidgets.QCheckBox, "checkbox_show_marked_areas"),
        ]
        for checkbox in self._checkboxes:
            checkbox.toggled.connect(
                lambda checked, label=checkbox.text(): self._show_status(
                    f"{label} layer {'shown' if checked else 'hidden'}."
                )
            )
        require_child(self._widget, QtWidgets.QCheckBox, "checkbox_show_marked_areas").toggled.connect(
            self._on_areas_toggled
        )
        require_child(self._widget, QtWidgets.QCheckBox, "checkbox_show_pins").toggled.connect(
            self._on_pins_toggled
        )
        require_child(self._widget, QtWidgets.QCheckBox, "checkbox_show_tooltips").toggled.connect(
            self._on_tooltips_toggled
        )
        self._show_thumbnails_checkbox = require_child(
            self._widget, QtWidgets.QCheckBox, "checkbox_show_thumbnails"
        )
        self._show_thumbnails_checkbox.setChecked(False)
        self._show_thumbnails_checkbox.toggled.connect(self._on_show_thumbnails_toggled)
        self._camera_combo = require_child(self._widget, QtWidgets.QComboBox, "combo_camera_preset")
        self._camera_combo.activated.connect(self._on_camera_combo_activated)
        self._render_selection_changed_callback: Callable[[str], None] | None = None
        self._save_camera_callback: Callable[[str], bool] | None = None
        self._load_camera_callback: Callable[[Path], bool] | None = None
        self._default_camera_callback: Callable[[], None] | None = None
        self._camera_entries: list[PresetEntry] = []
        require_child(self._widget, QtWidgets.QToolButton, "button_add_camera_preset").clicked.connect(
            self._save_camera_preset
        )
        self._viewport_aspect_callback: Callable[[str], None] | None = None
        self._active_viewport_aspect_preset = "free"
        self._configure_crop_controls()
        self.set_camera_presets([])
        self._configure_layer_settings_popovers()

    def _configure_layer_settings_popovers(self) -> None:
        for layer, (checkbox_name, _ui_path) in self._LAYER_SETTINGS.items():
            checkbox = require_child(self._widget, QtWidgets.QCheckBox, checkbox_name)
            self._layer_settings_checkboxes[layer] = checkbox
            button = require_child(self._widget, QtWidgets.QPushButton, f"button_{layer}_settings")
            button.clicked.connect(
                lambda _checked=False, current_layer=layer: self._toggle_layer_settings(current_layer)
            )
            self._layer_settings_buttons[layer] = button
            key_filter = _LayerSettingsKeyFilter(self, layer)
            checkbox.installEventFilter(key_filter)
            button.installEventFilter(key_filter)
            action = QtGui.QAction(f"{checkbox.text()} Settings…", checkbox)
            action.triggered.connect(
                lambda _checked=False, current_layer=layer: self._open_layer_settings(current_layer)
            )
            checkbox.addAction(action)
            checkbox.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.ActionsContextMenu)
        self._show_tooltips_checkbox = self._layer_settings_checkboxes["tooltip"]

    def _open_layer_settings(self, layer: str) -> None:
        """Explicit access keeps Space available for the layer visibility toggle."""
        self._show_layer_settings_popup(layer)
        popup = self._layer_settings_popups[layer]
        popup.activateWindow()
        popup.focusNextChild()

    def _toggle_layer_settings(self, layer: str, *, focus_settings: bool = False) -> None:
        popup = self._layer_settings_popups.get(layer)
        if popup is not None and popup.isVisible():
            self._dismiss_layer_settings(layer)
        elif focus_settings:
            self._open_layer_settings(layer)
        else:
            # Mouse users can focus a control directly inside the tool window.
            # Activating the window here repaints the main shell on first open.
            self._show_layer_settings_popup(layer)

    def _dismiss_layer_settings(self, layer: str) -> None:
        self._hide_layer_settings_popup(layer)
        self._widget.window().activateWindow()
        self._layer_settings_buttons[layer].setFocus(QtCore.Qt.FocusReason.ShortcutFocusReason)

    def _configure_crop_controls(self) -> None:
        self._crop_combo = require_child(self._widget, QtWidgets.QComboBox, "combo_crop_preset")
        self._crop_combo.clear()
        for preset in VIEWPORT_ASPECT_PRESETS:
            self._crop_combo.addItem(preset.label, preset.key)
        self._crop_combo.activated.connect(self._on_crop_combo_activated)
        self._hide_crop_bars_button = require_child(
            self._widget, QtWidgets.QToolButton, "check_hide_crop_bars"
        )
        self._hide_crop_bars_button.setToolTip("Hide crop bars")
        self._hide_crop_bars_button.toggled.connect(self._on_hide_crop_bars_toggled)
        self._crop_bars_hidden_callback: Callable[[bool], None] | None = None
        self.set_viewport_aspect_preset("free")

    def _on_crop_combo_activated(self, index: int) -> None:
        self._crop_combo.setCurrentIndex(index)
        key = normalize_viewport_aspect_preset(self._crop_combo.itemData(index))
        self._show_status(f"Viewport aspect ratio: {self._crop_combo.itemText(index)}")
        if callable(self._viewport_aspect_callback):
            self._viewport_aspect_callback(key)

    def _on_hide_crop_bars_toggled(self, hidden: bool) -> None:
        self._show_status(f"Crop bars {'hidden' if hidden else 'shown'}.")
        if callable(self._crop_bars_hidden_callback):
            self._crop_bars_hidden_callback(bool(hidden))

    def set_viewport_aspect_callback(self, callback: Callable[[str], None] | None) -> None:
        self._viewport_aspect_callback = callback

    def set_crop_bars_hidden_callback(self, callback: Callable[[bool], None] | None) -> None:
        self._crop_bars_hidden_callback = callback

    def crop_bars_hidden(self) -> bool:
        return self._hide_crop_bars_button.isChecked()

    def set_crop_bars_hidden(self, hidden: bool) -> None:
        with QtCore.QSignalBlocker(self._hide_crop_bars_button):
            self._hide_crop_bars_button.setChecked(bool(hidden))

    def set_viewport_aspect_preset(self, preset: object) -> None:
        key = normalize_viewport_aspect_preset(preset)
        self._active_viewport_aspect_preset = key
        index = self._crop_combo.findData(key)
        if index >= 0:
            with QtCore.QSignalBlocker(self._crop_combo):
                self._crop_combo.setCurrentIndex(index)

    def _on_areas_toggled(self, checked: bool) -> None:
        if callable(self._areas_visibility_callback):
            self._areas_visibility_callback(bool(checked))

    def set_areas_visibility_changed_callback(self, callback) -> None:
        self._areas_visibility_callback = callback

    def _on_pins_toggled(self, checked: bool) -> None:
        if callable(self._pins_visibility_callback):
            self._pins_visibility_callback(bool(checked))

    def set_pins_visibility_changed_callback(self, callback) -> None:
        self._pins_visibility_callback = callback

    def _on_tooltips_toggled(self, checked: bool) -> None:
        if callable(self._tooltips_visibility_callback):
            self._tooltips_visibility_callback(bool(checked))

    def set_tooltips_visibility_changed_callback(self, callback) -> None:
        self._tooltips_visibility_callback = callback

    def set_layer_settings_view_provider(
        self,
        provider: Callable[[str], QtWidgets.QWidget | None] | None,
    ) -> None:
        self._layer_settings_view_provider = provider

    def _show_tooltip_settings_popup(self) -> None:
        self._show_layer_settings_popup("tooltip")

    def _show_layer_settings_popup(self, layer: str) -> None:
        popup = self._layer_settings_popups.get(layer)
        if popup is None:
            _checkbox_name, ui_path = self._LAYER_SETTINGS[layer]
            popup = (
                self._layer_settings_view_provider(layer)
                if callable(self._layer_settings_view_provider)
                else None
            )
            if popup is None:
                popup = load_ui(self.package, ui_path)
            popup.setParent(
                self._widget.window(),
                QtCore.Qt.WindowType.Tool
                | QtCore.Qt.WindowType.FramelessWindowHint,
            )
            popup.setWindowModality(QtCore.Qt.WindowModality.NonModal)
            popup.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            mark_ephemeral_window(popup)
            popup.setWindowTitle(f"{self._layer_settings_checkboxes[layer].text()} Settings")
            dismiss = QtGui.QShortcut(QtGui.QKeySequence("Esc"), popup)
            dismiss.activated.connect(lambda current_layer=layer: self._dismiss_layer_settings(current_layer))
            popup.destroyed.connect(
                lambda *_args, current_layer=layer: self._layer_settings_popups.pop(
                    current_layer,
                    None,
                )
            )
            self._layer_settings_popups[layer] = popup
            if layer == "tooltip":
                self._tooltip_settings_popup = popup
        for other_layer in self._LAYER_SETTINGS:
            if other_layer != layer:
                self._hide_layer_settings_popup(other_layer)
        QtWidgets.QApplication.instance().installEventFilter(self._layer_settings_click_filter)
        self._active_layer_settings_layer = layer
        self._layer_settings_show_generation += 1
        generation = self._layer_settings_show_generation
        self._present_layer_settings_popup(layer, popup)
        # Tool windows can lose their first expose while Qt is still dispatching
        # the activating event, especially beside a native Quick host.
        # Re-present once the originating event has returned.
        QtCore.QTimer.singleShot(
            0,
            lambda: self._re_present_layer_settings_popup(layer, popup, generation),
        )

    def _present_layer_settings_popup(
        self,
        layer: str,
        popup: QtWidgets.QWidget,
    ) -> None:
        popup.ensurePolished()
        self._position_layer_settings_popup(layer, popup)
        popup.show()
        popup.raise_()

    def _re_present_layer_settings_popup(
        self,
        layer: str,
        popup: QtWidgets.QWidget,
        generation: int,
    ) -> None:
        if generation != self._layer_settings_show_generation:
            return
        if self._active_layer_settings_layer != layer:
            return
        if not self._widget.isVisible():
            return
        if self._layer_settings_popups.get(layer) is not popup:
            return
        self._present_layer_settings_popup(layer, popup)

    def _position_layer_settings_popup(self, layer: str, popup: QtWidgets.QWidget) -> None:
        anchor = self._layer_settings_checkboxes[layer]
        anchor_bottom = anchor.mapToGlobal(QtCore.QPoint(0, anchor.height()))
        hint = popup.minimumSizeHint()
        if not hint.isValid():
            hint = popup.sizeHint()
        target_size = hint.expandedTo(popup.minimumSize()).boundedTo(
            popup.maximumSize()
        )
        width = min(
            max(target_size.width(), anchor.width()),
            popup.maximumWidth(),
        )
        height = target_size.height()
        screen = anchor.screen() or QtGui.QGuiApplication.screenAt(anchor_bottom)
        available = (
            screen.availableGeometry()
            if screen is not None
            else QtCore.QRect(anchor_bottom, QtCore.QSize(width, height))
        )
        max_x = max(available.left(), available.right() - width + 1)
        max_y = max(available.top(), available.bottom() - height + 1)
        x = min(max(anchor_bottom.x(), available.left()), max_x)
        y = min(max(anchor_bottom.y(), available.top()), max_y)
        popup.resize(width, height)
        popup.move(x, y)

    def _close_layer_settings_if_clicked_outside(self, event: QtGui.QMouseEvent) -> None:
        layer = self._active_layer_settings_layer
        popup = self._layer_settings_popups.get(layer)
        if popup is None or not popup.isVisible():
            return
        position = event.globalPosition().toPoint()
        if popup.rect().contains(popup.mapFromGlobal(position)):
            return
        # Let the active arrow handle its own toggle on release.
        button = self._layer_settings_buttons[layer]
        if event.button() == QtCore.Qt.MouseButton.LeftButton and button.rect().contains(
            button.mapFromGlobal(position)
        ):
            return
        child_popup = QtWidgets.QApplication.activePopupWidget()
        if (
            child_popup is not None
            and popup.isAncestorOf(child_popup)
            and child_popup.rect().contains(child_popup.mapFromGlobal(position))
        ):
            return
        self._hide_layer_settings_popup(layer)

    def _hide_layer_settings_popup(self, layer: str) -> None:
        popup = self._layer_settings_popups.get(layer)
        if popup is not None:
            popup.hide()

        if self._active_layer_settings_layer == layer:
            QtWidgets.QApplication.instance().removeEventFilter(self._layer_settings_click_filter)
            self._active_layer_settings_layer = None
            self._layer_settings_show_generation += 1

    def close_layer_settings_popups(self) -> None:
        """Immediately hide every top-level settings window owned by this panel."""

        QtWidgets.QApplication.instance().removeEventFilter(self._layer_settings_click_filter)
        self._active_layer_settings_layer = None
        self._layer_settings_show_generation += 1
        for popup in tuple(self._layer_settings_popups.values()):
            popup.hide()

    def shutdown(self) -> None:
        self.close_layer_settings_popups()
        for popup in tuple(self._layer_settings_popups.values()):
            popup.close()
            popup.deleteLater()
        self._layer_settings_popups.clear()
        self._tooltip_settings_popup = None

    def set_camera_presets(
        self,
        entries: list[PresetEntry],
        selected: str | None = None,
    ) -> None:
        self._camera_entries = list(entries)
        with QtCore.QSignalBlocker(self._camera_combo):
            self._camera_combo.clear()
            self._camera_combo.addItem(self._CENTER_FIT_CAMERA_LABEL, self._CENTER_FIT_CAMERA_DATA)
            for entry in self._camera_entries[:50]:
                path = str(entry.path)
                self._camera_combo.addItem(entry.label, path)
            if selected:
                idx = self._camera_combo.findData(selected)
                if idx >= 0:
                    self._camera_combo.setCurrentIndex(idx)

    def show_default_camera(self) -> None:
        with QtCore.QSignalBlocker(self._camera_combo):
            index = self._camera_combo.findData(self._CENTER_FIT_CAMERA_DATA)
            if index >= 0:
                self._camera_combo.setCurrentIndex(index)

    def show_new_camera(self) -> None:
        with QtCore.QSignalBlocker(self._camera_combo):
            index = self._camera_combo.findData(self._NEW_CAMERA_DATA)
            if index < 0:
                self._camera_combo.addItem("none", self._NEW_CAMERA_DATA)
                index = self._camera_combo.count() - 1
            self._camera_combo.setCurrentIndex(index)

    def set_camera_save_callback(self, callback: Callable[[str], bool] | None) -> None:
        self._save_camera_callback = callback

    def set_camera_load_callback(self, callback: Callable[[Path], bool] | None) -> None:
        self._load_camera_callback = callback

    def set_default_camera_callback(self, callback: Callable[[], None] | None) -> None:
        self._default_camera_callback = callback

    def set_render_selection_changed_callback(self, callback: Callable[[str], None] | None) -> None:
        self._render_selection_changed_callback = callback

    def _save_camera_preset(self) -> None:
        if self._save_camera_callback is None:
            return
        name, ok = QtWidgets.QInputDialog.getText(self._widget, "Save Camera Preset", "Preset name:")
        if not ok:
            return
        label = str(name).strip()
        if not label:
            return
        if self._save_camera_callback(label):
            self._show_status(f"Saved camera preset: {label}")

    def _on_camera_combo_activated(self, index: int) -> None:
        self._camera_combo.setCurrentIndex(index)
        self._show_status(f"Camera preset: {self._camera_combo.currentText()}")
        path = self._camera_combo.itemData(index)
        if path == self._NEW_CAMERA_DATA:
            return
        if path == self._CENTER_FIT_CAMERA_DATA:
            if self._default_camera_callback is not None:
                self._default_camera_callback()
            return
        if self._load_camera_callback is not None:
            self._load_camera_callback(Path(str(path)))

    def _on_show_thumbnails_toggled(self, checked: bool) -> None:
        if not checked and callable(self._render_selection_changed_callback):
            self._render_selection_changed_callback("points_fast")
