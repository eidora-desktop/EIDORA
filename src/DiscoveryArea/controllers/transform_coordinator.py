from __future__ import annotations

import threading

from PySide6 import QtCore
from shared.qt.background_tasks import BackgroundTasks

from DiscoveryArea.domain import DiscoveryDataset
from DiscoveryArea.transform_service import DiscoveryTransformService
from DiscoveryArea.transforms import TransformCancelled, TransformSettings


class _TransformTask:
    def __init__(
        self,
        revision,
        project,
        scope,
        dataset,
        settings,
        cached_grid,
        event,
    ) -> None:
        self.revision = int(revision)
        self.project = project
        self.scope = scope
        self.dataset = dataset
        self.settings = settings
        self.cached_grid = cached_grid
        self.event = event

    def run(self, progress):
        if self.event.is_set():
            return None
        try:
            result = DiscoveryTransformService().transform_and_save(
                project=self.project,
                discovery_scope=self.scope,
                dataset=self.dataset,
                settings=self.settings,
                cached_grid=self.cached_grid,
                cancelled=self.event.is_set,
                progress=progress,
            )
        except TransformCancelled:
            return None
        return None if self.event.is_set() else result


class TransformCoordinator(QtCore.QObject):
    ready = QtCore.Signal(int, object)
    failed = QtCore.Signal(int, str)
    cancelled = QtCore.Signal(int)
    progress = QtCore.Signal(int, int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._revision = 0
        self._events = {}
        self._runner = BackgroundTasks(self)
        self._runner.finished.connect(self._completed)
        self._runner.failed.connect(self._failed)
        self._runner.progress.connect(self._progress)

    @property
    def revision(self) -> int:
        return self._revision

    def submit(
        self,
        project,
        scope: str,
        dataset: DiscoveryDataset,
        settings: TransformSettings,
        cached_grid=None,
    ) -> int:
        self.cancel_pending()
        revision = self._revision
        event = threading.Event()
        task = _TransformTask(
            revision,
            project,
            scope,
            dataset,
            settings,
            cached_grid,
            event,
        )
        self._events[revision] = event
        self._runner.submit(revision, task.run)
        return revision

    def cancel_pending(self) -> None:
        self._revision += 1
        for event in self._events.values():
            event.set()

    @QtCore.Slot(int, object)
    def _finished(self, revision: int, result: object) -> None:
        self._forget(revision)
        if revision == self._revision:
            self.ready.emit(revision, result)

    @QtCore.Slot(int, str)
    def _failed(self, revision: int, message: str) -> None:
        self._forget(revision)
        if revision == self._revision:
            self.failed.emit(revision, message)

    @QtCore.Slot(int)
    def _cancelled(self, revision: int) -> None:
        self._forget(revision)
        if revision == self._revision:
            self.cancelled.emit(revision)

    @QtCore.Slot(int, int)
    def _progress(self, revision: int, value: int) -> None:
        if revision == self._revision:
            self.progress.emit(revision, max(0, min(100, int(value))))

    def _forget(self, revision: int) -> None:
        self._events.pop(revision, None)

    def _completed(self, revision: int, result: object) -> None:
        if result is None:
            self._cancelled(revision)
        else:
            self._finished(revision, result)

    def shutdown(self) -> None:
        self.cancel_pending()
        self._runner.shutdown()
        self._events.clear()
