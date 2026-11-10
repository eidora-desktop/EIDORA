from __future__ import annotations

import time

from ProjectArea.thumbnail_generator import next_thumbnail_variant_label
from ProjectArea.thumbnail_service import create_source_thumbnails
from EmbeddingsArea.domain import EmbeddingRunState, MediaContentType
from EmbeddingsArea.session import EmbeddingsAreaSession
from DiscoveryArea.thumbnails.repository import ThumbnailVariantRepository

class EmbeddingGenerationJob:

    def __init__(self, project, run_id: str, *, create_thumbnails: bool = False, finished, failed, progress, message, telemetry) -> None:
        self._finished, self._failed, self._progress, self._message, self._telemetry = finished, failed, progress, message, telemetry
        self._project = project
        self._run_id = run_id
        self._create_thumbnails = bool(create_thumbnails)
        self._pause_requested = False
        self._cancel_requested = False
        self._embedding_progress = 0
        self._overall_progress = 0
        self._last_progress_update: tuple[int, str] | None = None

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
        completion_log: list[str] = []
        try:
            session = EmbeddingsAreaSession.for_project(self._project)
            service = session.service
            self._emit_overall_progress(0, "Preparing embedding inputs...")
            run = service.run_embedding_run(
                self._run_id,
                control_callback=self._control_action,
                progress_callback=self._on_embedding_progress,
                status_callback=self._on_embedding_status,
                event_callback=self._on_embedding_event,
            )
            if self._create_thumbnails and run.state == EmbeddingRunState.COMPLETED:
                self._create_default_thumbnail_variant(session, completion_log)
            if run.state == EmbeddingRunState.COMPLETED:
                self._emit_overall_progress(99, "Completing run...")
        except Exception as exc:  # noqa: BLE001
            self._failed(self._run_id, str(exc))
            return
        self._finished(self._run_id, run.state.value, run.label, "\n".join(completion_log))

    def _on_embedding_event(self, event) -> None:
        self._telemetry(self._run_id, event)
        self._emit_overall_progress(min(99, int(event.completed / max(1, event.total) * 99)), event.message)

    def _on_embedding_progress(self, value: int) -> None:
        self._embedding_progress = max(0, min(100, int(value)))
        ceiling = 99
        overall = int(self._embedding_progress * ceiling / 100)
        self._emit_overall_progress(overall, self._embedding_activity(self._embedding_progress))

    def _on_embedding_status(self, message: str) -> None:
        ceiling = 99
        overall = int(self._embedding_progress * ceiling / 100)
        self._emit_overall_progress(overall, message)

    @staticmethod
    def _embedding_activity(progress: int) -> str:
        if progress >= 100:
            return "Embedding artifact saved."
        if progress >= 99:
            return "Finalizing embedding artifact..."
        return f"Processing model batches: {progress}%..."

    def _emit_overall_progress(self, value: int, activity: str) -> None:
        normalized = max(self._overall_progress, min(99, max(0, int(value))))
        self._overall_progress = normalized
        update = (normalized, str(activity))
        if update == self._last_progress_update:
            return
        self._last_progress_update = update
        self._progress(self._run_id, *update)

    def _create_default_thumbnail_variant(
        self,
        session: EmbeddingsAreaSession,
        completion_log: list[str],
    ) -> None:
        repo = ThumbnailVariantRepository(self._project.paths.root)
        if repo.list_variants():
            self._emit_message("Thumbnail creation skipped: a thumbnail variant already exists.", completion_log)
            return

        configuration = session.service.get_embedding_run_configuration(self._run_id)
        source_ids = [binding.source_id for binding in configuration.input_bindings]
        if not source_ids:
            source_ids = list(configuration.run.selected_source_ids)
        summaries = {
            summary.source.id: summary
            for summary in session.media_library.list_source_summaries()
        }
        summary = next(
            (
                summaries[source_id]
                for source_id in source_ids
                if source_id in summaries
                and summaries[source_id].source.content_type in {MediaContentType.IMAGE, MediaContentType.VIDEO}
                and summaries[source_id].asset_count > summaries[source_id].missing_count + summaries[source_id].error_count
            ),
            None,
        )
        if summary is None:
            self._emit_message(
                "Thumbnail creation skipped: no usable image or video source was used by this embedding.",
                completion_log,
            )
            self._emit_overall_progress(99, "Completing run without thumbnails...")
            return

        label = next_thumbnail_variant_label(
            repo,
            f"{summary.source.label} (thumbs)",
        )
        from EmbeddingsArea.runtime.contracts import RuntimeProgress
        self._telemetry(self._run_id, RuntimeProgress("thumbnails", 0, 0, 0))
        self._emit_message("Embedding completed. Creating default thumbnails...", completion_log)
        total_inputs = processed = 0
        thumbnail_started = time.monotonic()

        def _progress(event: str, count: int, _skipped: int, message: str) -> None:
            nonlocal total_inputs, processed
            if event == "start":
                total_inputs = count
            elif event == "step":
                processed = count
            self._telemetry(self._run_id, RuntimeProgress("thumbnails", processed, total_inputs, time.monotonic() - thumbnail_started))
            self._emit_overall_progress(99, message)

        try:
            result = create_source_thumbnails(
                repo,
                session.media_repository,
                session.link_repository,
                summary,
                label=label,
                # Preview source media independently of model tensor resizing.
                resize_mode="original_aspect",
                progress_callback=_progress,
                should_cancel=lambda: self._cancel_requested,
            )
        except Exception as exc:  # noqa: BLE001
            self._emit_message(f"Default thumbnail generation failed: {exc}", completion_log)
            self._emit_overall_progress(99, "Thumbnail generation finished with an error.")
            return
        self._emit_message(f"Generated default thumbnail variant '{result.label}'.", completion_log)

    def _emit_message(self, message: str, completion_log: list[str]) -> None:
        completion_log.append(message)
        self._message(self._run_id, message)

