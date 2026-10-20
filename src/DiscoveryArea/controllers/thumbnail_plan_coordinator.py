from __future__ import annotations

import threading

from PySide6 import QtCore

from DiscoveryArea.canvas.thumbnail_planner import (
    ThumbnailPlanCancelled,
    ThumbnailPlanRequest,
    build_thumbnail_viewport_plan,
)


class _PlanSignals(QtCore.QObject):
    finished = QtCore.Signal(int, object)
    failed = QtCore.Signal(int, str)
    cancelled = QtCore.Signal(int)


class _PlanTask(QtCore.QRunnable):
    def __init__(
        self,
        request: ThumbnailPlanRequest,
        cancelled: threading.Event,
    ) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.request = request
        self.cancelled = cancelled
        self.signals = _PlanSignals()

    @QtCore.Slot()
    def run(self) -> None:
        revision = int(self.request.request_revision)
        try:
            plan = build_thumbnail_viewport_plan(
                self.request,
                cancelled=self.cancelled.is_set,
            )
        except ThumbnailPlanCancelled:
            self.signals.cancelled.emit(revision)
            return
        except Exception as exc:
            self.signals.failed.emit(
                revision,
                f"{type(exc).__name__}: {exc}",
            )
            return
        if self.cancelled.is_set():
            self.signals.cancelled.emit(revision)
        else:
            self.signals.finished.emit(revision, plan)


class ThumbnailPlanCoordinator(QtCore.QObject):
    """Own latest-only thumbnail planning task lifecycle."""

    plan_ready = QtCore.Signal(int, object)
    plan_failed = QtCore.Signal(int, str)
    stale_result_discarded = QtCore.Signal()

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._revision = 0
        self._tasks: dict[int, _PlanTask] = {}
        self._cancel_events: dict[int, threading.Event] = {}

    @property
    def current_revision(self) -> int:
        return int(self._revision)

    def begin_request(self) -> int:
        self.cancel_pending()
        return self.current_revision

    def submit(self, request: ThumbnailPlanRequest) -> None:
        revision = int(request.request_revision)
        if revision != self._revision:
            raise ValueError(
                "Thumbnail plan request revision is not the current coordinator revision."
            )
        cancelled = threading.Event()
        task = _PlanTask(request, cancelled)
        task.signals.finished.connect(self._on_finished)
        task.signals.failed.connect(self._on_failed)
        task.signals.cancelled.connect(self._on_cancelled)
        self._cancel_events[revision] = cancelled
        self._tasks[revision] = task
        QtCore.QThreadPool.globalInstance().start(task)

    def cancel_pending(self) -> None:
        self._revision += 1
        for event in self._cancel_events.values():
            event.set()

    @QtCore.Slot(int, object)
    def _on_finished(self, revision: int, payload: object) -> None:
        self._forget(revision)
        if int(revision) != self._revision:
            self.stale_result_discarded.emit()
            return
        self.plan_ready.emit(int(revision), payload)

    @QtCore.Slot(int, str)
    def _on_failed(self, revision: int, message: str) -> None:
        self._forget(revision)
        if int(revision) != self._revision:
            self.stale_result_discarded.emit()
            return
        self.plan_failed.emit(int(revision), str(message))

    @QtCore.Slot(int)
    def _on_cancelled(self, revision: int) -> None:
        self._forget(revision)

    def _forget(self, revision: int) -> None:
        self._tasks.pop(int(revision), None)
        self._cancel_events.pop(int(revision), None)
