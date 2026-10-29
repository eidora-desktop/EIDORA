"""Area-owned embedding and clustering job coordination."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import threading
from typing import Callable

from PySide6 import QtCore

from EmbeddingsArea.controllers.embedding_job_workers import _EmbeddingRunThread
from EmbeddingsArea.domain import EmbeddingRunState


@dataclass
class ExecutionState:
    kind: str
    id: str
    title: str
    state: str = "queued"
    progress: int = 0
    detail: str = "Waiting for background capacity…"
    admission: object | None = None
    worker: QtCore.QObject | None = None
    thread: QtCore.QThread | None = None
    generation: int = 0
    pending_state: str = ""
    pending_error: str = ""
    report: list[str] = field(default_factory=list)


class _ClusteringThread(QtCore.QThread):
    """Keep the callback-owning Qt object on the GUI thread until run exits."""

    result_ready = QtCore.Signal(str, object)
    progress = QtCore.Signal(str, int, str)

    def __init__(self, service, result_id: str, cancelled: threading.Event, parent=None) -> None:
        super().__init__(parent)
        self._service = service
        self._result_id = result_id
        self._cancelled = cancelled

    def run(self) -> None:
        try:
            result = self._service.execute(
                self._result_id,
                cancelled=self._cancelled.is_set,
                status=lambda message: self.progress.emit(
                    self._result_id,
                    self._stage_progress(message),
                    message,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            self.result_ready.emit(self._result_id, exc)
        else:
            self.result_ready.emit(self._result_id, result)

    @staticmethod
    def _stage_progress(message: str) -> int:
        if message == "Loading embeddings":
            return 10
        if message == "Preparing embeddings":
            return 25
        if message.startswith("Fitting"):
            return -1
        if message == "Finalizing results":
            return 88
        if message == "Validating results":
            return 93
        if message == "Saving groups":
            return 97
        if message == "Completed":
            return 100
        return 2


class _InitializationWorker(QtCore.QObject):
    finished = QtCore.Signal(object)

    def __init__(self, service) -> None:
        super().__init__()
        self._service = service

    @QtCore.Slot()
    def run(self) -> None:
        try:
            self._service.initialize()
        except Exception as exc:  # noqa: BLE001
            self.finished.emit(exc)
        else:
            self.finished.emit(None)


class ExecutionCoordinator(QtCore.QObject):
    """Own jobs beyond the lifetime of either visible panel."""

    changed = QtCore.Signal()
    progress = QtCore.Signal(str, str, int, str)
    output_ready = QtCore.Signal(str, str)
    groups_changed = QtCore.Signal()

    def __init__(self, activity_manager=None, parent=None) -> None:
        super().__init__(parent)
        self._activity_manager = activity_manager
        self._session = None
        self._project = None
        self._generation = 0
        self._jobs: dict[tuple[str, str], ExecutionState] = {}
        self._cluster_cancellations: dict[str, threading.Event] = {}
        self._shutting_down = False
        self.service = None  # ClusteringDialogController compatibility.
        self.initializing = False
        self.initialization_error = ""
        self.active_id: str | None = None
        self._initialization_thread: QtCore.QThread | None = None
        self._initialization_worker: _InitializationWorker | None = None
        self._initialization_admission = None
        self._initialization_generation = 0
        self._thread_keys: dict[QtCore.QThread, tuple[str, str]] = {}

    def activate(self, project, session) -> None:
        self._generation += 1
        self._cancel_all()
        self._project = project
        self._session = session
        self.service = session.clusters if session is not None else None
        self._shutting_down = False
        if self.service is not None:
            self._begin_initialization()
        self.changed.emit()

    def state(self, kind: str, identity: str) -> ExecutionState | None:
        return self._jobs.get((kind, identity))

    def states(self) -> tuple[ExecutionState, ...]:
        return tuple(self._jobs.values())

    @property
    def busy(self) -> bool:
        return any(job.state in {"queued", "running", "cancelling"} for job in self._jobs.values())

    def submit_embedding(self, run_id: str, *, create_thumbnails: bool = False) -> None:
        if self._shutting_down or self._session is None or self._project is None:
            raise ValueError("Open a project before running an embedding model.")
        key = ("embedding", run_id)
        if key in self._jobs and self._jobs[key].state in {"queued", "running", "cancelling"}:
            raise ValueError("This embedding run is already active.")
        run = self._session.service.get_embedding_run_configuration(run_id).run
        media_busy = getattr(self, "media_source_busy", None)
        if media_busy and any(media_busy(source_id) for source_id in run.selected_source_ids):
            raise ValueError("Wait for media loading or linking to finish before starting this run.")
        if run.state == EmbeddingRunState.CANCELLED:
            run = self._session.runs.reset_run(run_id)
        if run.state == EmbeddingRunState.DRAFT:
            self._session.service.runs_repository.upsert_run(
                replace(run, state=EmbeddingRunState.QUEUED)
            )
        state = ExecutionState("embedding", run_id, run.label, generation=self._generation)
        state.report.append("Queued · Waiting for background capacity…")
        self._jobs[key] = state
        generation = self._generation

        def start() -> None:
            if generation != self._generation or self._shutting_down:
                return
            self._start_embedding(state, create_thumbnails)

        state.admission = self._submit(state.title, start, lambda: self._cancel_worker(state))
        self.changed.emit()

    def start(self, result_id: str) -> None:
        """Queue clustering; named to match the existing clustering dialog contract."""
        if self._shutting_down or self.service is None:
            raise ValueError("Open a project before running clustering.")
        key = ("clustering", result_id)
        if key in self._jobs and self._jobs[key].state in {"queued", "running", "cancelling"}:
            raise ValueError("This clustering run is already active.")
        record = self.service.repository.enqueue(result_id)
        state = ExecutionState("clustering", result_id, str(record["label"]), generation=self._generation)
        self._jobs[key] = state
        generation = self._generation

        def start() -> None:
            if generation != self._generation or self._shutting_down:
                return
            self._start_clustering(state)

        state.admission = self._submit(state.title, start, lambda: self._cancel_worker(state))
        self.changed.emit()

    def submit_clustering(self, result_id: str) -> None:
        self.start(result_id)

    def pause_embedding(self, run_id: str) -> None:
        state = self.state("embedding", run_id)
        queued = bool(
            state is not None
            and state.admission is not None
            and getattr(state.admission, "queued", False)
        )
        if queued:
            state.admission.cancel()
            state.admission = None
            if self._session is not None:
                self._session.service.pause_embedding_run(run_id)
            self._jobs.pop(("embedding", run_id), None)
            self.changed.emit()
            return
        worker = state.worker if state is not None else None
        if worker is not None and hasattr(worker, "request_pause"):
            worker.request_pause()
            state.detail = "Pausing at the next checkpoint…"
            self.changed.emit()
            return
        if self._session is not None:
            self._session.service.pause_embedding_run(run_id)
            self.changed.emit()

    def cancel(self, kind_or_id: str, identity: str | None = None) -> None:
        # ClusteringDialogController calls cancel(result_id).
        kind = kind_or_id if identity is not None else "clustering"
        target = identity if identity is not None else kind_or_id
        state = self.state(kind, target)
        if state is None:
            if kind == "clustering" and self.service is not None:
                self.service.repository.cancel_queued(target)
                self.changed.emit()
            return
        queued = bool(state.admission is not None and getattr(state.admission, "queued", False))
        if queued:
            state.admission.cancel()
            self._mark_queued_cancelled(state)
            self._jobs.pop((kind, target), None)
            self.changed.emit()
            return
        state.state = "cancelling"
        state.detail = "Cancelling…"
        self._cancel_worker(state)
        self.changed.emit()

    def abort(self) -> None:
        if self.active_id:
            self.cancel("clustering", self.active_id)

    def shutdown(self) -> None:
        self._shutting_down = True
        self._generation += 1
        self._cancel_all()
        self.service = None

    def _begin_initialization(self) -> None:
        self.initializing = True
        self.initialization_error = ""
        generation = self._generation
        self._initialization_generation = generation

        def start() -> None:
            if generation != self._generation or self._shutting_down or self.service is None:
                return
            thread = QtCore.QThread(self)
            worker = _InitializationWorker(self.service)
            worker.moveToThread(thread)
            self._initialization_thread = thread
            self._initialization_worker = worker
            thread.started.connect(worker.run)
            worker.finished.connect(self._initialization_complete_current)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(self._initialization_thread_finished_current)
            thread.finished.connect(thread.deleteLater)
            thread.start()

        self._initialization_admission = self._submit("Load clustering results", start, lambda: None)

    @QtCore.Slot(object)
    def _initialization_complete_current(self, result: object) -> None:
        generation = self._initialization_generation
        if generation != self._generation:
            return
        self.initialization_error = str(result) if isinstance(result, Exception) else ""

    @QtCore.Slot()
    def _initialization_thread_finished_current(self) -> None:
        generation = self._initialization_generation
        admission = self._initialization_admission
        self._initialization_admission = None
        if admission is not None:
            admission.finish()
        self._initialization_thread = None
        self._initialization_worker = None
        if generation != self._generation:
            return
        self.initializing = False
        self.changed.emit()

    def _submit(self, title: str, start: Callable[[], None], cancel: Callable[[], None]):
        manager = getattr(self._activity_manager, "processes", None)
        if manager is None:
            start()
            return None
        admission = manager.submit(
            panel="Embeddings Vector Data",
            title=title,
            start=start,
            cancel=cancel,
        )
        manager.dispatch()
        return admission

    def _start_embedding(self, state: ExecutionState, create_thumbnails: bool) -> None:
        thread = _EmbeddingRunThread(
            self._project, state.id, create_thumbnails=create_thumbnails, parent=self,
        )
        state.thread = state.worker = thread
        self._thread_keys[thread] = ("embedding", state.id)
        state.state, state.detail = "running", "Preparing embedding model…"
        self._append_report(state, "Started · Preparing embedding model…")
        thread.progress.connect(self._embedding_progress)
        thread.message.connect(self._embedding_message)
        thread.result_ready.connect(self._embedding_finished)
        thread.failed.connect(self._embedding_failed)
        thread.finished.connect(self._job_thread_finished)
        thread.finished.connect(thread.deleteLater)
        thread.start()
        self.changed.emit()

    def _start_clustering(self, state: ExecutionState) -> None:
        cancellation = threading.Event()
        self._cluster_cancellations[state.id] = cancellation
        thread = _ClusteringThread(self.service, state.id, cancellation, parent=self)
        state.thread = state.worker = thread
        self._thread_keys[thread] = ("clustering", state.id)
        state.state, state.detail = "running", "Preparing clustering…"
        self.active_id = state.id
        thread.progress.connect(self._clustering_progress)
        thread.result_ready.connect(self._finish_clustering)
        thread.finished.connect(self._job_thread_finished)
        thread.finished.connect(thread.deleteLater)
        thread.start()
        self.changed.emit()

    def _on_progress(self, kind: str, identity: str, value: int, detail: str) -> None:
        state = self.state(kind, identity)
        if state is None or state.generation != self._generation:
            return
        state.progress = -1 if int(value) < 0 else max(max(0, state.progress), min(99, int(value)))
        state.detail = str(detail or state.detail)
        progress = "Working" if state.progress < 0 else f"{state.progress}%"
        self._append_report(state, f"{progress} · {state.detail}")
        self.progress.emit(kind, identity, state.progress, state.detail)

    @QtCore.Slot(str, int, str)
    def _embedding_progress(self, identity: str, value: int, detail: str) -> None:
        self._on_progress("embedding", identity, value, detail)

    @QtCore.Slot(str, str)
    def _embedding_message(self, identity: str, detail: str) -> None:
        state = self.state("embedding", identity)
        self._on_progress("embedding", identity, state.progress if state else 0, detail)

    @QtCore.Slot(str, str, str, str)
    def _embedding_finished(self, identity: str, result: str, _label: str, log: str) -> None:
        state = self.state("embedding", identity)
        if state is not None:
            for line in str(log or "").splitlines():
                self._append_report(state, line)
        self._finish("embedding", identity, result, None)

    @QtCore.Slot(str, str)
    def _embedding_failed(self, identity: str, message: str) -> None:
        self._finish("embedding", identity, "failed", message)

    @QtCore.Slot(str, int, str)
    def _clustering_progress(self, identity: str, value: int, detail: str) -> None:
        self._on_progress("clustering", identity, value, detail)

    @QtCore.Slot(str, object)
    def _finish_clustering(self, identity: str, result: object) -> None:
        if isinstance(result, Exception):
            try:
                persistent = str(self.service.repository.get(identity).get("state", "failed"))
            except Exception:
                persistent = "failed"
            self._finish("clustering", identity, persistent, str(result))
        else:
            self._finish("clustering", identity, "completed", None)
            self.groups_changed.emit()

    def _finish(self, kind: str, identity: str, result_state: str, error: str | None) -> None:
        state = self.state(kind, identity)
        if state is None:
            return
        state.pending_state = str(result_state)
        state.pending_error = error or ""
        if error:
            self._append_report(state, f"ERROR · {error}")

    def _thread_finished(self, kind: str, identity: str) -> None:
        state = self.state(kind, identity)
        if state is None:
            return
        result_state = state.pending_state or "failed"
        state.state = result_state
        state.progress = 100 if result_state == "completed" else state.progress
        state.detail = state.pending_error or result_state.replace("_", " ").capitalize()
        if not state.pending_error:
            self._append_report(state, state.detail)
        state.worker = None
        state.thread = None
        self._cluster_cancellations.pop(identity, None)
        if kind == "clustering" and self.active_id == identity:
            self.active_id = None
        admission = state.admission
        state.admission = None
        if admission is not None:
            admission.finish()
        if state.generation == self._generation:
            self.output_ready.emit(kind, identity)
            self.changed.emit()

    @staticmethod
    def _append_report(state: ExecutionState, message: str) -> None:
        message = str(message or "").strip()
        if message and (not state.report or state.report[-1] != message):
            state.report.append(message)

    @QtCore.Slot()
    def _job_thread_finished(self) -> None:
        thread = self.sender()
        key = self._thread_keys.pop(thread, None)
        if key is not None:
            self._thread_finished(*key)

    def _cancel_worker(self, state: ExecutionState) -> None:
        if state.kind == "embedding" and state.worker is not None:
            state.worker.request_cancel()
        elif state.kind == "clustering":
            cancellation = self._cluster_cancellations.get(state.id)
            if cancellation is not None:
                cancellation.set()

    def _mark_queued_cancelled(self, state: ExecutionState) -> None:
        if state.kind == "clustering" and self.service is not None:
            self.service.repository.cancel_queued(state.id)
        elif state.kind == "embedding" and self._session is not None:
            run = self._session.service.get_embedding_run_configuration(state.id).run
            self._session.service.runs_repository.upsert_run(
                replace(run, state=EmbeddingRunState.CANCELLED, error_message="")
            )

    def _cancel_all(self) -> None:
        if self._initialization_admission is not None:
            self._initialization_admission.cancel()
        if self._initialization_thread is not None:
            try:
                self._initialization_thread.quit()
                self._initialization_thread.wait()
            except RuntimeError:
                pass
        if self._initialization_admission is not None:
            self._initialization_admission.finish()
        self._initialization_admission = None
        self._initialization_thread = None
        self._initialization_worker = None
        self.initializing = False
        for state in tuple(self._jobs.values()):
            if state.state in {"queued", "running", "cancelling"}:
                if state.admission is not None:
                    state.admission.cancel()
                self._cancel_worker(state)
                thread = state.thread
                if thread is not None:
                    try:
                        thread.quit()
                        thread.wait()
                    except RuntimeError:
                        pass
                if state.admission is not None:
                    state.admission.finish()
                    state.admission = None
        self._jobs.clear()
        self._thread_keys.clear()
        self._cluster_cancellations.clear()
        self.active_id = None
