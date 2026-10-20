from __future__ import annotations

from collections.abc import Callable, Iterable
import copy
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from PySide6 import QtCore, QtGui, QtWidgets

from FiguresArea.animation_export_service import GifAnimationExportService
from FiguresArea.animation_preview_cache import AnimationPreviewCache
from DiscoveryArea.metadata_access import DiscoveryMetadataAccess
from DiscoveryArea.presets_repository import PresetRepository
from FiguresArea.animation_service import AnimationSequenceService
from FiguresArea.controllers.animation_export_dialog_controller import (
    AnimationExportDialogController,
)
from FiguresArea.controllers.plot_tab_controller import PlotTabController
from FiguresArea.domain import (
    AnimationFrame,
    AnimationTimelineState,
    FigurePlotSettings,
    SlicerAnimationFrame,
    SnapshotAnimationFrame,
)
from shared.qt.icons import apply_icon_color
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import add_widget_to_container, require_child

if TYPE_CHECKING:
    from project.domain import Project
    from shared.qt.activity import ActivityManager, ActivityToken


class _TimelineItemDelegate(QtWidgets.QStyledItemDelegate):
    """Paint horizontal list items as thumbnail cards."""

    def initStyleOption(
        self, option: QtWidgets.QStyleOptionViewItem, index: QtCore.QModelIndex
    ) -> None:
        super().initStyleOption(option, index)
        option.decorationPosition = QtWidgets.QStyleOptionViewItem.Position.Top
        option.decorationAlignment = QtCore.Qt.AlignmentFlag.AlignHCenter
        option.displayAlignment = (
            QtCore.Qt.AlignmentFlag.AlignHCenter
            | QtCore.Qt.AlignmentFlag.AlignTop
        )


class AnimationTabController(QtCore.QObject):
    """Controller for one ordered snapshot or slicer-driven animation."""

    def __init__(
        self,
        *,
        project: Project | None = None,
        project_root: Path | None,
        parent: QtWidgets.QWidget | None = None,
        status_callback: Callable[[str], None] | None = None,
        selection_callback: Callable[[str], None] | None = None,
        state_changed_callback: Callable[[], None] | None = None,
        activity_manager: ActivityManager | None = None,
    ) -> None:
        super().__init__(parent)
        self._project = project
        self._project_root = project_root
        self._status_callback = status_callback
        self._state_changed_callback = state_changed_callback
        self._activity_manager = activity_manager
        self._restoring = False
        self._state = AnimationTimelineState()
        self._playback_direction = 1
        self._preview_cache = AnimationPreviewCache()
        self._thumbnail_directory = TemporaryDirectory(
            prefix="eidora-animation-thumbnails-"
        )
        self._thumbnail_generation_queue: list[str] = []
        self._refreshing_all_thumbnails = False
        self._thumbnail_activity: ActivityToken | None = None
        self._thumbnail_progress_completed = 0
        self._thumbnail_progress_total = 0
        self._metadata_access: DiscoveryMetadataAccess | None = None
        self._export_service = GifAnimationExportService()
        self._proxy_source_pixmap = QtGui.QPixmap()
        self._proxy_target_size = QtCore.QSize()

        self._widget = load_ui("FiguresArea", "ui/animation_panel.ui", parent)
        self._preview_stack = require_child(
            self._widget, QtWidgets.QStackedWidget, "stack_animation_preview"
        )
        self._plot_preview_page = require_child(
            self._widget, QtWidgets.QWidget, "page_animation_plot_preview"
        )
        self._proxy_preview_page = require_child(
            self._widget, QtWidgets.QWidget, "page_animation_proxy_preview"
        )
        self._proxy_preview_host = require_child(
            self._widget, QtWidgets.QWidget, "panel_animation_proxy_host"
        )
        self._proxy_preview_label = require_child(
            self._widget, QtWidgets.QLabel, "label_animation_proxy_preview"
        )
        self._preview_animation_note = self._widget.findChild(
            QtWidgets.QLabel, "label_preview_animation_note"
        )
        self._plot_note_container = require_child(
            self._widget, QtWidgets.QWidget, "waiting_note_container"
        )
        self._proxy_note_container = require_child(
            self._widget, QtWidgets.QWidget, "preview_animation_label_container"
        )
        self._waiting_for_snapshot = require_child(
            self._widget, QtWidgets.QLabel, "label_waiting_for_snapshot"
        )
        self._waiting_for_snapshot.setEnabled(False)
        self._waiting_for_snapshot.hide()
        self._set_preview_animation_note_visible(False)
        ignored_policy = QtWidgets.QSizePolicy.Policy.Ignored
        self._proxy_preview_page.setSizePolicy(ignored_policy, ignored_policy)
        self._proxy_preview_label.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Fixed,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self._proxy_preview_label.setMinimumSize(0, 0)
        self._reserve_preview_note_height()
        proxy_host_layout = self._proxy_preview_host.layout()
        if proxy_host_layout is not None:
            proxy_host_layout.setAlignment(
                self._proxy_preview_label, QtCore.Qt.AlignmentFlag.AlignCenter
            )
        plot_host = require_child(
            self._widget, QtWidgets.QWidget, "panel_animation_plot_host"
        )
        self._plot = PlotTabController(
            project=project,
            project_root=project_root,
            figure_kind="animation",
            parent=plot_host,
            status_callback=status_callback,
            selection_callback=selection_callback,
            state_changed_callback=self._handle_plot_state_changed,
            plot_edit_callback=self._handle_plot_edit,
            activity_manager=activity_manager,
        )
        add_widget_to_container(plot_host, self._plot.widget)
        self._preview_stack.setCurrentWidget(self._plot_preview_page)

        self._drop_area = require_child(self._widget, QtWidgets.QFrame, "panel_animation_drop_area")
        self._drop_hint = require_child(self._widget, QtWidgets.QLabel, "label_animation_drop_hint")
        self._slicer_loader = require_child(
            self._widget, QtWidgets.QWidget, "panel_animation_slicer_loader"
        )
        self._slicer_presets = require_child(
            self._widget, QtWidgets.QComboBox, "combo_slicer_presets"
        )
        self._import_slicer_preset = require_child(
            self._widget, QtWidgets.QToolButton, "button_import_slicer_preset"
        )
        self._sequence_panel = require_child(
            self._widget, QtWidgets.QWidget, "panel_animation_sequence_list"
        )
        self._sequence_layout = self._drop_area.parentWidget().layout()
        self._plot_drop_target = self._plot.snapshot_drop_target
        self._list = require_child(self._widget, QtWidgets.QListWidget, "list_animation_sequence")
        self._timeline_progress = require_child(
            self._widget, QtWidgets.QProgressBar, "progress_timeline"
        )
        self._timeline_progress.setRange(0, 1)
        self._timeline_progress.setValue(0)
        self._timeline_progress.setFormat("Updating thumbnails… %v / %m")
        self._timeline_progress.hide()
        self._list_viewport = self._list.viewport()
        self._drop_indicator = QtWidgets.QFrame(self._list_viewport)
        self._drop_indicator.setObjectName("timeline_drop_insertion_indicator")
        self._drop_indicator.setAttribute(
            QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self._drop_indicator.setStyleSheet(
            "QFrame#timeline_drop_insertion_indicator { background: palette(highlight); }"
        )
        self._drop_indicator.hide()
        self._play = require_child(
            self._widget, QtWidgets.QAbstractButton, "button_animation_play"
        )
        self._previous = require_child(
            self._widget, QtWidgets.QAbstractButton, "button_animation_previous"
        )
        self._next = require_child(
            self._widget, QtWidgets.QAbstractButton, "button_animation_next"
        )
        self._speed = require_child(self._widget, QtWidgets.QSpinBox, "spin_speed_ms")
        self._repeat_style = require_child(
            self._widget, QtWidgets.QComboBox, "combo_repeat_style"
        )
        self._thumbnail_size = require_child(
            self._widget, QtWidgets.QSpinBox, "spin_snapshot_thumbnail_size"
        )
        self._apply_changes_to_all = require_child(
            self._widget,
            QtWidgets.QToolButton,
            "button_apply_plot_changes_to_all",
        )
        self._apply_changes_to_all.hide()

        self._playback_timer = QtCore.QTimer(self._widget)
        self._playback_timer.timeout.connect(self._advance_playback)
        self._thumbnail_generation_timer = QtCore.QTimer(self._widget)
        self._thumbnail_generation_timer.setSingleShot(True)
        self._thumbnail_generation_timer.timeout.connect(
            self._generate_next_thumbnail
        )
        self._preset_watcher = QtCore.QFileSystemWatcher(self._widget)
        self._preset_watcher.directoryChanged.connect(
            lambda _path: self._refresh_slicer_presets()
        )
        self._configure_list()
        self._show_empty_drop_state()
        self._drop_area.installEventFilter(self)
        self._plot_drop_target.installEventFilter(self)
        self._plot_preview_page.installEventFilter(self)
        self._list_viewport.installEventFilter(self)
        self._proxy_preview_host.installEventFilter(self)
        self._list.currentItemChanged.connect(self._show_current_item)
        self._list.model().rowsMoved.connect(lambda *_args: QtCore.QTimer.singleShot(0, self._sync_order))
        self._play.toggled.connect(self._set_playing)
        self._previous.clicked.connect(lambda: self._move_selection(-1))
        self._next.clicked.connect(lambda: self._move_selection(1))
        self._speed.valueChanged.connect(self._set_interval)
        self._repeat_style.currentIndexChanged.connect(self._set_repeat_style)
        self._thumbnail_size.valueChanged.connect(self._set_thumbnail_size)
        self._apply_changes_to_all.clicked.connect(
            self._apply_current_frame_changes_to_all
        )
        self._slicer_presets.currentIndexChanged.connect(
            self._update_slicer_import_state
        )
        self._import_slicer_preset.clicked.connect(self._import_selected_slicer_preset)
        self._speed.setValue(self._state.interval_ms)
        self._set_repeat_combo_value(self._state.repeat_style)
        self._thumbnail_size.setMinimum(16)
        self._thumbnail_size.setValue(self._state.thumbnail_size)
        self._update_play_icon()
        self._update_slicer_import_state()
        self._update_control_states()
        QtCore.QTimer.singleShot(0, self._position_apply_changes_button)
        self.set_project(project)
        apply_icon_color(self._widget)

    @property
    def widget(self) -> QtWidgets.QWidget:
        return self._widget

    @property
    def plot_controller(self) -> PlotTabController:
        return self._plot

    def figure_settings(self) -> FigurePlotSettings:
        return self._plot.figure_settings()

    def apply_figure_settings(
        self,
        settings: FigurePlotSettings,
        *,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> None:
        self._plot.apply_figure_settings(
            settings, progress_callback=progress_callback
        )

    def export_figure(
        self,
        path: Path,
        *,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> None:
        self._pause()
        if progress_callback is not None:
            progress_callback("Preparing animation export…", 10)
        self._export_service.export(
            frames=self._ordered_frames(),
            destination=Path(path),
            interval_ms=int(self._speed.value()),
            repeat_style=self._state.repeat_style,
            render_frame=self._export_frame_png,
        )
        if progress_callback is not None:
            progress_callback("Animation export complete", 100)

    def export_interactively(self) -> bool:
        """Configure and run an animation export in its dedicated modal dialog."""

        self._pause()
        title = str(self._widget.property("figureTitle") or "animation")
        result = AnimationExportDialogController(
            status_callback=self._show_status,
            activity_manager=self._activity_manager,
        ).exec(
            frames=self._ordered_frames(),
            interval_ms=int(self._speed.value()),
            repeat_style=self._state.repeat_style,
            source_dimensions=self._plot.plot_canvas_size_px(),
            render_frame=self._export_frame_png,
            suggested_stem=title,
        )
        return result is not None

    @staticmethod
    def export_file_options() -> list[tuple[str, list[str]]]:
        return [("GIF animation (*.gif)", ["gif"])]

    @staticmethod
    def default_export_format() -> str:
        return "gif"

    def set_project(self, project: Project | None) -> None:
        self._cancel_thumbnail_generation()
        self._project = project
        self._project_root = project.root if project is not None else None
        self._metadata_access = (
            DiscoveryMetadataAccess(project) if project is not None else None
        )
        self._plot.set_project(project)
        self._drop_area.setEnabled(project is not None)
        watched = self._preset_watcher.directories()
        if watched:
            self._preset_watcher.removePaths(watched)
        if project is not None:
            preset_root = project.paths.slicer_presets
            preset_root.mkdir(parents=True, exist_ok=True)
            self._preset_watcher.addPath(str(preset_root))
        self._refresh_slicer_presets()

    def shutdown(self) -> None:
        self._playback_timer.stop()
        self._cancel_thumbnail_generation()
        self._set_preview_animation_note_visible(False)
        self._preview_cache.close()
        self._thumbnail_directory.cleanup()

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if (
            obj is self._plot_preview_page
            and event.type() == QtCore.QEvent.Type.Resize
        ):
            self._position_apply_changes_button()
            return False
        if obj is self._proxy_preview_host and event.type() == QtCore.QEvent.Type.Resize:
            self._update_proxy_label_pixmap()
            return False
        if obj is self._drop_area or obj is self._plot_drop_target or obj is self._list_viewport:
            if obj is self._list_viewport:
                if event.type() == QtCore.QEvent.Type.DragMove:
                    external_snapshot = self._snapshot_path_from_mime(
                        event.mimeData()
                    )
                    if event.source() is self._list or external_snapshot is not None:
                        self._show_drop_insertion_indicator(event.position().toPoint())
                    else:
                        self._drop_indicator.hide()
                    if external_snapshot is not None:
                        event.acceptProposedAction()
                        return True
                elif event.type() in {
                    QtCore.QEvent.Type.DragLeave,
                    QtCore.QEvent.Type.Drop,
                }:
                    self._drop_indicator.hide()
            if event.type() == QtCore.QEvent.Type.DragEnter:
                if self._snapshot_path_from_mime(event.mimeData()) is not None:
                    event.acceptProposedAction()
                    return True
            if event.type() == QtCore.QEvent.Type.Drop:
                path = self._snapshot_path_from_mime(event.mimeData())
                if obj is self._plot_drop_target:
                    if path is not None and self.set_base_snapshot(path):
                        event.acceptProposedAction()
                        return True
                    return super().eventFilter(obj, event)
                insert_row = (
                    self._insertion_row_at(event.position().toPoint())
                    if obj is self._list_viewport
                    else None
                )
                if path is not None and self.add_snapshot(path, insert_row=insert_row):
                    event.acceptProposedAction()
                    return True
        return super().eventFilter(obj, event)

    def set_base_snapshot(self, snapshot_path: Path) -> bool:
        path = Path(snapshot_path)
        if path.suffix.lower() != ".json" or not path.exists():
            self._show_status("Could not load the animation base snapshot.")
            return False
        self._pause()
        if not self._plot.render_animation_base_snapshot(path):
            return False
        self._state.base_snapshot_path = str(path.resolve())
        self._clear_generated_thumbnails()
        self._update_waiting_for_snapshot_state()
        self._start_slicer_thumbnail_generation()
        frame = self._current_frame()
        if isinstance(frame, SlicerAnimationFrame):
            self._show_actual_frame(frame)
        self._notify_state_changed()
        self._show_status(f"Loaded animation base snapshot: {path.stem}")
        return True

    def _export_frame_png(self, frame: AnimationFrame, destination: Path) -> None:
        if isinstance(frame, SnapshotAnimationFrame):
            self._plot.export_animation_frame_png(frame, destination)
            return
        base_snapshot = self._base_snapshot_path()
        if base_snapshot is None:
            raise ValueError(
                "Drop a base snapshot into the animation preview before exporting."
            )
        self._plot.export_slicer_animation_frame_png(
            frame, base_snapshot, destination
        )

    def _show_drop_insertion_indicator(self, position: QtCore.QPoint) -> None:
        if self._list.count() < 1:
            self._drop_indicator.hide()
            return
        insertion_row = self._insertion_row_at(position)
        insertion_x = (
            self._list.visualItemRect(self._list.item(insertion_row)).left()
            if insertion_row < self._list.count()
            else self._list.visualItemRect(
                self._list.item(self._list.count() - 1)
            ).right() + 1
        )
        height = min(
            self._list_viewport.height() - 8,
            max(self._list.gridSize().height(), 24),
        )
        self._drop_indicator.setGeometry(max(0, insertion_x - 1), 4, 3, height)
        self._drop_indicator.show()
        self._drop_indicator.raise_()

    def _insertion_row_at(self, position: QtCore.QPoint) -> int:
        for row in range(self._list.count()):
            rect = self._list.visualItemRect(self._list.item(row))
            if position.x() < rect.center().x():
                return row
        return self._list.count()

    def _refresh_slicer_presets(self) -> None:
        selected_path = str(self._slicer_presets.currentData() or "")
        self._slicer_presets.blockSignals(True)
        self._slicer_presets.clear()
        self._slicer_presets.addItem("select slicer preset", None)
        indicator = self._slicer_presets.model().item(0)
        if indicator is not None:
            font = indicator.font()
            font.setItalic(True)
            indicator.setFont(font)
            indicator.setFlags(
                indicator.flags()
                & ~QtCore.Qt.ItemFlag.ItemIsEnabled
                & ~QtCore.Qt.ItemFlag.ItemIsSelectable
            )
        if self._project is not None:
            try:
                entries = PresetRepository(
                    self._project.paths.slicer_presets
                ).list_entries()
            except OSError:
                entries = []
            for entry in entries:
                self._slicer_presets.addItem(entry.label, str(entry.path))
        restored_index = self._slicer_presets.findData(selected_path)
        self._slicer_presets.setCurrentIndex(max(restored_index, 0))
        self._slicer_presets.blockSignals(False)
        self._update_slicer_import_state()

    def _selected_slicer_preset(
        self,
    ) -> tuple[str, Path, dict[str, Any]] | None:
        path_text = str(self._slicer_presets.currentData() or "").strip()
        if not path_text:
            return None
        path = Path(path_text)
        payload = PresetRepository(path.parent).load_payload_from_path(path)
        if not path.exists() or not isinstance(payload, dict):
            return None
        return self._slicer_presets.currentText(), path, payload

    def _update_slicer_import_state(self, _index: int | None = None) -> None:
        self._import_slicer_preset.setEnabled(
            self._selected_slicer_preset() is not None
        )

    def _import_selected_slicer_preset(self) -> None:
        selected = self._selected_slicer_preset()
        if selected is None or self._metadata_access is None:
            self._show_status("Select a valid slicer preset to import.")
            self._update_slicer_import_state()
            return
        label, path, payload = selected
        slicer_payload = (
            payload.get("slicer")
            if isinstance(payload.get("slicer"), dict)
            else payload
        )
        column = str(slicer_payload.get("column") or "").strip()
        try:
            if column not in self._metadata_access.column_names():
                raise ValueError(f"Slicer column is unavailable: {column or '(none)'}")
            values = self._metadata_access.distinct_values(column)
            frames = AnimationSequenceService.build_slicer_frames(
                preset_path=str(path),
                preset_label=label,
                preset_payload=payload,
                available_values=values,
            )
            if not frames:
                raise ValueError("The slicer preset generated no animation frames.")
        except (OSError, ValueError) as error:
            self._show_status(str(error))
            return

        self._replace_timeline(frames)
        self._show_timeline_state()
        self._list.setCurrentRow(0)
        self._update_waiting_for_snapshot_state()
        self._start_slicer_thumbnail_generation()
        self._notify_state_changed()
        self._show_status(
            f"Imported slicer preset '{label}' with {len(frames)} frames."
        )

    def _replace_timeline(self, frames: list[AnimationFrame]) -> None:
        self._pause()
        self._cancel_thumbnail_generation()
        self._remove_cached_rendered_thumbnails()
        self._reset_preview_cache()
        self._list.blockSignals(True)
        self._list.clear()
        self._list.blockSignals(False)
        self._state.frames = []
        self._state.current_frame_id = None
        self._state.thumbnails_dirty = False
        self._state.pending_apply_frame_ids.clear()
        for frame in frames:
            self._append_frame(frame)

    def _reset_preview_cache(self) -> None:
        self._preview_cache.close()
        self._preview_cache = AnimationPreviewCache()
        self._thumbnail_directory.cleanup()
        self._thumbnail_directory = TemporaryDirectory(
            prefix="eidora-animation-thumbnails-"
        )

    def _clear_generated_thumbnails(self) -> None:
        self._cancel_thumbnail_generation()
        self._remove_cached_rendered_thumbnails(
            frame
            for frame in self._state.frames
            if isinstance(frame, SlicerAnimationFrame)
        )
        self._reset_preview_cache()
        for frame in self._state.frames:
            if isinstance(frame, SnapshotAnimationFrame):
                cached_thumbnail = self._existing_rendered_thumbnail(frame)
                self._preview_cache.register_source(
                    frame.frame_id,
                    cached_thumbnail
                    or Path(frame.snapshot_path).with_suffix(".png"),
                )
        self._refresh_all_timeline_icons()

    def add_snapshot(self, snapshot_path: Path, *, insert_row: int | None = None) -> bool:
        path = Path(snapshot_path)
        if path.suffix.lower() != ".json" or not path.exists():
            self._show_status("Could not load the dropped snapshot.")
            return False
        payload = self._load_payload(path)
        if payload is None:
            self._show_status(f"Snapshot '{path.stem}' is invalid.")
            return False
        frame = SnapshotAnimationFrame(
            frame_id=uuid4().hex,
            label=str(payload.get("name") or path.stem),
            snapshot_path=str(path),
        )
        self._pause()
        if not self._plot.render_animation_frame(frame):
            return False
        row = (
            self._list.count()
            if insert_row is None
            else min(max(int(insert_row), 0), self._list.count())
        )
        self._append_frame(frame, row=row)
        self._show_snapshot_timeline_state()
        self._list.blockSignals(True)
        self._list.setCurrentRow(row)
        self._list.blockSignals(False)
        self._state.current_frame_id = frame.frame_id
        self._notify_state_changed()
        return True

    def animation_state_payload(self) -> dict[str, Any]:
        return {
            "frames": [self._frame_payload(frame) for frame in self._state.frames],
            "base_snapshot_path": (
                self._stored_path(self._state.base_snapshot_path)
                if self._state.base_snapshot_path
                else None
            ),
            "current_frame_id": self._state.current_frame_id,
            "interval_ms": int(self._speed.value()),
            "thumbnail_size": int(self._thumbnail_size.value()),
            "repeat_style": self._state.repeat_style,
            "thumbnails_dirty": self._state.thumbnails_dirty,
            "pending_apply_frame_ids": sorted(
                self._state.pending_apply_frame_ids
            ),
        }

    def snapshot_animation_state_payload(self) -> dict[str, Any]:
        return self.animation_state_payload()

    def apply_animation_state_payload(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        self._restoring = True
        skipped_frames = 0
        missing_base_snapshot = False
        try:
            self._pause()
            self._cancel_thumbnail_generation()
            self._reset_preview_cache()
            self._list.clear()
            self._state.frames.clear()
            self._state.pending_apply_frame_ids.clear()
            base_snapshot = self._resolved_path(payload.get("base_snapshot_path"))
            missing_base_snapshot = (
                base_snapshot is not None and not base_snapshot.exists()
            )
            self._state.base_snapshot_path = (
                str(base_snapshot)
                if base_snapshot is not None and base_snapshot.exists()
                else None
            )
            raw_frames = payload.get("frames")
            if isinstance(raw_frames, list):
                for raw in raw_frames:
                    frame = self._frame_from_payload(raw)
                    if frame is not None and self._frame_references_exist(frame):
                        self._append_frame(frame)
                    else:
                        skipped_frames += 1
            self._speed.setValue(self._bounded_int(payload.get("interval_ms"), 50, 5000, 150))
            self._thumbnail_size.setValue(self._bounded_int(payload.get("thumbnail_size"), 16, 100, 48))
            self._set_repeat_combo_value(
                AnimationSequenceService.normalize_repeat_style(
                    payload.get("repeat_style")
                )
            )
            self._state.thumbnails_dirty = bool(
                payload.get("thumbnails_dirty", False)
            )
            raw_pending_ids = payload.get("pending_apply_frame_ids")
            existing_frame_ids = {
                frame.frame_id for frame in self._state.frames
            }
            self._state.pending_apply_frame_ids = (
                {
                    str(frame_id)
                    for frame_id in raw_pending_ids
                    if str(frame_id) in existing_frame_ids
                }
                if isinstance(raw_pending_ids, list)
                else set()
            )
            current_id = str(payload.get("current_frame_id") or "")
            row = self._row_for_frame_id(current_id)
            if row < 0 and self._list.count():
                row = 0
            if row >= 0:
                self._list.setCurrentRow(row)
            self._sync_order()
        finally:
            self._restoring = False
        if self._list.count():
            self._show_snapshot_timeline_state()
        else:
            self._show_empty_drop_state()
        self._refresh_all_timeline_icons()
        self._update_waiting_for_snapshot_state()
        self._start_slicer_thumbnail_generation()
        self._update_control_states()
        if skipped_frames:
            self._show_status(f"Skipped {skipped_frames} animation frame(s) with missing references.")
        elif missing_base_snapshot:
            self._show_status(
                "The animation base snapshot is missing; drop a replacement snapshot "
                "into the preview."
            )

    def apply_snapshot_animation_state_payload(self, payload: object) -> None:
        self.apply_animation_state_payload(payload)

    def _configure_list(self) -> None:
        # ListMode keeps model order authoritative. IconMode with Snap/Free
        # movement stores arbitrary viewport positions, which is inappropriate
        # for a timeline and gives no reliable between-frame insertion target.
        self._list.setViewMode(QtWidgets.QListView.ViewMode.ListMode)
        self._list.setFlow(QtWidgets.QListView.Flow.LeftToRight)
        self._list.setWrapping(False)
        self._list.setResizeMode(QtWidgets.QListView.ResizeMode.Adjust)
        self._list.setMovement(QtWidgets.QListView.Movement.Static)
        self._list.setUniformItemSizes(True)
        self._list.setIconSize(
            QtCore.QSize(self._thumbnail_size.value(), self._thumbnail_size.value())
        )
        self._update_timeline_grid_size(self._thumbnail_size.value())
        self._list.setItemDelegate(_TimelineItemDelegate(self._list))
        self._list.setDragEnabled(True)
        self._list.setAcceptDrops(True)
        self._list.setDropIndicatorShown(True)
        self._list.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.InternalMove)
        self._list.setDragDropOverwriteMode(False)
        self._list.setDefaultDropAction(QtCore.Qt.DropAction.MoveAction)
        self._list.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self._list.horizontalScrollBar().rangeChanged.connect(
            lambda _minimum, _maximum: QtCore.QTimer.singleShot(
                0, self._update_timeline_height
            )
        )

    def _show_empty_drop_state(self) -> None:
        self._drop_hint.show()
        self._slicer_loader.show()
        self._drop_area.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        self._drop_area.setMinimumSize(220, 64)
        self._drop_area.setMaximumWidth(16777215)
        layout = self._drop_area.layout()
        if layout is not None:
            layout.setContentsMargins(8, 8, 8, 8)
        self._set_drop_area_alignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._drop_area.show()
        self._drop_area.raise_()

    def _show_timeline_state(self) -> None:
        self._drop_hint.hide()
        self._slicer_loader.hide()
        self._drop_area.hide()

    def _show_snapshot_timeline_state(self) -> None:
        self._show_timeline_state()

    def _set_drop_area_alignment(self, alignment: QtCore.Qt.AlignmentFlag) -> None:
        if self._sequence_layout is not None:
            self._sequence_layout.setAlignment(self._drop_area, alignment)

    def _append_frame(
        self, frame: AnimationFrame, *, row: int | None = None
    ) -> None:
        insertion_row = len(self._state.frames) if row is None else row
        self._state.frames = AnimationSequenceService.inserted(
            self._state.frames, frame, insertion_row
        )
        if isinstance(frame, SnapshotAnimationFrame):
            cached_thumbnail = self._existing_rendered_thumbnail(frame)
            self._preview_cache.register_source(
                frame.frame_id,
                cached_thumbnail
                or Path(frame.snapshot_path).with_suffix(".png"),
            )
        else:
            cached_thumbnail = self._existing_rendered_thumbnail(frame)
            if cached_thumbnail is not None:
                self._preview_cache.register_source(frame.frame_id, cached_thumbnail)
        item = QtWidgets.QListWidgetItem(self._placeholder_icon(), frame.label)
        item.setData(QtCore.Qt.ItemDataRole.UserRole, frame.frame_id)
        item.setToolTip(frame.label)
        # Frames are insertion neighbours, never free-position drop targets.
        # Clearing ItemIsDropEnabled makes Qt draw its insertion marker between
        # two thumbnails instead of treating a thumbnail as an overwrite area.
        item.setFlags(
            (item.flags() | QtCore.Qt.ItemFlag.ItemIsDragEnabled)
            & ~QtCore.Qt.ItemFlag.ItemIsDropEnabled
        )
        if row is None:
            self._list.addItem(item)
        else:
            self._list.insertItem(row, item)
        self._resize_item(item)
        self._refresh_timeline_icon(frame.frame_id)
        self._update_control_states()

    def _show_current_item(
        self,
        current: QtWidgets.QListWidgetItem | None,
        _previous: QtWidgets.QListWidgetItem | None,
    ) -> None:
        frame = self._frame_for_item(current)
        self._state.current_frame_id = frame.frame_id if frame is not None else None
        if frame is not None:
            if isinstance(frame, SnapshotAnimationFrame) and not Path(
                frame.snapshot_path
            ).exists():
                self._show_status(f"Animation snapshot is missing: {Path(frame.snapshot_path).name}")
            elif isinstance(frame, SlicerAnimationFrame) and self._base_snapshot_path() is None:
                self._update_waiting_for_snapshot_state()
            elif self._play.isChecked():
                self._show_proxy_frame(frame)
            else:
                self._show_actual_frame(frame)
        self._update_control_states()
        if not self._playback_timer.isActive():
            self._notify_state_changed()

    def _sync_order(self) -> None:
        current_id = self._state.current_frame_id
        frame_ids = [
            str(self._list.item(row).data(QtCore.Qt.ItemDataRole.UserRole) or "")
            for row in range(self._list.count())
        ]
        self._state.frames = AnimationSequenceService.reordered(
            self._state.frames, frame_ids
        )
        self._state.current_frame_id = current_id
        self._pause()
        self._notify_state_changed()

    def _ordered_frames(self) -> list[AnimationFrame]:
        return list(self._state.frames)

    def _set_playing(self, playing: bool) -> None:
        if playing and self._list.count() >= 2:
            self._playback_direction = 1
            preview_width, preview_height = self._plot.plot_preview_size_px()
            self._proxy_target_size = QtCore.QSize(
                max(1, preview_width), max(1, preview_height)
            )
            if self._state.repeat_style == "play_once" and self._list.currentRow() != 0:
                self._list.blockSignals(True)
                self._list.setCurrentRow(0)
                self._list.blockSignals(False)
                first_frame = self._current_frame()
                self._state.current_frame_id = (
                    first_frame.frame_id if first_frame is not None else None
                )
            frame = self._current_frame()
            if frame is not None:
                self._show_proxy_frame(frame)
            self._playback_timer.start(self._speed.value())
            self._set_preview_animation_note_visible(True)
        else:
            self._playback_timer.stop()
            self._set_preview_animation_note_visible(False)
            if playing:
                self._play.blockSignals(True)
                self._play.setChecked(False)
                self._play.blockSignals(False)
            self._restore_actual_preview()
        self._update_play_icon()
        if not playing:
            self._notify_state_changed()

    def _pause(self) -> None:
        was_playing = bool(
            self._play.isChecked()
            or self._playback_timer.isActive()
            or self._preview_stack.currentWidget() is self._proxy_preview_page
        )
        self._playback_timer.stop()
        self._set_preview_animation_note_visible(False)
        if self._play.isChecked():
            self._play.blockSignals(True)
            self._play.setChecked(False)
            self._play.blockSignals(False)
        self._update_play_icon()
        if was_playing:
            self._restore_actual_preview()

    def _show_proxy_frame(self, frame: AnimationFrame) -> None:
        pixmap = self._preview_cache.pixmap(frame.frame_id)
        if pixmap.isNull():
            item = self._item_for_frame_id(frame.frame_id)
            if item is not None:
                size = max(self._state.thumbnail_size, 16)
                pixmap = item.icon().pixmap(QtCore.QSize(size, size))
        self._proxy_source_pixmap = pixmap
        self._preview_stack.setCurrentWidget(self._proxy_preview_page)
        self._update_proxy_label_pixmap()

    def _update_proxy_label_pixmap(self) -> None:
        if self._proxy_source_pixmap.isNull():
            self._proxy_preview_label.clear()
            return
        available = self._proxy_preview_host.size()
        if available.width() <= 0 or available.height() <= 0:
            return
        target = self._proxy_target_size
        if not target.isValid() or target.isEmpty():
            target = available
        else:
            target = target.boundedTo(available)
        scaled = self._proxy_source_pixmap.scaled(
            target,
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
        # Match the fixed Matplotlib canvas surface. The image stays centered
        # inside this stable rectangle when its source aspect ratio differs.
        self._proxy_preview_label.setFixedSize(target)
        self._proxy_preview_label.setPixmap(scaled)

    def _show_actual_frame(self, frame: AnimationFrame) -> None:
        self._preview_stack.setCurrentWidget(self._plot_preview_page)
        if isinstance(frame, SnapshotAnimationFrame):
            self._plot.render_animation_frame(frame)
            return
        base_snapshot = self._base_snapshot_path()
        if base_snapshot is not None:
            self._plot.render_slicer_animation_frame(frame, base_snapshot)

    def _restore_actual_preview(self) -> None:
        frame = self._current_frame()
        self._preview_stack.setCurrentWidget(self._plot_preview_page)
        self._proxy_source_pixmap = QtGui.QPixmap()
        self._proxy_target_size = QtCore.QSize()
        self._proxy_preview_label.clear()
        if frame is not None:
            self._show_actual_frame(frame)

    def _move_selection(self, offset: int) -> None:
        count = self._list.count()
        if count < 1:
            return
        row = self._list.currentRow()
        if row < 0:
            row = 0
        else:
            row = (row + int(offset)) % count
        self._list.setCurrentRow(row)

    def _advance_playback(self) -> None:
        count = self._list.count()
        advance = AnimationSequenceService.advance(
            index=self._list.currentRow(),
            count=count,
            repeat_style=self._state.repeat_style,
            direction=self._playback_direction,
        )
        if advance.finished:
            self._pause()
            return
        self._playback_direction = advance.direction
        self._list.setCurrentRow(advance.index)

    def _set_interval(self, value: int) -> None:
        self._state.interval_ms = int(value)
        if self._playback_timer.isActive():
            self._playback_timer.start(int(value))
        self._notify_state_changed()

    def _set_repeat_style(self, _index: int) -> None:
        self._state.repeat_style = AnimationSequenceService.normalize_repeat_style(
            self._repeat_style.currentText()
        )
        self._playback_direction = 1
        self._notify_state_changed()

    def _set_repeat_combo_value(self, repeat_style: str) -> None:
        labels = {
            "from_start": "from start",
            "ping_pong": "ping pong",
            "play_once": "play once",
        }
        normalized = AnimationSequenceService.normalize_repeat_style(repeat_style)
        index = self._repeat_style.findText(labels[normalized])
        self._repeat_style.blockSignals(True)
        self._repeat_style.setCurrentIndex(max(index, 0))
        self._repeat_style.blockSignals(False)
        self._state.repeat_style = normalized

    def _set_thumbnail_size(self, value: int) -> None:
        self._state.thumbnail_size = max(16, int(value))
        self._list.setIconSize(QtCore.QSize(self._state.thumbnail_size, self._state.thumbnail_size))
        self._update_timeline_grid_size(self._state.thumbnail_size)
        for row in range(self._list.count()):
            self._resize_item(self._list.item(row))
        self._refresh_all_timeline_icons()
        self._notify_state_changed()

    def _resize_item(self, item: QtWidgets.QListWidgetItem) -> None:
        size = max(16, int(self._thumbnail_size.value()))
        label_space = self._list.fontMetrics().height() + 6
        item.setSizeHint(QtCore.QSize(size + 24, size + label_space))

    def _update_timeline_grid_size(self, thumbnail_size: int) -> None:
        size = max(16, int(thumbnail_size))
        label_space = self._list.fontMetrics().height() + 6
        self._list.setGridSize(QtCore.QSize(size + 24, size + label_space))
        self._update_timeline_height()

    def _update_timeline_height(self) -> None:
        horizontal_scrollbar = self._list.horizontalScrollBar()
        scrollbar_visible = horizontal_scrollbar.maximum() > horizontal_scrollbar.minimum()
        scrollbar_height = (
            self._list.style().pixelMetric(
                QtWidgets.QStyle.PixelMetric.PM_ScrollBarExtent,
                None,
                self._list,
            )
            if scrollbar_visible
            else 0
        )
        frame_height = 2 * self._list.frameWidth()
        layout = self._sequence_panel.layout()
        margins = layout.contentsMargins() if layout is not None else QtCore.QMargins()
        layout_top_and_bottom = margins.top() + margins.bottom()
        label_scrollbar_clearance = 4 if scrollbar_visible else 2
        self._sequence_panel.setFixedHeight(
            self._list.gridSize().height()
            + scrollbar_height
            + frame_height
            + layout_top_and_bottom
            + label_scrollbar_clearance
        )

    def _set_preview_animation_note_visible(self, visible: bool) -> None:
        if self._preview_animation_note is not None:
            self._preview_animation_note.setVisible(bool(visible))
            if visible:
                self._preview_animation_note.raise_()

    def _reserve_preview_note_height(self) -> None:
        containers = (self._plot_note_container, self._proxy_note_container)

        def required_height(container: QtWidgets.QWidget) -> int:
            layout = container.layout()
            if layout is None:
                return container.sizeHint().height()
            margins = layout.contentsMargins()
            widgets = [
                layout.itemAt(index).widget()
                for index in range(layout.count())
                if layout.itemAt(index).widget() is not None
            ]
            content_height = sum(widget.sizeHint().height() for widget in widgets)
            spacing_height = max(len(widgets) - 1, 0) * max(layout.spacing(), 0)
            return margins.top() + content_height + spacing_height + margins.bottom()

        reserved_height = max(required_height(container) for container in containers)
        for container in containers:
            container.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Preferred,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )
            container.setFixedHeight(reserved_height)

    def _update_waiting_for_snapshot_state(self) -> None:
        waiting = any(
            isinstance(frame, SlicerAnimationFrame) for frame in self._state.frames
        ) and self._base_snapshot_path() is None
        self._waiting_for_snapshot.setEnabled(waiting)
        self._waiting_for_snapshot.setVisible(waiting)

    def _base_snapshot_path(self) -> Path | None:
        text = str(self._state.base_snapshot_path or "").strip()
        if not text:
            return None
        path = Path(text)
        return path if path.exists() else None

    def _start_slicer_thumbnail_generation(self) -> None:
        self._cancel_thumbnail_generation()
        if self._base_snapshot_path() is None:
            self._update_control_states()
            return
        self._thumbnail_generation_queue = [
            frame.frame_id
            for frame in self._state.frames
            if isinstance(frame, SlicerAnimationFrame)
            and not self._preview_cache.has_source(frame.frame_id)
        ]
        if self._thumbnail_generation_queue:
            self._begin_thumbnail_activity()
            self._thumbnail_generation_timer.start(0)
        self._update_control_states()

    def _queue_frame_thumbnail_refresh(self, frame_ids: Iterable[str]) -> None:
        requested = [
            frame_id
            for frame_id in dict.fromkeys(frame_ids)
            if AnimationSequenceService.frame_by_id(self._state.frames, frame_id)
            is not None
        ]
        if not requested or not self._can_render_all_thumbnails():
            return
        requested_set = set(requested)
        self._thumbnail_generation_queue = [
            frame_id
            for frame_id in self._thumbnail_generation_queue
            if frame_id not in requested_set
        ]
        self._thumbnail_generation_queue.extend(requested)
        self._sync_thumbnail_activity_total()
        if not self._thumbnail_generation_timer.isActive():
            self._thumbnail_generation_timer.start(0)
        self._update_control_states()

    def _cancel_thumbnail_generation(self) -> None:
        self._thumbnail_generation_timer.stop()
        self._thumbnail_generation_queue.clear()
        self._refreshing_all_thumbnails = False
        self._finish_thumbnail_activity()

    def _refresh_timeline_thumbnails(self) -> None:
        if not self._state.frames or not self._can_render_all_thumbnails():
            return
        self._pause()
        self._cancel_thumbnail_generation()
        self._refreshing_all_thumbnails = True
        self._thumbnail_generation_queue = [
            frame.frame_id for frame in self._state.frames
        ]
        self._begin_thumbnail_activity()
        self._thumbnail_generation_timer.start(0)
        self._update_control_states()

    def _generate_next_thumbnail(self) -> None:
        if not self._thumbnail_generation_queue:
            self._finish_thumbnail_generation()
            self._update_control_states()
            return
        frame_id = self._thumbnail_generation_queue.pop(0)
        frame = AnimationSequenceService.frame_by_id(self._state.frames, frame_id)
        base_snapshot = self._base_snapshot_path()
        if frame is not None:
            destination = self._rendered_thumbnail_path(frame.frame_id)
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(frame, SnapshotAnimationFrame):
                    self._plot.export_animation_frame_png(frame, destination)
                elif base_snapshot is not None:
                    self._plot.export_slicer_animation_frame_png(
                        frame, base_snapshot, destination
                    )
                else:
                    raise ValueError(
                        "Drop a base snapshot before updating slicer thumbnails."
                    )
                self._preview_cache.register_source(frame.frame_id, destination)
                self._refresh_timeline_icon(frame.frame_id)
            except Exception as error:  # noqa: BLE001 - restore batch UI for renderer/cache failures
                self._show_status(
                    f"Could not generate animation frame '{frame.label}': {error}"
                )
                self._thumbnail_generation_queue.clear()
                self._refreshing_all_thumbnails = False
                self._finish_thumbnail_activity()
            else:
                self._thumbnail_progress_completed += 1
                self._timeline_progress.setValue(
                    self._thumbnail_progress_completed
                )
        if self._thumbnail_generation_queue:
            self._thumbnail_generation_timer.start(0)
        else:
            self._finish_thumbnail_generation()
            self._update_control_states()

    def _finish_thumbnail_generation(self) -> None:
        if self._refreshing_all_thumbnails:
            self._refreshing_all_thumbnails = False
            self._state.thumbnails_dirty = False
            self._notify_state_changed()
            self._show_status("Updated all animation timeline thumbnails.")
        if self._timeline_progress.isVisible():
            self._timeline_progress.setValue(self._thumbnail_progress_total)
            self._flush_thumbnail_progress()
        self._finish_thumbnail_activity()

    def _begin_thumbnail_activity(self) -> None:
        self._finish_thumbnail_activity()
        self._thumbnail_progress_completed = 0
        self._thumbnail_progress_total = len(self._thumbnail_generation_queue)
        self._timeline_progress.setRange(
            0, max(1, self._thumbnail_progress_total)
        )
        self._timeline_progress.setValue(0)
        self._timeline_progress.setVisible(self._thumbnail_progress_total > 1)
        if self._activity_manager is not None and self._thumbnail_progress_total > 0:
            self._thumbnail_activity = self._activity_manager.busy(
                "Updating animation thumbnails…"
            )

    def _sync_thumbnail_activity_total(self) -> None:
        total = self._thumbnail_progress_completed + len(
            self._thumbnail_generation_queue
        )
        self._thumbnail_progress_total = total
        self._timeline_progress.setMaximum(max(1, total))
        self._timeline_progress.setValue(self._thumbnail_progress_completed)
        self._timeline_progress.setVisible(total > 1)
        if self._thumbnail_activity is None:
            if total > 0 and self._activity_manager is not None:
                self._thumbnail_activity = self._activity_manager.busy(
                    "Updating animation thumbnails…"
                )

    def _finish_thumbnail_activity(self) -> None:
        if self._thumbnail_activity is not None:
            self._thumbnail_activity.finish()
            self._thumbnail_activity = None
        self._timeline_progress.hide()
        self._timeline_progress.setRange(0, 1)
        self._timeline_progress.setValue(0)
        self._thumbnail_progress_completed = 0
        self._thumbnail_progress_total = 0

    @staticmethod
    def _flush_thumbnail_progress() -> None:
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.processEvents(
                QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
            )

    def _refresh_all_timeline_icons(self) -> None:
        for frame in self._ordered_frames():
            self._refresh_timeline_icon(frame.frame_id)

    def _rendered_thumbnail_path(self, frame_id: str) -> Path:
        if self._project is not None:
            return (
                self._project.paths.state
                / "animation_thumbnails"
                / f"{frame_id}.png"
            )
        return Path(self._thumbnail_directory.name) / f"{frame_id}.png"

    def _existing_rendered_thumbnail(
        self, frame: AnimationFrame
    ) -> Path | None:
        path = self._rendered_thumbnail_path(frame.frame_id)
        if not path.exists():
            return None
        candidates = (
            (Path(frame.snapshot_path),)
            if isinstance(frame, SnapshotAnimationFrame)
            else (self._base_snapshot_path(), Path(frame.slicer_preset_path))
        )
        reference_paths = [
            candidate
            for candidate in candidates
            if candidate is not None and candidate.exists()
        ]
        try:
            if reference_paths and path.stat().st_mtime < max(
                reference.stat().st_mtime for reference in reference_paths
            ):
                return None
        except OSError:
            return None
        return path

    def _remove_cached_rendered_thumbnails(
        self, frames: Iterable[AnimationFrame] | None = None
    ) -> None:
        for frame in self._state.frames if frames is None else frames:
            try:
                self._rendered_thumbnail_path(frame.frame_id).unlink(
                    missing_ok=True
                )
            except OSError:
                pass

    def _refresh_timeline_icon(self, frame_id: str) -> None:
        item = self._item_for_frame_id(frame_id)
        if item is None:
            return
        pixmap = self._preview_cache.thumbnail(frame_id, self._state.thumbnail_size)
        item.setIcon(QtGui.QIcon(pixmap) if not pixmap.isNull() else self._placeholder_icon())

    def _update_control_states(self) -> None:
        navigation_enabled = self._list.count() >= 2
        has_slicer_frames = any(
            isinstance(frame, SlicerAnimationFrame) for frame in self._state.frames
        )
        slicer_thumbnails_ready = all(
            not isinstance(frame, SlicerAnimationFrame)
            or self._preview_cache.has_source(frame.frame_id)
            for frame in self._state.frames
        )
        generating_thumbnails = bool(
            self._thumbnail_generation_queue
            or self._thumbnail_generation_timer.isActive()
        )
        play_enabled = navigation_enabled and not generating_thumbnails and not (
            has_slicer_frames
            and (
                self._base_snapshot_path() is None
                or not slicer_thumbnails_ready
            )
        )
        self._play.setEnabled(play_enabled)
        self._previous.setEnabled(navigation_enabled)
        self._next.setEnabled(navigation_enabled)
        current_frame = self._current_frame()
        show_apply_changes = (
            len(self._state.frames) >= 2
            and current_frame is not None
            and (
                current_frame.frame_id in self._state.pending_apply_frame_ids
                or self._state.thumbnails_dirty
            )
        )
        self._apply_changes_to_all.setVisible(show_apply_changes)
        self._apply_changes_to_all.setEnabled(
            show_apply_changes
            and not generating_thumbnails
            and self._can_render_all_thumbnails()
        )
        if show_apply_changes:
            self._position_apply_changes_button()
            self._apply_changes_to_all.raise_()
        if not play_enabled:
            self._pause()

    def _update_play_icon(self) -> None:
        self._play.setText("")
        self._play.setProperty(
            "iconName", "animation_pause" if self._play.isChecked() else "animation_play"
        )
        apply_icon_color(self._play)

    def _position_apply_changes_button(self) -> None:
        button = self._apply_changes_to_all
        button.adjustSize()
        margin = 12
        button.move(
            max(margin, self._plot_preview_page.width() - button.width() - margin),
            max(margin, self._plot_preview_page.height() - button.height() - margin),
        )

    def _notify_state_changed(self) -> None:
        if not self._restoring and self._state_changed_callback is not None:
            self._state_changed_callback()

    def _handle_plot_state_changed(self) -> None:
        # Figure settings belong to the authoritative Matplotlib preview. The
        # timeline deliberately keeps its original lightweight thumbnails;
        # regenerating every frame here makes one Apply action scale with the
        # whole animation instead of the selected plot.
        if self._state.frames:
            self._state.thumbnails_dirty = True
        self._update_control_states()
        self._notify_state_changed()

    def _handle_plot_edit(self, edit: dict[str, Any]) -> None:
        frame = self._current_frame()
        if frame is None:
            return
        values = edit.get("values")
        if not isinstance(values, dict):
            return
        overrides = copy.deepcopy(frame.plot_overrides)
        if edit.get("kind") == "pin":
            annotation_id = str(edit.get("annotation_id") or "").strip()
            if not annotation_id:
                return
            pins = overrides.setdefault("pins", {})
            if not isinstance(pins, dict):
                pins = {}
                overrides["pins"] = pins
            pins[annotation_id] = copy.deepcopy(values)
        elif edit.get("kind") == "render_state":
            state = overrides.setdefault("state", {})
            if not isinstance(state, dict):
                state = {}
                overrides["state"] = state
            state.update(copy.deepcopy(values))
        else:
            return
        self._replace_frame(replace(frame, plot_overrides=overrides))
        self._state.pending_apply_frame_ids.add(frame.frame_id)
        self._queue_frame_thumbnail_refresh([frame.frame_id])
        self._update_control_states()
        self._notify_state_changed()

    def _apply_current_frame_changes_to_all(self) -> None:
        source = self._current_frame()
        if source is None:
            return
        updated_frames: list[AnimationFrame] = []
        for frame in self._state.frames:
            if frame.frame_id == source.frame_id:
                updated_frames.append(frame)
                continue
            merged = copy.deepcopy(frame.plot_overrides)
            source_pins = source.plot_overrides.get("pins")
            if isinstance(source_pins, dict):
                target_pins = merged.setdefault("pins", {})
                if not isinstance(target_pins, dict):
                    target_pins = {}
                    merged["pins"] = target_pins
                target_pins.update(copy.deepcopy(source_pins))
            source_state = source.plot_overrides.get("state")
            if isinstance(source_state, dict):
                target_state = merged.setdefault("state", {})
                if not isinstance(target_state, dict):
                    target_state = {}
                    merged["state"] = target_state
                target_state.update(copy.deepcopy(source_state))
            updated_frames.append(replace(frame, plot_overrides=merged))
        self._state.frames = updated_frames
        self._state.pending_apply_frame_ids.discard(source.frame_id)
        self._update_control_states()
        self._notify_state_changed()
        self._show_status(
            f"Applied current frame changes to {len(updated_frames) - 1} other frame(s)."
        )
        self._refresh_timeline_thumbnails()

    def _replace_frame(self, replacement: AnimationFrame) -> None:
        self._state.frames = [
            replacement if frame.frame_id == replacement.frame_id else frame
            for frame in self._state.frames
        ]

    def _can_render_all_thumbnails(self) -> bool:
        return not any(
            isinstance(frame, SlicerAnimationFrame) for frame in self._state.frames
        ) or self._base_snapshot_path() is not None

    def _current_frame(self) -> AnimationFrame | None:
        return self._frame_for_item(self._list.currentItem())

    def _frame_for_item(
        self, item: QtWidgets.QListWidgetItem | None
    ) -> AnimationFrame | None:
        if item is None:
            return None
        return AnimationSequenceService.frame_by_id(
            self._state.frames,
            str(item.data(QtCore.Qt.ItemDataRole.UserRole) or ""),
        )

    def _item_for_frame_id(self, frame_id: str) -> QtWidgets.QListWidgetItem | None:
        row = self._row_for_frame_id(frame_id)
        return self._list.item(row) if row >= 0 else None

    def _row_for_frame_id(self, frame_id: str) -> int:
        for row in range(self._list.count()):
            if str(self._list.item(row).data(QtCore.Qt.ItemDataRole.UserRole) or "") == frame_id:
                return row
        return -1

    def _frame_payload(self, frame: AnimationFrame) -> dict[str, Any]:
        if isinstance(frame, SnapshotAnimationFrame):
            return {
                "kind": "snapshot",
                "frame_id": frame.frame_id,
                "label": frame.label,
                "snapshot_path": self._stored_path(frame.snapshot_path),
                "plot_overrides": copy.deepcopy(frame.plot_overrides),
            }
        return {
            "kind": "slicer",
            "frame_id": frame.frame_id,
            "label": frame.label,
            "slicer_preset_path": self._stored_path(frame.slicer_preset_path),
            "column": frame.column,
            "active_values": list(frame.active_values),
            "range_start": frame.range_start,
            "range_end": frame.range_end,
            "plot_overrides": copy.deepcopy(frame.plot_overrides),
        }

    def _frame_from_payload(self, value: object) -> AnimationFrame | None:
        if not isinstance(value, dict):
            return None
        kind = str(value.get("kind") or "snapshot")
        if kind == "snapshot":
            snapshot_path = self._resolved_path(value.get("snapshot_path"))
            if snapshot_path is None:
                return None
            return SnapshotAnimationFrame(
                frame_id=str(value.get("frame_id") or uuid4().hex),
                label=str(value.get("label") or snapshot_path.stem),
                snapshot_path=str(snapshot_path),
                plot_overrides=self._normalized_plot_overrides(
                    value.get("plot_overrides")
                ),
            )
        if kind != "slicer":
            return None
        preset_path = self._resolved_path(
            value.get("slicer_preset_path", value.get("slicer_path"))
        )
        column = str(value.get("column") or "").strip()
        active_values = value.get("active_values", value.get("values"))
        if preset_path is None or not column or not isinstance(active_values, list):
            return None
        range_start = str(value.get("range_start") or "")
        range_end = str(value.get("range_end") or range_start)
        return SlicerAnimationFrame(
            frame_id=str(value.get("frame_id") or uuid4().hex),
            label=str(value.get("label") or f"{range_start} – {range_end}"),
            slicer_preset_path=str(preset_path),
            column=column,
            active_values=tuple(str(item) for item in active_values),
            range_start=range_start,
            range_end=range_end,
            plot_overrides=self._normalized_plot_overrides(
                value.get("plot_overrides")
            ),
        )

    @staticmethod
    def _normalized_plot_overrides(value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        normalized: dict[str, Any] = {}
        pins = value.get("pins")
        if isinstance(pins, dict):
            normalized_pins: dict[str, dict[str, Any]] = {}
            for annotation_id, pin_override in pins.items():
                key = str(annotation_id).strip()
                if not key or not isinstance(pin_override, dict):
                    continue
                normalized_pin: dict[str, Any] = {}
                if bool(pin_override.get("removed", False)):
                    normalized_pin["removed"] = True
                for position_key in ("box_anchor", "screen_offset"):
                    position = pin_override.get(position_key)
                    if not isinstance(position, list) or len(position) != 2:
                        continue
                    try:
                        normalized_pin[position_key] = [
                            float(position[0]),
                            float(position[1]),
                        ]
                    except (TypeError, ValueError):
                        continue
                if normalized_pin:
                    normalized_pins[key] = normalized_pin
            if normalized_pins:
                normalized["pins"] = normalized_pins
        state = value.get("state")
        if isinstance(state, dict):
            normalized_state = {
                str(key): copy.deepcopy(item)
                for key, item in state.items()
                if str(key)
                in {"camera", "appearance", "filters", "transform", "preset_paths"}
            }
            if normalized_state:
                normalized["state"] = normalized_state
        return normalized

    @staticmethod
    def _frame_references_exist(frame: AnimationFrame) -> bool:
        if isinstance(frame, SnapshotAnimationFrame):
            return Path(frame.snapshot_path).exists()
        return Path(frame.slicer_preset_path).exists()

    def _stored_path(self, value: str) -> str:
        path = Path(value)
        if self._project_root is not None:
            try:
                return str(path.resolve().relative_to(self._project_root.resolve()))
            except ValueError:
                pass
        return str(path)

    def _resolved_path(self, value: object) -> Path | None:
        text = str(value or "").strip()
        if not text:
            return None
        path = Path(text)
        if not path.is_absolute() and self._project_root is not None:
            path = self._project_root / path
        return path.resolve()

    @staticmethod
    def _snapshot_path_from_mime(mime: QtCore.QMimeData) -> Path | None:
        for url in mime.urls():
            if url.isLocalFile():
                path = Path(url.toLocalFile())
                if path.suffix.lower() == ".json":
                    return path
        text = str(mime.text() or "").strip()
        if text:
            path = Path(text)
            if path.suffix.lower() == ".json":
                return path
        return None

    @staticmethod
    def _load_payload(path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _placeholder_icon(self) -> QtGui.QIcon:
        size = max(16, int(self._thumbnail_size.value()))
        pixmap = QtGui.QPixmap(size, size)
        pixmap.fill(QtGui.QColor("#d9dde3"))
        return QtGui.QIcon(pixmap)

    @staticmethod
    def _bounded_int(value: object, minimum: int, maximum: int, default: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = default
        return min(max(number, minimum), maximum)

    def _show_status(self, message: str) -> None:
        if self._status_callback is not None:
            self._status_callback(message)


SnapshotAnimationTabController = AnimationTabController
