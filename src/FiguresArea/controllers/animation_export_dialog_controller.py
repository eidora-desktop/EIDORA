from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import re
from typing import TYPE_CHECKING, cast

from PySide6 import QtCore, QtGui, QtWidgets

from FiguresArea.animation_export_service import (
    AnimationExportService,
    AnimationExportWorkspace,
)
from FiguresArea.domain import (
    AnimationExportQuality,
    AnimationExportResult,
    AnimationExportSettings,
    AnimationFrame,
    AnimationRepeatStyle,
)
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child

if TYPE_CHECKING:
    from shared.qt.activity import ActivityManager, ActivityToken


class AnimationExportDialogController(QtCore.QObject):
    """Own one modal animated GIF configuration and progress workflow."""

    def __init__(
        self,
        *,
        service: AnimationExportService | None = None,
        preferences: QtCore.QSettings | None = None,
        status_callback: Callable[[str], None] | None = None,
        activity_manager: ActivityManager | None = None,
    ) -> None:
        super().__init__()
        self._service = service or AnimationExportService()
        self._preferences = preferences or QtCore.QSettings()
        self._status_callback = status_callback
        self._activity_manager = activity_manager
        self._status_activity: ActivityToken | None = None
        self._dialog: QtWidgets.QDialog | None = None
        self._workspace: AnimationExportWorkspace | None = None
        self._process: QtCore.QProcess | None = None
        self._kill_timer: QtCore.QTimer | None = None
        self._frames: list[AnimationFrame] = []
        self._interval_ms = 150
        self._repeat_style: AnimationRepeatStyle = "from_start"
        self._source_dimensions = (2, 2)
        self._render_frame: Callable[[AnimationFrame, Path], None] | None = None
        self._result: AnimationExportResult | None = None
        self._render_index = 0
        self._running = False
        self._stopping = False
        self._stderr = ""

    def exec(
        self,
        *,
        frames: list[AnimationFrame],
        interval_ms: int,
        repeat_style: AnimationRepeatStyle,
        source_dimensions: tuple[int, int],
        render_frame: Callable[[AnimationFrame, Path], None],
        suggested_stem: str,
    ) -> AnimationExportResult | None:
        widget = load_ui("FiguresArea", "ui/animation_export_dialog.ui", None)
        if not isinstance(widget, QtWidgets.QDialog):
            return None
        self._dialog = widget
        self._frames = list(frames)
        self._interval_ms = max(1, int(interval_ms))
        self._repeat_style = repeat_style
        self._source_dimensions = (
            max(2, int(source_dimensions[0])),
            max(2, int(source_dimensions[1])),
        )
        self._render_frame = render_frame
        self._result = None
        self._configure_widgets(self._safe_stem(suggested_stem))
        widget.installEventFilter(self)
        widget.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        try:
            widget.exec()
            return self._result
        finally:
            widget.removeEventFilter(self)
            self._dispose_active_work()
            self._dialog = None
            self._render_frame = None

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if watched is self._dialog and self._running:
            if event.type() == QtCore.QEvent.Type.Close:
                event.ignore()
                return True
            if (
                event.type() == QtCore.QEvent.Type.KeyPress
                and isinstance(event, QtGui.QKeyEvent)
                and event.key() == QtCore.Qt.Key.Key_Escape
            ):
                return True
        return super().eventFilter(watched, event)

    def _configure_widgets(self, suggested_stem: str) -> None:
        dialog = self._required_dialog()
        quality = self._combo("combo_animation_export_quality")
        for index, value in enumerate(("highest", "high", "medium", "low")):
            quality.setItemData(index, value)
        quality.setCurrentIndex(quality.findData("high"))
        self._load_preferences()
        self._restore_original_size()

        documents = QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.StandardLocation.DocumentsLocation
        ) or QtCore.QDir.homePath()
        self._edit("edit_animation_export_destination").setText(
            str(Path(documents) / f"{suggested_stem}.gif")
        )

        self._spin("spin_animation_export_width").valueChanged.connect(
            self._width_changed
        )
        self._spin("spin_animation_export_height").valueChanged.connect(
            self._height_changed
        )
        self._button("button_restore_animation_export_original_size").clicked.connect(
            self._restore_original_size
        )
        self._button("button_browse_animation_export_destination").clicked.connect(
            self._browse
        )
        self._button("button_cancel_animation_export").clicked.connect(
            self._cancel_or_stop
        )
        self._button("button_start_animation_export").clicked.connect(
            self._start_export
        )
        self._progress().setValue(0)

    def _restore_original_size(self) -> None:
        width = self._spin("spin_animation_export_width")
        height = self._spin("spin_animation_export_height")
        width_blocker = QtCore.QSignalBlocker(width)
        height_blocker = QtCore.QSignalBlocker(height)
        width.setValue(self._source_dimensions[0])
        height.setValue(self._source_dimensions[1])
        del width_blocker, height_blocker

    def _width_changed(self, width: int) -> None:
        if self._aspect_locked():
            ratio = self._source_dimensions[1] / self._source_dimensions[0]
            height = max(2, round(int(width) * ratio))
            target = self._spin("spin_animation_export_height")
            blocker = QtCore.QSignalBlocker(target)
            target.setValue(height)
            del blocker

    def _height_changed(self, height: int) -> None:
        if self._aspect_locked():
            ratio = self._source_dimensions[0] / self._source_dimensions[1]
            width = max(2, round(int(height) * ratio))
            target = self._spin("spin_animation_export_width")
            blocker = QtCore.QSignalBlocker(target)
            target.setValue(width)
            del blocker

    def _aspect_locked(self) -> bool:
        return require_child(
            self._required_dialog(),
            QtWidgets.QAbstractButton,
            "checkbox_animation_export_lock_aspect_ratio",
        ).isChecked()

    def _browse(self) -> None:
        path, _selected_filter = QtWidgets.QFileDialog.getSaveFileName(
            self._required_dialog(),
            "Export Animated GIF",
            self._edit("edit_animation_export_destination").text().strip(),
            "Animated GIF (*.gif)",
        )
        if path:
            self._edit("edit_animation_export_destination").setText(
                str(Path(path).with_suffix(".gif"))
            )

    def _start_export(self) -> None:
        try:
            settings = self._collect_settings()
            self._service.validate_request(self._frames, settings)
        except (OSError, ValueError) as error:
            self._show_configuration_error(str(error))
            return
        try:
            self._workspace = self._service.create_workspace(
                frames=self._frames,
                settings=settings,
                repeat_style=self._repeat_style,
            )
        except (OSError, ValueError) as error:
            self._show_configuration_error(str(error))
            return
        self._save_preferences(settings)
        self._running = True
        self._stopping = False
        self._render_index = 0
        self._stderr = ""
        self._set_export_running(True)
        self._progress().setRange(0, 100)
        if self._activity_manager is not None:
            self._status_activity = self._activity_manager.progress(
                "Preparing animation export…",
                value=0,
                total=100,
                cancel=self._stop_export,
            )
        self._set_progress(5, "Preparing animation frames…")
        self._required_dialog().raise_()
        self._required_dialog().activateWindow()
        QtCore.QTimer.singleShot(0, self._render_next_frame)

    def _render_next_frame(self) -> None:
        if not self._running or self._stopping:
            return
        workspace = self._workspace
        render_frame = self._render_frame
        if workspace is None or render_frame is None:
            self._fail("The animated GIF export was not initialized.")
            return
        if self._render_index >= len(self._frames):
            try:
                workspace.materialize_playback_sequence()
            except (OSError, ValueError) as error:
                self._fail(str(error))
                return
            self._start_encoder()
            return
        frame = self._frames[self._render_index]
        path = workspace.render_path(self._render_index)
        try:
            render_frame(frame, path)
        except Exception as error:  # noqa: BLE001 - renderer errors belong in the dialog
            self._fail(f"Could not render '{frame.label}': {error}")
            return
        workspace.register_rendered(frame, path)
        self._render_index += 1
        self._set_progress(
            5 + round(70 * self._render_index / max(1, len(self._frames))),
            "Rendering animation frames…",
        )
        QtCore.QTimer.singleShot(0, self._render_next_frame)

    def _start_encoder(self) -> None:
        workspace = self._workspace
        if workspace is None:
            self._fail("The animated GIF export workspace is unavailable.")
            return
        process = QtCore.QProcess(self._required_dialog())
        process.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.SeparateChannels)
        process.readyReadStandardOutput.connect(self._read_encoder_progress)
        process.readyReadStandardError.connect(self._read_encoder_error)
        process.errorOccurred.connect(self._encoder_error)
        process.finished.connect(self._encoder_finished)
        self._process = process
        process.start(
            self._service.worker_program(),
            self._service.worker_arguments(
                workspace=workspace,
                interval_ms=self._interval_ms,
                repeat_style=self._repeat_style,
            ),
        )

    def _read_encoder_progress(self) -> None:
        process = self._process
        workspace = self._workspace
        if process is None or workspace is None:
            return
        output = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        for line in output.splitlines():
            key, separator, value = line.partition("=")
            if separator and key == "frame":
                try:
                    frame = max(0, int(value))
                except ValueError:
                    continue
                total = max(1, len(workspace.playback_frames))
                self._set_progress(
                    75 + min(20, round(20 * frame / total)),
                    "Encoding animation…",
                )

    def _read_encoder_error(self) -> None:
        if self._process is not None:
            self._stderr += bytes(self._process.readAllStandardError()).decode(
                "utf-8", errors="replace"
            )

    def _encoder_error(self, error: QtCore.QProcess.ProcessError) -> None:
        if error == QtCore.QProcess.ProcessError.FailedToStart and self._running:
            self._fail("The animated GIF encoder could not be started.")

    def _encoder_finished(
        self,
        exit_code: int,
        exit_status: QtCore.QProcess.ExitStatus,
    ) -> None:
        if not self._running:
            return
        self._read_encoder_error()
        if self._stopping:
            self._return_to_configuration()
            return
        if exit_status != QtCore.QProcess.ExitStatus.NormalExit or exit_code != 0:
            detail = self._stderr.strip().splitlines()
            self._fail(detail[-1] if detail else "The animated GIF could not be encoded.")
            return
        workspace = self._workspace
        if workspace is None:
            self._fail("The animated GIF export workspace is unavailable.")
            return
        self._set_progress(98, "Finalizing animation export…")
        try:
            result = self._service.finalize(
                workspace=workspace,
                expected_dimensions=self._service.output_dimensions(workspace.settings),
            )
        except (OSError, RuntimeError) as error:
            self._fail(str(error))
            return
        self._result = result
        self._set_progress(100, "Animation export complete")
        self._running = False
        workspace.cleanup()
        self._workspace = None
        if self._status_callback is not None:
            self._status_callback(f"Exported animated GIF to {result.destination}")
        self._finish_status_activity()
        self._required_dialog().accept()

    def _cancel_or_stop(self) -> None:
        if self._running:
            self._stop_export()
        else:
            self._required_dialog().reject()

    def _stop_export(self) -> None:
        if not self._running:
            return
        self._stopping = True
        self._button("button_cancel_animation_export").setEnabled(False)
        process = self._process
        if process is None or process.state() == QtCore.QProcess.ProcessState.NotRunning:
            self._return_to_configuration()
            return
        process.terminate()
        timer = QtCore.QTimer(self._required_dialog())
        timer.setSingleShot(True)
        timer.timeout.connect(self._force_kill_encoder)
        timer.start(2000)
        self._kill_timer = timer

    def _force_kill_encoder(self) -> None:
        if (
            self._process is not None
            and self._process.state() != QtCore.QProcess.ProcessState.NotRunning
        ):
            self._process.kill()

    def _fail(self, message: str) -> None:
        if (
            self._process is not None
            and self._process.state() != QtCore.QProcess.ProcessState.NotRunning
        ):
            self._process.kill()
        if self._status_activity is not None:
            self._status_activity.retain_failed(
                f"Animation export failed: {message}"
            )
            self._status_activity = None
        self._return_to_configuration()
        self._show_configuration_error(message)

    def _return_to_configuration(self) -> None:
        self._running = False
        self._stopping = False
        if self._kill_timer is not None:
            self._kill_timer.stop()
            self._kill_timer = None
        if self._workspace is not None:
            self._workspace.cleanup()
            self._workspace = None
        self._process = None
        self._progress().setValue(0)
        self._finish_status_activity()
        self._set_export_running(False)

    def _dispose_active_work(self) -> None:
        self._running = False
        if self._kill_timer is not None:
            self._kill_timer.stop()
            self._kill_timer = None
        if self._process is not None:
            if self._process.state() != QtCore.QProcess.ProcessState.NotRunning:
                self._process.kill()
                self._process.waitForFinished(1000)
            self._process = None
        if self._workspace is not None:
            self._workspace.cleanup()
            self._workspace = None
        self._finish_status_activity()

    def _set_progress(self, value: int, message: str) -> None:
        self._progress().setValue(value)
        if self._status_activity is not None:
            self._status_activity.update(message=message, value=value, total=100)

    def _finish_status_activity(self) -> None:
        if self._status_activity is None:
            return
        self._status_activity.finish()
        self._status_activity = None

    def _collect_settings(self) -> AnimationExportSettings:
        text = self._edit("edit_animation_export_destination").text().strip()
        if not text:
            raise ValueError("Choose an output file.")
        destination = Path(text).expanduser().with_suffix(".gif")
        self._edit("edit_animation_export_destination").setText(str(destination))
        return AnimationExportSettings(
            destination=destination,
            quality=self._current_quality(),
            width=self._spin("spin_animation_export_width").value(),
            height=self._spin("spin_animation_export_height").value(),
            gif_dithering=require_child(
                self._required_dialog(),
                QtWidgets.QCheckBox,
                "checkbox_animation_export_gif_dithering",
            ).isChecked(),
        )

    def _load_preferences(self) -> None:
        group = "FiguresArea/animationExport/gif"
        quality_value = str(self._preferences.value(f"{group}/quality", "high"))
        quality = self._combo("combo_animation_export_quality")
        quality_index = quality.findData(quality_value)
        quality.setCurrentIndex(
            quality_index if quality_index >= 0 else quality.findData("high")
        )
        dithering = self._preferences.value(f"{group}/gifDithering", True, type=bool)
        require_child(
            self._required_dialog(),
            QtWidgets.QCheckBox,
            "checkbox_animation_export_gif_dithering",
        ).setChecked(bool(dithering))

    def _save_preferences(self, settings: AnimationExportSettings) -> None:
        group = "FiguresArea/animationExport/gif"
        self._preferences.setValue(f"{group}/quality", settings.quality)
        self._preferences.setValue(f"{group}/gifDithering", settings.gif_dithering)
        self._preferences.sync()

    def _current_quality(self) -> AnimationExportQuality:
        value = str(self._combo("combo_animation_export_quality").currentData() or "high")
        if value not in {"highest", "high", "medium", "low"}:
            return "high"
        return cast(AnimationExportQuality, value)

    def _show_configuration_error(self, message: str) -> None:
        QtWidgets.QMessageBox.warning(
            self._required_dialog(), "Export failed", str(message)
        )

    def _set_export_running(self, running: bool) -> None:
        for name in (
            "edit_animation_export_destination",
            "button_browse_animation_export_destination",
            "spin_animation_export_width",
            "spin_animation_export_height",
            "checkbox_animation_export_lock_aspect_ratio",
            "button_restore_animation_export_original_size",
            "combo_animation_export_quality",
            "checkbox_animation_export_gif_dithering",
            "button_start_animation_export",
        ):
            widget = self._required_dialog().findChild(QtWidgets.QWidget, name)
            if widget is not None:
                widget.setEnabled(not running)
        cancel = self._button("button_cancel_animation_export")
        cancel.setEnabled(True)

    def _required_dialog(self) -> QtWidgets.QDialog:
        if self._dialog is None:
            raise RuntimeError("The animated GIF export dialog is not open.")
        return self._dialog

    def _combo(self, name: str) -> QtWidgets.QComboBox:
        return require_child(self._required_dialog(), QtWidgets.QComboBox, name)

    def _spin(self, name: str) -> QtWidgets.QSpinBox:
        return require_child(self._required_dialog(), QtWidgets.QSpinBox, name)

    def _edit(self, name: str) -> QtWidgets.QLineEdit:
        return require_child(self._required_dialog(), QtWidgets.QLineEdit, name)

    def _button(self, name: str) -> QtWidgets.QAbstractButton:
        return require_child(self._required_dialog(), QtWidgets.QAbstractButton, name)

    def _progress(self) -> QtWidgets.QProgressBar:
        return require_child(
            self._required_dialog(),
            QtWidgets.QProgressBar,
            "progress_animation_export",
        )

    @staticmethod
    def _safe_stem(value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]+", "_", str(value)).strip("_") or "animation"
