from __future__ import annotations

import threading

from PySide6 import QtCore

from DiscoveryArea.domain import DiscoveryDataset
from DiscoveryArea.transform_service import DiscoveryTransformService
from DiscoveryArea.transforms import TransformCancelled, TransformSettings


class _TransformSignals(QtCore.QObject):
    finished = QtCore.Signal(int, object)
    failed = QtCore.Signal(int, str)
    cancelled = QtCore.Signal(int)
    progress = QtCore.Signal(int, int)


class _TransformTask(QtCore.QRunnable):
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
        super().__init__()
        self.setAutoDelete(False)
        self.revision = int(revision)
        self.project = project
        self.scope = scope
        self.dataset = dataset
        self.settings = settings
        self.cached_grid = cached_grid
        self.event = event
        self.signals = _TransformSignals()

    @QtCore.Slot()
    def run(self) -> None:
        try:
            result = DiscoveryTransformService().transform_and_save(
                project=self.project,
                discovery_scope=self.scope,
                dataset=self.dataset,
                settings=self.settings,
                cached_grid=self.cached_grid,
                cancelled=self.event.is_set,
                progress=lambda value: self.signals.progress.emit(
                    self.revision, int(value)
                ),
            )
        except TransformCancelled:
            self.signals.cancelled.emit(self.revision)
        except Exception as exc:
            self.signals.failed.emit(self.revision, f"{type(exc).__name__}: {exc}")
        else:
            self.signals.finished.emit(self.revision, result)


class TransformCoordinator(QtCore.QObject):
    ready = QtCore.Signal(int, object)
    failed = QtCore.Signal(int, str)
    cancelled = QtCore.Signal(int)
    progress = QtCore.Signal(int, int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._revision = 0
        self._tasks = {}
        self._events = {}

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
        task.signals.finished.connect(self._finished)
        task.signals.failed.connect(self._failed)
        task.signals.cancelled.connect(self._cancelled)
        task.signals.progress.connect(self._progress)
        self._tasks[revision] = task
        self._events[revision] = event
        QtCore.QThreadPool.globalInstance().start(task)
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
        self._tasks.pop(revision, None)
        self._events.pop(revision, None)
