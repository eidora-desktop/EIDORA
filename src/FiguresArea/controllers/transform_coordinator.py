from __future__ import annotations

import threading

import numpy as np
from PySide6 import QtCore

from DiscoveryArea.transforms import (
    TransformCancelled,
    TransformSettings,
    transform_coordinates,
)


class _Signals(QtCore.QObject):
    ready = QtCore.Signal(int, object)
    failed = QtCore.Signal(int, str)
    cancelled = QtCore.Signal(int)


class _Task(QtCore.QRunnable):
    def __init__(self, revision: int, coordinates, metadata_ids, settings, event) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.revision = revision
        self.coordinates = np.asarray(coordinates, dtype=np.float32)
        self.metadata_ids = np.asarray(metadata_ids, dtype=np.int64)
        self.settings = settings
        self.event = event
        self.signals = _Signals()

    @QtCore.Slot()
    def run(self) -> None:
        try:
            result = transform_coordinates(
                self.coordinates,
                self.metadata_ids,
                self.settings,
                cancelled=self.event.is_set,
            )
        except TransformCancelled:
            self.signals.cancelled.emit(self.revision)
        except Exception as exc:
            self.signals.failed.emit(self.revision, f"{type(exc).__name__}: {exc}")
        else:
            self.signals.ready.emit(self.revision, result)


class FigureTransformCoordinator(QtCore.QObject):
    ready = QtCore.Signal(int, object)
    failed = QtCore.Signal(int, str)
    cancelled = QtCore.Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._revision = 0
        self._tasks: dict[int, _Task] = {}
        self._events: dict[int, threading.Event] = {}

    def submit(self, coordinates, metadata_ids, settings: TransformSettings) -> int:
        self.cancel_pending()
        revision = self._revision
        event = threading.Event()
        task = _Task(revision, coordinates, metadata_ids, settings, event)
        task.signals.ready.connect(self._ready)
        task.signals.failed.connect(self._failed)
        task.signals.cancelled.connect(self._cancelled)
        self._tasks[revision] = task
        self._events[revision] = event
        QtCore.QThreadPool.globalInstance().start(task)
        return revision

    def cancel_pending(self) -> None:
        self._revision += 1
        for event in self._events.values():
            event.set()

    @QtCore.Slot(int, object)
    def _ready(self, revision: int, result: object) -> None:
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

    def _forget(self, revision: int) -> None:
        self._tasks.pop(revision, None)
        self._events.pop(revision, None)
