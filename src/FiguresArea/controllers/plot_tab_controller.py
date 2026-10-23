from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
import copy
from contextlib import closing
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, TYPE_CHECKING

from shared.plot_fonts import initialize_plot_fonts

initialize_plot_fonts()

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_agg import FigureCanvasAgg
from PySide6 import QtCore, QtGui, QtWidgets

from FiguresArea.domain import (
    SnapshotAnimationFrame,
    SlicerAnimationFrame,
    FigureComposition,
    FigureElement,
    FigureElementDirection,
    FigureElementKind,
    FigurePlotSettings,
    PlotCell,
)
from FiguresArea.controllers.figure_preview_adapter import FigurePreviewAdapter
from FiguresArea.controllers.transform_coordinator import FigureTransformCoordinator
from FiguresArea.renderer import FigureLayoutMap, FigureMatplotlibRenderer
from FiguresArea.template_settings import default_plot_settings
from DiscoveryArea.presets_repository import PresetRepository
from DiscoveryArea.method_catalog import DiscoveryMethodCatalog
from DiscoveryArea.transforms import (
    TransformResult,
    TransformSettings,
    source_fingerprint,
    transform_coordinates,
)
from DiscoveryArea.thumbnails.resolver import ThumbnailResolver
from shared.qt.icons import apply_icon_color
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child

if TYPE_CHECKING:
    from shared.qt.activity import ActivityManager


@dataclass
class _PlotCell:
    row: int
    column: int
    block: QtWidgets.QFrame
    drop_target: QtWidgets.QFrame
    canvas: QtWidgets.QWidget | None = None
    add_buttons: list[QtWidgets.QPushButton] = field(default_factory=list)
    drop_label: QtWidgets.QLabel | None = None
    settings_button: QtWidgets.QToolButton | None = None
    preset_panel: QtWidgets.QFrame | None = None
    preset_combos: dict[str, QtWidgets.QWidget] = field(default_factory=dict)


@dataclass
class _PinDragState:
    cell: tuple[int, int]
    annotation_id: str
    start_overlay_pos: QtCore.QPoint
    start_rect: QtCore.QRect
    start_mouse_data: tuple[float, float]
    start_box_anchor_data: tuple[float, float]
    point_data: tuple[float, float]


class PlotTabController(QtCore.QObject):
    """Controller for one figure plot tab with one authoritative Matplotlib canvas."""

    _ADD_BUTTON_SIZE = 28
    _GLOBAL_ADD_BUTTON_HEIGHT = 32
    _CONTROL_MARGIN = 72
    _MIN_PREVIEW_SCALE = 0.01

    def __init__(
        self,
        *,
        project: Any | None = None,
        project_root: Path | None,
        figure_kind: str = "journal",
        parent: QtWidgets.QWidget | None = None,
        status_callback: Callable[[str], None] | None = None,
        selection_callback: Callable[[str], None] | None = None,
        state_changed_callback: Callable[[], None] | None = None,
        plot_edit_callback: Callable[[dict[str, Any]], None] | None = None,
        activity_manager: ActivityManager | None = None,
    ) -> None:
        super().__init__(parent)
        self._project = project
        self._project_root = project_root
        self._status_callback = status_callback
        self._selection_callback = selection_callback
        self._state_changed_callback = state_changed_callback
        self._plot_edit_callback = plot_edit_callback
        self._activity_manager = activity_manager
        self._discovery_method_catalog = DiscoveryMethodCatalog()
        self._plot_settings = self._default_settings_for_figure_kind(figure_kind)
        self._composition = FigureComposition(settings=self._plot_settings)
        self._sync_composition_grid()
        self._sync_parameter_elements()
        self._renderer = FigureMatplotlibRenderer()
        self._widget = load_ui("FiguresArea", "ui/plot_base_layout.ui", parent)
        self._plot_viewport = require_child(self._widget, QtWidgets.QWidget, "plot_viewport")
        self._canvas_host = require_child(self._widget, QtWidgets.QWidget, "matplotlib_canvas_host")
        self._overlay = require_child(self._widget, QtWidgets.QWidget, "plot_interaction_overlay")
        self._plot_grid_shell = self._overlay
        self._drop_targets: dict[QtWidgets.QWidget, tuple[int, int]] = {}
        self._add_button_drop_targets: dict[QtWidgets.QWidget, tuple[tuple[int, int], str]] = {}
        self._context_menu_targets: dict[QtWidgets.QWidget, tuple[int, int]] = {}
        self._settings_button_targets: dict[QtWidgets.QWidget, tuple[int, int]] = {}
        self._element_context_menu_targets: dict[QtWidgets.QWidget, str] = {}
        self._element_widgets: dict[str, QtWidgets.QFrame] = {}
        self._preset_control_targets: dict[QtWidgets.QWidget, tuple[int, int]] = {}
        self._preset_combo_targets: dict[QtWidgets.QComboBox, tuple[tuple[int, int], str]] = {}
        self._refreshing_preset_controls = False
        self._transform_coordinator = FigureTransformCoordinator(self._widget)
        self._transform_coordinator.ready.connect(self._figure_transform_ready)
        self._transform_coordinator.failed.connect(self._figure_transform_failed)
        self._transform_coordinator.cancelled.connect(self._figure_transform_cancelled)
        self._pending_transform: dict[str, Any] | None = None
        self._transform_cache: OrderedDict[tuple[object, ...], TransformResult] = OrderedDict()
        self._active_preset_control_cell: tuple[int, int] | None = None
        self._layout_map = FigureLayoutMap(canvas_size_px=(1, 1))
        self._preview_scale = 1.0
        self._canvas: FigureCanvas | None = None
        self._preview = FigurePreviewAdapter(self._canvas_host)
        self._active_context_menu: QtWidgets.QMenu | None = None
        self._global_main_title_button: QtWidgets.QPushButton | None = None
        self._controls_visible = False
        self._active_settings_button_cell: tuple[int, int] | None = None
        self._pin_drag: _PinDragState | None = None
        self._pin_drag_frame: QtWidgets.QFrame | None = None
        self._pin_hover_frame: QtWidgets.QFrame | None = None
        self._plot_cells: dict[tuple[int, int], _PlotCell] = {
            (0, 0): self._create_plot_overlay_cell((0, 0), is_main=True)
        }
        self._plot_area = self._plot_cells[(0, 0)].drop_target
        self._main_plot_block = self._plot_cells[(0, 0)].block
        self._plot_drop_count = 0
        self._plot_viewport.installEventFilter(self)
        self._overlay.installEventFilter(self)
        self._canvas_host.installEventFilter(self)
        self._plot_viewport.setMouseTracking(True)
        self._overlay.setMouseTracking(True)
        self._canvas_host.setMouseTracking(True)
        self._overlay.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, False)
        self._overlay.raise_()
        self._redraw_figure(show_activity=False, notify_state=False)

    @staticmethod
    def _default_settings_for_figure_kind(figure_kind: str) -> FigurePlotSettings:
        return default_plot_settings(figure_kind)

    @property
    def widget(self) -> QtWidgets.QWidget:
        return self._widget

    @property
    def snapshot_drop_target(self) -> QtWidgets.QFrame:
        return self._plot_area

    def set_project(self, project: Any | None) -> None:
        self._transform_coordinator.cancel_pending()
        self._pending_transform = None
        self._project = project
        self._project_root = project.root if project is not None else None
        self._transform_cache.clear()

    def shutdown(self) -> None:
        self._transform_coordinator.cancel_pending()
        self._pending_transform = None

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if obj is self._plot_viewport:
            if event.type() == QtCore.QEvent.Type.Resize:
                QtCore.QTimer.singleShot(0, self._apply_preview_scale)
                return False
            if event.type() == QtCore.QEvent.Type.Enter:
                self._set_add_controls_visible(True)
                return False
            if event.type() == QtCore.QEvent.Type.Leave:
                self._set_add_controls_visible(False)
                return False
        if obj in {self._overlay, self._canvas_host}:
            if obj is self._overlay and self._pin_drag is not None and self._handle_pin_drag_event(obj, event):
                return True
            if self._handle_pin_interaction_event(obj, event):
                return True
            if event.type() == QtCore.QEvent.Type.Enter:
                self._set_add_controls_visible(True)
                return False
            if event.type() == QtCore.QEvent.Type.Leave:
                self._set_add_controls_visible(False)
                return False
        if isinstance(obj, QtWidgets.QWidget) and obj in self._element_context_menu_targets:
            if event.type() == QtCore.QEvent.Type.ContextMenu:
                self._show_element_context_menu(obj, event)
                return True
            if event.type() == QtCore.QEvent.Type.MouseButtonDblClick:
                self._edit_figure_element_text(self._element_context_menu_targets[obj])
                return True
            if event.type() == QtCore.QEvent.Type.MouseButtonPress:
                self._select_element(self._element_context_menu_targets[obj])
                self._edit_figure_element_text(self._element_context_menu_targets[obj])
                return False
            if event.type() == QtCore.QEvent.Type.Enter:
                obj.setStyleSheet("background: rgba(47, 111, 237, 24); border: 1px solid palette(highlight);")
                return False
            if event.type() == QtCore.QEvent.Type.Leave:
                obj.setStyleSheet("background: transparent;")
                return False
        if isinstance(obj, QtWidgets.QWidget) and obj in self._settings_button_targets:
            cell = self._settings_button_targets[obj]
            if event.type() == QtCore.QEvent.Type.Enter:
                self._show_settings_button_for_cell(cell)
                return False
            if event.type() == QtCore.QEvent.Type.Leave:
                QtCore.QTimer.singleShot(0, lambda c=cell: self._hide_plot_cell_controls_if_not_hovered(c))
                return False
        if isinstance(obj, QtWidgets.QWidget) and obj in self._preset_control_targets:
            cell = self._preset_control_targets[obj]
            if event.type() == QtCore.QEvent.Type.Enter:
                self._show_settings_button_for_cell(cell)
                return False
            if event.type() == QtCore.QEvent.Type.Leave:
                QtCore.QTimer.singleShot(0, lambda c=cell: self._hide_plot_cell_controls_if_not_hovered(c))
                return False
        if isinstance(obj, QtWidgets.QWidget) and obj in self._context_menu_targets:
            if self._handle_pin_interaction_event(obj, event):
                return True
            if event.type() == QtCore.QEvent.Type.ContextMenu:
                self._show_plot_cell_context_menu(obj, event)
                return True
            if event.type() == QtCore.QEvent.Type.MouseButtonPress:
                self._select_plot_cell(self._context_menu_targets[obj])
                return False
            if event.type() == QtCore.QEvent.Type.Enter:
                self._show_settings_button_for_cell(self._context_menu_targets[obj])
                return False
            if event.type() == QtCore.QEvent.Type.Leave:
                obj.unsetCursor()
                self._set_pin_hover_rect(None)
                QtCore.QTimer.singleShot(0, lambda c=self._context_menu_targets[obj]: self._hide_plot_cell_controls_if_not_hovered(c))
                return False
        if isinstance(obj, QtWidgets.QWidget) and obj in self._add_button_drop_targets:
            if event.type() == QtCore.QEvent.Type.Enter:
                self._set_add_controls_visible(True)
                return False
            return self._handle_add_button_drop_event(obj, event)
        if isinstance(obj, QtWidgets.QWidget) and (
            obj in self._drop_targets or obj in self._context_menu_targets or obj in self._element_context_menu_targets
        ):
            if event.type() == QtCore.QEvent.Type.Enter:
                self._set_add_controls_visible(True)
                return False
        if not isinstance(obj, QtWidgets.QWidget) or obj not in self._drop_targets:
            return super().eventFilter(obj, event)
        if event.type() == QtCore.QEvent.Type.DragEnter:
            if self._snapshot_path_from_mime(event.mimeData()) is not None:
                event.acceptProposedAction()
                return True
        if event.type() == QtCore.QEvent.Type.Drop:
            path = self._snapshot_path_from_mime(event.mimeData())
            cell = self._drop_targets[obj]
            if path is not None and self._render_snapshot(path, target=self._plot_cells[cell].drop_target):
                event.acceptProposedAction()
                return True
        return super().eventFilter(obj, event)

    def plot_canvas_size_px(self) -> tuple[int, int]:
        return self._layout_map.canvas_size_px

    def plot_preview_size_px(self) -> tuple[int, int]:
        return self._preview.preview_size_px(
            self._layout_map.canvas_size_px,
            self._preview_scale,
            min_scale=self._MIN_PREVIEW_SCALE,
        )

    def build_matplotlib_figure(self):
        return self._renderer.build(self._composition).figure

    def export_figure(
        self,
        path: Path,
        *,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> None:
        self._renderer.export(self._composition, path, progress_callback=progress_callback)

    def render_animation_frame(self, frame: SnapshotAnimationFrame) -> bool:
        """Render one animation frame into the authoritative preview canvas."""

        return self._render_snapshot(
            Path(frame.snapshot_path),
            announce=False,
            plot_overrides=frame.plot_overrides,
        )

    def render_animation_base_snapshot(self, snapshot_path: Path) -> bool:
        """Load the shared snapshot used by generated slicer frames."""

        return self._render_snapshot(Path(snapshot_path), announce=False)

    def render_slicer_animation_frame(
        self,
        frame: SlicerAnimationFrame,
        base_snapshot_path: Path,
    ) -> bool:
        """Render a generated slicer frame into the authoritative preview."""

        try:
            prepared = self._prepare_slicer_animation_plot_cell(
                frame, Path(base_snapshot_path)
            )
        except ValueError as error:
            self._show_status(str(error))
            return False
        plot_cell = self._composition.plot_cells.get((0, 0))
        if plot_cell is None:
            plot_cell = self._composition.add_plot_cell(0, 0)
        self._apply_plot_overrides(prepared, frame.plot_overrides)
        self._apply_prepared_snapshot(plot_cell, prepared)
        self._update_subtitle_text_for_cell((0, 0))
        self._redraw_figure()
        return True

    def export_animation_frame_png(
        self, frame: SnapshotAnimationFrame, destination: Path
    ) -> None:
        """Render one full-quality animation frame without changing the preview."""

        prepared = self._prepare_snapshot_plot_cell(Path(frame.snapshot_path))
        self._apply_plot_overrides(prepared, frame.plot_overrides)
        composition = copy.deepcopy(self._composition)
        plot_cell = composition.plot_cells.get((0, 0))
        if plot_cell is None:
            plot_cell = composition.add_plot_cell(0, 0)
        self._apply_prepared_snapshot(plot_cell, prepared)
        self._update_subtitle_text_for_cell((0, 0), composition=composition)
        figure = self._renderer.build(composition).figure
        try:
            canvas = FigureCanvasAgg(figure)
            canvas.print_png(str(destination))
        finally:
            figure.clear()

    def export_slicer_animation_frame_png(
        self,
        frame: SlicerAnimationFrame,
        base_snapshot_path: Path,
        destination: Path,
    ) -> None:
        """Render one generated slicer frame without changing the preview."""

        prepared = self._prepare_slicer_animation_plot_cell(
            frame, Path(base_snapshot_path)
        )
        self._apply_plot_overrides(prepared, frame.plot_overrides)
        composition = copy.deepcopy(self._composition)
        plot_cell = composition.plot_cells.get((0, 0))
        if plot_cell is None:
            plot_cell = composition.add_plot_cell(0, 0)
        self._apply_prepared_snapshot(plot_cell, prepared)
        self._update_subtitle_text_for_cell((0, 0), composition=composition)
        figure = self._renderer.build(composition).figure
        try:
            canvas = FigureCanvasAgg(figure)
            canvas.print_png(str(destination))
        finally:
            figure.clear()

    def figure_settings(self) -> FigurePlotSettings:
        return self._composition.settings

    def figure_composition(self) -> FigureComposition:
        return self._composition

    def apply_figure_composition(self, composition: FigureComposition) -> None:
        self._composition = composition
        self._plot_settings = composition.settings
        self._sync_composition_grid()
        self._sync_parameter_elements()
        self._redraw_figure(show_activity=False)

    def activate(self) -> None:
        self._apply_preview_scale(force=True)

    def apply_figure_settings(
        self,
        settings: FigurePlotSettings,
        *,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> None:
        if progress_callback is not None:
            progress_callback("Updating figure layout…", 10)
        self._plot_settings = settings
        self._composition.settings = settings
        self._sync_composition_grid()
        self._sync_parameter_elements()
        if progress_callback is not None:
            progress_callback("Rendering figure preview…", 30)
        self._redraw_figure(show_activity=progress_callback is None)
        if progress_callback is not None:
            progress_callback("Finalizing figure preview…", 90)
        self._show_status("Applied figure settings.")
        if self._state_changed_callback is not None:
            self._state_changed_callback()
        if progress_callback is not None:
            progress_callback("Figure settings applied", 100)

    def _sync_composition_grid(self) -> None:
        rows = max(int(getattr(self._composition.settings, "rows", 1)), 1)
        columns = max(int(getattr(self._composition.settings, "columns", 1)), 1)
        desired = {(row, column) for row in range(rows) for column in range(columns)}
        for cell in sorted(desired):
            if cell not in self._composition.plot_cells:
                self._composition.add_plot_cell(*cell)
        for cell in list(self._composition.plot_cells):
            if cell in desired:
                continue
            self._remove_elements_for_plot_cell(cell)
            self._composition.plot_cells.pop(cell, None)
            overlay_cell = self._plot_cells.pop(cell, None) if hasattr(self, "_plot_cells") else None
            if overlay_cell is not None:
                self._delete_overlay_cell(overlay_cell)
        if hasattr(self, "_plot_cells"):
            for cell in sorted(desired):
                if cell not in self._plot_cells:
                    self._plot_cells[cell] = self._create_plot_overlay_cell(cell, is_main=cell == (0, 0))

    def _sync_parameter_elements(self) -> None:
        settings = self._composition.settings
        self._sync_main_title_element(settings)
        self._sync_subtitle_elements(settings)
        self._sync_legend_elements(settings)
        self._sync_caption_elements(settings)

    def _sync_main_title_element(self, settings: FigurePlotSettings) -> None:
        element_id = "main_title_settings"
        existing = self._composition.figure_elements.get(element_id)
        if not settings.main_title_show:
            if existing is not None:
                self._remove_figure_element(element_id)
            return
        if existing is None:
            existing = FigureElement(
                element_id=element_id,
                kind="main_title",
                direction=settings.main_title_location,
                source_cell=None,
                text="Main title",
                order=-1000,
            )
            self._composition.figure_elements[element_id] = existing
        existing.direction = settings.main_title_location
        existing.visible = True
        existing.font_family = settings.main_title_font
        existing.font_size = float(settings.main_title_size)
        existing.alignment = settings.main_title_alignment

    def _sync_subtitle_elements(self, settings: FigurePlotSettings) -> None:
        prefix = "plot_title_settings_"
        desired_ids: set[str] = set()
        if settings.subtitle_show:
            for cell in sorted(self._composition.plot_cells):
                element_id = f"{prefix}{cell[0]}_{cell[1]}"
                desired_ids.add(element_id)
                element = self._composition.figure_elements.get(element_id)
                if element is None:
                    element = FigureElement(
                        element_id=element_id,
                        kind="plot_title",
                        direction=settings.subtitle_location,
                        source_cell=cell,
                        text=self._default_subtitle_text(cell),
                        order=cell[0] * 1000 + cell[1],
                    )
                    self._composition.figure_elements[element_id] = element
                    self._composition.plot_cells[cell].element_ids.append(element_id)
                element.direction = settings.subtitle_location
                element.visible = True
                element.font_family = settings.subtitle_font
                element.font_size = float(settings.subtitle_size)
                element.alignment = settings.subtitle_alignment
        for element_id, element in list(self._composition.figure_elements.items()):
            if not element_id.startswith(prefix) or element_id in desired_ids:
                continue
            self._remove_figure_element(element_id)

    def _sync_caption_elements(self, settings: FigurePlotSettings) -> None:
        self._sync_main_caption_element(settings)
        self._sync_subplot_caption_elements(settings)

    def _sync_legend_elements(self, settings: FigurePlotSettings) -> None:
        prefix = "legend_settings_"
        desired_ids: set[str] = set()
        if settings.legend_show:
            if settings.legend_scope == "global":
                element_id = f"{prefix}global"
                desired_ids.add(element_id)
                element = self._composition.figure_elements.get(element_id)
                if element is None:
                    element = FigureElement(
                        element_id=element_id,
                        kind="legend",
                        direction="right",
                        source_cell=None,
                        text="Legend",
                        order=500,
                    )
                    self._composition.figure_elements[element_id] = element
                element.source_cell = None
                element.visible = True
                element.font_size = float(settings.legend_font_size)
            else:
                for cell in sorted(self._composition.plot_cells):
                    element_id = f"{prefix}{cell[0]}_{cell[1]}"
                    desired_ids.add(element_id)
                    element = self._composition.figure_elements.get(element_id)
                    if element is None:
                        element = FigureElement(
                            element_id=element_id,
                            kind="legend",
                            direction="right",
                            source_cell=cell,
                            text="Legend",
                            order=500 + (cell[0] * 1000) + cell[1],
                        )
                        self._composition.figure_elements[element_id] = element
                        self._composition.plot_cells[cell].element_ids.append(element_id)
                    element.source_cell = cell
                    element.visible = True
                    element.font_size = float(settings.legend_font_size)

        for element_id, element in list(self._composition.figure_elements.items()):
            if not element_id.startswith(prefix) or element_id in desired_ids:
                continue
            self._remove_figure_element(element_id)

    def _sync_main_caption_element(self, settings: FigurePlotSettings) -> None:
        element_id = "main_caption_settings"
        existing = self._composition.figure_elements.get(element_id)
        if not settings.main_caption_show:
            if existing is not None:
                existing.visible = False
                if self._composition.selected_element_id == element_id:
                    self._composition.selected_element_id = None
            return
        if existing is None:
            existing = FigureElement(
                element_id=element_id,
                kind="caption",
                direction="bottom",
                source_cell=None,
                text="Main caption",
                order=1000,
            )
            self._composition.figure_elements[element_id] = existing
        existing.direction = "bottom"
        existing.visible = True
        existing.font_family = settings.main_caption_font
        existing.font_size = float(settings.main_caption_size)
        existing.alignment = "justify" if settings.main_caption_justify else "left"
        existing.line_spacing = settings.main_caption_line_spacing

    def _sync_subplot_caption_elements(self, settings: FigurePlotSettings) -> None:
        prefix = "subplot_caption_settings_"
        desired_ids: set[str] = set()
        if settings.subplot_caption_show:
            for cell in sorted(self._composition.plot_cells):
                element_id = f"{prefix}{cell[0]}_{cell[1]}"
                desired_ids.add(element_id)
                element = self._composition.figure_elements.get(element_id)
                if element is None:
                    element = FigureElement(
                        element_id=element_id,
                        kind="caption",
                        direction="bottom",
                        source_cell=cell,
                        text=f"Caption {cell[0] + 1}, {cell[1] + 1}",
                        order=2000 + (cell[0] * 1000) + cell[1],
                    )
                    self._composition.figure_elements[element_id] = element
                    self._composition.plot_cells[cell].element_ids.append(element_id)
                element.direction = "bottom"
                element.visible = True
                element.font_family = settings.subplot_caption_font
                element.font_size = float(settings.subplot_caption_size)
                element.alignment = settings.subplot_caption_alignment
                element.line_spacing = settings.subplot_caption_line_spacing
        else:
            for element_id, element in self._composition.figure_elements.items():
                if not element_id.startswith(prefix):
                    continue
                element.visible = False
                if self._composition.selected_element_id == element_id:
                    self._composition.selected_element_id = None
        for element_id, element in list(self._composition.figure_elements.items()):
            if not element_id.startswith(prefix) or element_id in desired_ids:
                continue
            if settings.subplot_caption_show:
                self._remove_figure_element(element_id)

    def _apply_preview_scale(self, *, force: bool = False) -> None:
        previous_scale = self._preview_scale
        self._preview_scale = self._preview_scale_for_viewport()
        if not force and abs(previous_scale - self._preview_scale) < 0.001:
            self._position_canvas_stack()
            self._sync_overlay_geometry()
            return
        self._update_preview_canvas()
        self._position_canvas_stack()
        self._sync_overlay_geometry()

    def _preview_scale_for_viewport(self) -> float:
        original_width, original_height = self.plot_canvas_size_px()
        available_width = self._plot_viewport.width()
        available_height = self._plot_viewport.height()
        if available_width <= 0 or available_height <= 0:
            return 1.0
        return max(min(1.0, available_width / float(original_width), available_height / float(original_height)), self._MIN_PREVIEW_SCALE)

    def _redraw_figure(
        self,
        *,
        show_activity: bool = True,
        notify_state: bool = True,
    ) -> None:
        if show_activity and self._activity_manager is not None:
            with self._activity_manager.busy("Refreshing plot…"):
                self._redraw_figure(show_activity=False, notify_state=notify_state)
            return
        result = self._renderer.build(self._composition)
        self._layout_map = result.layout_map
        self._preview_scale = self._preview_scale_for_viewport()
        if self._canvas is not None:
            self._canvas.setParent(None)
            self._canvas.deleteLater()
        self._canvas = FigureCanvas(result.figure)
        self._canvas.setObjectName("matplotlib_render_canvas")
        self._canvas.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
        self._canvas.setFixedSize(*self._layout_map.canvas_size_px)
        self._canvas.draw()
        self._preview.capture(self._canvas)
        self._update_preview_canvas()
        self._position_canvas_stack()
        self._sync_overlay_geometry()
        if notify_state and self._state_changed_callback is not None:
            self._state_changed_callback()

    def _update_preview_canvas(self) -> None:
        self._preview.update(
            self._layout_map.canvas_size_px,
            self._preview_scale,
            min_scale=self._MIN_PREVIEW_SCALE,
        )

    def _position_canvas_stack(self) -> None:
        width, height = self.plot_preview_size_px()
        overlay_width = width + (2 * self._CONTROL_MARGIN)
        overlay_height = height + (2 * self._CONTROL_MARGIN)
        self._canvas_host.setFixedSize(width, height)
        self._overlay.setFixedSize(overlay_width, overlay_height)
        canvas_left = max((self._plot_viewport.width() - width) // 2, 0)
        canvas_top = max((self._plot_viewport.height() - height) // 2, 0)
        self._canvas_host.move(canvas_left, canvas_top)
        self._overlay.move(canvas_left - self._CONTROL_MARGIN, canvas_top - self._CONTROL_MARGIN)
        self._canvas_host.show()
        self._overlay.show()
        self._overlay.raise_()

    def _sync_overlay_geometry(self) -> None:
        self._sync_composition_grid()
        for cell_key, cell in self._plot_cells.items():
            rect = self._overlay_rect_from_canvas_rect(self._layout_map.plot_rects_px.get(cell_key, (0, 0, 1, 1)))
            cell.block.setGeometry(*rect)
            cell.drop_target.setGeometry(0, 0, rect[2], rect[3])
            self._sync_drop_label(cell, cell_key)
            self._position_settings_button(cell)
            self._position_preset_controls(cell)
            self._populate_preset_controls(cell)
            cell.block.show()
            cell.drop_target.show()
            if cell.settings_button is not None:
                cell.settings_button.raise_()
            if cell.preset_panel is not None:
                cell.preset_panel.raise_()
        self._clear_all_plot_cell_add_buttons()
        self._sync_element_overlay_geometry()
        self._set_add_controls_visible(self._controls_visible)

    def _create_plot_overlay_cell(self, cell: tuple[int, int], *, is_main: bool = False) -> _PlotCell:
        row, column = cell
        block = QtWidgets.QFrame(self._overlay)
        block.setObjectName("main_plot_block" if is_main else f"drop_plot_{row}_{column}")
        block.setProperty("plotRow", row)
        block.setProperty("plotColumn", column)
        block.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        block.setStyleSheet("background: transparent;")
        block.setMouseTracking(True)
        target = QtWidgets.QFrame(block)
        target.setObjectName("main_plot_area" if is_main else f"drop_plot_area_{row}_{column}")
        target.setProperty("plotRow", row)
        target.setProperty("plotColumn", column)
        target.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        target.setStyleSheet("background: transparent; border: none;")
        target.setMouseTracking(True)
        drop_label = QtWidgets.QLabel("drag & drop snapshot here", target)
        drop_label.setObjectName(f"label_plot_drop_here_{row}_{column}")
        drop_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        drop_label.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        drop_label.setStyleSheet(
            """
            QLabel {
                color: palette(mid);
                background: transparent;
                border: 1px dashed palette(mid);
                border-radius: 4px;
                padding: 6px;
            }
            """
        )
        self._register_drop_target(target, cell)
        self._register_context_menu_target(block, cell)
        self._register_context_menu_target(target, cell)
        plot_cell = _PlotCell(row=row, column=column, block=block, drop_target=target, drop_label=drop_label)
        self._create_settings_button(plot_cell)
        self._create_preset_controls(plot_cell)
        self._sync_drop_label(plot_cell, cell)
        return plot_cell

    def _delete_overlay_cell(self, plot_cell: _PlotCell) -> None:
        self._delete_preset_controls(plot_cell)
        if plot_cell.settings_button is not None:
            self._settings_button_targets.pop(plot_cell.settings_button, None)
            plot_cell.settings_button.setParent(None)
            plot_cell.settings_button.deleteLater()
            plot_cell.settings_button = None
        for button in plot_cell.add_buttons:
            self._add_button_drop_targets.pop(button, None)
            button.setParent(None)
            button.deleteLater()
        self._drop_targets.pop(plot_cell.drop_target, None)
        self._context_menu_targets.pop(plot_cell.block, None)
        self._context_menu_targets.pop(plot_cell.drop_target, None)
        plot_cell.block.setParent(None)
        plot_cell.block.deleteLater()

    def _create_settings_button(self, plot_cell: _PlotCell) -> None:
        button = QtWidgets.QToolButton(plot_cell.block)
        button.setObjectName(f"button_plot_settings_{plot_cell.row}_{plot_cell.column}")
        button.setProperty("iconName", "settings")
        button.setText("Presets")
        button.setToolTip("Presets")
        button.setAutoRaise(False)
        button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setIconSize(QtCore.QSize(18, 18))
        button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(self._settings_button_stylesheet())
        button.clicked.connect(lambda _checked=False, c=(plot_cell.row, plot_cell.column): self._toggle_preset_controls_for_cell(c))
        button.installEventFilter(self)
        self._settings_button_targets[button] = (plot_cell.row, plot_cell.column)
        apply_icon_color(button)
        plot_cell.settings_button = button
        self._position_settings_button(plot_cell)
        button.hide()

    def _create_preset_controls(self, plot_cell: _PlotCell) -> None:
        panel = QtWidgets.QFrame(plot_cell.block)
        panel.setObjectName(f"plot_preset_controls_{plot_cell.row}_{plot_cell.column}")
        panel.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        panel.setStyleSheet(
            """
            QFrame {
                background: palette(base);
                border: 1px solid palette(mid);
                border-radius: 4px;
            }
            QComboBox {
                min-width: 120px;
                max-width: 180px;
            }
            QLabel {
                border: none;
                background: transparent;
            }
            """
        )
        layout = QtWidgets.QFormLayout(panel)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setHorizontalSpacing(6)
        layout.setVerticalSpacing(4)
        layout.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        labels = {
            "snapshot": "snapshot",
            "discovery": "projection",
            "transform": "transformation",
            "camera": "camera",
            "appearance": "appearance",
            "filter": "filter",
        }
        for key in ("snapshot", "discovery", "transform", "camera", "appearance", "filter"):
            label = QtWidgets.QLabel(f"{labels[key]}:", panel)
            label.setObjectName(f"label_plot_{key}_{plot_cell.row}_{plot_cell.column}")
            combo = QtWidgets.QComboBox(panel)
            combo.setObjectName(f"combo_plot_{key}_{plot_cell.row}_{plot_cell.column}")
            combo.setProperty("presetKind", key)
            combo.setToolTip(key)
            combo.installEventFilter(self)
            combo.currentIndexChanged.connect(lambda _index, c=(plot_cell.row, plot_cell.column), k=key: self._handle_plot_preset_combo_changed(c, k))
            layout.addRow(label, combo)
            plot_cell.preset_combos[key] = combo
            self._preset_control_targets[combo] = (plot_cell.row, plot_cell.column)
            self._preset_combo_targets[combo] = ((plot_cell.row, plot_cell.column), key)
        panel.installEventFilter(self)
        self._preset_control_targets[panel] = (plot_cell.row, plot_cell.column)
        plot_cell.preset_panel = panel
        panel.hide()

    def _delete_preset_controls(self, plot_cell: _PlotCell) -> None:
        if plot_cell.preset_panel is not None:
            self._preset_control_targets.pop(plot_cell.preset_panel, None)
        for combo in plot_cell.preset_combos.values():
            self._preset_control_targets.pop(combo, None)
            self._preset_combo_targets.pop(combo, None)
        plot_cell.preset_combos.clear()
        if plot_cell.preset_panel is not None:
            plot_cell.preset_panel.setParent(None)
            plot_cell.preset_panel.deleteLater()
            plot_cell.preset_panel = None

    def _clear_all_plot_cell_add_buttons(self) -> None:
        if self._global_main_title_button is not None:
            self._global_main_title_button.setParent(None)
            self._global_main_title_button.deleteLater()
            self._global_main_title_button = None
        for cell in self._plot_cells.values():
            for button in cell.add_buttons:
                self._add_button_drop_targets.pop(button, None)
                button.setParent(None)
                button.deleteLater()
            cell.add_buttons.clear()

    def _register_drop_target(self, widget: QtWidgets.QWidget, cell: tuple[int, int]) -> None:
        widget.setAcceptDrops(True)
        widget.installEventFilter(self)
        self._drop_targets[widget] = cell

    def _register_context_menu_target(self, widget: QtWidgets.QWidget, cell: tuple[int, int]) -> None:
        widget.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.DefaultContextMenu)
        widget.installEventFilter(self)
        self._context_menu_targets[widget] = cell

    def _sync_drop_label(self, plot_cell: _PlotCell, cell: tuple[int, int]) -> None:
        label = plot_cell.drop_label
        if label is None:
            return
        label.setGeometry(0, 0, plot_cell.drop_target.width(), plot_cell.drop_target.height())
        domain_cell = self._composition.plot_cells.get(cell)
        is_empty = domain_cell is None or (
            not bool(domain_cell.snapshot_path) and not bool(domain_cell.coordinates)
        )
        label.setVisible(is_empty)
        label.raise_()

    def _position_settings_button(self, plot_cell: _PlotCell) -> None:
        button = plot_cell.settings_button
        if button is None:
            return
        button.adjustSize()
        width = max(button.sizeHint().width(), 86)
        height = max(button.sizeHint().height(), 28)
        button.setFixedSize(width, height)
        button.move(max(plot_cell.block.width() - button.width() - 4, 0), 4)

    @staticmethod
    def _settings_button_stylesheet() -> str:
        return """
            QToolButton {
                color: palette(button-text);
                background: palette(button);
                border: 1px solid palette(mid);
                border-radius: 4px;
                padding: 3px 8px;
            }
            QToolButton:hover {
                background: palette(light);
                border-color: palette(highlight);
            }
            QToolButton:pressed {
                background: palette(midlight);
            }
            QToolButton::menu-indicator {
                image: none;
                width: 0px;
            }
            """

    def _position_preset_controls(self, plot_cell: _PlotCell) -> None:
        panel = plot_cell.preset_panel
        if panel is None:
            return
        panel.adjustSize()
        width = min(max(panel.sizeHint().width(), 1), max(plot_cell.block.width() - 8, 1))
        height = min(max(panel.sizeHint().height(), 1), max(plot_cell.block.height() - 8, 1))
        panel.setFixedSize(width, height)
        y = 32 if plot_cell.block.height() >= height + 36 else 4
        panel.move(max(plot_cell.block.width() - width - 4, 0), y)

    def _register_add_button_drop_target(
        self,
        button: QtWidgets.QPushButton,
        *,
        source_cell: tuple[int, int],
        direction: str,
    ) -> None:
        button.setAcceptDrops(True)
        button.installEventFilter(self)
        self._add_button_drop_targets[button] = (source_cell, direction)

    def _configure_add_button(
        self,
        button: QtWidgets.QPushButton,
        direction: str,
        *,
        source_cell: tuple[int, int],
    ) -> None:
        button.setProperty("iconName", "add_circle")
        button.setMenu(self._subplot_add_menu(direction, source_cell=source_cell))

    def _main_title_add_menu(self) -> QtWidgets.QMenu:
        menu = QtWidgets.QMenu(self._widget)
        menu.addAction("Add Main Title…", lambda checked=False: self._add_figure_element("main_title", "top", None))
        return menu

    def _subplot_add_menu(self, direction: str, *, source_cell: tuple[int, int]) -> QtWidgets.QMenu:
        menu = QtWidgets.QMenu(self._widget)
        add_plot_action = menu.addAction("Add Plot", lambda checked=False, d=direction, c=source_cell: self._add_plot_adjacent(c, d))
        add_plot_action.setEnabled(direction in self._valid_add_directions(source_cell))
        menu.addAction("Add Plot Subtitle…", lambda checked=False, d=direction, c=source_cell: self._add_figure_element("plot_title", d, c))
        menu.addAction("Add Caption…", lambda checked=False, d=direction, c=source_cell: self._add_figure_element("caption", d, c))
        return menu

    def _add_plot_adjacent(self, source_cell: tuple[int, int], direction: str) -> QtWidgets.QFrame:
        source = self._plot_cells.get(source_cell)
        if source is None:
            raise KeyError(f"Unknown source plot cell: {source_cell}")
        if direction not in self._valid_add_directions(source_cell):
            self._show_status("A plot area cannot be added in that direction.")
            return source.block
        target_cell = self._adjacent_cell(source_cell, direction)
        existing = self._plot_cells.get(target_cell)
        if existing is not None:
            self._show_status("A plot area already exists in that grid cell.")
            return existing.block
        self._plot_drop_count += 1
        plot_cell = self._create_plot_overlay_cell(target_cell)
        self._plot_cells[target_cell] = plot_cell
        self._composition.add_plot_cell(*target_cell)
        self._redraw_figure()
        self._show_status(f"Added plot area at row {target_cell[0]}, column {target_cell[1]}.")
        return plot_cell.block

    @staticmethod
    def _adjacent_cell(source_cell: tuple[int, int], direction: str) -> tuple[int, int]:
        row, column = source_cell
        offsets = {
            "top": (-1, 0),
            "left": (0, -1),
            "right": (0, 1),
            "bottom": (1, 0),
        }
        row_offset, column_offset = offsets[direction]
        return row + row_offset, column + column_offset

    def _valid_add_directions(self, source_cell: tuple[int, int]) -> set[str]:
        row, column = source_cell
        valid: set[str] = set()
        if self._adjacent_cell(source_cell, "top") not in self._plot_cells and not any(
            other_column == column and other_row < row for other_row, other_column in self._plot_cells
        ):
            valid.add("top")
        if self._adjacent_cell(source_cell, "bottom") not in self._plot_cells and not any(
            other_column == column and other_row > row for other_row, other_column in self._plot_cells
        ):
            valid.add("bottom")
        if self._adjacent_cell(source_cell, "left") not in self._plot_cells and not any(
            other_row == row and other_column < column for other_row, other_column in self._plot_cells
        ):
            valid.add("left")
        if self._adjacent_cell(source_cell, "right") not in self._plot_cells and not any(
            other_row == row and other_column > column for other_row, other_column in self._plot_cells
        ):
            valid.add("right")
        return valid

    @staticmethod
    def _add_button_directions() -> tuple[str, ...]:
        return "top", "left", "right", "bottom"

    def _rebuild_main_title_add_button(self) -> None:
        if self._global_main_title_button is None:
            button = QtWidgets.QPushButton(self._overlay)
            button.setObjectName("add_top_button")
            button.setProperty("iconName", "add_circle")
            button.setFlat(False)
            button.setIconSize(QtCore.QSize(24, 24))
            button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            button.setMenu(self._main_title_add_menu())
            button.setStyleSheet(self._add_button_stylesheet(radius=16))
            apply_icon_color(button)
            self._global_main_title_button = button
        self._position_main_title_add_button()
        self._global_main_title_button.raise_()

    def _position_main_title_add_button(self) -> None:
        if self._global_main_title_button is None:
            return
        button = self._global_main_title_button
        button.setFixedSize(self._overlay.width(), self._GLOBAL_ADD_BUTTON_HEIGHT)
        button.move(0, 0)
        button.setVisible(self._controls_visible and not self._has_main_title())

    def _has_main_title(self) -> bool:
        return any(
            element.kind == "main_title" and element.visible
            for element in self._composition.figure_elements.values()
        )

    def _create_subplot_add_button(
        self,
        source_cell: tuple[int, int],
        direction: str,
        parent: QtWidgets.QWidget,
    ) -> QtWidgets.QPushButton:
        row, column = source_cell
        button = QtWidgets.QPushButton(parent)
        button.setObjectName(f"add_{direction}_button_for_plot_{row}_{column}")
        button.setProperty("addDirection", direction)
        button.setFlat(False)
        button.setFixedSize(self._ADD_BUTTON_SIZE, self._ADD_BUTTON_SIZE)
        button.setIconSize(QtCore.QSize(24, 24))
        button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(self._add_button_stylesheet(radius=14))
        self._configure_add_button(button, direction, source_cell=source_cell)
        self._register_add_button_drop_target(button, source_cell=source_cell, direction=direction)
        apply_icon_color(button)
        self._position_overlay_add_button(button, direction, source_cell)
        button.setVisible(self._controls_visible)
        button.raise_()
        return button

    def _position_overlay_add_button(
        self,
        button: QtWidgets.QPushButton,
        direction: str,
        source_cell: tuple[int, int],
    ) -> None:
        x, y, width, height = self._overlay_rect_from_canvas_rect(
            self._layout_map.plot_rects_px.get(source_cell, (0, 0, 1, 1))
        )
        button_width = button.width()
        button_height = button.height()
        positions = {
            "top": (x + (width - button_width) // 2, y - button_height - 2),
            "left": (x - button_width - 2, y + (height - button_height) // 2),
            "right": (min(x + width + 2, self._overlay.width() - button_width), y + (height - button_height) // 2),
            "bottom": (x + (width - button_width) // 2, min(y + height + 2, self._overlay.height() - button_height)),
        }
        button.move(*positions[direction])

    @staticmethod
    def _add_button_stylesheet(*, radius: int) -> str:
        _ = radius
        return f"""
            QPushButton {{
                padding: 1px;
            }}
            QPushButton::menu-indicator {{
                image: none;
                width: 0px;
            }}
            """

    def _raise_plot_cell_buttons(self, cell: _PlotCell) -> None:
        for button in cell.add_buttons:
            if button.parent() is not self._overlay:
                button.setParent(self._overlay)
            button.setVisible(self._controls_visible)
            self._position_overlay_add_button(button, str(button.property("addDirection")), (cell.row, cell.column))
            button.raise_()

    def _set_add_controls_visible(self, visible: bool) -> None:
        self._controls_visible = visible
        if self._global_main_title_button is not None:
            self._global_main_title_button.setVisible(False)
        for cell in self._plot_cells.values():
            for button in cell.add_buttons:
                button.setVisible(False)

    def _handle_add_button_drop_event(self, widget: QtWidgets.QWidget, event: QtCore.QEvent) -> bool:
        if event.type() == QtCore.QEvent.Type.DragEnter:
            if self._snapshot_path_from_mime(event.mimeData()) is not None:
                event.acceptProposedAction()
                return True
        if event.type() == QtCore.QEvent.Type.Drop:
            path = self._snapshot_path_from_mime(event.mimeData())
            if path is None:
                return False
            source_cell, direction = self._add_button_drop_targets[widget]
            if direction not in self._valid_add_directions(source_cell):
                return False
            self._add_plot_adjacent(source_cell, direction)
            target_cell = self._adjacent_cell(source_cell, direction)
            target = self._plot_cells.get(target_cell)
            if target is not None and self._render_snapshot(path, target=target.drop_target):
                event.acceptProposedAction()
                return True
        return super().eventFilter(widget, event)

    def _populate_preset_controls(self, plot_cell: _PlotCell) -> None:
        if not plot_cell.preset_combos:
            return
        domain_cell = self._composition.plot_cells.get((plot_cell.row, plot_cell.column))
        if domain_cell is None:
            return
        selected_paths = self._selected_preset_paths(domain_cell)
        discovery_path = self._optional_path_text(selected_paths.get("discovery"))
        transformable = bool(
            discovery_path
            and self._projection_supports_arrangement_transform(
                Path(discovery_path)
            )
        )
        entries = {
            "snapshot": self._snapshot_entries(),
            "discovery": self._discovery_entries(),
            "transform": self._preset_entries("transform"),
            "camera": self._preset_entries("camera"),
            "appearance": self._preset_entries("appearance"),
            "filter": self._preset_entries("filters"),
        }
        self._refreshing_preset_controls = True
        try:
            for key, combo in plot_cell.preset_combos.items():
                if not isinstance(combo, QtWidgets.QComboBox):
                    continue
                current = selected_paths.get(key)
                combo.clear()
                if key == "transform":
                    projection = self._safe_dict(domain_cell.render_state.get("snapshot_projection"))
                    frozen_transform = bool(
                        domain_cell.snapshot_path
                        and projection.get("coordinate_space") == "transformed"
                    )
                    matching_preset = bool(
                        current
                        and any(str(path) == str(current) for _label, path in entries[key])
                    )
                    if frozen_transform:
                        combo.addItem(
                            (
                                "Snapshot arrangement"
                                if matching_preset
                                else "Snapshot arrangement (custom)"
                            ),
                            "__snapshot__",
                        )
                    combo.setEnabled(transformable)
                    label = self._widget.findChild(
                        QtWidgets.QLabel,
                        f"label_plot_transform_{plot_cell.row}_{plot_cell.column}",
                    )
                    if label is not None:
                        label.setEnabled(transformable)
                    if not transformable:
                        if not frozen_transform:
                            combo.addItem(
                                "Unavailable for this projection", "__unavailable__"
                            )
                        combo.setCurrentIndex(0)
                        continue
                    combo.addItem("None (Original)", "__original__")
                else:
                    combo.setEnabled(True)
                    combo.addItem("custom", "__custom__")
                for label, path in entries[key]:
                    combo.addItem(label, str(path))
                if key == "transform" and frozen_transform:
                    if matching_preset:
                        index = combo.findData(str(current))
                        combo.setCurrentIndex(index)
                    else:
                        index = combo.findData("__snapshot__")
                        combo.setCurrentIndex(index if index >= 0 else 0)
                elif key == "transform" and not current:
                    index = combo.findData("__original__")
                    combo.setCurrentIndex(index if index >= 0 else 0)
                elif current:
                    index = combo.findData(str(current))
                    combo.setCurrentIndex(index if index >= 0 else 0)
                else:
                    combo.setCurrentIndex(0)
        finally:
            self._refreshing_preset_controls = False

    def _show_preset_controls_for_cell(self, cell: tuple[int, int]) -> None:
        self._active_preset_control_cell = cell
        self._show_settings_button_for_cell(cell)
        for cell_key, plot_cell in self._plot_cells.items():
            panel = plot_cell.preset_panel
            if panel is None:
                continue
            if cell_key == cell:
                self._populate_preset_controls(plot_cell)
                self._position_preset_controls(plot_cell)
                panel.show()
                panel.raise_()
            else:
                panel.hide()

    def _toggle_preset_controls_for_cell(self, cell: tuple[int, int]) -> None:
        plot_cell = self._plot_cells.get(cell)
        if plot_cell is None or plot_cell.preset_panel is None:
            return
        if plot_cell.preset_panel.isVisible():
            plot_cell.preset_panel.hide()
            self._active_preset_control_cell = None
            return
        self._show_preset_controls_for_cell(cell)

    def _show_settings_button_for_cell(self, cell: tuple[int, int]) -> None:
        self._active_settings_button_cell = cell
        for cell_key, plot_cell in self._plot_cells.items():
            button = plot_cell.settings_button
            if button is None:
                continue
            button.setVisible(cell_key == cell)
            if cell_key == cell:
                self._position_settings_button(plot_cell)
                button.raise_()

    def _hide_plot_cell_controls_if_not_hovered(self, cell: tuple[int, int]) -> None:
        plot_cell = self._plot_cells.get(cell)
        if plot_cell is None:
            return
        global_position = QtGui.QCursor.pos()
        local_position = plot_cell.block.mapFromGlobal(global_position)
        if plot_cell.block.rect().contains(local_position):
            return
        if plot_cell.settings_button is not None:
            plot_cell.settings_button.hide()
        if plot_cell.preset_panel is not None:
            plot_cell.preset_panel.hide()
        if self._active_settings_button_cell == cell:
            self._active_settings_button_cell = None
        if self._active_preset_control_cell == cell:
            self._active_preset_control_cell = None

    def _handle_plot_preset_combo_changed(self, cell: tuple[int, int], kind: str) -> None:
        if self._refreshing_preset_controls:
            return
        plot_cell = self._plot_cells.get(cell)
        if plot_cell is None:
            return
        combo = plot_cell.preset_combos.get(kind)
        if combo is None:
            return
        value = combo.currentData()
        if value is None or str(value) in {"__custom__", "__snapshot__"}:
            return
        if kind == "transform" and str(value) == "__original__":
            self._apply_original_projection(cell)
            self._show_preset_controls_for_cell(cell)
            return
        path = Path(str(value))
        if kind == "snapshot":
            self._transform_coordinator.cancel_pending()
            self._pending_transform = None
            self._render_snapshot(path, target=plot_cell.drop_target)
            self._show_preset_controls_for_cell(cell)
            return
        self._apply_component_preset(cell, kind, path)
        self._show_preset_controls_for_cell(cell)

    def _apply_component_preset(self, cell: tuple[int, int], kind: str, path: Path) -> bool:
        plot_cell = self._composition.plot_cells.get(cell)
        if plot_cell is None:
            return False
        if kind == "transform":
            return self._start_figure_transform(cell, path)
        self._transform_coordinator.cancel_pending()
        self._pending_transform = None
        selected_paths = self._selected_preset_paths(plot_cell)
        selected_paths["snapshot"] = None
        selected_paths[kind] = str(path)
        plot_cell.snapshot_path = None
        if kind == "discovery":
            coordinates, metadata_ids = self._load_discovery_coordinates(path)
            if not coordinates:
                self._show_status(f"Discovery '{path.stem}' has no plottable coordinates.")
                return False
            plot_cell.coordinates = list(coordinates)
            plot_cell.metadata_ids = list(metadata_ids)
            plot_cell.metadata = self._metadata_for_ids(metadata_ids)
            selected_paths["transform"] = None
            plot_cell.render_state.pop("transform", None)
            plot_cell.render_state.pop("snapshot_projection", None)
            self._reanchor_plot_pins(plot_cell)
        else:
            payload = self._payload_from_path(path)
            if payload is None:
                self._show_status(f"Could not load {kind} preset '{path.stem}'.")
                return False
            if kind == "camera":
                plot_cell.render_state["camera"] = self._safe_dict(payload.get("camera")) or self._safe_dict(payload)
            elif kind == "appearance":
                plot_cell.render_state["appearance"] = payload
            elif kind == "filter":
                plot_cell.render_state["filters"] = payload
            else:
                return False
        plot_cell.render_state["preset_paths"] = selected_paths
        self._update_subtitle_text_for_cell(cell)
        self._redraw_figure()
        self._show_status(f"Applied {kind} preset '{path.stem}'.")
        state_key = {
            "camera": "camera",
            "appearance": "appearance",
            "filter": "filters",
        }.get(kind)
        if state_key is not None:
            self._notify_plot_edit(
                {
                    "kind": "render_state",
                    "values": {
                        state_key: copy.deepcopy(plot_cell.render_state[state_key]),
                        "preset_paths": copy.deepcopy(selected_paths),
                    },
                }
            )
        return True

    def _start_figure_transform(self, cell: tuple[int, int], path: Path) -> bool:
        plot_cell = self._composition.plot_cells.get(cell)
        if plot_cell is None:
            return False
        payload = self._payload_from_path(path)
        if payload is None:
            self._show_status(f"Could not load transformation preset '{path.stem}'.")
            return False
        try:
            preset_version = int(payload.get("preset_version", 1))
        except (TypeError, ValueError):
            preset_version = -1
        if preset_version not in {1, 2}:
            self._show_status(f"Unsupported transformation preset '{path.stem}'.")
            return False
        selected_paths = self._selected_preset_paths(plot_cell)
        source_path_text = selected_paths.get("discovery")
        if not source_path_text:
            self._show_status("Select a Projection before applying a Transformation.")
            return False
        if not self._projection_supports_arrangement_transform(
            Path(source_path_text)
        ):
            self._show_status(
                "Transformations are unavailable for this coordinate-truthful projection."
            )
            return False
        coordinates, metadata_ids = self._load_discovery_coordinates(Path(source_path_text))
        if not coordinates:
            self._show_status("The selected Projection has no plottable coordinates.")
            return False
        try:
            settings = TransformSettings.from_preset_dict(payload.get("transform"))
        except (TypeError, ValueError, OverflowError) as exc:
            self._show_status(f"Could not apply transformation preset '{path.stem}': {exc}")
            return False
        selected_paths["snapshot"] = None
        selected_paths["transform"] = str(path)
        projection_fingerprint = source_fingerprint(
            np.asarray(coordinates, dtype=np.float32),
            np.asarray(metadata_ids, dtype=np.int64),
        )
        cache_key = (
            projection_fingerprint,
            settings.snap_percent,
            settings.grid_aspect_preset,
            round(settings.minimum_spacing, 8),
        )
        cached = self._transform_cache.get(cache_key)
        if cached is not None:
            self._transform_coordinator.cancel_pending()
            self._pending_transform = None
            self._transform_cache.move_to_end(cache_key)
            self._commit_figure_transform(
                cell, path, selected_paths, settings, cached
            )
            return True
        revision = self._transform_coordinator.submit(
            np.asarray(coordinates, dtype=np.float32),
            np.asarray(metadata_ids, dtype=np.int64),
            settings,
        )
        self._pending_transform = {
            "revision": revision,
            "cell": cell,
            "path": path,
            "source": str(source_path_text),
            "selected_paths": selected_paths,
            "settings": settings,
            "cache_key": cache_key,
        }
        self._show_status(f"Applying transformation preset '{path.stem}'…")
        return True

    @QtCore.Slot(int, object)
    def _figure_transform_ready(self, revision: int, payload: object) -> None:
        pending = self._pending_transform
        if (
            pending is None
            or int(pending.get("revision", -1)) != revision
            or not isinstance(payload, TransformResult)
        ):
            return
        cell = pending["cell"]
        selected_paths = dict(pending["selected_paths"])
        settings = pending["settings"]
        path = Path(pending["path"])
        cache_key = pending.get("cache_key")
        if isinstance(cache_key, tuple):
            self._transform_cache[cache_key] = payload
            self._transform_cache.move_to_end(cache_key)
            while len(self._transform_cache) > 8:
                self._transform_cache.popitem(last=False)
        self._pending_transform = None
        self._commit_figure_transform(cell, path, selected_paths, settings, payload)

    def _commit_figure_transform(
        self,
        cell: tuple[int, int],
        path: Path,
        selected_paths: dict[str, str | None],
        settings: TransformSettings,
        payload: TransformResult,
    ) -> None:
        plot_cell = self._composition.plot_cells.get(cell)
        if plot_cell is None:
            return
        plot_cell.snapshot_path = None
        plot_cell.coordinates = [tuple(map(float, point)) for point in payload.coordinates]
        plot_cell.metadata_ids = [int(value) for value in payload.metadata_ids]
        plot_cell.metadata = self._metadata_for_ids(plot_cell.metadata_ids)
        plot_cell.render_state["transform"] = {
            "preset_version": 2,
            "transform": settings.to_preset_dict(),
        }
        plot_cell.render_state.pop("snapshot_projection", None)
        plot_cell.render_state["preset_paths"] = selected_paths
        self._reanchor_plot_pins(plot_cell)
        self._update_subtitle_text_for_cell(cell)
        self._redraw_figure()
        self._show_status(f"Applied transformation preset '{path.stem}'.")
        self._notify_plot_edit(
            {
                "kind": "render_state",
                "values": {
                    "transform": copy.deepcopy(plot_cell.render_state["transform"]),
                    "preset_paths": copy.deepcopy(selected_paths),
                },
            }
        )

    @QtCore.Slot(int, str)
    def _figure_transform_failed(self, revision: int, message: str) -> None:
        pending = self._pending_transform
        if pending is None or int(pending.get("revision", -1)) != revision:
            return
        self._pending_transform = None
        self._show_status(f"Transformation failed: {message}")
        cell = pending.get("cell")
        if isinstance(cell, tuple):
            self._show_preset_controls_for_cell(cell)

    @QtCore.Slot(int)
    def _figure_transform_cancelled(self, revision: int) -> None:
        pending = self._pending_transform
        if pending is not None and int(pending.get("revision", -1)) == revision:
            self._pending_transform = None

    def _selected_preset_paths(self, plot_cell: Any) -> dict[str, str | None]:
        state = self._safe_dict(plot_cell.render_state)
        paths = self._safe_dict(state.get("preset_paths"))
        return {
            "snapshot": str(plot_cell.snapshot_path) if plot_cell.snapshot_path else self._optional_path_text(paths.get("snapshot")),
            "discovery": self._optional_path_text(paths.get("discovery")),
            "transform": self._optional_path_text(paths.get("transform")),
            "camera": self._optional_path_text(paths.get("camera")),
            "appearance": self._optional_path_text(paths.get("appearance")),
            "filter": self._optional_path_text(paths.get("filter")),
        }

    @staticmethod
    def _optional_path_text(value: object) -> str | None:
        text = str(value or "").strip()
        return text or None

    def _snapshot_entries(self) -> list[tuple[str, Path]]:
        return self._preset_entries("snapshots")

    def _preset_entries(self, preset_folder: str) -> list[tuple[str, Path]]:
        if self._project_root is None:
            return []
        root = self._project_root / "presets" / preset_folder
        if not root.exists():
            return []
        return [(entry.label, entry.path) for entry in PresetRepository(root).list_entries()]

    def _discovery_entries(self) -> list[tuple[str, Path]]:
        if self._project_root is None:
            return []
        root = self._project_root / "discovery"
        if not root.exists():
            return []
        entries: list[tuple[str, Path]] = []
        for path in sorted(root.glob("*.sqlite"), key=lambda item: item.name.lower()):
            entries.append((path.stem, path))
        return entries

    def _render_snapshot(
        self,
        snapshot_path: Path,
        *,
        target: QtWidgets.QFrame | None = None,
        announce: bool = True,
        plot_overrides: dict[str, Any] | None = None,
        show_activity: bool = True,
    ) -> bool:
        if show_activity and self._activity_manager is not None:
            with self._activity_manager.busy("Loading snapshot into figure…"):
                return self._render_snapshot(
                    snapshot_path,
                    target=target,
                    announce=announce,
                    plot_overrides=plot_overrides,
                    show_activity=False,
                )
        try:
            prepared = self._prepare_snapshot_plot_cell(snapshot_path)
        except ValueError as error:
            self._show_status(str(error))
            return False
        cell_key = self._cell_key_for_target(target or self._plot_area)
        if cell_key is None:
            cell_key = (0, 0)
        self._apply_plot_overrides(prepared, plot_overrides)
        plot_cell = self._composition.plot_cells[cell_key]
        self._apply_prepared_snapshot(plot_cell, prepared)
        self._update_subtitle_text_for_cell(cell_key)
        self._redraw_figure(show_activity=show_activity)
        if announce:
            self._show_status(f"Rendered snapshot '{snapshot_path.stem}'.")
        return True

    def _prepare_snapshot_plot_cell(self, snapshot_path: Path) -> PlotCell:
        """Resolve one snapshot into renderer-ready data for preview or export."""

        payload = self._snapshot_payload(snapshot_path)
        coordinates, metadata_ids = self._coordinates_for_snapshot_payload(payload)
        if not coordinates:
            raise ValueError(
                f"Snapshot '{snapshot_path.stem}' has no plottable coordinates."
            )
        states = payload.get("states") if isinstance(payload.get("states"), dict) else {}
        state = self._snapshot_render_state(payload)
        if self._project is not None:
            from DiscoveryArea.snapshot_provenance import SnapshotMethodsService
            state["methods_report"] = SnapshotMethodsService().load(self._project, snapshot_path).to_payload()
        state["pinboard"] = self._snapshot_pinboard_state(
            states, metadata_ids=metadata_ids
        )
        state["figure_options"] = {
            "pins_visible": self._default_snapshot_pins_visible(
                self._safe_dict(state.get("layers")),
                self._safe_dict(state.get("pinboard")),
            )
        }
        state["preset_paths"] = self._snapshot_preset_paths(payload, snapshot_path)
        return PlotCell(
            row=0,
            column=0,
            snapshot_path=str(snapshot_path),
            snapshot_label=self._snapshot_label(payload, snapshot_path),
            coordinates=list(coordinates),
            metadata_ids=list(metadata_ids),
            metadata=self._metadata_for_ids(metadata_ids),
            render_state=state,
        )

    def _prepare_slicer_animation_plot_cell(
        self,
        frame: SlicerAnimationFrame,
        base_snapshot_path: Path,
    ) -> PlotCell:
        prepared = self._prepare_snapshot_plot_cell(base_snapshot_path)
        prepared.snapshot_label = frame.label
        prepared.render_state["slicer_selection"] = {
            "column": frame.column,
            "values": list(frame.active_values),
        }
        return prepared

    @staticmethod
    def _apply_prepared_snapshot(target: PlotCell, source: PlotCell) -> None:
        target.snapshot_path = source.snapshot_path
        target.snapshot_label = source.snapshot_label
        target.coordinates = list(source.coordinates)
        target.metadata_ids = list(source.metadata_ids)
        target.metadata = dict(source.metadata)
        target.render_state = dict(source.render_state)

    def _apply_plot_overrides(
        self, plot_cell: PlotCell, overrides: dict[str, Any] | None
    ) -> None:
        if not isinstance(overrides, dict):
            return
        state_overrides = overrides.get("state")
        if isinstance(state_overrides, dict):
            for key, value in state_overrides.items():
                if key != "pinboard":
                    plot_cell.render_state[str(key)] = copy.deepcopy(value)
            if "transform" in state_overrides:
                self._apply_transform_state_to_plot_cell(plot_cell)
        pin_overrides = overrides.get("pins")
        if not isinstance(pin_overrides, dict):
            return
        pinboard = plot_cell.render_state.get("pinboard")
        if not isinstance(pinboard, dict):
            return
        annotations = pinboard.get("annotations")
        if not isinstance(annotations, list):
            return
        updated: list[dict[str, Any]] = []
        for annotation in annotations:
            if not isinstance(annotation, dict):
                continue
            annotation_id = str(annotation.get("annotation_id") or "")
            pin_override = pin_overrides.get(annotation_id)
            if not isinstance(pin_override, dict):
                updated.append(annotation)
                continue
            if bool(pin_override.get("removed", False)):
                continue
            merged = dict(annotation)
            for key in ("box_anchor", "screen_offset"):
                value = pin_override.get(key)
                if isinstance(value, list) and len(value) == 2:
                    merged[key] = [float(value[0]), float(value[1])]
            updated.append(merged)
        pinboard["annotations"] = updated

    def _apply_transform_state_to_plot_cell(self, plot_cell: PlotCell) -> bool:
        paths = self._safe_dict(plot_cell.render_state.get("preset_paths"))
        source_text = self._optional_path_text(paths.get("discovery"))
        if source_text is None:
            return False
        coordinates, metadata_ids = self._load_discovery_coordinates(Path(source_text))
        if not coordinates:
            return False
        if not self._projection_supports_arrangement_transform(Path(source_text)):
            plot_cell.coordinates = list(coordinates)
            plot_cell.metadata_ids = list(metadata_ids)
            plot_cell.metadata = self._metadata_for_ids(metadata_ids)
            plot_cell.render_state.pop("transform", None)
            plot_cell.render_state.pop("snapshot_projection", None)
            self._reanchor_plot_pins(plot_cell)
            return True
        transform_state = plot_cell.render_state.get("transform")
        if not isinstance(transform_state, dict):
            plot_cell.coordinates = list(coordinates)
            plot_cell.metadata_ids = list(metadata_ids)
            plot_cell.metadata = self._metadata_for_ids(metadata_ids)
            plot_cell.render_state.pop("snapshot_projection", None)
            self._reanchor_plot_pins(plot_cell)
            return True
        try:
            recipe = transform_state.get("transform", transform_state)
            settings = TransformSettings.from_preset_dict(recipe)
            projection_fingerprint = source_fingerprint(
                np.asarray(coordinates, dtype=np.float32),
                np.asarray(metadata_ids, dtype=np.int64),
            )
            cache_key = (
                projection_fingerprint,
                settings.snap_percent,
                settings.grid_aspect_preset,
                round(settings.minimum_spacing, 8),
            )
            result = self._transform_cache.get(cache_key)
            if result is None:
                result = transform_coordinates(
                    np.asarray(coordinates, dtype=np.float32),
                    np.asarray(metadata_ids, dtype=np.int64),
                    settings,
                )
                self._transform_cache[cache_key] = result
                while len(self._transform_cache) > 8:
                    self._transform_cache.popitem(last=False)
            else:
                self._transform_cache.move_to_end(cache_key)
        except (TypeError, ValueError, OverflowError):
            return False
        plot_cell.coordinates = [tuple(map(float, point)) for point in result.coordinates]
        plot_cell.metadata_ids = [int(value) for value in result.metadata_ids]
        plot_cell.metadata = self._metadata_for_ids(plot_cell.metadata_ids)
        plot_cell.render_state.pop("snapshot_projection", None)
        self._reanchor_plot_pins(plot_cell)
        return True

    def _projection_supports_arrangement_transform(
        self, discovery_path: Path
    ) -> bool:
        method_id = self._projection_method_id(discovery_path)
        if method_id == "t-sne":
            method_id = "tsne"
        definition = self._discovery_method_catalog.get(method_id)
        return bool(
            definition is not None and definition.supports_arrangement_transform
        )

    def _projection_method_id(self, discovery_path: Path) -> str:
        if self._project_root is None:
            return ""
        target = Path(discovery_path).resolve()
        presets_root = self._project_root / "presets" / "discovery"
        for preset_path in sorted(presets_root.glob("*.json")):
            payload = self._payload_from_path(preset_path)
            if payload is None:
                continue
            value = payload.get("discovery_file")
            if not value:
                continue
            candidate = Path(str(value))
            if not candidate.is_absolute():
                candidate = self._project_root / candidate
            if candidate.resolve() != target:
                continue
            return str(
                payload.get("method_id")
                or payload.get("type")
                or payload.get("algorithm")
                or ""
            ).strip().lower()
        if target.name == "random_discovery.sqlite":
            return "random"
        return ""

    def _cell_key_for_target(self, target: QtWidgets.QFrame) -> tuple[int, int] | None:
        for cell_key, cell in self._plot_cells.items():
            if target is cell.drop_target or target is cell.block:
                return cell_key
        return self._drop_targets.get(target)

    def _show_plot_cell_context_menu(self, widget: QtWidgets.QWidget, event: QtCore.QEvent) -> None:
        cell = self._context_menu_targets.get(widget)
        if cell is None:
            return
        menu = self._build_plot_cell_context_menu(cell)
        self._popup_context_menu(menu, widget, event)

    def _build_plot_cell_context_menu(self, cell: tuple[int, int]) -> QtWidgets.QMenu:
        menu = QtWidgets.QMenu(self._widget)
        clear_action = menu.addAction("Clear Plot")
        clear_action.triggered.connect(lambda checked=False, c=cell: self._clear_plot_cell(c))
        return menu

    def _show_pin_context_menu(
        self,
        widget: QtWidgets.QWidget,
        event: QtCore.QEvent,
        cell: tuple[int, int],
        annotation_id: str,
    ) -> None:
        menu = self._build_pin_context_menu(cell, annotation_id)
        self._popup_context_menu(menu, widget, event)

    def _build_pin_context_menu(self, cell: tuple[int, int], annotation_id: str) -> QtWidgets.QMenu:
        menu = QtWidgets.QMenu(self._widget)
        remove_action = menu.addAction("Remove Pin")
        remove_action.triggered.connect(
            lambda checked=False, c=cell, a=annotation_id: self._remove_plot_pin(c, a)
        )
        return menu

    def _remove_plot_pin(self, cell: tuple[int, int], annotation_id: str) -> bool:
        plot_cell = self._composition.plot_cells.get(cell)
        if plot_cell is None:
            return False
        pinboard = self._safe_dict(plot_cell.render_state.get("pinboard"))
        annotations = pinboard.get("annotations")
        if not isinstance(annotations, list):
            return False
        kept_annotations = [
            annotation
            for annotation in annotations
            if not isinstance(annotation, dict) or str(annotation.get("annotation_id") or "") != annotation_id
        ]
        if len(kept_annotations) == len(annotations):
            return False
        pinboard["annotations"] = kept_annotations
        plot_cell.render_state["pinboard"] = pinboard
        self._set_pin_hover_rect(None)
        self._redraw_figure()
        self._show_status("Removed pin.")
        self._notify_plot_edit(
            {
                "kind": "pin",
                "annotation_id": annotation_id,
                "values": {"removed": True},
            }
        )
        return True

    def _clear_plot_cell(self, cell: tuple[int, int]) -> bool:
        plot_cell = self._composition.plot_cells.get(cell)
        if plot_cell is None:
            return False
        plot_cell.snapshot_path = None
        plot_cell.snapshot_label = None
        plot_cell.coordinates.clear()
        plot_cell.metadata_ids.clear()
        plot_cell.metadata.clear()
        plot_cell.render_state.clear()
        self._update_subtitle_text_for_cell(cell)
        self._redraw_figure()
        self._show_status(f"Cleared plot area at row {cell[0]}, column {cell[1]}.")
        return True

    def _update_subtitle_text_for_cell(
        self,
        cell: tuple[int, int],
        *,
        composition: FigureComposition | None = None,
    ) -> None:
        target = composition or self._composition
        element = target.figure_elements.get(f"plot_title_settings_{cell[0]}_{cell[1]}")
        if element is not None and element.auto_text:
            element.text = self._default_subtitle_text(cell, composition=target)

    def _default_subtitle_text(
        self,
        cell: tuple[int, int],
        *,
        composition: FigureComposition | None = None,
    ) -> str:
        plot_cell = (composition or self._composition).plot_cells.get(cell)
        if plot_cell is not None and plot_cell.snapshot_label:
            return plot_cell.snapshot_label
        return f"Plot {cell[0] + 1}, {cell[1] + 1}"

    @staticmethod
    def _snapshot_label(payload: dict[str, Any], snapshot_path: Path) -> str:
        for key in ("label", "name"):
            value = payload.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return snapshot_path.stem

    def _add_figure_element(
        self,
        kind: FigureElementKind,
        direction: FigureElementDirection,
        source_cell: tuple[int, int] | None,
    ) -> FigureElement:
        if kind == "main_title":
            source_cell = None
            existing = self._existing_main_title(direction)
            if existing is not None:
                self._select_element(existing.element_id)
                self._show_status(f"Selected existing main title on {direction}.")
                return existing
        elif source_cell not in self._plot_cells:
            raise KeyError(f"Unknown source plot cell: {source_cell}")
        element_id = self._composition.next_element_id(kind)
        element = FigureElement(
            element_id=element_id,
            kind=kind,
            direction=direction,
            source_cell=source_cell,
            text=self._default_element_text(kind),
            font_size=14.0 if kind == "main_title" else 10.0,
            order=len(self._composition.figure_elements),
        )
        self._composition.figure_elements[element_id] = element
        if source_cell is not None:
            self._composition.plot_cells[source_cell].element_ids.append(element_id)
        self._redraw_figure()
        self._select_element(element_id)
        if self._element_can_edit_text(element):
            self._edit_figure_element_text(element_id)
        self._show_status(f"Added {kind.replace('_', ' ')} to {direction}.")
        return element

    def _existing_main_title(self, direction: str) -> FigureElement | None:
        for element in self._composition.figure_elements.values():
            if element.kind == "main_title" and element.direction == direction and element.source_cell is None:
                return element
        return None

    def _sync_element_overlay_geometry(self) -> None:
        for element_id, element in self._composition.figure_elements.items():
            if not element.visible:
                continue
            zone = self._element_widgets.get(element_id)
            if zone is None:
                zone = QtWidgets.QFrame(self._overlay)
                zone.setObjectName(f"figure_element_{element_id}")
                zone.setProperty("figureElementId", element_id)
                zone.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
                zone.setStyleSheet("background: transparent;")
                zone.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.DefaultContextMenu)
                zone.installEventFilter(self)
                self._element_context_menu_targets[zone] = element_id
                self._element_widgets[element_id] = zone
            zone.setGeometry(*self._overlay_rect_from_canvas_rect(self._layout_map.element_rects_px.get(element_id, (0, 0, 1, 1))))
            zone.show()
            zone.raise_()
        for element_id, zone in list(self._element_widgets.items()):
            element = self._composition.figure_elements.get(element_id)
            if element is not None and element.visible and element_id in self._layout_map.element_rects_px:
                continue
            self._element_context_menu_targets.pop(zone, None)
            self._element_widgets.pop(element_id, None)
            zone.setParent(None)
            zone.deleteLater()

    def _show_element_context_menu(self, widget: QtWidgets.QWidget, event: QtCore.QEvent) -> None:
        element_id = self._element_context_menu_targets.get(widget)
        if element_id is None:
            return
        self._select_element(element_id)
        menu = self._build_element_context_menu(widget, element_id)
        if menu is None:
            return
        self._popup_context_menu(menu, widget, event)

    def _build_element_context_menu(self, parent: QtWidgets.QWidget, element_id: str) -> QtWidgets.QMenu | None:
        element = self._composition.figure_elements.get(element_id)
        if element is None:
            return None
        menu = QtWidgets.QMenu(self._widget)
        if self._element_can_edit_text(element):
            edit_action = menu.addAction(f"Edit {self._element_menu_label(element).title()}…")
            edit_action.triggered.connect(lambda checked=False, e=element_id: self._edit_figure_element_text(e))
            menu.addSeparator()
        remove_action = menu.addAction(f"Remove {self._element_menu_label(element).title()}")
        remove_action.triggered.connect(lambda checked=False, e=element_id: self._remove_figure_element(e))
        return menu

    def _popup_context_menu(self, menu: QtWidgets.QMenu, widget: QtWidgets.QWidget, event: QtCore.QEvent) -> None:
        self._active_context_menu = menu
        menu.aboutToHide.connect(self._clear_active_context_menu)
        global_position = event.globalPos() if hasattr(event, "globalPos") else widget.mapToGlobal(QtCore.QPoint(0, widget.height()))
        menu.popup(global_position)

    def _clear_active_context_menu(self) -> None:
        self._active_context_menu = None

    def _edit_figure_element_text(self, element_id: str) -> bool:
        element = self._composition.figure_elements.get(element_id)
        if element is None or not self._element_can_edit_text(element):
            return False
        if element.kind == "caption":
            return self._edit_caption_element_text(element_id)
        text, accepted = QtWidgets.QInputDialog.getText(
            self._widget,
            f"Edit {self._element_menu_label(element)}",
            f"{self._element_menu_label(element).capitalize()} text:",
            QtWidgets.QLineEdit.EchoMode.Normal,
            element.text,
        )
        if not accepted:
            return False
        element.text = str(text)
        if element.kind == "plot_title":
            element.auto_text = False
        self._redraw_figure()
        self._select_element(element_id)
        self._show_status(f"Updated {self._element_menu_label(element)}.")
        return True

    def _snapshot_methods_caption(self, source_cell=None) -> str:
        from shared.methods_report import MethodsReport, combine_reports
        reports, seen = [], set()
        cells = self._composition.plot_cells
        selected = [cells[source_cell]] if source_cell in cells else list(cells.values())
        for cell in selected:
            payload = cell.render_state.get("methods_report")
            if not payload and cell.snapshot_path and self._project is not None:
                from DiscoveryArea.snapshot_provenance import SnapshotMethodsService
                try:
                    payload = SnapshotMethodsService().load(self._project, Path(cell.snapshot_path)).to_payload()
                except (OSError, ValueError):
                    continue
            if not payload:
                continue
            identity = json.dumps(payload, sort_keys=True)
            if identity in seen:
                continue
            seen.add(identity)
            reports.append(MethodsReport.from_payload(payload))
        if not reports:
            return ""
        return combine_reports("Snapshot methods", reports, warnings=(
            "These methods describe the source snapshots; review any subsequent figure-specific edits.",
        )).caption_text()

    def _edit_caption_element_text(self, element_id: str) -> bool:
        element = self._composition.figure_elements.get(element_id)
        if element is None:
            return False
        dialog = load_ui("FiguresArea", "ui/caption_text_dialog.ui", None)
        if not isinstance(dialog, QtWidgets.QDialog):
            return False
        dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        editor = require_child(dialog, QtWidgets.QTextEdit, "text_caption_editor")
        bold_button = require_child(dialog, QtWidgets.QToolButton, "button_caption_bold")
        italic_button = require_child(dialog, QtWidgets.QToolButton, "button_caption_italic")
        underline_button = require_child(dialog, QtWidgets.QToolButton, "button_caption_underline")
        if element.rich_text:
            editor.setHtml(element.rich_text)
        else:
            editor.setPlainText(element.text)

        def merge_format(text_format: QtGui.QTextCharFormat) -> None:
            cursor = editor.textCursor()
            if not cursor.hasSelection():
                cursor.select(QtGui.QTextCursor.SelectionType.WordUnderCursor)
            cursor.mergeCharFormat(text_format)
            editor.mergeCurrentCharFormat(text_format)

        def set_bold(enabled: bool) -> None:
            text_format = QtGui.QTextCharFormat()
            text_format.setFontWeight(QtGui.QFont.Weight.Bold if enabled else QtGui.QFont.Weight.Normal)
            merge_format(text_format)

        def set_italic(enabled: bool) -> None:
            text_format = QtGui.QTextCharFormat()
            text_format.setFontItalic(enabled)
            merge_format(text_format)

        def set_underline(enabled: bool) -> None:
            text_format = QtGui.QTextCharFormat()
            text_format.setFontUnderline(enabled)
            merge_format(text_format)

        insert_button = require_child(dialog, QtWidgets.QPushButton, "button_insert_snapshot_caption")
        caption = self._snapshot_methods_caption(element.source_cell)
        insert_button.setEnabled(bool(caption))

        def insert_methods() -> None:
            editor.moveCursor(QtGui.QTextCursor.MoveOperation.End)
            editor.insertPlainText(("\n\n" if editor.toPlainText().strip() else "") + caption)

        connections = [(bold_button.toggled, set_bold), (italic_button.toggled, set_italic),
                       (underline_button.toggled, set_underline), (insert_button.clicked, insert_methods)]
        for signal, slot in connections:
            signal.connect(slot)
        try:
            accepted = dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted
        finally:
            for signal, slot in connections:
                signal.disconnect(slot)
        if not accepted:
            return False
        element.rich_text = editor.toHtml()
        element.text = editor.toPlainText().strip()
        self._redraw_figure()
        self._select_element(element_id)
        self._show_status("Updated caption.")
        return True

    def _remove_figure_element(self, element_id: str) -> bool:
        element = self._composition.figure_elements.pop(element_id, None)
        zone = self._element_widgets.pop(element_id, None)
        if zone is not None:
            self._element_context_menu_targets.pop(zone, None)
            zone.setParent(None)
            zone.deleteLater()
        if element is not None and element.source_cell is not None:
            cell = self._composition.plot_cells.get(element.source_cell)
            if cell is not None and element_id in cell.element_ids:
                cell.element_ids.remove(element_id)
        if self._composition.selected_element_id == element_id:
            self._composition.selected_element_id = None
        self._redraw_figure()
        return element is not None

    def _remove_elements_for_plot_cell(self, cell: tuple[int, int]) -> None:
        plot_cell = self._composition.plot_cells.get(cell)
        if plot_cell is None:
            return
        for element_id in list(plot_cell.element_ids):
            self._remove_figure_element(element_id)

    def _select_element(self, element_id: str) -> None:
        self._composition.selected_element_id = element_id
        element = self._composition.figure_elements.get(element_id)
        if element is not None and self._selection_callback is not None:
            self._selection_callback(element.kind)

    def _select_plot_cell(self, cell: tuple[int, int]) -> None:
        if cell in self._plot_cells and self._selection_callback is not None:
            self._selection_callback("figure")

    def _overlay_rect_from_canvas_rect(self, rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        x, y, width, height = rect
        scale = max(float(self._preview_scale), self._MIN_PREVIEW_SCALE)
        return (
            int(round(x * scale)) + self._CONTROL_MARGIN,
            int(round(y * scale)) + self._CONTROL_MARGIN,
            max(int(round(width * scale)), 1),
            max(int(round(height * scale)), 1),
        )

    def _handle_pin_interaction_event(self, widget: QtWidgets.QWidget, event: QtCore.QEvent) -> bool:
        if self._pin_drag is not None:
            return self._handle_pin_drag_event(widget, event)
        if event.type() not in {
            QtCore.QEvent.Type.ContextMenu,
            QtCore.QEvent.Type.MouseMove,
            QtCore.QEvent.Type.MouseButtonPress,
            QtCore.QEvent.Type.MouseButtonRelease,
        }:
            return False
        if isinstance(event, QtGui.QContextMenuEvent):
            overlay_pos = widget.mapTo(self._overlay, event.pos())
            hit = self._pin_hit_at_overlay_pos(overlay_pos)
            if hit is None:
                return False
            self._show_pin_context_menu(widget, event, hit[0], hit[1])
            return True
        if not isinstance(event, QtGui.QMouseEvent):
            return False
        overlay_pos = widget.mapTo(self._overlay, event.position().toPoint())
        hit = self._pin_hit_at_overlay_pos(overlay_pos)
        if event.type() == QtCore.QEvent.Type.MouseMove:
            widget.setCursor(QtCore.Qt.CursorShape.SizeAllCursor if hit is not None else QtCore.Qt.CursorShape.ArrowCursor)
            self._set_pin_hover_rect(hit[2] if hit is not None else None)
            return False
        if event.type() == QtCore.QEvent.Type.MouseButtonPress and event.button() == QtCore.Qt.MouseButton.LeftButton and hit is not None:
            self._start_pin_drag(hit[0], hit[1], hit[2], overlay_pos)
            return True
        return False

    def _handle_pin_drag_event(self, widget: QtWidgets.QWidget, event: QtCore.QEvent) -> bool:
        if self._pin_drag is None:
            return False
        if event.type() not in {QtCore.QEvent.Type.MouseMove, QtCore.QEvent.Type.MouseButtonRelease}:
            return False
        if not isinstance(event, QtGui.QMouseEvent):
            return False
        overlay_pos = widget.mapTo(self._overlay, event.position().toPoint())
        delta = overlay_pos - self._pin_drag.start_overlay_pos
        if self._pin_drag_frame is not None:
            self._pin_drag_frame.setGeometry(self._pin_drag.start_rect.translated(delta))
            self._pin_drag_frame.raise_()
        if event.type() == QtCore.QEvent.Type.MouseButtonRelease and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._finish_pin_drag(delta)
        return True

    def _pin_hit_at_overlay_pos(self, overlay_pos: QtCore.QPoint) -> tuple[tuple[int, int], str, QtCore.QRect] | None:
        for cell, annotations in sorted(self._layout_map.pin_rects_px.items(), reverse=True):
            for annotation_id, rect in reversed(list(annotations.items())):
                overlay_rect = QtCore.QRect(*self._overlay_rect_from_canvas_rect(rect))
                if overlay_rect.contains(overlay_pos):
                    return cell, annotation_id, overlay_rect
        return None

    def _set_pin_hover_rect(self, rect: QtCore.QRect | None) -> None:
        if rect is None:
            if self._pin_hover_frame is not None:
                self._pin_hover_frame.hide()
            return
        if self._pin_hover_frame is None:
            frame = QtWidgets.QFrame(self._overlay)
            frame.setObjectName("pin_hover_rect")
            frame.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            frame.setStyleSheet("background: rgba(47, 111, 237, 18); border: 1px solid palette(highlight);")
            self._pin_hover_frame = frame
        self._pin_hover_frame.setGeometry(rect)
        self._pin_hover_frame.show()
        self._pin_hover_frame.raise_()

    def _start_pin_drag(
        self,
        cell: tuple[int, int],
        annotation_id: str,
        rect: QtCore.QRect,
        overlay_pos: QtCore.QPoint,
    ) -> None:
        geometry = self._layout_map.pin_geometries.get(cell, {}).get(annotation_id)
        if geometry is None:
            return
        mouse_data = self._overlay_pos_to_data(cell, overlay_pos, geometry.xlim, geometry.ylim)
        if mouse_data is None:
            return
        self._pin_drag = _PinDragState(
            cell=cell,
            annotation_id=annotation_id,
            start_overlay_pos=QtCore.QPoint(overlay_pos),
            start_rect=QtCore.QRect(rect),
            start_mouse_data=mouse_data,
            start_box_anchor_data=geometry.box_anchor_data,
            point_data=geometry.point_data,
        )
        self._set_pin_hover_rect(None)
        frame = QtWidgets.QFrame(self._overlay)
        frame.setObjectName("pin_drag_preview_rect")
        frame.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        frame.setStyleSheet("background: rgba(47, 111, 237, 24); border: 1px dashed palette(highlight);")
        frame.setGeometry(rect)
        frame.show()
        frame.raise_()
        self._pin_drag_frame = frame
        self._overlay.setCursor(QtCore.Qt.CursorShape.SizeAllCursor)
        self._overlay.grabMouse()

    def _finish_pin_drag(self, overlay_delta: QtCore.QPoint) -> None:
        drag = self._pin_drag
        if drag is None:
            return
        try:
            release_overlay_pos = drag.start_overlay_pos + overlay_delta
            geometry = self._layout_map.pin_geometries.get(drag.cell, {}).get(drag.annotation_id)
            if geometry is None:
                return
            release_data = self._overlay_pos_to_data(drag.cell, release_overlay_pos, geometry.xlim, geometry.ylim)
            if release_data is None:
                return
            box_anchor = (
                float(drag.start_box_anchor_data[0]) + (float(release_data[0]) - float(drag.start_mouse_data[0])),
                float(drag.start_box_anchor_data[1]) + (float(release_data[1]) - float(drag.start_mouse_data[1])),
            )
            if self._set_pin_box_anchor(drag.cell, drag.annotation_id, box_anchor, point_data=drag.point_data):
                plot_cell = self._composition.plot_cells.get(drag.cell)
                pinboard = (
                    self._safe_dict(plot_cell.render_state.get("pinboard"))
                    if plot_cell is not None
                    else {}
                )
                annotation = next(
                    (
                        item
                        for item in pinboard.get("annotations", [])
                        if isinstance(item, dict)
                        and str(item.get("annotation_id") or "")
                        == drag.annotation_id
                    ),
                    {},
                )
                self._redraw_figure()
                self._show_status("Moved pin.")
                self._notify_plot_edit(
                    {
                        "kind": "pin",
                        "annotation_id": drag.annotation_id,
                        "values": {
                            "box_anchor": list(annotation.get("box_anchor", [])),
                            "screen_offset": list(annotation.get("screen_offset", [])),
                        },
                    }
                )
        finally:
            if self._pin_drag_frame is not None:
                self._pin_drag_frame.setParent(None)
                self._pin_drag_frame.deleteLater()
                self._pin_drag_frame = None
            self._pin_drag = None
            self._overlay.releaseMouse()
            self._overlay.unsetCursor()

    def _notify_plot_edit(self, edit: dict[str, Any]) -> None:
        if self._plot_edit_callback is not None:
            self._plot_edit_callback(copy.deepcopy(edit))

    def _overlay_pos_to_data(
        self,
        cell: tuple[int, int],
        overlay_pos: QtCore.QPoint,
        xlim: tuple[float, float],
        ylim: tuple[float, float],
    ) -> tuple[float, float] | None:
        plot_rect = self._layout_map.plot_rects_px.get(cell)
        if plot_rect is None:
            return None
        scale = max(float(self._preview_scale), self._MIN_PREVIEW_SCALE)
        canvas_x = (float(overlay_pos.x()) - self._CONTROL_MARGIN) / scale
        canvas_y = (float(overlay_pos.y()) - self._CONTROL_MARGIN) / scale
        plot_x, plot_y, plot_width, plot_height = plot_rect
        if plot_width <= 0 or plot_height <= 0:
            return None
        x_fraction = (canvas_x - float(plot_x)) / float(plot_width)
        y_fraction = (canvas_y - float(plot_y)) / float(plot_height)
        data_x = float(xlim[0]) + (x_fraction * (float(xlim[1]) - float(xlim[0])))
        data_y = float(ylim[1]) - (y_fraction * (float(ylim[1]) - float(ylim[0])))
        if not all(math.isfinite(value) for value in (data_x, data_y)):
            return None
        return data_x, data_y

    def _set_pin_box_anchor(
        self,
        cell: tuple[int, int],
        annotation_id: str,
        box_anchor: tuple[float, float],
        *,
        point_data: tuple[float, float],
    ) -> bool:
        plot_cell = self._composition.plot_cells.get(cell)
        if plot_cell is None:
            return False
        pinboard = self._safe_dict(plot_cell.render_state.get("pinboard"))
        annotations = pinboard.get("annotations")
        if not isinstance(annotations, list):
            return False
        for annotation in annotations:
            if not isinstance(annotation, dict):
                continue
            if str(annotation.get("annotation_id") or "") != annotation_id:
                continue
            annotation["box_anchor"] = [float(box_anchor[0]), float(box_anchor[1])]
            annotation["screen_offset"] = self._screen_offset_for_box_anchor(cell, point_data, box_anchor)
            return True
        return False

    def _screen_offset_for_box_anchor(
        self,
        cell: tuple[int, int],
        point_data: tuple[float, float],
        box_anchor: tuple[float, float],
    ) -> list[float]:
        point_canvas = self._data_to_canvas_pos(cell, point_data)
        box_canvas = self._data_to_canvas_pos(cell, box_anchor)
        if point_canvas is None or box_canvas is None:
            return [0.0, 0.0]
        return [float(box_canvas[0] - point_canvas[0]), float(box_canvas[1] - point_canvas[1])]

    def _data_to_canvas_pos(self, cell: tuple[int, int], point: tuple[float, float]) -> tuple[float, float] | None:
        geometry = next(iter(self._layout_map.pin_geometries.get(cell, {}).values()), None)
        xlim = geometry.xlim if geometry is not None else None
        ylim = geometry.ylim if geometry is not None else None
        plot_rect = self._layout_map.plot_rects_px.get(cell)
        if xlim is None or ylim is None or plot_rect is None:
            return None
        plot_x, plot_y, plot_width, plot_height = plot_rect
        x_span = float(xlim[1]) - float(xlim[0])
        y_span = float(ylim[1]) - float(ylim[0])
        if abs(x_span) <= 1e-12 or abs(y_span) <= 1e-12:
            return None
        x_fraction = (float(point[0]) - float(xlim[0])) / x_span
        y_fraction = (float(ylim[1]) - float(point[1])) / y_span
        return float(plot_x) + (x_fraction * float(plot_width)), float(plot_y) + (y_fraction * float(plot_height))

    @staticmethod
    def _element_can_edit_text(element: FigureElement) -> bool:
        return element.kind in {"main_title", "plot_title", "caption"}

    @staticmethod
    def _element_menu_label(element: FigureElement) -> str:
        labels = {
            "main_title": "title",
            "plot_title": "plot subtitle",
            "legend": "legend",
            "caption": "caption",
        }
        return labels[element.kind]

    @staticmethod
    def _default_element_text(kind: str) -> str:
        return {
            "main_title": "Main title",
            "plot_title": "Plot subtitle",
            "legend": "Legend",
            "caption": "Caption",
        }[kind]

    def _snapshot_path_from_mime(self, mime: QtCore.QMimeData) -> Path | None:
        for url in mime.urls():
            if url.isLocalFile():
                candidate = Path(url.toLocalFile())
                if candidate.suffix.lower() == ".json":
                    return candidate
        text = mime.text().strip()
        if text:
            candidate = Path(text)
            if candidate.suffix.lower() == ".json":
                return candidate
        return self._snapshot_path_from_item_model_data(mime)

    @staticmethod
    def _snapshot_path_from_item_model_data(mime: QtCore.QMimeData) -> Path | None:
        payload = mime.data("application/x-qabstractitemmodeldatalist")
        if payload.isEmpty():
            return None
        stream = QtCore.QDataStream(payload, QtCore.QIODevice.OpenModeFlag.ReadOnly)
        while not stream.atEnd():
            stream.readInt32()
            stream.readInt32()
            values = stream.readQVariant()
            if not isinstance(values, dict):
                continue
            raw = values.get(int(QtCore.Qt.ItemDataRole.UserRole))
            if not raw or str(raw) == "empty":
                continue
            candidate = Path(str(raw))
            if candidate.suffix.lower() == ".json":
                return candidate
        return None

    def _snapshot_payload(self, snapshot_path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _coordinates_for_snapshot_payload(self, payload: dict[str, Any]) -> tuple[list[tuple[float, float]], list[int]]:
        projection = payload.get("projection")
        if isinstance(projection, dict):
            coordinate_ref = str(projection.get("coordinates_ref") or "").strip()
            if coordinate_ref:
                coordinate_path = Path(coordinate_ref)
                if not coordinate_path.is_absolute() and self._project_root is not None:
                    coordinate_path = (self._project_root / coordinate_path).resolve()
                coordinates, metadata_ids = self._load_arrangement_coordinates(coordinate_path)
                if coordinates:
                    return coordinates, metadata_ids
        arrangement_ref = str(payload.get("arrangement_ref") or "").strip()
        if arrangement_ref:
            arrangement_path = Path(arrangement_ref)
            if not arrangement_path.is_absolute() and self._project_root is not None:
                arrangement_path = (self._project_root / arrangement_path).resolve()
            coordinates, metadata_ids = self._load_arrangement_coordinates(arrangement_path)
            if coordinates:
                return coordinates, metadata_ids
        discovery_ref = str(payload.get("discovery_ref") or "").strip()
        if not discovery_ref:
            return [], []
        discovery_path = Path(discovery_ref)
        if not discovery_path.is_absolute() and self._project_root is not None:
            discovery_path = (self._project_root / discovery_path).resolve()
        return self._load_discovery_coordinates(discovery_path)

    def _apply_original_projection(self, cell: tuple[int, int]) -> bool:
        self._transform_coordinator.cancel_pending()
        self._pending_transform = None
        plot_cell = self._composition.plot_cells.get(cell)
        if plot_cell is None:
            return False
        selected_paths = self._selected_preset_paths(plot_cell)
        source_path_text = selected_paths.get("discovery")
        if not source_path_text:
            self._show_status("Select a Projection before restoring Original coordinates.")
            return False
        coordinates, metadata_ids = self._load_discovery_coordinates(Path(source_path_text))
        if not coordinates:
            self._show_status("The selected Projection has no plottable coordinates.")
            return False
        plot_cell.snapshot_path = None
        plot_cell.coordinates = list(coordinates)
        plot_cell.metadata_ids = list(metadata_ids)
        plot_cell.metadata = self._metadata_for_ids(metadata_ids)
        plot_cell.render_state.pop("transform", None)
        plot_cell.render_state.pop("snapshot_projection", None)
        selected_paths["snapshot"] = None
        selected_paths["transform"] = None
        plot_cell.render_state["preset_paths"] = selected_paths
        self._reanchor_plot_pins(plot_cell)
        self._update_subtitle_text_for_cell(cell)
        self._redraw_figure()
        self._show_status("Restored Original projection coordinates.")
        self._notify_plot_edit(
            {
                "kind": "render_state",
                "values": {"transform": None, "preset_paths": copy.deepcopy(selected_paths)},
            }
        )
        return True

    @staticmethod
    def _reanchor_plot_pins(plot_cell: PlotCell) -> None:
        pinboard = plot_cell.render_state.get("pinboard")
        if not isinstance(pinboard, dict):
            return
        annotations = pinboard.get("annotations")
        if not isinstance(annotations, list):
            return
        point_by_id = {
            int(metadata_id): [float(point[0]), float(point[1])]
            for metadata_id, point in zip(
                plot_cell.metadata_ids, plot_cell.coordinates, strict=False
            )
        }
        for annotation in annotations:
            if not isinstance(annotation, dict):
                continue
            try:
                point = point_by_id[int(annotation.get("metadata_id"))]
            except (TypeError, ValueError, KeyError):
                continue
            annotation["point"] = point
            annotation.pop("box_anchor", None)

    @staticmethod
    def _load_arrangement_coordinates(path: Path) -> tuple[list[tuple[float, float]], list[int]]:
        try:
            with np.load(path, allow_pickle=False) as payload:
                coordinates = np.asarray(payload["coordinates"], dtype=np.float32)
                metadata_ids = np.asarray(payload["metadata_ids"], dtype=np.int64).reshape(-1)
        except (OSError, ValueError, KeyError):
            return [], []
        if coordinates.ndim != 2 or coordinates.shape[1] < 2 or coordinates.shape[0] != metadata_ids.shape[0]:
            return [], []
        return [tuple(map(float, point[:2])) for point in coordinates], [int(value) for value in metadata_ids]

    @staticmethod
    def _load_discovery_coordinates(discovery_path: Path) -> tuple[list[tuple[float, float]], list[int]]:
        if not discovery_path.exists():
            return [], []
        try:
            with closing(sqlite3.connect(discovery_path)) as connection:
                try:
                    rows = connection.execute("SELECT metadata_id, x, y FROM coordinates ORDER BY metadata_id").fetchall()
                    has_metadata_ids = True
                except sqlite3.Error:
                    rows = connection.execute("SELECT x, y FROM coordinates ORDER BY rowid").fetchall()
                    has_metadata_ids = False
        except sqlite3.Error:
            return [], []
        coordinates: list[tuple[float, float]] = []
        metadata_ids: list[int] = []
        for row in rows:
            try:
                if has_metadata_ids:
                    metadata_ids.append(int(row[0]))
                    coordinates.append((float(row[1]), float(row[2])))
                else:
                    metadata_ids.append(len(metadata_ids))
                    coordinates.append((float(row[0]), float(row[1])))
            except (TypeError, ValueError, IndexError):
                continue
        return coordinates, metadata_ids

    def _snapshot_render_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        states = payload.get("states") if isinstance(payload.get("states"), dict) else {}
        refs = payload.get("refs") if isinstance(payload.get("refs"), dict) else {}
        appearance = self._payload_from_ref(refs.get("appearance")) or self._safe_dict(states.get("appearance"))
        filters = self._payload_from_ref(refs.get("filters")) or self._safe_dict(states.get("filters"))
        camera_payload = self._payload_from_ref(refs.get("camera"))
        camera = (
            self._safe_dict(camera_payload.get("camera"))
            if isinstance(camera_payload, dict)
            else self._safe_dict(states.get("camera"))
        )
        layers = self._safe_dict(states.get("layers"))
        pinboard = self._snapshot_pinboard_state(states, metadata_ids=[])
        state = {
            "appearance": appearance,
            "filters": filters,
            "camera": camera,
            "layers": layers,
            "pinboard": pinboard,
            "figure_options": {"pins_visible": self._default_snapshot_pins_visible(layers, pinboard)},
        }
        projection = payload.get("projection")
        if isinstance(projection, dict):
            state["snapshot_projection"] = copy.deepcopy(projection)
        return state

    def _snapshot_pinboard_state(self, states: dict[str, Any], *, metadata_ids: list[int]) -> dict[str, Any]:
        pinboard = self._safe_dict(states.get("pinboard"))
        annotations = self._normalized_snapshot_pin_annotations(pinboard.get("annotations"))
        if metadata_ids:
            annotations = self._pins_with_resolved_thumbnails(
                annotations,
                pinboard=pinboard,
                metadata_ids=metadata_ids,
            )
        pinboard["annotations"] = annotations
        return pinboard

    @staticmethod
    def _default_snapshot_pins_visible(layers: dict[str, Any], pinboard: dict[str, Any]) -> bool:
        if "pins_visible" in layers:
            return bool(layers.get("pins_visible"))
        layer_state = pinboard.get("layer_state")
        if isinstance(layer_state, dict) and "pins_visible" in layer_state:
            return bool(layer_state.get("pins_visible"))
        return True

    @staticmethod
    def _normalized_snapshot_pin_annotations(value: object) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        output: list[dict[str, Any]] = []
        for entry in value:
            if not isinstance(entry, dict) or not bool(entry.get("visible", True)):
                continue
            annotation_id = str(entry.get("annotation_id") or "").strip()
            if not annotation_id:
                continue
            try:
                metadata_id = int(entry.get("metadata_id"))
            except (TypeError, ValueError):
                continue
            point = entry.get("point")
            box_anchor = entry.get("box_anchor")
            offset = entry.get("screen_offset", [48.0, -36.0])
            if not (isinstance(point, (list, tuple)) and len(point) >= 2):
                continue
            if not (isinstance(offset, (list, tuple)) and len(offset) >= 2):
                offset = [48.0, -36.0]
            try:
                point_values = [float(point[0]), float(point[1])]
                offset_values = [float(offset[0]), float(offset[1])]
            except (TypeError, ValueError):
                continue
            box_anchor_values = None
            if isinstance(box_anchor, (list, tuple)) and len(box_anchor) >= 2:
                try:
                    box_anchor_values = [float(box_anchor[0]), float(box_anchor[1])]
                except (TypeError, ValueError):
                    box_anchor_values = None
            lines = entry.get("lines")
            payload = {
                "annotation_id": annotation_id,
                "metadata_id": metadata_id,
                "point": point_values,
                "screen_offset": offset_values,
                "visible": True,
                "created_at": str(entry.get("created_at") or ""),
                "lines": [str(line) for line in lines] if isinstance(lines, list) else [],
            }
            if box_anchor_values is not None:
                payload["box_anchor"] = box_anchor_values
            output.append(payload)
        return output

    def _pins_with_resolved_thumbnails(
        self,
        annotations: list[dict[str, Any]],
        *,
        pinboard: dict[str, Any],
        metadata_ids: list[int],
    ) -> list[dict[str, Any]]:
        if self._project is None or not annotations:
            return annotations
        style = self._safe_dict(pinboard.get("style"))
        if not bool(style.get("thumbnail_visible", False)):
            return annotations
        variant_id = str(pinboard.get("thumbnail_variant_id") or "none").strip()
        if not variant_id or variant_id == "none":
            return annotations
        try:
            thumbnail_size = int(style.get("thumbnail_size", 96))
        except (TypeError, ValueError):
            thumbnail_size = 96
        resolver = ThumbnailResolver(self._project)
        wanted = {int(value) for value in metadata_ids}
        resolved: list[dict[str, Any]] = []
        for annotation in annotations:
            if int(annotation["metadata_id"]) not in wanted:
                resolved.append(annotation)
                continue
            ref = resolver.resolve(int(annotation["metadata_id"]), variant_id=variant_id, target_px=thumbnail_size)
            if ref is not None:
                annotation = dict(annotation)
                annotation["thumbnail_path"] = str(ref.path)
            resolved.append(annotation)
        return resolved

    def _payload_from_ref(self, value: object) -> dict[str, Any] | None:
        text = str(value or "").strip()
        if not text:
            return None
        path = Path(text)
        if not path.is_absolute() and self._project_root is not None:
            path = (self._project_root / path).resolve()
        return self._payload_from_path(path)

    def _payload_from_path(self, path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _snapshot_preset_paths(self, payload: dict[str, Any], snapshot_path: Path) -> dict[str, str | None]:
        refs = payload.get("refs") if isinstance(payload.get("refs"), dict) else {}
        return {
            "snapshot": str(snapshot_path),
            "discovery": self._resolved_path_text(payload.get("discovery_ref")),
            "transform": self._resolved_path_text(refs.get("transform")),
            "camera": self._resolved_path_text(refs.get("camera")),
            "appearance": self._resolved_path_text(refs.get("appearance")),
            "filter": self._resolved_path_text(refs.get("filters")),
        }

    def _resolved_path_text(self, value: object) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        path = Path(text)
        if not path.is_absolute() and self._project_root is not None:
            path = (self._project_root / path).resolve()
        return str(path)

    def _metadata_for_ids(self, metadata_ids: list[int]) -> dict[int, dict[str, str]]:
        if self._project_root is None or not metadata_ids:
            return {}
        database = self._project_root / "metadata" / "metadata.db"
        if not database.exists():
            return {}
        wanted = set(int(value) for value in metadata_ids)
        metadata: dict[int, dict[str, str]] = {}
        try:
            with closing(sqlite3.connect(database)) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute("SELECT * FROM metadata").fetchall()
        except sqlite3.Error:
            return {}
        for row in rows:
            try:
                metadata_id = int(row["id"])
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            if metadata_id not in wanted:
                continue
            metadata[metadata_id] = {key: "" if row[key] is None else str(row[key]) for key in row.keys()}
        return metadata

    @staticmethod
    def _safe_dict(value: object) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    def _show_status(self, message: str) -> None:
        if self._status_callback is not None:
            self._status_callback(message)
