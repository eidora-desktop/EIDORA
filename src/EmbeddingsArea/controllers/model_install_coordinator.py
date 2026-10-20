from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6 import QtCore

from EmbeddingsArea.domain import ModelCatalogEntry

if TYPE_CHECKING:
    from shared.qt.activity import ActivityManager


@dataclass
class PendingModelInstall:
    id: str
    label: str
    modality_labels: tuple[str, ...]
    cancel_event: threading.Event
    progress: int = 0
    abort_requested: bool = False
    state: str = "queued"
    admission: object | None = None


class _InstallSignals(QtCore.QObject):
    progress = QtCore.Signal(str, int)
    finished = QtCore.Signal(object, object)


class ModelInstallCoordinator(QtCore.QObject):
    """Own Model Zoo downloads independently of the dialog that starts them."""

    changed = QtCore.Signal(str)
    completed = QtCore.Signal(object, object)

    def __init__(self, activity_manager: ActivityManager | None = None) -> None:
        super().__init__()
        self._activity_manager = activity_manager
        self._pending: dict[str, PendingModelInstall] = {}
        self._signals = _InstallSignals()
        self._signals.progress.connect(self._apply_progress)
        self._signals.finished.connect(self._apply_finished)
        self._shutdown = False

    @staticmethod
    def key(model_id: str) -> str:
        return str(model_id).strip().casefold()

    def pending_installs(self) -> tuple[PendingModelInstall, ...]:
        return tuple(self._pending.values())

    def pending(self, model_id: str) -> PendingModelInstall | None:
        return self._pending.get(self.key(model_id))

    def start(self, entry: ModelCatalogEntry, catalog: object) -> bool:
        key = self.key(entry.id)
        if self._shutdown or entry.is_installed or key in self._pending:
            return False
        pending = PendingModelInstall(
            id=entry.id,
            label=entry.label,
            modality_labels=entry.modality_labels,
            cancel_event=threading.Event(),
        )
        self._pending[key] = pending
        self.changed.emit(entry.id)

        def start_worker() -> None:
            if self._shutdown or pending.abort_requested:
                return
            pending.state = "installing"
            self.changed.emit(entry.id)
            threading.Thread(
                target=self._install_worker,
                args=(entry, catalog, pending.cancel_event),
                name="model-install",
                daemon=True,
            ).start()

        if self._activity_manager is None:
            start_worker()
        else:
            pending.admission = self._activity_manager.processes.submit(
                panel="Models",
                title=f"Install {entry.label}",
                start=start_worker,
                cancel=pending.cancel_event.set,
            )
            self._activity_manager.processes.dispatch()
        return True

    def cancel(self, model_id: str) -> bool:
        key = self.key(model_id)
        pending = self._pending.get(key)
        if pending is None or pending.abort_requested:
            return False
        pending.abort_requested = True
        pending.cancel_event.set()
        if pending.state == "queued":
            admission = pending.admission
            if admission is not None:
                admission.cancel()
            self._pending.pop(key, None)
            self.changed.emit(pending.id)
            self.completed.emit(pending.id, "cancelled")
        else:
            self.changed.emit(pending.id)
        return True

    def shutdown(self) -> None:
        self._shutdown = True
        for pending in tuple(self._pending.values()):
            pending.abort_requested = True
            pending.cancel_event.set()
            admission = pending.admission
            if admission is not None:
                admission.cancel()

    def _install_worker(
        self,
        entry: ModelCatalogEntry,
        catalog: object,
        cancel_event: threading.Event,
    ) -> None:
        try:
            catalog.install_discoverable_model(
                entry,
                cancel_event=cancel_event,
                progress_callback=lambda value: self._signals.progress.emit(entry.id, int(value)),
            )
        except Exception as exc:  # noqa: BLE001
            self._signals.finished.emit(entry, str(exc))
            return
        self._signals.finished.emit(entry, None)

    def _apply_progress(self, model_id: str, progress: int) -> None:
        if self._shutdown:
            return
        pending = self.pending(model_id)
        if pending is None:
            return
        pending.progress = max(0, min(100, int(progress)))
        self.changed.emit(pending.id)

    def _apply_finished(self, entry: object, error: object) -> None:
        if not isinstance(entry, ModelCatalogEntry):
            return
        pending = self._pending.pop(self.key(entry.id), None)
        if pending is None:
            return
        admission = pending.admission
        if self._shutdown:
            if admission is not None:
                admission.finish()
            return
        normalized_error = "cancelled" if pending.cancel_event.is_set() else error
        self.changed.emit(entry.id)
        self.completed.emit(entry, normalized_error)
        if admission is not None:
            admission.finish()
