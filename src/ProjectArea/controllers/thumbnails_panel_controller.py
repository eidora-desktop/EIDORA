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
from ProjectArea.controllers.new_thumbnails_dialog_controller import NewThumbnailsDialogController
from ProjectArea.domain import (
    THUMBNAIL_RESIZE_MODES,
    THUMBNAIL_VIDEO_FRAME_MODES,
    ProcessInputKind,
    ProcessInputReference,
    ProcessRecord,
    ProcessRequest,
    ProcessState,
)
from ProjectArea.process_repository import ProcessRepository
from shared.qt.item_views import PANEL_ITEM_ROW_HEIGHT
from shared.qt.item_views import configure_panel_item_view
from shared.qt.panel_controller import UiPanelController
from shared.qt.tree_details import DetailSection, create_tree_details_panel
from shared.qt.widgets import require_child

if TYPE_CHECKING:
    from ProjectArea.thumbnail_generator import ThumbnailGenerationResult


_PROCESS_PARAMETER_LABELS = {
    "resize_mode": {value: label for label, value in THUMBNAIL_RESIZE_MODES},
    "video_frame_mode": {
        value: label for label, value in THUMBNAIL_VIDEO_FRAME_MODES
    },
}


def _create_source_thumbnails(*args: Any, **kwargs: Any) -> ThumbnailGenerationResult:
    """Import thumbnail computation inside the admitted worker."""

    from ProjectArea.thumbnail_service import create_source_thumbnails

    return create_source_thumbnails(*args, **kwargs)


def _create_artifact_thumbnails(*args: Any, **kwargs: Any) -> ThumbnailGenerationResult:
    """Import thumbnail computation inside the admitted worker."""

    from ProjectArea.thumbnail_service import create_artifact_thumbnails

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


@dataclass
class _ProcessRow:
    item: QtWidgets.QTreeWidgetItem
    progress: QtWidgets.QProgressBar | None
    status_label: QtWidgets.QLabel | None
    cancel: QtWidgets.QPushButton | None
    detail_item: QtWidgets.QTreeWidgetItem | None = None
    detail_panel: QtWidgets.QWidget | None = None
    detail_values: dict[str, QtWidgets.QLabel] = field(default_factory=dict)
    detail_signature: tuple[tuple[str, tuple[str, ...]], ...] = ()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ThumbnailsPanelController(UiPanelController):
    package = "ProjectArea"
    ui_path = "ui/thumbnails_panel.ui"

    def __init__(self, *args, **kwargs) -> None:
        self._session: EmbeddingsAreaSession | None = None
        self._repo: ThumbnailVariantRepository | None = None
        self._process_repo: ProcessRepository | None = None
        self._source_summaries: list[MediaSourceSummary] = []
        self._source_inputs: tuple[ProcessInputReference, ...] = ()
        self._thumbnail_inputs: tuple[ProcessInputReference, ...] = ()
        self._changed_callback: Callable[[], None] | None = None
        self._active: dict[str, _ActiveProcess] = {}
        self._session_records: dict[str, ProcessRecord] = {}
        self._display_records: dict[str, ProcessRecord] = {}
        self._rows: dict[str, _ProcessRow] = {}
        self._project_generation = 0
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="project-thumbnails")
        super().__init__(*args, **kwargs)

    def _wire(self) -> None:
        self._tree = require_child(self._widget, QtWidgets.QTreeWidget, "tree_thumbnails")
        self._add_button = require_child(self._widget, QtWidgets.QPushButton, "button_add_thumbnails")
        configure_panel_item_view(self._tree)
        self._add_button.clicked.connect(lambda _checked=False: self.open_create_dialog())
        self._tree.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._open_context_menu)
        header = self._tree.header()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self._poll = QtCore.QTimer(self._widget)
        self._poll.setInterval(50)
        self._poll.timeout.connect(self._poll_jobs)
        self._render_empty("Open or create a project to view thumbnails.")

    def set_session(self, session: EmbeddingsAreaSession | None) -> None:
        self._session = session

    def set_changed_callback(self, callback: Callable[[], None] | None) -> None:
        self._changed_callback = callback

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
            self._add_button.setEnabled(False)
            self._render_empty("Open or create a project to view thumbnails.")
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
            if summary.source.content_type in {MediaContentType.IMAGE, MediaContentType.VIDEO}
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
            if variant.sizes and self._variant_item_count(variant) > 0
        )
        self._add_button.setEnabled(True)
        self._render_records()

    def available_inputs(self) -> list[ProcessInputReference]:
        busy = getattr(self, "_media_source_busy", None)
        return [*(item for item in self._source_inputs if not busy or not busy(item.id)), *self._thumbnail_inputs]

    def set_media_source_busy_callback(self, callback) -> None:
        self._media_source_busy = callback

    def open_create_dialog(
        self,
        preselected_input: ProcessInputReference | None = None,
        *,
        initial_name: str = "",
        initial_parameters: dict[str, object] | None = None,
    ) -> None:
        if self._project is None or self._process_repo is None:
            self._show_status("Open a project before creating thumbnails.")
            return
        if not isinstance(preselected_input, ProcessInputReference):
            preselected_input = None
        names = {variant.label for variant in self._repo.list_variants()} if self._repo else set()
        names.update(self._process_repo.list_output_names())
        names.update(active.record.output_name for active in self._active.values())
        controller = NewThumbnailsDialogController(
            self.available_inputs(), names,
            preselected_input=preselected_input,
            initial_name=initial_name,
            initial_parameters=initial_parameters,
        )
        request = controller.exec()
        if request is not None:
            self._queue_request(request)

    def open_for_source(self, source_id: str) -> None:
        selected = next(
            (item for item in self.available_inputs() if item.kind == ProcessInputKind.MEDIA_SOURCE and item.id == source_id),
            None,
        )
        if selected is None:
            self._show_status("Link this image or video source to metadata before processing it.")
            return
        self.open_create_dialog(selected)

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

    def _queue_request(self, request: ProcessRequest) -> None:
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
                "The selected input is no longer available for thumbnail creation."
            )
            return
        request = replace(request, input=eligible_input)
        if request.output_name.strip().casefold() in {
            variant.label.casefold()
            for variant in (self._repo.list_variants() if self._repo is not None else ())
        } or request.output_name.strip().casefold() in {
            active.record.output_name.strip().casefold()
            for active in self._active.values()
        } or self._process_repo.output_name_exists(request.output_name):
            self._show_status("A thumbnail set or pending output already uses that name.")
            return
        if request.operation_id != "create_thumbnails":
            raise ValueError(f"Unsupported thumbnail operation: {request.operation_id}")
        record = ProcessRecord(
            id=uuid4().hex,
            operation_id=request.operation_id,
            operation_label="Create Thumbnails",
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
                panel="Project Thumbnails",
                title=record.output_name,
                start=start,
                cancel=active.cancel_event.set,
            )
            self._activity_manager.processes.dispatch()
        else:
            start()
        self._poll.start()
        self._render_records(select_id=record.id)
        self._show_status(f"Queued {record.operation_label} for '{record.input.label}'.")

    def _start_process(self, active: _ActiveProcess, generation: int) -> None:
        if self._repo is None or self._session is None:
            return
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
                    video_frame_mode=str(record.parameters.get("video_frame_mode") or "middle"),
                    **common,
                )
        else:
            active.future = self._executor.submit(
                _create_artifact_thumbnails,
                self._repo,
                record.input.id,
                **common,
            )
        self._update_record_row(record)

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
                from ProjectArea.thumbnail_generator import ThumbnailGenerationCancelled

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
            record = replace(record, progress=max(record.progress, min(99, progress)), message=message)
            changed = True
        if changed:
            active.record = record
            self._update_record_row(record)

    def _finish_completed(self, active: _ActiveProcess, result: ThumbnailGenerationResult) -> None:
        warnings = bool(result.failed_files or result.missing_files)
        state = ProcessState.COMPLETED_WITH_WARNINGS if warnings else ProcessState.COMPLETED
        parameters = dict(active.record.parameters)
        parameters["sizes"] = DEFAULT_THUMBNAIL_SIZES
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
            self._show_status(f"Created thumbnail set '{record.output_name}'.")

    def _finish_failed(self, active: _ActiveProcess, message: str) -> None:
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
            self._render_records(select_id=record.id)

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
            self._render_records(select_id=record.id)

    def _cancel(self, process_id: str) -> None:
        active = self._active.get(process_id)
        if active is None:
            return
        active.cancel_event.set()
        active.record = replace(active.record, state=ProcessState.CANCELLING, message="Cancelling…")
        active.repository.save(active.record)
        admission = active.admission
        if admission is not None and hasattr(admission, "cancel"):
            admission.cancel()
        if active.future is not None:
            active.future.cancel()
        elif active.record.id in self._active:
            self._finish_cancelled(active)
            return
        self._update_record_row(active.record)

    def _cancel_all(self) -> None:
        for process_id in tuple(self._active):
            self._cancel(process_id)

    def _render_records(self, *, select_id: str = "") -> None:
        if self._process_repo is None:
            self._render_empty("Open or create a project to view thumbnails.")
            return
        old_items = [
            self._tree.topLevelItem(index)
            for index in range(self._tree.topLevelItemCount())
        ]
        scroll_value = self._tree.verticalScrollBar().value()
        expanded = {
            str(self._tree.topLevelItem(i).data(0, QtCore.Qt.ItemDataRole.UserRole) or "")
            for i in range(self._tree.topLevelItemCount())
            if self._tree.topLevelItem(i).isExpanded()
        }
        current = select_id or self._selected_process_id()
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
                "No thumbnails yet. Use Add New… to create one.",
                old_items=old_items,
            )
            return
        for record in records:
            item = self._create_record_row(record, set())
            item.setExpanded(record.id in expanded)
            if record.id == current:
                self._tree.setCurrentItem(item)
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
            operation_label="Create Thumbnails",
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

    def _create_record_row(
        self,
        record: ProcessRecord,
        artifact_ids: set[str],
    ) -> QtWidgets.QTreeWidgetItem:
        item = QtWidgets.QTreeWidgetItem([
            record.output_name,
            record.input.label,
            str(record.generated_files or record.input.item_count),
            "",
        ])
        item.setData(0, QtCore.Qt.ItemDataRole.UserRole, record.id)
        item.setSizeHint(0, QtCore.QSize(0, PANEL_ITEM_ROW_HEIGHT))
        self._tree.addTopLevelItem(item)
        status = QtWidgets.QWidget(self._tree)
        layout = QtWidgets.QHBoxLayout(status)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(6)
        progress: QtWidgets.QProgressBar | None = None
        status_label: QtWidgets.QLabel | None = None
        if record.state.terminal:
            status_label = QtWidgets.QLabel(self._state_text(record.state), status)
            status_label.setObjectName("label_process_status")
            layout.addWidget(status_label, 1)
        else:
            progress = QtWidgets.QProgressBar(status)
            progress.setObjectName("progress_process")
            layout.addWidget(progress, 1)
        cancel: QtWidgets.QPushButton | None = None
        if record.id in self._active:
            cancel = QtWidgets.QPushButton("Cancel", status)
            cancel.clicked.connect(
                lambda _checked=False, value=record.id: self._cancel(value)
            )
            layout.addWidget(cancel)
        self._tree.setItemWidget(item, 3, status)
        self._rows[record.id] = _ProcessRow(item, progress, status_label, cancel)
        self._update_record_row(record, artifact_ids=artifact_ids)
        return item

    def _update_record_row(
        self,
        record: ProcessRecord,
        *,
        artifact_ids: set[str] | None = None,
    ) -> None:
        row = self._rows.get(record.id)
        if row is None:
            return
        if record.state.terminal and row.progress is not None:
            status_widget = self._tree.itemWidget(row.item, 3)
            if status_widget is not None:
                self._tree.removeItemWidget(row.item, 3)
                status_widget.deleteLater()
            status_widget = QtWidgets.QWidget(self._tree)
            status_layout = QtWidgets.QHBoxLayout(status_widget)
            status_layout.setContentsMargins(0, 0, 6, 0)
            status_layout.setSpacing(6)
            row.status_label = QtWidgets.QLabel(self._state_text(record.state), status_widget)
            row.status_label.setObjectName("label_thumbnail_status")
            status_layout.addWidget(row.status_label, 1)
            self._tree.setItemWidget(row.item, 3, status_widget)
            row.progress = None
            row.cancel = None
        row.item.setText(0, record.output_name)
        row.item.setText(1, record.input.label)
        row.item.setText(2, str(record.generated_files or record.input.item_count))
        if row.progress is not None:
            if record.state == ProcessState.RUNNING and record.progress == 0:
                row.progress.setRange(0, 0)
            else:
                row.progress.setRange(0, 100)
                row.progress.setValue(record.progress)
            row.progress.setFormat(self._state_text(record.state))
        if row.status_label is not None:
            row.status_label.setText(self._state_text(record.state))
        if row.cancel is not None:
            row.cancel.setEnabled(record.state != ProcessState.CANCELLING)

        sections = self._process_detail_sections(record, False)
        signature = tuple(
            (title, tuple(key for key, _name, _value in entries))
            for title, entries in sections
            if entries
        )
        if row.detail_panel is None or signature != row.detail_signature:
            if row.detail_item is None:
                row.detail_item = QtWidgets.QTreeWidgetItem(["", "", "", ""])
                row.detail_item.setFlags(
                    row.detail_item.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable
                )
                row.item.addChild(row.detail_item)
                row.detail_item.setFirstColumnSpanned(True)
            elif row.detail_panel is not None:
                self._tree.removeItemWidget(row.detail_item, 0)
                row.detail_panel.deleteLater()
            row.detail_panel, row.detail_values = create_tree_details_panel(
                self._tree,
                "panel_thumbnail_details",
                sections,
            )
            row.detail_signature = signature
            self._tree.setItemWidget(row.detail_item, 0, row.detail_panel)
        else:
            for _title, entries in sections:
                for key, _name, value in entries:
                    row.detail_values[key].setText(value)
        if row.detail_item is not None and row.detail_panel is not None:
            row.detail_item.setSizeHint(0, row.detail_panel.sizeHint())

    def _process_detail_sections(
        self,
        record: ProcessRecord,
        artifact_removed: bool,
    ) -> tuple[DetailSection, ...]:
        details = [
            ("state", "State", self._state_text(record.state)),
            ("message", "Message", record.message or "—"),
            ("created", "Created", record.created_at or "—"),
            ("started", "Started", record.started_at or "—"),
            ("finished", "Finished", record.finished_at or "—"),
        ]
        parameters = [
            (
                f"parameter:{key}",
                str(key).replace("_", " ").capitalize(),
                self._parameter_text(str(key), value),
            )
            for key, value in record.parameters.items()
        ]
        results = [
            ("generated", "Generated", f"{record.generated_files:,}"),
            ("failed", "Failed", f"{record.failed_files:,}"),
            ("missing", "Missing", f"{record.missing_files:,}"),
        ]
        if artifact_removed:
            results.append(("artifact", "Artifact", "Removed"))
        errors = [
            (f"error:{index}", "Error", error)
            for index, error in enumerate(record.errors)
        ]
        return (
            ("Details", details),
            ("Parameters", parameters),
            ("Results", results),
            ("Errors", errors),
        )

    @staticmethod
    def _parameter_text(name: str, value: object) -> str:
        if name in _PROCESS_PARAMETER_LABELS:
            return _PROCESS_PARAMETER_LABELS[name].get(str(value), str(value))
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if isinstance(value, (list, tuple)):
            return ", ".join(str(item) for item in value)
        return str(value)

    @staticmethod
    def _state_text(state: ProcessState) -> str:
        return {
            ProcessState.QUEUED: "Queued",
            ProcessState.RUNNING: "Running",
            ProcessState.CANCELLING: "Cancelling",
            ProcessState.COMPLETED: "Done",
            ProcessState.COMPLETED_WITH_WARNINGS: "Done with warnings",
            ProcessState.FAILED: "Failed",
            ProcessState.CANCELLED: "Canceled",
            ProcessState.INTERRUPTED: "Interrupted",
        }[state]

    def _selected_process_id(self) -> str:
        item = self._tree.currentItem()
        if item is not None and item.parent() is not None:
            item = item.parent()
        return str(item.data(0, QtCore.Qt.ItemDataRole.UserRole) or "") if item else ""

    def _open_context_menu(self, position: QtCore.QPoint) -> None:
        item = self._tree.itemAt(position)
        if item is None:
            return
        if item.parent() is not None:
            item = item.parent()
        self._tree.setCurrentItem(item)
        record = self._display_records.get(self._selected_process_id())
        if record is None:
            return
        is_saved = record.state in {
            ProcessState.COMPLETED,
            ProcessState.COMPLETED_WITH_WARNINGS,
        }
        menu = QtWidgets.QMenu(self._widget)
        if is_saved:
            create_from = menu.addAction("Create Thumbnails From…")
            run_again = menu.addAction("Run Again…")
            rename = menu.addAction("Rename…")
            menu.addSeparator()
            remove = menu.addAction("Remove…")
            if self.input_in_use(record.output_id):
                remove.setEnabled(False)
                remove.setToolTip("This thumbnail set is used by queued or running work.")
        elif record.state.terminal:
            retry = menu.addAction("Retry…")
            dismiss = menu.addAction("Dismiss")
        else:
            return
        selected = menu.exec(self._tree.viewport().mapToGlobal(position))
        if is_saved and selected == create_from:
            thumbnail_input = next(
                (candidate for candidate in self.available_inputs() if candidate.kind == ProcessInputKind.ARTIFACT and candidate.id == record.output_id),
                None,
            )
            self.open_create_dialog(thumbnail_input)
        elif is_saved and selected == run_again:
            current_input = next(
                (candidate for candidate in self.available_inputs() if candidate.kind == record.input.kind and candidate.id == record.input.id),
                None,
            )
            self.open_create_dialog(current_input, initial_parameters=record.parameters)
        elif is_saved and selected == rename:
            self._rename_variant(record.output_id)
        elif is_saved and selected == remove:
            self._remove_variant(record.output_id)
        elif not is_saved and selected == retry:
            current_input = next(
                (candidate for candidate in self.available_inputs() if candidate.kind == record.input.kind and candidate.id == record.input.id),
                None,
            )
            self.open_create_dialog(current_input, initial_parameters=record.parameters)
        elif not is_saved and selected == dismiss:
            self._session_records.pop(record.id, None)
            self._render_records()

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
            "Rename Thumbnails",
            "Name:",
            text=variant.label,
        )
        if not accepted or not name.strip():
            return
        try:
            self._repo.rename_variant(variant_id, name.strip())
        except Exception as exc:  # noqa: BLE001
            self._show_status(f"Could not rename thumbnails: {exc}")
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
            "Remove Thumbnails",
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
        item = QtWidgets.QTreeWidgetItem([message, "", "", ""])
        item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable & ~QtCore.Qt.ItemFlag.ItemIsEnabled)
        self._tree.addTopLevelItem(item)
        self._remove_old_items(old_items)

    def _remove_old_items(self, items: list[QtWidgets.QTreeWidgetItem]) -> None:
        for item in items:
            index = self._tree.indexOfTopLevelItem(item)
            if index >= 0:
                self._tree.takeTopLevelItem(index)
