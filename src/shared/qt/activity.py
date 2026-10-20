from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from itertools import count
from typing import Literal
import weakref

from PySide6 import QtCore, QtGui, QtWidgets

from shared.qt.icons import apply_icon_color
from shared.qt.background_processes import BackgroundProcessManager


class _ActivityPresenter:
    def __init__(self, manager: "ActivityManager", button: QtWidgets.QToolButton) -> None:
        self._manager = manager
        self.button = button
        self.bar = manager._progress_bar
        self.rows: dict[int, QtWidgets.QWidget] = {}

    def refresh_rows(self) -> None:
        for row in self.rows.values():
            row.deleteLater()
        self.rows = {}
        for activity_id, record in self._manager._activities.items():
            if record.kind != "progress":
                continue
            row = QtWidgets.QWidget(self._manager.indicator)
            layout = QtWidgets.QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            bar = QtWidgets.QProgressBar(row)
            bar.setObjectName("progress_job")
            if record.total > 0:
                bar.setRange(0, record.total)
                bar.setValue(record.value)
            else:
                bar.setRange(0, 0)
            layout.addWidget(bar)
            self.rows[activity_id] = row

    def refresh_summary(self) -> None:
        records = list(self._manager.processes.records.values())
        visible_activities = [record for record in self._manager._activities.values() if record.visible]
        running = sum(record.state == "running" for record in records) + len(visible_activities)
        queued = sum(record.state == "queued" for record in records)
        paused = sum(record.state == "paused" for record in records)
        if not records and len(visible_activities) == 1:
            self.button.setText(f"{visible_activities[0].message} ▴")
            self.button.show()
            return
        parts = []
        if running:
            parts.append(f"{running} running")
        if queued:
            parts.append(f"{queued} queued")
        if paused:
            parts.append(f"{paused} paused")
        self.button.setText(" · ".join(parts) + (" ▴" if parts else ""))
        self.button.setVisible(bool(parts))


ActivityKind = Literal["busy", "progress"]


@dataclass
class _ActivityRecord:
    kind: ActivityKind
    message: str
    value: int = 0
    total: int = 0
    visible: bool = True
    overlay: QtWidgets.QFrame | None = None
    cancel: Callable[[], None] | None = None
    cancel_requested: bool = False
    failed: bool = False


class ActivityToken:
    """A finish-once handle for one UI activity."""

    def __init__(self, manager: "ActivityManager", activity_id: int) -> None:
        self._manager = weakref.ref(manager)
        self._activity_id = activity_id
        self._finished = False

    def update(
        self,
        *,
        message: str | None = None,
        value: int | None = None,
        total: int | None = None,
    ) -> None:
        if self._finished:
            return
        manager = self._manager()
        if manager is not None:
            manager._update(self._activity_id, message=message, value=value, total=total)

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        manager = self._manager()
        if manager is not None:
            manager._finish(self._activity_id)

    def retain_completed(self) -> None:
        """Keep a completed progress activity visible until new status arrives."""

        if self._finished:
            return
        self._finished = True
        manager = self._manager()
        if manager is not None:
            manager._retain_completed(self._activity_id)

    def retain_failed(self, message: str) -> None:
        """Keep a failed progress activity and its reason visible."""

        if self._finished:
            return
        self._finished = True
        manager = self._manager()
        if manager is not None:
            manager._retain_failed(self._activity_id, message)

    def __enter__(self) -> "ActivityToken":
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.finish()


class ActivityManager(QtCore.QObject):
    """Coordinates native wait cursors and status-bar activity text."""

    def __init__(self, status_bar: QtWidgets.QStatusBar) -> None:
        super().__init__(status_bar)
        self._status_bar = status_bar
        self._ids = count(1)
        self._activities: dict[int, _ActivityRecord] = {}
        self._retained_activity_ids: set[int] = set()
        self._busy_ids: set[int] = set()
        self._owns_override_cursor = False

        self.indicator = QtWidgets.QWidget(status_bar)
        self.indicator.setObjectName("global_activity_indicator")
        layout = QtWidgets.QHBoxLayout(self.indicator)
        layout.setContentsMargins(6, 0, 0, 0)
        layout.setSpacing(6)
        self._label = QtWidgets.QLabel(self.indicator)
        self._label.setObjectName("label_global_activity")
        self._label.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight
            | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(self._label)
        self._progress_bar = QtWidgets.QProgressBar(self.indicator)
        self._progress_bar.setObjectName("progress_global_activity")
        self._progress_bar.setTextVisible(True)
        self._progress_bar.setFixedWidth(140)
        self._progress_bar.hide()
        layout.addWidget(self._progress_bar)
        self._cancel_button = QtWidgets.QToolButton(self.indicator)
        self._cancel_button.setObjectName("button_cancel_global_activity")
        self._cancel_button.setProperty("iconName", "close")
        self._cancel_button.setAccessibleName("Abort export")
        self._cancel_button.setToolTip("Abort export")
        self._cancel_button.setAutoRaise(True)
        self._cancel_button.setIconSize(QtCore.QSize(10, 10))
        self._cancel_button.setFixedSize(14, 14)
        self._cancel_button.clicked.connect(self._cancel_visible_activity)
        self._cancel_button.hide()
        layout.addWidget(self._cancel_button)
        self._process_button = QtWidgets.QToolButton(self.indicator)
        self._process_button.setObjectName("button_activities")
        self._process_button.hide()
        layout.addWidget(self._process_button)
        # Keep transient activity controls within the status bar's normal line
        # height. Without this bound, showing the indicator briefly increases
        # the status bar and shifts the entire workspace by a few pixels.
        indicator_height = max(
            self._label.sizeHint().height(),
            self._cancel_button.height(),
        ) + 2
        self.indicator.setFixedHeight(indicator_height)
        apply_icon_color(self.indicator)
        status_bar.addPermanentWidget(self.indicator)
        status_bar.messageChanged.connect(self._clear_retained_activities)
        self.indicator.hide()
        self.processes = BackgroundProcessManager(changed=self._background_processes_changed)
        self.presenter = _ActivityPresenter(self, self._process_button)

    @property
    def records(self):
        return self.processes.records.items()

    def _background_processes_changed(self) -> None:
        self.presenter.refresh_summary()
        if self.processes.records:
            self.indicator.show()
        elif not self._activities:
            self.indicator.hide()
        self.changed.emit()

    changed = QtCore.Signal()

    def busy(self, message: str) -> ActivityToken:
        """Show the native wait cursor for short synchronous work."""

        return self._start("busy", message, show_progress=False)

    def progress(
        self,
        message: str,
        *,
        value: int = 0,
        total: int = 0,
        cancel: Callable[[], None] | None = None,
    ) -> ActivityToken:
        """Report a longer foreground operation in the status bar."""

        return self._start(
            "progress", message, value=value, total=total, cancel=cancel
        )

    def begin(
        self,
        message: str,
        *,
        delay_ms: int = 150,
        immediate: bool = False,
        value: int = 0,
        total: int = 0,
        scope: QtWidgets.QWidget | None = None,
    ) -> ActivityToken:
        """Compatibility entry point for callers that need delayed activity UI."""

        self._clear_retained_activities()
        activity_id = next(self._ids)
        record = _ActivityRecord(
            kind="busy",
            message=str(message),
            value=max(0, int(value)),
            total=max(0, int(total)),
            visible=bool(immediate),
        )
        if scope is not None:
            record.overlay = self._create_overlay(scope, record)
        self._activities[activity_id] = record
        if record.kind == "busy":
            self._add_busy(activity_id)
        if immediate:
            if record.overlay is not None:
                record.overlay.show()
                record.overlay.raise_()
            self._refresh_indicator()
            self._flush_non_input_events()
        else:
            QtCore.QTimer.singleShot(
                max(0, int(delay_ms)),
                lambda current_id=activity_id: self._make_visible(current_id),
            )
        return ActivityToken(self, activity_id)

    def _start(
        self,
        kind: ActivityKind,
        message: str,
        *,
        value: int = 0,
        total: int = 0,
        show_progress: bool = True,
        cancel: Callable[[], None] | None = None,
    ) -> ActivityToken:
        self._clear_retained_activities()
        activity_id = next(self._ids)
        self._activities[activity_id] = _ActivityRecord(
            kind=kind,
            message=str(message),
            value=max(0, int(value)),
            total=max(0, int(total)),
            visible=True,
            cancel=cancel,
        )
        if kind == "busy":
            self._add_busy(activity_id)
            if not show_progress:
                self._activities[activity_id].total = -1
        self._refresh_indicator()
        self._flush_non_input_events()
        return ActivityToken(self, activity_id)

    def _add_busy(self, activity_id: int) -> None:
        self._busy_ids.add(activity_id)
        if self._owns_override_cursor:
            return
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.setOverrideCursor(QtGui.QCursor(QtCore.Qt.CursorShape.WaitCursor))
            self._owns_override_cursor = True

    def _make_visible(self, activity_id: int) -> None:
        record = self._activities.get(activity_id)
        if record is None:
            return
        record.visible = True
        if record.overlay is not None:
            record.overlay.show()
            record.overlay.raise_()
        self._refresh_indicator()

    def _update(
        self,
        activity_id: int,
        *,
        message: str | None,
        value: int | None,
        total: int | None,
    ) -> None:
        record = self._activities.get(activity_id)
        if record is None:
            return
        if message is not None:
            record.message = str(message)
        if total is not None:
            record.total = max(0, int(total))
        if value is not None:
            record.value = max(0, int(value))
        if record.total > 0:
            record.value = min(record.value, record.total)
        self._refresh_indicator()
        self._flush_non_input_events()

    def _finish(self, activity_id: int) -> None:
        record = self._activities.pop(activity_id, None)
        self._retained_activity_ids.discard(activity_id)
        if record is None:
            return
        self._busy_ids.discard(activity_id)
        if record.overlay is not None:
            record.overlay.hide()
            record.overlay.deleteLater()
        if not self._busy_ids and self._owns_override_cursor:
            app = QtWidgets.QApplication.instance()
            if app is not None:
                app.restoreOverrideCursor()
            self._owns_override_cursor = False
        self._refresh_indicator()

    def _retain_completed(self, activity_id: int) -> None:
        record = self._activities.get(activity_id)
        if record is None:
            return
        if record.total > 0:
            record.value = record.total
        record.cancel = None
        record.cancel_requested = False
        record.failed = False
        self._retained_activity_ids.add(activity_id)
        self._refresh_indicator()

    def _retain_failed(self, activity_id: int, message: str) -> None:
        record = self._activities.get(activity_id)
        if record is None:
            return
        record.message = str(message)
        record.cancel = None
        record.cancel_requested = False
        record.failed = True
        self._retained_activity_ids.add(activity_id)
        self._refresh_indicator()

    def _clear_retained_activities(self, *_args: object) -> None:
        if not self._retained_activity_ids:
            return
        for activity_id in tuple(self._retained_activity_ids):
            self._activities.pop(activity_id, None)
        self._retained_activity_ids.clear()
        self._refresh_indicator()

    def _refresh_indicator(self) -> None:
        if hasattr(self, "presenter"):
            self.presenter.refresh_rows()
            self.presenter.refresh_summary()
        visible = [
            (activity_id, record)
            for activity_id, record in self._activities.items()
            if record.visible
        ]
        if not visible:
            self._progress_bar.hide()
            self._cancel_button.hide()
            if not (hasattr(self, "processes") and self.processes.records):
                self.indicator.hide()
            return
        _activity_id, record = visible[-1]
        self._label.setText(record.message)
        if record.kind == "progress":
            if record.total > 0:
                self._progress_bar.setRange(0, record.total)
                self._progress_bar.setValue(record.value)
                self._progress_bar.setFormat("Failed" if record.failed else "%p%")
                self._progress_bar.show()
            else:
                self._progress_bar.hide()
            self._cancel_button.setVisible(record.cancel is not None)
            self._cancel_button.setEnabled(not record.cancel_requested)
        else:
            self._progress_bar.hide()
            self._cancel_button.hide()
        self.indicator.show()

    def run(
        self,
        title: str,
        function: Callable[[], object],
        completed: Callable[[object], None],
        failed: Callable[[str], None],
    ):
        from shared.qt.scheduled_future import ScheduledFuture

        if not hasattr(self, "_background_executor"):
            from concurrent.futures import ThreadPoolExecutor
            self._background_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="activity")
        operation = ScheduledFuture(
            self,
            self._background_executor,
            function,
            panel="Activity",
            title=title,
            completed=completed,
            failed=failed,
        )
        self.processes.dispatch()
        return operation

    def shutdown(self) -> None:
        self.processes.shutdown()
        executor = getattr(self, "_background_executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
        for activity_id in tuple(self._activities):
            self._finish(activity_id)

    def _cancel_visible_activity(self) -> None:
        visible = [
            record for record in self._activities.values() if record.visible
        ]
        if not visible:
            return
        record = visible[-1]
        if record.cancel is None or record.cancel_requested:
            return
        record.cancel_requested = True
        self._cancel_button.setEnabled(False)
        record.cancel()

    @staticmethod
    def _create_overlay(
        target: QtWidgets.QWidget, record: _ActivityRecord
    ) -> QtWidgets.QFrame:
        overlay = QtWidgets.QFrame(target)
        overlay.setObjectName("activity_overlay")
        overlay.setGeometry(target.rect())
        overlay.setStyleSheet(
            "QFrame#activity_overlay { background: rgba(0, 0, 0, 40); }"
        )
        layout = QtWidgets.QVBoxLayout(overlay)
        layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        label = QtWidgets.QLabel(record.message, overlay)
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        overlay.hide()
        return overlay

    @staticmethod
    def _flush_non_input_events() -> None:
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
