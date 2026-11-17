from __future__ import annotations

import threading

from PySide6 import QtCore
from shared.qt.background_tasks import BackgroundTasks

from DiscoveryArea.canvas.thumbnail_planner import (
    ThumbnailPlanCancelled,
    ThumbnailPlanRequest,
    build_thumbnail_viewport_plan,
)


class _PlanTask:
    def __init__(
        self,
        request: ThumbnailPlanRequest,
        cancelled: threading.Event,
    ) -> None:
        self.request = request
        self.cancelled = cancelled

    def run(self, _progress):
        if self.cancelled.is_set():
            return None
        try:
            plan = build_thumbnail_viewport_plan(self.request, cancelled=self.cancelled.is_set)
        except ThumbnailPlanCancelled:
            return None
        return None if self.cancelled.is_set() else plan


class ThumbnailPlanCoordinator(QtCore.QObject):
    """Own latest-only thumbnail planning task lifecycle."""

    plan_ready = QtCore.Signal(int, object)
    plan_failed = QtCore.Signal(int, str)
    stale_result_discarded = QtCore.Signal()

    def __init__(self, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self._revision = 0
        self._cancel_events: dict[int, threading.Event] = {}
        self._runner = BackgroundTasks(self)
        self._runner.finished.connect(self._completed)
        self._runner.failed.connect(self._on_failed)

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
        self._cancel_events[revision] = cancelled
        self._runner.submit(revision, task.run)

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
        self._cancel_events.pop(int(revision), None)

    def _completed(self, revision: int, result: object) -> None:
        if result is None:
            self._on_cancelled(revision)
        else:
            self._on_finished(revision, result)

    def shutdown(self) -> None:
        self.cancel_pending()
        self._runner.shutdown()
        self._cancel_events.clear()
