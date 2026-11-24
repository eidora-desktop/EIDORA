from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from queue import Empty, SimpleQueue
import threading
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from PySide6 import QtCore, QtWidgets

from DiscoveryArea.thumbnails.repository import (
    DEFAULT_THUMBNAIL_SIZES,
    ThumbnailVariant,
    ThumbnailVariantRepository,
)
from EmbeddingsArea.domain import MediaContentType, MediaSourceSummary
from EmbeddingsArea.session import EmbeddingsAreaSession
from MediaArea.controllers.new_thumbnails_dialog_controller import NewThumbnailsDialogController
from MediaArea.controllers.preview_asset_row_controller import PreviewAssetRowController
from MediaArea.domain import (
    ProcessInputKind,
    ProcessInputReference,
    ProcessRecord,
    ProcessRequest,
    ProcessState,
)
from MediaArea.process_repository import ProcessRepository
from MediaArea.thumbnail_pause import ThumbnailPause
from shared.qt.item_views import configure_panel_item_view
from shared.qt.panel_controller import UiPanelController
from shared.qt.widgets import delete_removed_widgets, require_child

if TYPE_CHECKING:
    from MediaArea.thumbnail_generator import ThumbnailGenerationResult


def _create_source_thumbnails(*args: Any, **kwargs: Any) -> ThumbnailGenerationResult:
    """Import thumbnail computation inside the admitted worker."""

    from MediaArea.thumbnail_service import create_source_thumbnails

    return create_source_thumbnails(*args, **kwargs)


def _create_artifact_thumbnails(*args: Any, **kwargs: Any) -> ThumbnailGenerationResult:
    """Import thumbnail computation inside the admitted worker."""

    from MediaArea.thumbnail_service import create_artifact_thumbnails

    return create_artifact_thumbnails(*args, **kwargs)


@dataclass
class _ActiveProcess:
    record: ProcessRecord
    cancel_event: threading.Event
    events: SimpleQueue[tuple[str, int, int, str]]
    repository: ProcessRepository
    generation: int
    future: Future[ThumbnailGenerationResult] | None = None
    admission: object | None = None
    pause: ThumbnailPause = field(init=False)

    def __post_init__(self) -> None:
        events = self.events
        self.pause = ThumbnailPause(self.cancel_event, lambda: events.put(("paused", 0, 0, "Paused")))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ThumbnailsPanelController(UiPanelController):
    package = "MediaArea"
    ui_path = "ui/thumbnails_panel.ui"

    def __init__(self, *args, **kwargs) -> None:
        self._session: EmbeddingsAreaSession | None = None
        self._repo: ThumbnailVariantRepository | None = None
        self._process_repo: ProcessRepository | None = None
        self._source_summaries: list[MediaSourceSummary] = []
        self._source_inputs: tuple[ProcessInputReference, ...] = ()
        self._thumbnail_inputs: tuple[ProcessInputReference, ...] = ()
        self._changed_callback: Callable[[], None] | None = None
        self._availability_changed_callback: Callable[[], None] | None = None
        self._active: dict[str, _ActiveProcess] = {}
        self._session_records: dict[str, ProcessRecord] = {}
        self._display_records: dict[str, ProcessRecord] = {}
        self._rows: dict[str, PreviewAssetRowController] = {}
        self._project_generation = 0
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="project-thumbnails")
        super().__init__(*args, **kwargs)

    def _wire(self) -> None:
        self._tree = require_child(self._widget, QtWidgets.QTreeWidget, "tree_thumbnails")
        configure_panel_item_view(self._tree)
        self._tree.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._open_context_menu)
        self._tree.itemExpanded.connect(self._sync_row_expansion)
        self._tree.itemCollapsed.connect(self._sync_row_expansion)
        header = self._tree.header()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self._poll = QtCore.QTimer(self._widget)
        self._poll.setInterval(50)
        self._poll.timeout.connect(self._poll_jobs)
        self._render_empty("Open or create a project to view Preview Assets.")

    def set_session(self, session: EmbeddingsAreaSession | None) -> None:
        self._session = session

    def set_changed_callback(self, callback: Callable[[], None] | None) -> None:
        self._changed_callback = callback

    def set_availability_changed_callback(self, callback: Callable[[], None]) -> None:
        self._availability_changed_callback = callback

    def _notify_availability_changed(self) -> None:
        if self._availability_changed_callback is not None:
            self._availability_changed_callback()

    def set_project(
        self,
        project,
        *,
        repository: ThumbnailVariantRepository | None = None,
    ) -> None:
        self._project_generation += 1
        self._session_records = {}
        self._cancel_all()
        super().set_project(project)
        self._repo = repository if project is not None else None
        if project is not None and self._repo is None:
            self._repo = ThumbnailVariantRepository(project.paths.root)
        self._process_repo = ProcessRepository(project.paths.state / "processes.db") if project is not None else None
        if self._process_repo is not None and self._repo is not None:
            published = {variant.variant_id for variant in self._repo.list_variants()}
            for record in self._process_repo.list_records():
                if not record.state.terminal and record.output_id not in published:
                    self._repo.discard_staged_variant(record.output_id)
                    interrupted = replace(
                        record,
                        state=ProcessState.INTERRUPTED,
                        message="Interrupted when the project was closed.",
                        finished_at=_now(),
                    )
                    self._session_records[interrupted.id] = interrupted
                self._process_repo.delete(record.id)
        self.refresh()

    def refresh(self) -> None:
        if self._project is None or self._session is None or self._process_repo is None:
            self._source_summaries = []
            self._source_inputs = ()
            self._thumbnail_inputs = ()
            self._render_empty("Open or create a project to view Preview Assets.")
            self._notify_availability_changed()
            return
        self._source_summaries = list(self._session.media_library.list_source_summaries())
        self._source_inputs = tuple(
            ProcessInputReference(
                kind=ProcessInputKind.MEDIA_SOURCE,
                id=summary.source.id,
                label=summary.source.label,
                media_type=summary.source.content_type.value,
                item_count=summary.linked_count,
            )
            for summary in self._source_summaries
            if summary.source.content_type in {MediaContentType.IMAGE, MediaContentType.VIDEO, MediaContentType.AUDIO, MediaContentType.TEXT}
            and summary.linked_count > 0
            and summary.asset_count - summary.missing_count - summary.error_count > 0
        )
        self._thumbnail_inputs = tuple(
            ProcessInputReference(
                kind=ProcessInputKind.ARTIFACT,
                id=variant.variant_id,
                label=variant.label,
                media_type="image",
                item_count=self._variant_item_count(variant),
            )
            for variant in self._repo.list_variants()
            if variant.preview_kind == "image" and variant.sizes and self._variant_item_count(variant) > 0
        )
        self._render_records()
        self._notify_availability_changed()

    def available_inputs(self) -> list[ProcessInputReference]:
        busy = getattr(self, "_media_source_busy", None)
        return [*(item for item in self._source_inputs if not busy or not busy(item.id)), *self._thumbnail_inputs]

    def set_media_source_busy_callback(self, callback) -> None:
        self._media_source_busy = callback

    def start_embedding_thumbnails(self, run_id: str) -> bool:
        if self._session is None or self._repo is None or self._active:
            return False
        from MediaArea.thumbnail_service import default_embedding_thumbnail_request

        self.refresh()
        configuration = self._session.service.get_embedding_run_configuration(run_id)
        request = default_embedding_thumbnail_request(configuration, self.available_inputs(), self._repo)
        if request is None:
            return False
        return self._queue_request(request) is not None

    def open_create_dialog(
        self,
        preselected_input: ProcessInputReference | None = None,
        *,
        fixed_input: bool = False,
        initial_name: str = "",
        initial_parameters: dict[str, object] | None = None,
    ) -> None:
        if self._project is None or self._process_repo is None:
            self._show_status("Open a project before creating Preview Assets.")
            return
        if not isinstance(preselected_input, ProcessInputReference):
            preselected_input = None
        names = {variant.label for variant in self._repo.list_variants()} if self._repo else set()
        names.update(self._process_repo.list_output_names())
        names.update(active.record.output_name for active in self._active.values())
        controller = NewThumbnailsDialogController(
            self.available_inputs(), names,
            preselected_input=preselected_input,
            fixed_input=fixed_input,
            initial_name=initial_name,
            initial_parameters=initial_parameters,
        )
        request = controller.exec()
        if request is not None:
            self._queue_request(request)

    def open_for_source(self, source_id: str) -> None:
        self.refresh()
        selected = next(
            (item for item in self.available_inputs() if item.kind == ProcessInputKind.MEDIA_SOURCE and item.id == source_id),
            None,
        )
        if selected is None:
            self._show_status("Link this image or video source to metadata before processing it.")
            return
        self.open_create_dialog(selected, fixed_input=True)

    def can_process_source(self, source_id: str) -> bool:
        return any(
            item.kind == ProcessInputKind.MEDIA_SOURCE and item.id == source_id
            for item in self.available_inputs()
        )

    def input_in_use(self, artifact_id: str) -> bool:
        return any(
            active.record.input.kind == ProcessInputKind.ARTIFACT
            and active.record.input.id == artifact_id
            and active.generation == self._project_generation
            and not active.record.state.terminal
            for active in self._active.values()
        )

    def source_in_use(self, source_id: str) -> bool:
        return any(
            active.record.input.kind == ProcessInputKind.MEDIA_SOURCE
            and active.record.input.id == source_id
            and active.generation == self._project_generation
            and not active.record.state.terminal
            for active in self._active.values()
        )

    def shutdown(self) -> None:
        self._cancel_all()
        self._poll.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _queue_request(self, request: ProcessRequest) -> str | None:
        if self._process_repo is None:
            return
        eligible_input = next(
            (
                candidate
                for candidate in self.available_inputs()
                if candidate.kind == request.input.kind
                and candidate.id == request.input.id
            ),
            None,
        )
        if eligible_input is None:
            self._show_status(
                "The selected input is no longer available for creating Preview Assets."
            )
            return
        request = replace(request, input=eligible_input, parameters={
            key: value for key, value in request.parameters.items()
            if key != "video_frame_mode" or eligible_input.media_type == "video"
        })
        if request.output_name.strip().casefold() in {
            variant.label.casefold()
            for variant in (self._repo.list_variants() if self._repo is not None else ())
        } or request.output_name.strip().casefold() in {
            active.record.output_name.strip().casefold()
            for active in self._active.values()
        } or self._process_repo.output_name_exists(request.output_name):
            self._show_status("That name is already used for Preview Assets or a pending job.")
            return
        if request.operation_id != "create_thumbnails":
            raise ValueError(f"Unsupported thumbnail operation: {request.operation_id}")
        record = ProcessRecord(
            id=uuid4().hex,
            operation_id=request.operation_id,
            operation_label="Create Preview Assets",
            input=request.input,
            output_id=request.output_id,
            output_name=request.output_name,
            parameters=request.parameters,
            state=ProcessState.QUEUED,
            message="Waiting for background capacity…",
            created_at=_now(),
        )
        self._process_repo.add(record)
        active = _ActiveProcess(
            record, threading.Event(), SimpleQueue(), self._process_repo, self._project_generation
        )
        self._active[record.id] = active
        generation = self._project_generation

        def start() -> None:
            if generation != self._project_generation or active.cancel_event.is_set():
                self._finish_cancelled(active)
                return
            self._start_process(active, generation)

        if self._activity_manager is not None and hasattr(self._activity_manager, "processes"):
            active.admission = self._activity_manager.processes.submit(
                panel="Preview Assets",
                footer_label="Creating Preview Assets",
                title=record.output_name,
                start=start,
                cancel=lambda: self._cancel(record.id),
            )
            self._activity_manager.processes.dispatch()
        else:
            start()
        self._poll.start()
        self._render_records(scroll_to_id=record.id)
        self._notify_availability_changed()
        self._show_status(f"Queued {record.operation_label} for '{record.input.label}'.")
        return record.id

    def _start_process(self, active: _ActiveProcess, generation: int) -> None:
        if self._repo is None or self._session is None:
            return
        from shared.diagnostics.service import LOGGER

        LOGGER.info("Thumbnail creation started")
        record = replace(
            active.record,
            state=ProcessState.RUNNING,
            progress=0,
            message="Preparing input…",
            started_at=_now(),
        )
        active.record = record
        active.repository.save(record)
        summary = next(
            (item for item in self._source_summaries if item.source.id == record.input.id),
            None,
        )
        events = active.events
        common = dict(
            label=record.output_name,
            resize_mode=str(record.parameters.get("resize_mode") or "original_aspect"),
            progress_callback=lambda *event: events.put(event),
            should_cancel=active.cancel_event.is_set,
            pause=active.pause,
            variant_id=record.output_id,
        )
        if record.input.kind == ProcessInputKind.MEDIA_SOURCE:
            if summary is None:
                active.future = self._executor.submit(self._raise_missing_input)
            else:
                active.future = self._executor.submit(
                    _create_source_thumbnails,
                    self._repo,
                    self._session.media_repository,
                    self._session.link_repository,
                    summary,
                    preview_parameters=record.parameters,
                    video_frame_mode=(str(record.parameters.get("video_frame_mode") or "middle")
                                      if record.input.media_type == "video" else None),
                    **common,
                )
        else:
            active.future = self._executor.submit(
                _create_artifact_thumbnails,
                self._repo,
                record.input.id,
                preview_parameters=record.parameters,
                **common,
            )
        self._update_record_row(record)
        if active.admission is not None:
            active.admission.update(message=record.message, value=-1)

    @staticmethod
    def _raise_missing_input() -> ThumbnailGenerationResult:
        raise ValueError("The selected media source is no longer available.")

    def _poll_jobs(self) -> None:
        for active in list(self._active.values()):
            self._drain_events(active)
            future = active.future
            if future is None or not future.done():
                continue
            active.future = None
            self._drain_events(active)
            try:
                result = future.result()
            except CancelledError:
                self._finish_cancelled(active)
            except Exception as exc:  # noqa: BLE001
                from MediaArea.thumbnail_generator import ThumbnailGenerationCancelled

                if isinstance(exc, ThumbnailGenerationCancelled):
                    self._finish_cancelled(active)
                else:
                    self._finish_failed(active, str(exc))
            else:
                self._finish_completed(active, result)
        if not self._active:
            self._poll.stop()

    def _drain_events(self, active: _ActiveProcess) -> None:
        changed = False
        record = active.record
        while True:
            try:
                event, value1, _value2, message = active.events.get_nowait()
            except Empty:
                break
            if event == "paused":
                if record.state != ProcessState.PAUSING or not active.pause.paused:
                    continue
                record = replace(record, state=ProcessState.PAUSED)
            if event == "start":
                progress = 2
            elif event == "step":
                total = max(1, record.input.item_count)
                progress = 5 + int(85 * max(0, value1) / total)
            elif event == "index":
                progress = 95
            elif event == "complete":
                progress = 99
            else:
                progress = record.progress
            if record.state == ProcessState.CANCELLING:
                message = "Cancelling…"
            elif record.state == ProcessState.PAUSING:
                message = "Pausing after active files finish…"
            record = replace(record, progress=max(record.progress, min(99, progress)), message=message)
            if event == "paused":
                active.repository.save(record)
            changed = True
        if changed:
            active.record = record
            self._update_record_row(record)
            if active.admission is not None:
                active.admission.update(message=record.message, value=-1 if record.state == ProcessState.RUNNING and record.progress == 0 else record.progress)

    def _finish_completed(self, active: _ActiveProcess, result: ThumbnailGenerationResult) -> None:
        warnings = bool(result.failed_files or result.missing_files)
        state = ProcessState.COMPLETED_WITH_WARNINGS if warnings else ProcessState.COMPLETED
        parameters = dict(active.record.parameters)
        kind = parameters.get("preview_kind", "image")
        parameters["sizes"] = () if kind == "text_excerpt" else (256,) if kind != "image" else DEFAULT_THUMBNAIL_SIZES
        if kind != "image":
            parameters["total_bytes"] = result.total_bytes
        record = replace(
            active.record, state=state, progress=100,
            message="Completed with warnings." if warnings else "Completed.", finished_at=_now(),
            generated_files=result.generated_files, failed_files=result.failed_files,
            missing_files=result.missing_files,
            errors=tuple(str(error)[:1000] for error in result.errors[:10]),
            parameters=parameters,
        )
        current_project = active.generation == self._project_generation
        active.record = record
        active.repository.delete(record.id)
        if current_project:
            self._display_records[record.id] = record
            if kind == "image":
                self._thumbnail_inputs = (
                    *(
                        candidate
                        for candidate in self._thumbnail_inputs
                        if candidate.id != record.output_id
                    ),
                    ProcessInputReference(
                        ProcessInputKind.ARTIFACT,
                        record.output_id,
                        record.output_name,
                        "image",
                        record.generated_files,
                    ),
                )
            self._update_record_row(record)
            if self._changed_callback is not None:
                self._changed_callback()
        admission = active.admission
        if admission is not None and hasattr(admission, "finish"):
            admission.finish()
        self._active.pop(record.id, None)
        if current_project:
            self._notify_availability_changed()
            self._show_status(f"Created Preview Assets '{record.output_name}'.")

    def _finish_failed(self, active: _ActiveProcess, message: str) -> None:
        from shared.diagnostics.service import record_failure

        guidance = record_failure("Could not create Preview Assets", message)
        if guidance:
            message = f"{message}\n\n{guidance}"
        current_project = active.generation == self._project_generation
        bounded_message = str(message)[:2000]
        record = replace(
            active.record, state=ProcessState.FAILED, message=bounded_message,
            finished_at=_now(), errors=(bounded_message[:1000],),
        )
        self._finish_unsuccessful(active, record)
        if current_project:
            self._show_status(
                f"{active.record.operation_label} failed: {bounded_message}"
            )

    def _finish_cancelled(self, active: _ActiveProcess) -> None:
        record = replace(
            active.record, state=ProcessState.CANCELLED,
            message="Canceled; unpublished files were removed.", finished_at=_now(),
        )
        active.record = record
        active.repository.delete(record.id)
        current_project = active.generation == self._project_generation
        if current_project:
            self._session_records[record.id] = record
        admission = active.admission
        if admission is not None and hasattr(admission, "finish"):
            admission.finish()
        self._active.pop(record.id, None)
        if current_project:
            self._notify_availability_changed()
            self._render_records(scroll_to_id=record.id)

    def _finish_unsuccessful(self, active: _ActiveProcess, record: ProcessRecord) -> None:
        active.record = record
        active.repository.delete(record.id)
        current_project = active.generation == self._project_generation
        if current_project:
            self._session_records[record.id] = record
        admission = active.admission
        if admission is not None and hasattr(admission, "finish"):
            admission.finish()
        self._active.pop(record.id, None)
        if current_project:
            self._notify_availability_changed()
            self._render_records(scroll_to_id=record.id)

    def _toggle_pause(self, process_id: str) -> None:
        active = self._active.get(process_id)
        if active is None:
            return
        record = active.record
        if record.state == ProcessState.PAUSED:
            active.pause.resume()
            record = replace(record, state=ProcessState.RUNNING, message="Resuming…")
        elif record.state == ProcessState.RUNNING and record.progress < 95:
            # Retain admission while paused: the coordinator keeps staged work
            # in memory and resumes in the same bounded executor slot.
            active.pause.request()
            record = replace(record, state=ProcessState.PAUSING, message="Pausing after active files finish…")
        else:
            return
        active.record = record
        active.repository.save(record)
        self._update_record_row(record)
        if active.admission is not None:
            active.admission.update(message=record.message, value=record.progress)

    def _cancel(self, process_id: str) -> None:
        active = self._active.get(process_id)
        if active is None or active.cancel_event.is_set():
            return
        active.cancel_event.set()
        active.pause.resume()
        active.record = replace(active.record, state=ProcessState.CANCELLING, message="Cancelling…")
        active.repository.save(active.record)
        admission = active.admission
        if admission is not None and hasattr(admission, "cancel"):
            admission.cancel()
            admission.update(message="Cancelling Preview Assets job…", value=active.record.progress)
        if active.future is not None:
            active.future.cancel()
        elif active.record.id in self._active:
            self._finish_cancelled(active)
            return
        self._update_record_row(active.record)

    def _cancel_all(self) -> None:
        for process_id in tuple(self._active):
            self._cancel(process_id)

    def _render_records(self, *, scroll_to_id: str = "") -> None:
        if self._process_repo is None:
            self._render_empty("Open or create a project to view Preview Assets.")
            return
        old_items = [
            self._tree.topLevelItem(index)
            for index in range(self._tree.topLevelItemCount())
        ]
        scroll_value = self._tree.verticalScrollBar().value()
        expanded = {row.record.output_id for row in self._rows.values() if row.item.isExpanded()}
        live_records = {
            process_id: active.record
            for process_id, active in self._active.items()
            if active.generation == self._project_generation
        }
        active_records = list(live_records.values())
        session_records = sorted(
            self._session_records.values(),
            key=lambda record: record.created_at,
            reverse=True,
        )
        variants = self._repo.list_variants() if self._repo is not None else []
        saved_records = [self._record_for_variant(variant, variants) for variant in variants]
        active_output_ids = {record.output_id for record in active_records}
        records = [
            *active_records,
            *session_records,
            *(record for record in saved_records if record.output_id not in active_output_ids),
        ]
        self._display_records = {record.id: record for record in records}
        self._rows = {}
        if not records:
            self._render_empty(
                "No Preview Assets yet. Create previews from a Media Source.",
                old_items=old_items,
            )
            return
        for record in records:
            item = self._create_record_row(record)
            item.setExpanded(record.output_id in expanded)
            if record.id == scroll_to_id:
                self._tree.scrollToItem(item)
        self._remove_old_items(old_items)
        self._tree.verticalScrollBar().setValue(scroll_value)

    def _record_for_variant(
        self,
        variant: ThumbnailVariant,
        variants: list[ThumbnailVariant],
    ) -> ProcessRecord:
        source = next(
            (summary for summary in self._source_summaries if summary.source.id == variant.source_id),
            None,
        )
        source_variant = next(
            (candidate for candidate in variants if candidate.variant_id == variant.source_id),
            None,
        )
        input_kind = (
            ProcessInputKind.ARTIFACT
            if variant.input_kind == ProcessInputKind.ARTIFACT.value
            or source_variant is not None
            or variant.source_dir.startswith(str(self._repo.variants_root))
            else ProcessInputKind.MEDIA_SOURCE
        )
        input_label = variant.input_label or (
            source.source.label
            if source is not None
            else source_variant.label
            if source_variant is not None
            else variant.source_id or variant.source_dir
        )
        count = self._variant_item_count(variant)
        state = (
            ProcessState.COMPLETED_WITH_WARNINGS
            if variant.failed_files or variant.missing_files
            else ProcessState.COMPLETED
        )
        return ProcessRecord(
            id=f"saved:{variant.variant_id}",
            operation_id="create_thumbnails",
            operation_label="Create Preview Assets",
            input=ProcessInputReference(
                input_kind,
                variant.source_id,
                input_label,
                variant.source_media_type,
                count,
            ),
            output_id=variant.variant_id,
            output_name=variant.label,
            parameters={
                "resize_mode": variant.resize_mode,
                "video_frame_mode": variant.video_frame_mode,
                "sizes": variant.sizes,
                "preview_kind": variant.preview_kind,
                "total_bytes": variant.total_bytes,
                **variant.parameters,
            },
            state=state,
            progress=100,
            message="Created successfully." if state == ProcessState.COMPLETED else "Created with warnings.",
            created_at=variant.created_at,
            finished_at=variant.created_at,
            generated_files=count,
            failed_files=variant.failed_files,
            missing_files=variant.missing_files,
            errors=tuple(str(error)[:1000] for error in variant.errors[:10]),
        )

    def _variant_item_count(self, variant: ThumbnailVariant) -> int:
        if variant.generated_files is not None:
            return int(variant.generated_files)
        return self._repo.count_variant_items(variant.variant_id) if self._repo is not None else 0

    def _create_record_row(self, record: ProcessRecord) -> QtWidgets.QTreeWidgetItem:
        row = PreviewAssetRowController(self._tree, record)
        self._rows[record.id] = row
        row.pause.clicked.connect(lambda _checked=False: self._toggle_pause(record.id))
        row.cancel.clicked.connect(lambda _checked=False: self._cancel(record.id))
        row.options.clicked.connect(lambda _checked=False: self._open_record_menu(
            record.id, row.options.mapToGlobal(row.options.rect().bottomLeft()),
        ))
        self._update_record_row(record)
        return row.item

    def _update_record_row(self, record: ProcessRecord) -> None:
        row = self._rows.get(record.id)
        if row is not None:
            self._display_records[record.id] = record
            row.update(record, cancellable=record.id in self._active)

    def _sync_row_expansion(self, item: QtWidgets.QTreeWidgetItem) -> None:
        row = self._rows.get(str(item.data(0, QtCore.Qt.ItemDataRole.UserRole) or ""))
        if row is not None:
            row.sync_expansion()

    def _open_context_menu(self, position: QtCore.QPoint) -> None:
        item = self._tree.itemAt(position)
        if item is None:
            return
        if item.parent() is not None:
            item = item.parent()
        process_id = str(item.data(0, QtCore.Qt.ItemDataRole.UserRole) or "")
        self._open_record_menu(process_id, self._tree.viewport().mapToGlobal(position))

    def _open_record_menu(self, process_id: str, position: QtCore.QPoint) -> None:
        record = self._display_records.get(process_id)
        row = self._rows.get(process_id)
        if record is None or row is None:
            return
        inputs = self.available_inputs()
        current_input = next((candidate for candidate in inputs
                              if candidate.kind == record.input.kind and candidate.id == record.input.id), None)
        thumbnail_input = next((candidate for candidate in inputs
                                if candidate.kind == ProcessInputKind.ARTIFACT and candidate.id == record.output_id), None)
        is_saved = record.state in {ProcessState.COMPLETED, ProcessState.COMPLETED_WITH_WARNINGS}
        menu = QtWidgets.QMenu(self._widget)
        actions = {}
        if is_saved:
            if record.parameters.get("preview_kind") in {"audio_clip", "video_clip", "animated_gif", "text_excerpt"}:
                actions["inspect"] = menu.addAction("Inspect…")
                actions["inspect"].setEnabled(record.generated_files > 0)
            actions["rename"] = menu.addAction("Rename…")
            actions["run_again"] = menu.addAction("Run Again…")
            actions["run_again"].setEnabled(current_input is not None)
            actions["create_from"] = menu.addAction("Create Previews From…")
            actions["create_from"].setEnabled(thumbnail_input is not None)
            menu.addSeparator()
            actions["remove"] = menu.addAction("Remove…")
            if self.input_in_use(record.output_id):
                actions["remove"].setEnabled(False)
                actions["remove"].setToolTip("These Preview Assets are used by queued or running work.")
        elif record.state.terminal:
            actions["retry"] = menu.addAction("Retry…")
            actions["retry"].setEnabled(current_input is not None)
            menu.addSeparator()
            actions["dismiss"] = menu.addAction("Dismiss")
        else:
            if record.state == ProcessState.PAUSED:
                actions["resume"] = menu.addAction("Resume")
                actions["resume"].setEnabled(process_id in self._active)
            actions["cancel"] = menu.addAction("Cancel")
            actions["cancel"].setEnabled(process_id in self._active and record.state != ProcessState.CANCELLING)
        selected = self._exec_row_context_menu(menu, position)
        action = next((key for key, value in actions.items() if value is selected and value.isEnabled()), None)
        if action == "inspect":
            if record.parameters.get("preview_kind") == "text_excerpt":
                from MediaArea.controllers.text_inspector_controller import TextInspectorController
                TextInspectorController(self._repo, record.output_id, record.output_name).exec()
            else:
                from MediaArea.controllers.clip_inspector_controller import ClipInspectorController
                ClipInspectorController(self._repo, record.output_id, record.output_name, record.parameters).exec()
        elif action == "resume":
            self._toggle_pause(process_id)
        elif action == "create_from":
            self.open_create_dialog(thumbnail_input, fixed_input=True)
        elif action in {"run_again", "retry"}:
            self.open_create_dialog(current_input, fixed_input=True, initial_parameters=record.parameters)
        elif action == "rename":
            self._rename_variant(record.output_id)
        elif action == "remove":
            self._remove_variant(record.output_id)
        elif action == "dismiss":
            self._session_records.pop(record.id, None)
            self._render_records()
        elif action == "cancel":
            self._cancel(process_id)

    @staticmethod
    def _exec_row_context_menu(menu: QtWidgets.QMenu, position: QtCore.QPoint):
        return menu.exec(position)

    def _rename_variant(self, variant_id: str) -> None:
        if self._repo is None:
            return
        variant = next(
            (candidate for candidate in self._repo.list_variants() if candidate.variant_id == variant_id),
            None,
        )
        if variant is None:
            return
        name, accepted = QtWidgets.QInputDialog.getText(
            self._widget,
            "Rename Preview Assets",
            "Name:",
            text=variant.label,
        )
        if not accepted or not name.strip():
            return
        try:
            self._repo.rename_variant(variant_id, name.strip())
        except Exception as exc:  # noqa: BLE001
            self._show_status(f"Could not rename Preview Assets: {exc}")
            return
        self.refresh()
        if self._changed_callback is not None:
            self._changed_callback()

    def _remove_variant(self, variant_id: str) -> None:
        if self._repo is None:
            return
        variant = next(
            (candidate for candidate in self._repo.list_variants() if candidate.variant_id == variant_id),
            None,
        )
        if variant is None:
            return
        answer = QtWidgets.QMessageBox.question(
            self._widget,
            "Remove Preview Assets",
            f"Remove '{variant.label}' and its generated files?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._repo.delete_variant(variant_id)
        self.refresh()
        if self._changed_callback is not None:
            self._changed_callback()

    def _render_empty(
        self,
        message: str,
        *,
        old_items: list[QtWidgets.QTreeWidgetItem] | None = None,
    ) -> None:
        self._rows = {}
        if old_items is None:
            old_items = [
                self._tree.topLevelItem(index)
                for index in range(self._tree.topLevelItemCount())
            ]
        item = QtWidgets.QTreeWidgetItem([message])
        item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable & ~QtCore.Qt.ItemFlag.ItemIsEnabled)
        self._tree.addTopLevelItem(item)
        item.setFirstColumnSpanned(True)
        self._remove_old_items(old_items)

    def _remove_old_items(self, items: list[QtWidgets.QTreeWidgetItem]) -> None:
        widgets = []
        for item in items:
            index = self._tree.indexOfTopLevelItem(item)
            if index >= 0:
                for node in (item, *(item.child(i) for i in range(item.childCount()))):
                    widget = self._tree.itemWidget(node, 0)
                    if widget is not None:
                        widgets.append(widget)
                self._tree.takeTopLevelItem(index)
        delete_removed_widgets(self._tree, widgets)
