from __future__ import annotations

import time

from EmbeddingsArea.domain import EmbeddingRunState
from EmbeddingsArea.session import EmbeddingsAreaSession

class EmbeddingGenerationJob:

    def __init__(self, project, run_id: str, *, finished, failed, progress, message, telemetry) -> None:
        self._finished, self._failed, self._progress, self._message, self._telemetry = finished, failed, progress, message, telemetry
        self._project = project
        self._run_id = run_id
        self._pause_requested = False
        self._cancel_requested = False
        self._overall_progress = 0
        self._last_progress_update: tuple[int, str] | None = None
        self._last_event_at = float("-inf")
        self._last_event_stage = ""

    def request_pause(self) -> None:
        self._pause_requested = True

    def request_cancel(self) -> None:
        self._cancel_requested = True

    def _control_action(self) -> str:
        if self._cancel_requested:
            return "cancel"
        if self._pause_requested:
            return "pause"
        return "continue"

    def run(self) -> None:
        try:
            session = EmbeddingsAreaSession.for_project(self._project)
            service = session.service
            self._progress(self._run_id, -1, "Preparing embedding inputs…")
            run = service.run_embedding_run(
                self._run_id,
                control_callback=self._control_action,
                status_callback=self._on_embedding_status,
                event_callback=self._on_embedding_event,
            )
            if run.state == EmbeddingRunState.COMPLETED:
                self._emit_overall_progress(99, "Completing run...")
        except Exception as exc:  # noqa: BLE001
            self._failed(self._run_id, str(exc))
            return
        self._finished(self._run_id, run.state.value, run.label, "")

    def _on_embedding_event(self, event) -> None:
        now = time.monotonic()
        if (event.stage == self._last_event_stage and event.completed < event.total
                and now - self._last_event_at < 0.25):
            return
        self._last_event_at, self._last_event_stage = now, event.stage
        self._telemetry(self._run_id, event)
        value = min(99, int(event.completed / max(1, event.total) * 99))
        self._emit_overall_progress(value, event.message)

    def _on_embedding_status(self, message: str) -> None:
        self._message(self._run_id, message)

    def _emit_overall_progress(self, value: int, activity: str) -> None:
        normalized = max(self._overall_progress, min(99, max(0, int(value))))
        self._overall_progress = normalized
        update = (normalized, str(activity))
        if update == self._last_progress_update:
            return
        self._last_progress_update = update
        self._progress(self._run_id, *update)

