from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal
import hashlib
import json
import math
import re
from pathlib import Path
import shutil
import uuid

from EmbeddingsArea.clustering import ClusteringMethodDescriptor
from EmbeddingsArea.clustering.service import ClusterService
from EmbeddingsArea.domain import (
    EmbeddingDataset,
    EmbeddingModelDescriptor,
    InstalledModel,
    EmbeddingRun,
    EmbeddingRunConfiguration,
    EmbeddingRunInput,
    EmbeddingRunInputBinding,
    EmbeddingRunParameterValue,
    EmbeddingRunPreparation,
    EmbeddingRunState,
    MediaContentType,
    MediaLinkPreview,
    MediaMetadataMergePreview,
    MediaMatchStrategy,
    MediaSource,
    MediaSourceKind,
    MediaSourceSummary,
    ModelCatalogEntry,
    SavedEmbeddingDataset,
)
from EmbeddingsArea.model_catalog import DiscoverableModelGroup, ModelCatalog
from EmbeddingsArea.repository import (
    EmbeddingArtifactRepository,
    EmbeddingRunRepository,
    MediaLinkRepository,
    MediaRepository,
)
from MetadataArea.repository import MetadataRepository
from EmbeddingsArea.media_library import MediaLibraryService
from EmbeddingsArea.media_linking import MediaLinkService

if TYPE_CHECKING:
    from project.domain import Project


class EmbeddingCatalogService:
    def __init__(self, model_catalog: ModelCatalog) -> None:
        self._model_catalog = model_catalog

    @property
    def model_catalog(self) -> ModelCatalog:
        return self._model_catalog

    def list_local_models(self) -> list[EmbeddingModelDescriptor]:
        return self._model_catalog.list_local_models()

    def list_installed_models(self) -> list[InstalledModel]:
        return self._model_catalog.list_installed_models()

    def list_discoverable_models(self) -> list[ModelCatalogEntry]:
        return self._model_catalog.list_discoverable_models()

    def list_discoverable_model_groups(self, *, force_refresh: bool = False) -> list[DiscoverableModelGroup]:
        return self._model_catalog.list_discoverable_model_groups(force_refresh=force_refresh)

    def install_discoverable_model(self, entry: ModelCatalogEntry, **kwargs) -> EmbeddingModelDescriptor:
        return self._model_catalog.install_discoverable_model(entry, **kwargs)

    def read_local_model_readme(self, model_id: str) -> str:
        return self._model_catalog.read_local_model_readme(model_id)

    def fetch_discoverable_model_readme(self, entry: ModelCatalogEntry) -> str:
        return self._model_catalog.fetch_discoverable_model_readme(entry)

    def fetch_discoverable_model_size(self, entry: ModelCatalogEntry) -> int | None:
        return self._model_catalog.fetch_discoverable_model_size(entry)

    def get_model(self, model_id: str) -> EmbeddingModelDescriptor | None:
        return self._model_catalog.get_model(model_id)

    def list_compatible_models(self, content_types: set[MediaContentType]) -> list[EmbeddingModelDescriptor]:
        return self._model_catalog.list_compatible_models(content_types)

    def remove_local_model(self, model_id: str) -> None:
        installed = {model.descriptor.id: model for model in self.list_installed_models()}
        target = installed.get(model_id)
        if target is None:
            raise ValueError(f"Unknown installed model: {model_id}")
        if not target.is_removable or target.descriptor.directory is None:
            raise ValueError("This installed model cannot be removed from the app yet.")
        shutil.rmtree(target.descriptor.directory, ignore_errors=True)


class EmbeddingRunService:
    def __init__(
        self,
        project: Project,
        media_library: MediaLibraryService,
        media_links: MediaLinkService,
        runs_repository: EmbeddingRunRepository,
        catalog: EmbeddingCatalogService,
        artifact_repository: EmbeddingArtifactRepository | None = None,
    ) -> None:
        self._project = project
        self._media_library = media_library
        self._media_links = media_links
        self._runs_repository = runs_repository
        self._catalog = catalog
        self._artifact_repository = artifact_repository or EmbeddingArtifactRepository()

    def list_datasets(self) -> list[EmbeddingDataset]:
        return [
            EmbeddingDataset(
                name=dataset.name,
                dimensions=dataset.dimensions,
                item_count=dataset.item_count,
            )
            for dataset in self.list_saved_datasets()
        ]

    def list_saved_datasets(self) -> list[SavedEmbeddingDataset]:
        from EmbeddingsArea.runtime.readers import load_embedding_summary

        datasets: list[SavedEmbeddingDataset] = []
        for run in self._artifact_repository.list_saved_runs(self._runs_repository):
            if not run.artifact_path:
                continue
            path = Path(run.artifact_path)
            if not path.exists():
                continue
            summary = load_embedding_summary(path)
            provenance = summary.get("provenance") or {}
            attempts = provenance.get("attempts", []) if isinstance(provenance, dict) else []
            execution_note = next((str(value["fallback_reason"]) for value in attempts if isinstance(value, dict) and value.get("fallback_reason")), "")
            datasets.append(
                SavedEmbeddingDataset(
                    execution_note=execution_note,
                    run_id=run.id,
                    name=path.stem,
                    dimensions=int(summary.get("dimensions") or 0),
                    item_count=int(summary.get("item_count") or 0),
                    artifact_path=str(path),
                    created_at=run.created_at,
                    updated_at=run.updated_at,
                )
            )
        return datasets

    def create_draft_run(self, label: str | None = None) -> EmbeddingRun:
        now = _utc_now()
        run = EmbeddingRun(
            id=f"run_{uuid.uuid4().hex}",
            label=label.strip() if label and label.strip() else "",
            state=EmbeddingRunState.DRAFT,
            created_at=now,
            updated_at=now,
        )
        self._runs_repository.upsert_run(run)
        return run

    def list_runs(self) -> list[EmbeddingRun]:
        return [self._normalize_transient_run_state(run) for run in self._runs_repository.list_runs()]

    def discard_restored_transient_runs(self, app_started_at: datetime) -> int:
        """Keep only published results and embedding attempts with usable checkpoints."""
        from EmbeddingsArea.runtime.checkpoints import CheckpointStore

        discarded = 0
        for run in self._runs_repository.list_runs():
            if not _timestamp_precedes(run.created_at, app_started_at):
                continue
            if run.state == EmbeddingRunState.COMPLETED:
                continue
            checkpoint = None
            if run.state not in {EmbeddingRunState.DRAFT, EmbeddingRunState.CANCELLED}:
                try:
                    checkpoint = CheckpointStore(
                        self._project.paths.embeddings / ".runs" / run.id
                    ).load()
                except (OSError, ValueError, TypeError):
                    checkpoint = None
            resumable = bool(
                checkpoint is not None
                and checkpoint.total_items > 0
                and 0 <= checkpoint.next_index < checkpoint.total_items
            )
            if resumable:
                if run.state in {EmbeddingRunState.QUEUED, EmbeddingRunState.RUNNING}:
                    progress = int(checkpoint.next_index / checkpoint.total_items * 100)
                    self._runs_repository.upsert_run(
                        replace(
                            run,
                            state=EmbeddingRunState.INTERRUPTED,
                            progress=max(run.progress, progress),
                            processed_items=max(run.processed_items, checkpoint.next_index),
                            updated_at=_utc_now(),
                            error_message="Previous run was interrupted. Resume when ready.",
                        )
                    )
                continue
            self._clear_run_runtime_artifacts(run.id, run.artifact_path)
            self._runs_repository.delete_run(run.id)
            discarded += 1
        return discarded

    def require_run(self, run_id: str) -> EmbeddingRun:
        run = self._runs_repository.get_run(run_id)
        if run is None:
            raise ValueError(f"Unknown embedding run: {run_id}")
        return run

    def count_run_inputs(self, run_id: str) -> int:
        self.require_run(run_id)
        return self._runs_repository.count_run_inputs(run_id)

    def list_run_inputs(self, run_id: str, *, limit: int = 200, offset: int = 0) -> list[EmbeddingRunInput]:
        self.require_run(run_id)
        return self._runs_repository.fetch_run_inputs(run_id, limit=limit, offset=offset)

    def rename_run(self, run_id: str, label: str) -> EmbeddingRun:
        run = self.require_run(run_id)
        normalized = label.strip()
        if not normalized:
            raise ValueError("Enter a name for the embedding.")
        if any(
            other.id != run_id and other.label.strip().casefold() == normalized.casefold()
            for other in self._runs_repository.list_runs()
        ):
            raise ValueError("An embedding output with this name already exists.")
        updated = replace(run, label=normalized, updated_at=_utc_now())
        self._runs_repository.upsert_run(updated)
        return updated

    def update_run_configuration(
        self,
        run_id: str,
        *,
        model_id: str | None = None,
        source_ids: tuple[str, ...] | None = None,
        input_bindings: dict[str, str] | None = None,
        parameter_values: dict[tuple[str, str], str] | None = None,
    ) -> EmbeddingRun:
        run = self.require_run(run_id)
        active_model_id = run.selected_model_id if model_id is None else model_id.strip()
        active_source_ids = run.selected_source_ids if source_ids is None else tuple(
            source_id.strip() for source_id in source_ids if source_id.strip()
        )
        normalized_bindings = self._normalized_input_bindings(input_bindings)
        if input_bindings is not None:
            active_source_ids = tuple(source_id for source_id in normalized_bindings.values() if source_id)
        sources = [self._media_library.analysis_source(source_id) for source_id in active_source_ids]
        model = None
        if active_model_id:
            model = self._catalog.get_model(active_model_id)
            if model is None:
                raise ValueError(f"Unknown embedding model: {active_model_id}")
            if normalized_bindings:
                active_source_ids = self._ordered_source_ids(model, normalized_bindings)
                sources = [self._media_library.analysis_source(source_id) for source_id in active_source_ids]
                self._validate_required_bindings(model, normalized_bindings)
                requirements = {value.id: value for value in model.input_requirements}
                for input_id, source_id in normalized_bindings.items():
                    requirement = requirements.get(input_id)
                    if requirement is None:
                        raise ValueError(f"Unknown model input: {input_id}.")
                    if self._media_library.analysis_source(source_id).content_type != requirement.modality:
                        raise ValueError(f"Select a {requirement.modality.value} source for {requirement.label}.")
            source_types = {source.content_type for source in sources}
            if source_types and not source_types.issubset(set(model.compatible_content_types)):
                readable = ", ".join(sorted(content_type.value for content_type in source_types))
                raise ValueError(f"Model {model.label} is not compatible with {readable} source data.")
        normalized_parameter_values = self._normalized_parameter_values(parameter_values)
        if normalized_parameter_values:
            if model is None:
                raise ValueError("Select a model before configuring parameters.")
            self._validate_parameter_values(model, normalized_parameter_values)
        reset_run = (
            (model_id is not None and active_model_id != run.selected_model_id)
            or (source_ids is not None and active_source_ids != run.selected_source_ids)
            or input_bindings is not None
            or parameter_values is not None
        )
        model_changed = model_id is not None and active_model_id != run.selected_model_id
        active_label = run.label
        if model_changed and self.is_generated_run_label(active_label):
            active_label = (
                self._next_run_label(active_model_id, exclude_run_id=run_id)
                if active_model_id
                else ""
            )
        updated = replace(
            run,
            label=active_label,
            selected_model_id=active_model_id,
            selected_source_ids=active_source_ids,
            updated_at=_utc_now(),
            state=EmbeddingRunState.DRAFT if reset_run else run.state,
            progress=0 if reset_run else run.progress,
            prepared_revision=0 if reset_run else run.prepared_revision,
            prepared_input_count=0 if reset_run else run.prepared_input_count,
            processed_items=0 if reset_run else run.processed_items,
            failed_items=0 if reset_run else run.failed_items,
            last_chunk_at="" if reset_run else run.last_chunk_at,
            error_message="" if reset_run else run.error_message,
            artifact_path="" if reset_run else run.artifact_path,
            prepared_signature="" if reset_run else run.prepared_signature,
        )
        self._runs_repository.upsert_run(updated)
        if input_bindings is not None:
            self._runs_repository.replace_run_input_bindings(
                updated.id,
                [EmbeddingRunInputBinding(input_id=input_id, source_id=source_id) for input_id, source_id in normalized_bindings.items()],
            )
        if parameter_values is not None:
            self._runs_repository.replace_run_parameter_values(
                updated.id,
                [
                    EmbeddingRunParameterValue(scope=scope, parameter_id=parameter_id, value=value)
                    for (scope, parameter_id), value in normalized_parameter_values.items()
                ],
            )
        return updated

    def get_run_configuration(self, run_id: str) -> EmbeddingRunConfiguration:
        run = self.require_run(run_id)
        return EmbeddingRunConfiguration(
            run=run,
            input_bindings=tuple(self._runs_repository.list_run_input_bindings(run_id)),
            parameter_values=tuple(self._runs_repository.list_run_parameter_values(run_id)),
        )

    def prepare_run(self, run_id: str) -> EmbeddingRunPreparation:
        run = self.require_run(run_id)
        if not run.selected_model_id:
            raise ValueError("Choose a model before preparing the embedding run.")
        if not run.selected_source_ids:
            raise ValueError("Choose a media source before preparing the embedding run.")
        refreshed = self.update_run_configuration(
            run.id,
            model_id=run.selected_model_id,
            source_ids=run.selected_source_ids,
        )
        configuration = self.get_run_configuration(refreshed.id)
        sources = {source_id: self._media_library.require_source(source_id)
                   for source_id in refreshed.selected_source_ids}
        for source in sources.values():
            if source.source_kind == MediaSourceKind.METADATA_COLUMN:
                self._media_library.scan_source(source.id)
        pairs = [
            (asset, link)
            for asset, link in self._media_links.assets_with_optional_links(refreshed.selected_source_ids)
            if sources[asset.source_id].source_kind == MediaSourceKind.METADATA_COLUMN
            or link is not None
        ]
        populated = {asset.source_id for asset, _link in pairs}
        if any(source_id not in populated for source_id in sources):
            raise ValueError(
                "Each selected asset source must contain linked items. "
                "Link assets or choose a nonempty metadata column."
            )
        source_revisions = {source_id: self._media_library.require_source(source_id).source_revision
                            for source_id in sources}
        inputs = []
        for ordinal, (asset, link) in enumerate(pairs):
            if sources[asset.source_id].source_kind == MediaSourceKind.METADATA_COLUMN:
                item_id = int(asset.source_key)
            else:
                item_id = link.metadata_row_id
            inputs.append(EmbeddingRunInput(
                run_id=refreshed.id,
                ordinal=ordinal,
                media_item_id=asset.id,
                source_id=asset.source_id,
                source_key=asset.source_key,
                absolute_path=asset.absolute_path,
                content_type=asset.content_type,
                metadata_row_id=item_id,
                size_bytes=asset.size_bytes,
                mtime_ns=asset.mtime_ns,
                link_revision=source_revisions[asset.source_id],
            ))
        model = self._catalog.get_model(refreshed.selected_model_id)
        if model.runtime_contract.get("components"):
            from EmbeddingsArea.input_preparation import pair_model_inputs

            inputs = pair_model_inputs(inputs, {
                source_id: source.content_type.value for source_id, source in sources.items()
            })
        queued = replace(
            refreshed,
            state=EmbeddingRunState.QUEUED,
            progress=0,
            prepared_revision=refreshed.prepared_revision + 1,
            prepared_input_count=len(inputs),
            updated_at=_utc_now(),
            error_message="",
            prepared_signature="",
        )
        snapshot = self._snapshot_payload(queued, configuration, inputs)
        signature = self._snapshot_signature(snapshot)
        self._clear_run_runtime_artifacts(run.id, run.artifact_path)
        from shared.persistence.atomic_write import write_text_atomic
        snapshot_path = self._project.paths.embeddings / ".runs" / run.id / "prepared.json"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(snapshot_path, json.dumps(snapshot, sort_keys=True, default=str))
        queued = replace(queued, prepared_signature=signature)
        self._runs_repository.save_prepared_run(queued, inputs)
        return EmbeddingRunPreparation(run=queued, input_count=len(inputs))

    def run_embedding_run(
        self,
        run_id: str,
        chunk_size: int | None = None,
        control_callback: Callable[[], Literal["continue", "pause", "cancel"]] | None = None,
        progress_callback: Callable[[int], None] | None = None,
        status_callback: Callable[[str], None] | None = None,
        event_callback=None,
    ) -> EmbeddingRun:
        run = self.require_run(run_id)
        if run.state == EmbeddingRunState.DRAFT or not run.prepared_signature:
            try:
                run = self.prepare_run(run.id).run
            except Exception as exc:
                current = self._runs_repository.get_run(run.id) or run
                self._runs_repository.upsert_run(
                    replace(
                        current,
                        state=EmbeddingRunState.DRAFT,
                        updated_at=_utc_now(),
                        error_message=str(exc),
                    )
                )
                raise
        if run.state not in {EmbeddingRunState.QUEUED, EmbeddingRunState.PAUSED, EmbeddingRunState.INTERRUPTED, EmbeddingRunState.FAILED}:
            raise ValueError(
                f"Embedding run must be queued, paused, or interrupted before execution, not {run.state.value}."
            )
        model = self._catalog.get_model(run.selected_model_id)
        if model is None:
            raise ValueError(f"Unknown embedding model: {run.selected_model_id}")
        if model.package_status != "ready" or (
            model.backend.strip().casefold() == "onnx" and model.package_schema_version != 3
        ):
            raise ValueError("This model package requires repackaging with the EIDORA schema v3 contract.")
        if model.package_schema_version == 3:
            from EmbeddingsArea.model_package import load_model_package, validate_package_files

            if model.config_path is None or model.directory is None:
                raise ValueError("The model package has no readable package directory.")
            validate_package_files(model.directory, load_model_package(model.config_path))
        configuration = self.get_run_configuration(run.id)
        snapshot_inputs = self._runs_repository.fetch_run_inputs(run.id, limit=self._runs_repository.count_run_inputs(run.id), offset=0)
        if not snapshot_inputs:
            raise ValueError("The embedding run has no prepared inputs.")
        effective_chunk_size = _adaptive_embedding_chunk_size(len(snapshot_inputs), chunk_size)
        batch_size = int(next((value.value for value in configuration.parameter_values if value.scope == "run" and value.parameter_id == "batch_size"), "32"))
        if not 1 <= batch_size <= 512:
            raise ValueError("Inference batch size must be between 1 and 512.")
        if model.runtime_contract and any(
            list(item.get("shape", []))[:1] == [1]
            for item in [*model.runtime_contract.get("inputs", []), model.runtime_contract.get("output", {})]
            if isinstance(item, dict)
        ):
            batch_size = 1
        requested_device = next(
            (
                value.value.strip().casefold()
                for value in configuration.parameter_values
                if value.scope == "run" and value.parameter_id == "device"
            ),
            "auto",
        )
        if requested_device not in {"auto", "cpu", "cuda", "coreml"}:
            raise ValueError("Execution device must be Auto, CPU, CUDA, or CoreML.")
        current_snapshot = self._snapshot_payload(run, configuration, snapshot_inputs)
        computed_signature = self._snapshot_signature(current_snapshot)
        if computed_signature != run.prepared_signature:
            raise ValueError("Prepared run snapshot mismatch. Reconfigure or re-prepare this run before execution.")
        if model.runtime_contract:
            snapshot_path = self._project.paths.embeddings / ".runs" / run.id / "prepared.json"
            saved_snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            if self._snapshot_signature(saved_snapshot) != run.prepared_signature:
                raise ValueError("Saved preparation contract does not match this run.")
            effective_contract = saved_snapshot["effective_contract"]
        else:
            effective_contract = {}
        from EmbeddingsArea.runtime.checkpoints import CheckpointStore
        from EmbeddingsArea.runtime.contracts import RunCancelled, RunPaused, RuntimeConfig
        from EmbeddingsArea.runtime.pipeline import EmbeddingPipeline

        running = replace(
            run,
            state=EmbeddingRunState.RUNNING,
            progress=max(0, int(run.progress)),
            updated_at=_utc_now(),
            error_message="",
        )
        self._runs_repository.upsert_run(running)
        if progress_callback is not None:
            progress_callback(running.progress)

        total_inputs = max(1, len(snapshot_inputs))

        def _record_progress(progress: int) -> None:
            checkpoint = CheckpointStore(self._project.paths.embeddings / ".runs" / running.id).load()
            processed = checkpoint.next_index if checkpoint is not None else 0
            current = self._runs_repository.get_run(running.id) or running
            self._runs_repository.upsert_run(
                replace(
                    current,
                    state=EmbeddingRunState.RUNNING,
                    progress=progress,
                    processed_items=processed,
                    last_chunk_at=_utc_now(),
                    updated_at=_utc_now(),
                    error_message="",
                )
            )
            if progress_callback is not None:
                progress_callback(progress)

        def _control_action() -> Literal["continue", "pause", "cancel"]:
            if control_callback is not None:
                requested = control_callback()
                if requested in {"pause", "cancel"}:
                    return requested
            current = self._runs_repository.get_run(running.id)
            if current is None:
                return "cancel"
            if current.state == EmbeddingRunState.CANCELLED:
                return "cancel"
            if current.state == EmbeddingRunState.PAUSED:
                return "pause"
            return "continue"

        preprocess_values = {
            parameter.id: parameter.default
            for parameter in model.preprocess_parameters
            if parameter.default
        }
        for value in configuration.parameter_values:
            if value.scope == "preprocess" and value.parameter_id:
                preprocess_values[value.parameter_id] = value.value

        def heartbeat():
            current = self._runs_repository.get_run(running.id)
            if current is not None:
                self._runs_repository.upsert_run(replace(current, updated_at=_utc_now()))

        runner = EmbeddingPipeline().run
        runner_options = {}
        if model.runtime_adapter_id.startswith("onnx"):
            from EmbeddingsArea.runtime.process_runner import run_isolated
            runner = run_isolated
            runner_options["heartbeat_callback"] = heartbeat
        try:
            result = runner(
                **runner_options,
                run_id=running.id,
                model_id=model.id,
                model_label=model.label,
                inputs=self._runtime_inputs_for_run(run.id),
                run_dir=self._project.paths.embeddings / ".runs" / running.id,
                output_path=self._project.paths.embeddings / f"{running.id}.npz",
                config=RuntimeConfig(chunk_size=effective_chunk_size, batch_size=batch_size),
                progress_callback=_record_progress,
                status_callback=status_callback,
                event_callback=event_callback,
                prepared_signature=run.prepared_signature,
                control_callback=_control_action,
                runtime_adapter_id=model.runtime_adapter_id or "fake",
                model_path=model.model_path,
                tokenizer_path=model.tokenizer_path or model.directory,
                input_names=model.runtime_input_names,
                output_name=model.runtime_output_name,
                preprocess_values=preprocess_values,
                preprocessing_contract=model.preprocessing_contract,
                runtime_contract={
                    **effective_contract, "package_fingerprint": model.package_fingerprint,
                    **({"selected_modalities": [part.content_type.value for part in snapshot_inputs[0].components]}
                       if model.runtime_contract.get("components") else {}),
                },
                package_directory=model.directory,
                requested_device=requested_device,
            )
        except RunPaused:
            current = self._runs_repository.get_run(running.id) or running
            paused = replace(
                current,
                state=EmbeddingRunState.PAUSED,
                updated_at=_utc_now(),
                error_message="",
            )
            self._runs_repository.upsert_run(paused)
            return paused
        except RunCancelled:
            self._clear_run_runtime_artifacts(running.id, "")
            current = self._runs_repository.get_run(running.id) or running
            cancelled = replace(
                current,
                state=EmbeddingRunState.CANCELLED,
                updated_at=_utc_now(),
                error_message="",
            )
            self._runs_repository.upsert_run(cancelled)
            return cancelled
        except Exception as exc:
            current = self._runs_repository.get_run(running.id) or running
            failed = replace(
                current,
                state=EmbeddingRunState.FAILED,
                updated_at=_utc_now(),
                error_message=str(exc),
            )
            self._runs_repository.upsert_run(failed)
            raise
        completed = replace(
            running,
            state=EmbeddingRunState.COMPLETED,
            progress=100,
            processed_items=result.processed_count,
            failed_items=max(0, len(snapshot_inputs) - result.processed_count),
            last_chunk_at=_utc_now(),
            updated_at=_utc_now(),
            artifact_path=str(result.artifact_path),
            error_message="",
        )
        self._runs_repository.upsert_run(completed)
        return completed

    def pause_run(self, run_id: str) -> EmbeddingRun:
        run = self.require_run(run_id)
        if run.state in {EmbeddingRunState.COMPLETED, EmbeddingRunState.FAILED, EmbeddingRunState.CANCELLED}:
            raise ValueError(f"Cannot pause embedding run in {run.state.value} state.")
        if run.state == EmbeddingRunState.PAUSED:
            return run
        if run.state not in {EmbeddingRunState.QUEUED, EmbeddingRunState.RUNNING}:
            raise ValueError(f"Embedding run must be queued or running before pause, not {run.state.value}.")
        paused = replace(run, state=EmbeddingRunState.PAUSED, updated_at=_utc_now(), error_message="")
        self._runs_repository.upsert_run(paused)
        return paused

    def cancel_run(self, run_id: str) -> EmbeddingRun:
        run = self.require_run(run_id)
        if run.state in {EmbeddingRunState.COMPLETED, EmbeddingRunState.FAILED, EmbeddingRunState.CANCELLED}:
            return run
        if run.state not in {
            EmbeddingRunState.QUEUED,
            EmbeddingRunState.RUNNING,
            EmbeddingRunState.PAUSED,
            EmbeddingRunState.INTERRUPTED,
        }:
            raise ValueError(f"Embedding run must be started before aborting, not {run.state.value}.")
        cancelled = replace(run, state=EmbeddingRunState.CANCELLED, updated_at=_utc_now(), error_message="")
        self._runs_repository.upsert_run(cancelled)
        return cancelled

    def reset_run(self, run_id: str) -> EmbeddingRun:
        run = self.require_run(run_id)
        if run.state in {EmbeddingRunState.RUNNING, EmbeddingRunState.QUEUED, EmbeddingRunState.PAUSED}:
            raise ValueError(f"Cannot reset embedding run in {run.state.value} state.")
        self._clear_run_runtime_artifacts(run.id, run.artifact_path)
        reset = replace(
            run,
            state=EmbeddingRunState.DRAFT,
            progress=0,
            prepared_revision=0,
            prepared_input_count=0,
            processed_items=0,
            failed_items=0,
            last_chunk_at="",
            updated_at=_utc_now(),
            error_message="",
            artifact_path="",
            prepared_signature="",
        )
        self._runs_repository.upsert_run(reset)
        self._runs_repository.replace_run_inputs(run.id, [])
        return reset

    def delete_run(self, run_id: str) -> None:
        run = self.require_run(run_id)
        if run.state == EmbeddingRunState.RUNNING:
            raise ValueError(f"Cannot delete embedding run in {run.state.value} state.")
        self._clear_run_runtime_artifacts(run.id, run.artifact_path)
        self._runs_repository.delete_run(run.id)

    def inspect_run_checkpoint(self, run_id: str) -> dict[str, int | bool]:
        from EmbeddingsArea.runtime.checkpoints import CheckpointStore

        self.require_run(run_id)
        checkpoint = CheckpointStore(self._project.paths.embeddings / ".runs" / run_id).load()
        if checkpoint is None:
            return {"resumable": False, "next_index": 0, "total_items": 0, "completed_chunks": 0}
        resumable = checkpoint.total_items > 0 and 0 <= checkpoint.next_index < checkpoint.total_items
        return {
            "resumable": resumable,
            "next_index": int(checkpoint.next_index),
            "total_items": int(checkpoint.total_items),
            "completed_chunks": int(checkpoint.completed_chunks),
        }

    def get_saved_embedding_summary(self, run_id: str) -> dict[str, Any]:
        from EmbeddingsArea.runtime.readers import load_embedding_summary

        run = self.require_run(run_id)
        if run.state != EmbeddingRunState.COMPLETED:
            raise ValueError(f"Run {run.label} is not completed.")
        if not run.artifact_path:
            raise ValueError(f"Run {run.label} does not have a saved embedding file.")
        path = Path(run.artifact_path)
        if not path.exists():
            raise FileNotFoundError(path)
        summary = load_embedding_summary(path)
        saved_parameters = self._runs_repository.list_run_parameter_values(run.id)
        summary.update(
            {
                "run_id": run.id,
                "run_label": run.label,
                "run_state": run.state.value,
                "artifact_path": str(path),
                "created_at": run.created_at,
                "updated_at": run.updated_at,
                "error_message": run.error_message,
                "selected_model_id": run.selected_model_id,
                "selected_source_ids": list(run.selected_source_ids),
                "prepared_input_count": run.prepared_input_count,
                "parameter_values": {
                    scope: {value.parameter_id: value.value for value in saved_parameters if value.scope == scope}
                    for scope in ("run", "preprocess", "postprocess")
                },
                "preprocess_parameters": {
                    value.parameter_id: value.value
                    for value in saved_parameters
                    if value.scope == "preprocess"
                },
            }
        )
        return summary

    def _runtime_inputs_for_run(self, run_id: str):
        from EmbeddingsArea.runtime.contracts import RuntimeInput

        total = self._runs_repository.count_run_inputs(run_id)
        inputs = []
        offset = 0
        page_size = 500
        while offset < total:
            page = self._runs_repository.fetch_run_inputs(run_id, limit=page_size, offset=offset)
            inputs.extend(
                RuntimeInput(
                    ordinal=item.ordinal,
                    media_item_id=item.media_item_id,
                    identifier=item.source_key,
                    path=item.absolute_path,
                    content_type=item.content_type.value,
                    components={
                        part.content_type.value: RuntimeInput(
                            ordinal=item.ordinal, media_item_id=part.media_item_id,
                            identifier=part.source_key, path=part.absolute_path,
                            content_type=part.content_type.value,
                        ) for part in item.components
                    },
                )
                for item in page
            )
            offset += len(page)
            if not page:
                break
        return inputs

    def _normalize_transient_run_state(self, run: EmbeddingRun) -> EmbeddingRun:
        if run.state != EmbeddingRunState.RUNNING:
            return run
        if not _is_stale_running_run(run.updated_at):
            return run
        from EmbeddingsArea.runtime.checkpoints import CheckpointStore

        checkpoint = CheckpointStore(self._project.paths.embeddings / ".runs" / run.id).load()
        if checkpoint is None or checkpoint.total_items <= 0:
            interrupted = replace(
                run,
                state=EmbeddingRunState.INTERRUPTED,
                updated_at=_utc_now(),
                error_message="Previous run was interrupted. Re-run to resume.",
            )
            self._runs_repository.upsert_run(interrupted)
            return interrupted
        resumed_progress = int((checkpoint.next_index / checkpoint.total_items) * 100)
        interrupted = replace(
            run,
            state=EmbeddingRunState.INTERRUPTED,
            progress=max(run.progress, resumed_progress),
            processed_items=max(run.processed_items, checkpoint.next_index),
            updated_at=_utc_now(),
            error_message="Previous run was interrupted. Re-run to resume.",
        )
        self._runs_repository.upsert_run(interrupted)
        return interrupted

    def _clear_run_runtime_artifacts(self, run_id: str, artifact_path: str) -> None:
        run_dir = self._project.paths.embeddings / ".runs" / run_id
        if run_dir.exists():
            shutil.rmtree(run_dir, ignore_errors=True)
        path = Path(artifact_path).expanduser() if artifact_path else self._project.paths.embeddings / f"{run_id}.npz"
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass

    def _snapshot_payload(
        self,
        run: EmbeddingRun,
        configuration: EmbeddingRunConfiguration,
        inputs: list[EmbeddingRunInput],
    ) -> dict[str, object]:
        model = self._catalog.get_model(run.selected_model_id) if run.selected_model_id else None
        effective = {}
        if model is not None and model.runtime_contract:
            from EmbeddingsArea.preparation_contract import resolve_contract
            effective = resolve_contract(model.runtime_contract, {
                value.parameter_id: value.value for value in configuration.parameter_values if value.scope == "preprocess"
            })
        return {
            "effective_contract": effective,
            "run_id": run.id,
            "prepared_revision": run.prepared_revision,
            "model_id": run.selected_model_id,
            "model_package_fingerprint": model.package_fingerprint if model is not None else "",
            "selected_source_ids": list(run.selected_source_ids),
            "input_bindings": sorted(
                [{"input_id": binding.input_id, "source_id": binding.source_id} for binding in configuration.input_bindings],
                key=lambda item: item["input_id"],
            ),
            "parameter_values": sorted(
                [{"scope": value.scope, "parameter_id": value.parameter_id, "value": value.value} for value in configuration.parameter_values],
                key=lambda item: (item["scope"], item["parameter_id"]),
            ),
            "inputs": [
                {
                    "ordinal": item.ordinal,
                    "media_asset_id": item.media_item_id,
                    "source_id": item.source_id,
                    "source_key": item.source_key,
                    "content_type": item.content_type.value,
                    "metadata_row_id": item.metadata_row_id,
                    "size_bytes": item.size_bytes,
                    "mtime_ns": item.mtime_ns,
                    "link_revision": item.link_revision,
                    "path": str(item.absolute_path),
                    **({"components": [asdict(part) for part in item.components]} if item.components else {}),
                }
                for item in sorted(inputs, key=lambda item: item.ordinal)
            ],
        }

    @staticmethod
    def _snapshot_signature(payload: dict[str, object]) -> str:
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def next_default_run_label(self, model_id: str | None = None, *, exclude_run_id: str | None = None) -> str:
        return self._next_run_label(model_id=model_id, exclude_run_id=exclude_run_id)

    def is_generated_run_label(self, label: str) -> bool:
        text = str(label or "").strip()
        if not text:
            return True
        bases = {
            self._default_embedding_label_base(model.id)
            for model in self._catalog.list_local_models()
        }
        bases.add("Embeddings")
        return any(
            re.fullmatch(rf"{re.escape(base)}(?: \d+)?", text, flags=re.IGNORECASE)
            for base in bases
        )

    def _next_run_label(self, model_id: str | None = None, *, exclude_run_id: str | None = None) -> str:
        base_name = self._default_embedding_label_base(model_id)
        excluded = str(exclude_run_id or "")
        existing_names = {
            run.label.strip().casefold()
            for run in self._runs_repository.list_runs()
            if run.id != excluded
        }
        if base_name.casefold() not in existing_names:
            return base_name
        index = 2
        while f"{base_name} {index}".casefold() in existing_names:
            index += 1
        return f"{base_name} {index}"

    def _default_embedding_label_base(self, model_id: str | None = None) -> str:
        model = self._catalog.get_model(model_id or "") if model_id else None
        if model is None:
            models = self._catalog.list_local_models()
            model = models[0] if models else None
        label = _simple_embedding_model_label(model.label) if model is not None else ""
        if not label:
            return "Embeddings"
        if label.casefold().endswith("embeddings"):
            return label
        return f"{label} Embeddings"

    @staticmethod
    def _normalized_input_bindings(bindings: dict[str, str] | None) -> dict[str, str]:
        if not bindings:
            return {}
        return {
            str(input_id).strip(): str(source_id).strip()
            for input_id, source_id in bindings.items()
            if str(input_id).strip() and str(source_id).strip()
        }

    @staticmethod
    def _normalized_parameter_values(values: dict[tuple[str, str], str] | None) -> dict[tuple[str, str], str]:
        if not values:
            return {}
        normalized: dict[tuple[str, str], str] = {}
        for key, value in values.items():
            scope, parameter_id = key
            normalized_scope = str(scope).strip().lower()
            normalized_id = str(parameter_id).strip()
            if not normalized_scope or not normalized_id:
                continue
            normalized[(normalized_scope, normalized_id)] = str(value) if normalized_id.split(".")[-1] in {"prefix", "suffix"} else str(value).strip()
        return normalized

    @staticmethod
    def _ordered_source_ids(model: EmbeddingModelDescriptor, bindings: dict[str, str]) -> tuple[str, ...]:
        ordered: list[str] = []
        for requirement in model.input_requirements:
            source_id = bindings.get(requirement.id, "")
            if source_id:
                ordered.append(source_id)
        for source_id in bindings.values():
            if source_id and source_id not in ordered:
                ordered.append(source_id)
        return tuple(ordered)

    @staticmethod
    def _validate_required_bindings(model: EmbeddingModelDescriptor, bindings: dict[str, str]) -> None:
        missing = [
            requirement.label
            for requirement in model.input_requirements
            if requirement.required and requirement.source_kind == "media_source" and not bindings.get(requirement.id)
        ]
        if missing:
            raise ValueError(f"Select media sources for required inputs: {', '.join(missing)}.")

    @staticmethod
    def _validate_parameter_values(
        model: EmbeddingModelDescriptor,
        values: dict[tuple[str, str], str],
    ) -> None:
        if model.runtime_contract:
            from EmbeddingsArea.preparation_contract import resolve_contract
            resolve_contract(model.runtime_contract, {key[1]: value for key, value in values.items() if key[0] == "preprocess"})
        known: dict[tuple[str, str], tuple[str, ...]] = {}
        for parameter in model.preprocess_parameters:
            known[("preprocess", parameter.id)] = parameter.choices
        for parameter in model.postprocess_parameters:
            known[("postprocess", parameter.id)] = parameter.choices
        for key, value in values.items():
            if key[0] == "run":
                continue
            if key not in known:
                raise ValueError(f"Unknown model parameter: {key[0]}:{key[1]}")
            choices = known[key]
            if choices and value and value not in choices:
                raise ValueError(f"Unsupported value '{value}' for parameter {key[1]}.")


class EmbeddingDatasetService:
    def __init__(
        self,
        media_library: MediaLibraryService,
        runs: EmbeddingRunService,
    ) -> None:
        self._media_library = media_library
        self._runs = runs

    def list_datasets(self) -> list[EmbeddingDataset]:
        datasets: list[EmbeddingDataset] = []
        for saved in self._runs.list_saved_datasets():
            run = self._runs.require_run(saved.run_id)
            source_labels = []
            for source_id in run.selected_source_ids:
                try:
                    source_labels.append(self._media_library.require_source(source_id).label)
                except ValueError:
                    source_labels.append(source_id)
            datasets.append(
                EmbeddingDataset(
                    execution_note=saved.execution_note,
                    run_id=saved.run_id,
                    name=run.label,
                    dimensions=saved.dimensions,
                    item_count=saved.item_count,
                    artifact_path=saved.artifact_path,
                    model_id=run.selected_model_id,
                    source_labels=tuple(source_labels),
                    created_at=saved.created_at,
                    updated_at=saved.updated_at,
                )
            )
        return datasets

    def get_dataset_summary(self, run_id: str) -> dict[str, Any]:
        summary = self._runs.get_saved_embedding_summary(run_id)
        run = self._runs.require_run(run_id)
        source_labels = []
        for source_id in run.selected_source_ids:
            try:
                source_labels.append(self._media_library.require_source(source_id).label)
            except ValueError:
                source_labels.append(source_id)
        summary["source_labels"] = source_labels
        return summary

    def rename_dataset(self, run_id: str, label: str) -> EmbeddingRun:
        return self._runs.rename_run(run_id, label)

    def delete_dataset(self, run_id: str) -> None:
        self._runs.delete_run(run_id)


class EmbeddingsService:
    """Backward-compatible facade exposing the split EmbeddingsArea services."""

    def __init__(
        self,
        project: Project,
        media_repository: MediaRepository | None = None,
        runs_repository: EmbeddingRunRepository | None = None,
        metadata_repository: MetadataRepository | None = None,
        model_catalog: ModelCatalog | None = None,
        link_repository: MediaLinkRepository | None = None,
    ) -> None:
        self._project = project
        self._media_repository = media_repository or MediaRepository(project.paths.media / "media_index.db")
        self._runs_repository = runs_repository or EmbeddingRunRepository(project.paths.embeddings / "embeddings.db")
        self._metadata_repository = metadata_repository or MetadataRepository(project.paths.metadata_database, project.paths.metadata_columns)
        self._model_catalog = model_catalog or ModelCatalog()
        self._link_repository = link_repository or MediaLinkRepository(project.paths.media / "media_index.db")
        self.media_library = MediaLibraryService(project, self._media_repository, self._metadata_repository, self._link_repository)
        self.media_links = MediaLinkService(self._media_repository, self._metadata_repository, self._link_repository)
        self.catalog = EmbeddingCatalogService(self._model_catalog)
        self.runs = EmbeddingRunService(project, self.media_library, self.media_links, self._runs_repository, self.catalog)
        self.datasets = EmbeddingDatasetService(self.media_library, self.runs)
        self.clusters = ClusterService(
            project,
            self.datasets,
            self._runs_repository,
            self._metadata_repository,
            model_catalog=self._model_catalog,
        )


    @property
    def exchange(self):
        from EmbeddingsArea.exchange.service import EmbeddingExchangeService
        return EmbeddingExchangeService(self._project, self._runs_repository, self._metadata_repository)

    @property
    def media_repository(self) -> MediaRepository:
        return self._media_repository

    @property
    def runs_repository(self) -> EmbeddingRunRepository:
        return self._runs_repository

    @property
    def model_catalog(self) -> ModelCatalog:
        return self._model_catalog

    def register_media_source(
        self,
        path: Path | str,
        content_type: MediaContentType | str | None = None,
        *,
        recursive: bool | None = None,
    ) -> MediaSource:
        return self.media_library.register_source(path, content_type, recursive=recursive)

    def register_metadata_text_source(self, metadata_column: str) -> MediaSource:
        return self.media_library.register_metadata_text_source(metadata_column)

    def scan_media_source(self, source_id: str) -> MediaSourceSummary:
        return self.media_library.scan_source(source_id)

    def list_embedding_input_sources(self) -> list[MediaSourceSummary]:
        return self.media_library.list_analysis_sources()

    def list_media_source_summaries(self) -> list[MediaSourceSummary]:
        return self.media_library.list_source_summaries()

    def list_metadata_columns(self) -> list[str]:
        return self.media_links.list_metadata_columns()

    def preview_match(
        self,
        source_id: str,
        metadata_column: str,
        strategy: MediaMatchStrategy = MediaMatchStrategy.IDENTIFIER_EQUALS,
    ) -> MediaLinkPreview:
        return self.media_links.preview_links(source_id, metadata_column, strategy=strategy)

    def apply_match(
        self,
        source_id: str,
        metadata_column: str,
        strategy: MediaMatchStrategy = MediaMatchStrategy.IDENTIFIER_EQUALS,
    ) -> MediaLinkPreview:
        return self.media_links.apply_links(source_id, metadata_column, strategy=strategy)

    def create_missing_metadata_rows(
        self,
        source_id: str,
        metadata_column: str,
        strategy: MediaMatchStrategy = MediaMatchStrategy.IDENTIFIER_EQUALS,
    ) -> int:
        return self.media_links.create_missing_metadata_rows(source_id, metadata_column, strategy=strategy)

    def bootstrap_filename_metadata(self, source_id: str) -> MediaLinkPreview:
        return self.media_links.bootstrap_filename_metadata(source_id)

    def list_datasets(self) -> list[EmbeddingDataset]:
        return self.datasets.list_datasets()

    def list_installed_models(self) -> list[InstalledModel]:
        return self.catalog.list_installed_models()

    def list_discoverable_models(self) -> list[ModelCatalogEntry]:
        return self.catalog.list_discoverable_models()

    def remove_local_model(self, model_id: str) -> None:
        self.catalog.remove_local_model(model_id)

    def install_discoverable_model(self, entry: ModelCatalogEntry, **kwargs) -> EmbeddingModelDescriptor:
        return self.catalog.install_discoverable_model(entry, **kwargs)

    def read_local_model_readme(self, model_id: str) -> str:
        return self.catalog.read_local_model_readme(model_id)

    def fetch_discoverable_model_readme(self, entry: ModelCatalogEntry) -> str:
        return self.catalog.fetch_discoverable_model_readme(entry)

    def create_draft_embedding_run(self, label: str | None = None) -> EmbeddingRun:
        return self.runs.create_draft_run(label)

    def next_default_embedding_run_label(self, model_id: str | None = None, *, exclude_run_id: str | None = None) -> str:
        return self.runs.next_default_run_label(model_id, exclude_run_id=exclude_run_id)

    def is_generated_embedding_run_label(self, label: str) -> bool:
        return self.runs.is_generated_run_label(label)

    def list_embedding_runs(self) -> list[EmbeddingRun]:
        return self.runs.list_runs()

    def count_embedding_run_inputs(self, run_id: str) -> int:
        return self.runs.count_run_inputs(run_id)

    def list_embedding_run_inputs(self, run_id: str, *, limit: int = 200, offset: int = 0) -> list[EmbeddingRunInput]:
        return self.runs.list_run_inputs(run_id, limit=limit, offset=offset)

    def list_local_models(self) -> list[EmbeddingModelDescriptor]:
        return self.catalog.list_local_models()

    def get_saved_embedding_summary(self, run_id: str) -> dict[str, Any]:
        summary = self.datasets.get_dataset_summary(run_id)
        summary["clusters"] = self.clusters.list_cluster_results(run_id)
        return summary

    def list_cluster_results(self, run_id: str) -> list[dict[str, object]]:
        return self.clusters.list_cluster_results(run_id)

    def rename_cluster_result(self, run_id: str, cluster_id: str, label: str) -> dict[str, object]:
        return self.clusters.rename_cluster_result(run_id, cluster_id, label)

    def list_clustering_methods(self) -> list[ClusteringMethodDescriptor]:
        return self.clusters.list_methods()

    def create_clusters(
        self,
        run_id: str,
        method_id: str,
        label: str,
        parameters: dict[str, object],
        progress_callback: Callable[[int], None] | None = None,
    ) -> dict[str, object]:
        return self.clusters.create_clusters(
            run_id,
            method_id=method_id,
            label=label,
            parameters=parameters,
            progress_callback=progress_callback,
        )

    def create_kmeans_clusters(self, run_id: str, *, label: str, cluster_count: int) -> dict[str, object]:
        return self.clusters.create_kmeans_clusters(run_id, label=label, cluster_count=cluster_count)

    def rename_embedding_run(self, run_id: str, label: str) -> EmbeddingRun:
        return self.datasets.rename_dataset(run_id, label)

    def update_embedding_run_configuration(
        self,
        run_id: str,
        *,
        model_id: str | None = None,
        source_ids: tuple[str, ...] | None = None,
        input_bindings: dict[str, str] | None = None,
        parameter_values: dict[tuple[str, str], str] | None = None,
    ) -> EmbeddingRun:
        return self.runs.update_run_configuration(
            run_id,
            model_id=model_id,
            source_ids=source_ids,
            input_bindings=input_bindings,
            parameter_values=parameter_values,
        )

    def get_embedding_run_configuration(self, run_id: str) -> EmbeddingRunConfiguration:
        return self.runs.get_run_configuration(run_id)

    def prepare_embedding_run(self, run_id: str) -> EmbeddingRunPreparation:
        return self.runs.prepare_run(run_id)

    def run_embedding_run(
        self,
        run_id: str,
        chunk_size: int | None = None,
        control_callback: Callable[[], Literal["continue", "pause", "cancel"]] | None = None,
        progress_callback: Callable[[int], None] | None = None,
        status_callback: Callable[[str], None] | None = None,
        event_callback=None,
    ) -> EmbeddingRun:
        return self.runs.run_embedding_run(
            run_id,
            chunk_size=chunk_size,
            control_callback=control_callback,
            progress_callback=progress_callback,
            status_callback=status_callback,
            event_callback=event_callback,
        )

    def pause_embedding_run(self, run_id: str) -> EmbeddingRun:
        return self.runs.pause_run(run_id)

    def cancel_embedding_run(self, run_id: str) -> EmbeddingRun:
        return self.runs.cancel_run(run_id)

    def reset_embedding_run(self, run_id: str) -> EmbeddingRun:
        return self.runs.reset_run(run_id)

    def delete_embedding_run(self, run_id: str) -> None:
        self.datasets.delete_dataset(run_id)

    def inspect_embedding_run_checkpoint(self, run_id: str) -> dict[str, int | bool]:
        return self.runs.inspect_run_checkpoint(run_id)

    def preview_metadata_from_source(
        self,
        source_id: str,
        *,
        existing_identifier_column: str = "",
        incoming_identifier_column: str = "",
    ) -> MediaMetadataMergePreview:
        return self.media_library.preview_metadata_from_source(
            source_id,
            existing_identifier_column=existing_identifier_column,
            incoming_identifier_column=incoming_identifier_column,
        )

    def create_metadata_from_source(
        self,
        source_id: str,
        *,
        existing_identifier_column: str = "",
        incoming_identifier_column: str = "",
    ) -> dict[str, object]:
        return self.media_library.create_metadata_from_source(
            source_id,
            existing_identifier_column=existing_identifier_column,
            incoming_identifier_column=incoming_identifier_column,
        )

    def remove_media_source(self, source_id: str) -> None:
        self.media_library.remove_source(source_id)

    def rename_media_source(self, source_id: str, label: str) -> MediaSource:
        return self.media_library.rename_source(source_id, label)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _emit_progress(callback: Callable[[int], None] | None, value: int) -> None:
    if callback is not None:
        callback(max(0, min(100, int(value))))


def _is_stale_running_run(updated_at: str, stale_after_seconds: int = 5) -> bool:
    raw = str(updated_at or "").strip()
    if not raw:
        return True
    try:
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        timestamp = datetime.fromisoformat(normalized)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    age_seconds = (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
    return age_seconds >= stale_after_seconds


def _timestamp_precedes(value: str, cutoff: datetime) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return True
    try:
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        timestamp = datetime.fromisoformat(normalized)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return timestamp.astimezone(timezone.utc) < cutoff.astimezone(timezone.utc)


def _simple_embedding_model_label(label: str) -> str:
    text = re.sub(r"\s*\([^)]*\)\s*", " ", str(label or "")).strip()
    text = " ".join(text.split())
    return text


def _adaptive_embedding_chunk_size(total_items: int, requested_chunk_size: int | None) -> int:
    if requested_chunk_size is not None:
        return max(1, int(requested_chunk_size))
    total = max(1, int(total_items))
    if total <= 1:
        return 1
    target_updates = 20
    return max(1, min(256, int(math.ceil(total / target_updates))))
