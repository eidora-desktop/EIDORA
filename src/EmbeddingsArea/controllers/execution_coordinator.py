"""Area-owned embedding and clustering job coordination."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import threading
import re
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4
from typing import Callable

from PySide6 import QtCore

from EmbeddingsArea.controllers.embedding_job_workers import _EmbeddingRunThread
from EmbeddingsArea.domain import EmbeddingRun, EmbeddingRunState
from shared.diagnostics.service import LOGGER, record_failure


REPORT_HISTORY_LIMIT = 500


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
    report_offset: int = 0
    operation: dict = field(default_factory=dict)
    source_run_id: str = ""
    display_run: EmbeddingRun | None = None
    output_id: str = ""
    result_detail: str = ""
    create_thumbnails: bool = False


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


class _InitializationThread(QtCore.QThread):
    result_ready = QtCore.Signal(object)

    def __init__(self, service, parent=None) -> None:
        super().__init__(parent)
        self._service = service

    @QtCore.Slot()
    def run(self) -> None:
        try:
            self._service.initialize()
        except Exception as exc:  # noqa: BLE001
            self.result_ready.emit(exc)
        else:
            self.result_ready.emit(None)


class ExecutionCoordinator(QtCore.QObject):
    """Own jobs beyond the lifetime of either visible panel."""

    changed = QtCore.Signal()
    progress = QtCore.Signal(str, str, int, str)
    output_ready = QtCore.Signal(str, str)
    reduction_started = QtCore.Signal(str)
    groups_changed = QtCore.Signal()
    thumbnails_requested = QtCore.Signal(str)

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
        run = self._session.runs.require_unpublished_run(run_id)
        media_busy = getattr(self, "media_source_busy", None)
        if media_busy and any(media_busy(source_id) for source_id in run.selected_source_ids):
            raise ValueError("Wait for media loading or linking to finish before starting this run.")
        if run.state == EmbeddingRunState.CANCELLED:
            run = self._session.runs.reset_run(run_id)
        if run.state == EmbeddingRunState.DRAFT:
            self._session.service.runs_repository.upsert_run(
                replace(run, state=EmbeddingRunState.QUEUED)
            )
        state = ExecutionState("embedding", run_id, run.label, generation=self._generation,
                               create_thumbnails=create_thumbnails)
        state.report.append("Queued: Waiting for background capacity…")
        self._jobs[key] = state
        generation = self._generation

        def start() -> None:
            if generation != self._generation or self._shutting_down:
                return
            self._start_embedding(state)

        state.admission = self._submit(state.title, start, lambda: self._cancel_worker(state))
        self.changed.emit()

    def start(self, result_id: str) -> None:
        """Queue clustering; named to match the existing clustering dialog contract."""
        if self._shutting_down or self.service is None:
            raise ValueError("Open a project before running clustering.")
        key = ("clustering", result_id)
        if key in self._jobs and self._jobs[key].state in {"queued", "running", "cancelling"}:
            raise ValueError("This clustering run is already active.")
        source = self.service.repository.get(result_id)
        if source and self.active_reduction(str(source["run_id"])) is not None:
            raise ValueError("Wait for vector reduction to finish before clustering.")
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

    def reduction_source_busy(self, run_id: str) -> bool:
        if self.active_reduction(run_id) is not None:
            return True
        for state in self.states():
            if state.state not in {"queued", "running", "cancelling"}:
                continue
            if state.kind in {"embedding", "reduction"} and state.id == run_id:
                return True
            if state.kind == "clustering" and self.service is not None:
                record = self.service.repository.get(state.id)
                if record and str(record["run_id"]) == run_id:
                    return True
        return False

    def reduction_state(self, run_id):
        direct = self.state("reduction", run_id)
        if direct is not None and direct.generation == self._generation:
            return direct
        return None

    def active_reduction(self, run_id):
        return next((state for state in self.states()
                     if state.kind == "reduction" and run_id in {state.id, state.source_run_id}
                     and state.generation == self._generation
                     and state.state in {"queued", "running", "cancelling"}), None)

    def reduction_card_runs(self):
        """Unpublished destination cards; the saved run takes over on publication."""
        return [replace(state.display_run,
                        state=EmbeddingRunState.RUNNING if state.state == "cancelling" else EmbeddingRunState(state.state),
                        error_message=state.pending_error)
                for state in self.states() if state.kind == "reduction"
                and state.generation == self._generation and state.display_run is not None
                and state.state != "completed"]

    def dismiss_reduction(self, run_id):
        state = self.reduction_state(run_id)
        if state is not None and state.display_run is not None and state.state in {"failed", "cancelled"}:
            self._jobs.pop(("reduction", run_id))
            self.changed.emit()

    def submit_reduction(self, run_id, request) -> str:
        if self._shutting_down or self._session is None or self._project is None:
            raise ValueError("Open a project before reducing vector data.")
        if self.reduction_source_busy(run_id):
            raise ValueError("Wait for the active vector-data job to finish before reducing.")
        run = self._session.runs.require_run(run_id)
        if not run.artifact_path:
            raise ValueError("Select published vector data to reduce.")
        self._session.run_repository.validate_run_label(
            request.label, exclude_run_id=run_id if request.replace_original else "",
        )
        from EmbeddingsArea.runtime.readers import load_embedding_summary
        summary = load_embedding_summary(Path(run.artifact_path))
        destination_id = run_id if request.replace_original else f"run_{uuid4().hex}"
        now = datetime.now(timezone.utc).isoformat()
        display_run = None if request.replace_original else EmbeddingRun(
            destination_id, request.label, EmbeddingRunState.QUEUED,
            selected_model_id=run.selected_model_id, created_at=now, updated_at=now)
        state = ExecutionState("reduction", destination_id, request.label, generation=self._generation,
            source_run_id=run_id, display_run=display_run,
            operation=dict(method=request.method, source_label=run.label, source_dimensions=summary.get("dimensions"),
                           item_count=summary.get("item_count"), dimensions=request.dimensions,
                           output_label=request.label, output_mode="replace" if request.replace_original else "copy"))
        self._append_report(state, "Queued")
        self._jobs[("reduction", destination_id)] = state
        project = self._project

        def start():
            if state.generation != self._generation or self._shutting_down:
                return
            from EmbeddingsArea.controllers.reduction_worker import ReductionThread
            thread = ReductionThread(project, run_id, request, self, output_id=destination_id)
            state.thread = state.worker = thread
            self._thread_keys[thread] = ("reduction", destination_id)
            state.state, state.detail, state.progress = "running", "Preparing reduction…", -1
            thread.progress.connect(self._reduction_progress)
            thread.result_ready.connect(self._finish_reduction)
            thread.finished.connect(self._job_thread_finished)
            thread.finished.connect(thread.deleteLater)
            thread.start()

        state.admission = self._submit(state.title, start, lambda: self._cancel_worker(state))
        self.changed.emit()
        self.reduction_started.emit(destination_id)
        return destination_id

    @QtCore.Slot(str, int, str)
    def _reduction_progress(self, identity, value, detail):
        state = self.state("reduction", identity)
        if state is None or self.sender() is not state.thread:
            return
        if state.generation != self._generation:
            return
        match = re.search(r": ([\d,]+) / ([\d,]+)", detail)
        state.progress = -1
        if match:
            done, total = (int(part.replace(",", "")) for part in match.groups())
            state.progress = min(100, int(100 * done / total)) if total else -1
            stage = detail[:match.start()] + "…"
        else:
            stage = detail
        state.detail = detail
        self._append_report(state, stage)
        self.progress.emit("reduction", identity, state.progress, detail)

    @QtCore.Slot(str, object)
    def _finish_reduction(self, identity, result):
        from EmbeddingsArea.reduction.parameters import ReductionCancelled
        state = self.state("reduction", identity)
        if state is None or self.sender() is not state.thread:
            return
        if isinstance(result, ReductionCancelled):
            state.result_detail = str(result)
            self._finish("reduction", identity, "cancelled", None)
        elif isinstance(result, Exception):
            self._finish("reduction", identity, "failed", str(result))
        else:
            state.output_id = result.run_id
            state.operation.update(result.recipe)
            state.operation.update(dimensions=result.dimensions, variance_retained=result.variance_retained)
            state.result_detail = "Completed"
            if result.warning:
                state.result_detail += " " + result.warning
            self._finish("reduction", identity, "completed", None)

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
        if kind == "embedding" and self._session is not None and (state is None or state.state == "paused"):
            # A paused run has no worker to cancel, including after reopening
            # the project. Persist the transition through the normal run service.
            run = self._session.runs.require_run(target)
            if run.state == EmbeddingRunState.PAUSED:
                cancelled = self._session.service.cancel_embedding_run(target)
                if state is not None:
                    state.state = cancelled.state.value
                    state.detail = cancelled.state.value.capitalize()
                    self._append_report(state, state.detail)
                self.changed.emit()
                return
        if state is None:
            if kind == "clustering" and self.service is not None:
                self.service.repository.cancel_queued(target)
                self.changed.emit()
            return
        if state.state not in {"queued", "running"}:
            return
        queued = bool(state.admission is not None and getattr(state.admission, "queued", False))
        if queued:
            state.admission.cancel()
            self._mark_queued_cancelled(state)
            if kind == "reduction":
                state.state = "cancelled"
                state.detail = "Reduction cancelled; original vectors are unchanged."
                state.admission = None
                self._append_report(state, state.detail)
            else:
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
            thread = _InitializationThread(self.service, self)
            self._initialization_thread = thread
            thread.result_ready.connect(self._initialization_complete_current)
            thread.finished.connect(self._initialization_thread_finished_current)
            thread.finished.connect(thread.deleteLater)
            thread.start()

        self._initialization_admission = self._submit("Load clustering results", start, lambda: None)

    @QtCore.Slot(object)
    def _initialization_complete_current(self, result: object) -> None:
        if self.sender() is not self._initialization_thread:
            return
        generation = self._initialization_generation
        if generation != self._generation:
            return
        self.initialization_error = str(result) if isinstance(result, Exception) else ""

    @QtCore.Slot()
    def _initialization_thread_finished_current(self) -> None:
        if self.sender() is not self._initialization_thread:
            return
        generation = self._initialization_generation
        admission = self._initialization_admission
        self._initialization_admission = None
        if admission is not None:
            admission.finish()
        self._initialization_thread = None
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

    def _start_embedding(self, state: ExecutionState) -> None:
        LOGGER.info("Embedding job started")
        thread = _EmbeddingRunThread(
            self._project, state.id, parent=self,
        )
        state.thread = state.worker = thread
        self._thread_keys[thread] = ("embedding", state.id)
        state.state, state.detail = "running", "Preparing embedding inputs…"
        state.progress = -1
        self._append_report(state, "Started: Preparing embedding inputs…")
        thread.progress.connect(self._embedding_progress)
        thread.message.connect(self._embedding_message)
        thread.result_ready.connect(self._embedding_finished)
        thread.failed.connect(self._embedding_failed)
        thread.finished.connect(self._job_thread_finished)
        thread.finished.connect(thread.deleteLater)
        thread.start()
        self.changed.emit()

    def _start_clustering(self, state: ExecutionState) -> None:
        LOGGER.info("Clustering job started")
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
        if int(value) // 10 != state.progress // 10:
            LOGGER.info("%s job progress: %s%%", kind, value)
        state.progress = -1 if int(value) < 0 else max(max(0, state.progress), min(99, int(value)))
        state.detail = str(detail or state.detail)
        self.progress.emit(kind, identity, state.progress, state.detail)

    @QtCore.Slot(str, int, str)
    def _embedding_progress(self, identity: str, value: int, detail: str) -> None:
        self._on_progress("embedding", identity, value, detail)

    @QtCore.Slot(str, str)
    def _embedding_message(self, identity: str, detail: str) -> None:
        state = self.state("embedding", identity)
        if state is None or state.generation != self._generation:
            return
        self._append_report(state, detail)
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
        LOGGER.info("%s job result: %s", kind, result_state)
        state.pending_error = error or ""
        if error:
            guidance = record_failure(f"{kind} job failed", error)
            if guidance:
                state.pending_error += "\n\n" + guidance
            self._append_report(state, "Execution failed. See ‘Error details’ in this report.")

    def _thread_finished(self, kind: str, identity: str) -> None:
        state = self.state(kind, identity)
        if state is None:
            return
        result_state = state.pending_state or "failed"
        state.state = result_state
        state.progress = 100 if result_state == "completed" else state.progress
        state.detail = state.pending_error or state.result_detail or result_state.replace("_", " ").capitalize()
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
            self.output_ready.emit(kind, state.output_id or identity)
            self.changed.emit()
            if (kind == "embedding" and result_state == "completed"
                    and state.create_thumbnails and not self._shutting_down
                    and state.generation == self._generation):
                state.create_thumbnails = False
                self.thumbnails_requested.emit(identity)

    @staticmethod
    def _append_report(state: ExecutionState, message: str) -> None:
        message = str(message or "").strip()
        if message and (not state.report or state.report[-1] != message):
            state.report.append(message)
            excess = max(0, len(state.report) - REPORT_HISTORY_LIMIT)
            if excess:
                del state.report[:excess]
                state.report_offset += excess

    @QtCore.Slot()
    def _job_thread_finished(self) -> None:
        thread = self.sender()
        key = self._thread_keys.pop(thread, None)
        if key is not None:
            self._thread_finished(*key)

    def _cancel_worker(self, state: ExecutionState) -> None:
        if state.kind in {"embedding", "reduction"} and state.worker is not None:
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
