from __future__ import annotations

from collections.abc import Callable
import hashlib
import time
from pathlib import Path

import numpy as np

from EmbeddingsArea.runtime.adapters import AdapterRunConfig, get_runtime_adapter, resolve_runtime_adapter_id
from EmbeddingsArea.runtime.artifacts import ArtifactStore
from EmbeddingsArea.runtime.checkpoints import CheckpointState, CheckpointStore
from EmbeddingsArea.runtime.contracts import (
    PipelineResult,
    RunCancelled,
    RunControlCallback,
    RunPaused,
    RuntimeConfig,
    RuntimeProgress,
    RuntimeInput,
)

ProgressCallback = Callable[[int], None]
StatusCallback = Callable[[str], None]


class EmbeddingPipeline:
    """Execute embedding chunks with checkpoints, cancellation, and finalization."""

    def run(
        self,
        *,
        run_id: str,
        model_id: str,
        model_label: str,
        inputs: list[RuntimeInput],
        run_dir: Path,
        output_path: Path,
        config: RuntimeConfig,
        progress_callback: ProgressCallback | None = None,
        event_callback: Callable[[RuntimeProgress], None] | None = None,
        status_callback: StatusCallback | None = None,
        prepared_signature: str = "",
        control_callback: RunControlCallback | None = None,
        runtime_adapter_id: str = "fake",
        model_path: Path | None = None,
        tokenizer_path: Path | None = None,
        input_names: tuple[str, ...] = (),
        output_name: str = "",
        preprocess_values: dict[str, str] | None = None,
        preprocessing_contract: dict[str, object] | None = None,
        runtime_contract: dict[str, object] | None = None,
        package_directory: Path | None = None,
        requested_device: str = "auto",
        phase_callback=None,
        execution_attempts=None,
        original_requested_device=None,
    ) -> PipelineResult:
        if not inputs:
            raise RuntimeError("No inputs provided for embedding generation.")
        chunk_size = max(1, int(config.chunk_size))
        artifact_store = ArtifactStore(run_dir)
        checkpoint_store = CheckpointStore(run_dir)
        input_signature = _input_signature(inputs)
        expected_signature = prepared_signature or input_signature
        _emit_status(status_callback, "Loading model runtime...")
        adapter = get_runtime_adapter(resolve_runtime_adapter_id(runtime_adapter_id))
        try:
            model_fingerprint = _file_digest(model_path) if model_path else ""
            external_data = model_path.with_name(model_path.name + ".data") if model_path else None
            if external_data is not None and external_data.exists():
                model_fingerprint += ":" + _file_digest(external_data)
            adapter.setup(
                AdapterRunConfig(
                    model_path=model_path,
                    tokenizer_path=tokenizer_path,
                    input_names=input_names,
                    output_name=output_name,
                    preprocess_values=dict(preprocess_values or {}),
                    preprocessing_contract=dict(preprocessing_contract or {}),
                    runtime_contract=dict(runtime_contract or {}),
                    package_directory=package_directory,
                    requested_device=requested_device,
                    phase_callback=phase_callback,
                )
            )
            session = getattr(adapter, "_session", None)
            providers = (
                adapter.execution_providers() if hasattr(adapter, "execution_providers")
                else session.get_providers() if session is not None and hasattr(session, "get_providers")
                else [adapter.descriptor.backend]
            )
            _emit_status(status_callback, "Execution providers: " + ", ".join(providers))
            _emit_status(status_callback, "Preparing embedding workspace...")
            if phase_callback:
                phase_callback("persistence")
            existing_manifest = artifact_store.read_manifest()
            if existing_manifest is None:
                from eidora_version import __version__

                contract = runtime_contract or {}
                model_metadata = contract.get("metadata") or next(
                    (component.get("metadata") for component in contract.get("components", {}).values()
                     if component.get("metadata")), {}
                )
                artifact_store.init_manifest(
                    {
                        "run_id": run_id,
                        "model_fingerprint": model_fingerprint,
                        "provenance": {
                            "eidora_version": __version__,
                            "model_version": str(contract.get("version") or ""),
                            "model_metadata": dict(model_metadata),
                            "bibtex": str(contract.get("bibtex") or ""),
                            "input_signature": input_signature,
                            "prepared_signature": expected_signature,
                            "model_sha256": model_fingerprint,
                            "package_fingerprint": str((runtime_contract or {}).get("package_fingerprint") or ""),
                            "modality": str((runtime_contract or {}).get("modality") or ""),
                            "shared_space": dict((runtime_contract or {}).get("embedding", {})).get("shared_space", {}),
                            "fusion": (runtime_contract or {}).get("fusion", ""),
                            "selected_modalities": (runtime_contract or {}).get("selected_modalities", []),
                            "requested_device": original_requested_device or requested_device,
                            "final_execution_mode": "cpu" if providers == ["CPUExecutionProvider"] else "accelerated_session",
                            "attempts": execution_attempts or [],
                            "preparation_version": (runtime_contract or {}).get("preparation_version"),
                            "execution_providers": providers,
                        },
                        "model_id": model_id,
                        "model_name": model_label,
                        "backend": adapter.descriptor.backend,
                        "runtime_adapter": adapter.descriptor.adapter_id,
                        "source": "runtime_chunks",
                        "input_modalities": sorted({
                            modality for item in inputs
                            for modality in (item.components or {item.content_type: item})
                        }),
                        "preprocess": (
                            {key: value["preparation"] for key, value in runtime_contract["components"].items()}
                            if (runtime_contract or {}).get("components") else
                            dict((runtime_contract or {}).get("preparation") or preprocessing_contract or preprocess_values or {})
                        ),
                        "postprocess": [runtime_contract["fusion"]] if (runtime_contract or {}).get("fusion") else [],
                        "raw_dimensions": None,
                        "l2_normalized": False,
                        "chunk_size": chunk_size,
                        "input_count": len(inputs),
                        "input_signature": input_signature,
                        "prepared_signature": expected_signature,
                    }
                )
            else:
                if existing_manifest.get("model_fingerprint", model_fingerprint) != model_fingerprint:
                    raise RuntimeError("The model file changed since this run was prepared; start a new run.")
                existing_signature = str(
                    existing_manifest.get("prepared_signature") or existing_manifest.get("input_signature") or ""
                )
                if existing_signature and existing_signature != expected_signature:
                    raise RuntimeError("Existing runtime artifacts do not match the prepared run snapshot.")

            checkpoint = checkpoint_store.load()
            start_index = 0
            completed_chunks = artifact_store.chunk_count()
            if checkpoint is not None:
                if checkpoint.total_items not in (0, len(inputs)):
                    raise RuntimeError("Checkpoint item count does not match prepared run inputs.")
                if not (0 <= checkpoint.next_index <= len(inputs)):
                    raise RuntimeError("Checkpoint next_index is out of bounds for prepared run inputs.")
                if checkpoint.completed_chunks < 0:
                    raise RuntimeError("Checkpoint completed_chunks is invalid.")
                start_index = checkpoint.next_index
                if completed_chunks > checkpoint.completed_chunks:
                    start_index = min(len(inputs), max(start_index, artifact_store.completed_rows()))
                elif completed_chunks < checkpoint.completed_chunks:
                    raise RuntimeError("Checkpoint references more chunks than exist on disk; cannot resume safely.")
                else:
                    completed_chunks = checkpoint.completed_chunks
            elif completed_chunks:
                start_index = min(len(inputs), artifact_store.completed_rows())

            if artifact_store.completed_rows() != start_index:
                raise RuntimeError("Checkpoint row position disagrees with committed chunks.")
            offset = 0
            for ids, vectors in artifact_store.iter_chunks():
                expected_ids = np.asarray([item.ordinal for item in inputs[offset:offset + len(ids)]])
                if vectors.ndim != 2 or len(vectors) != len(ids) or not np.array_equal(ids, expected_ids):
                    raise RuntimeError("Committed chunk identities do not match prepared inputs.")
                offset += len(ids)
        except BaseException:
            adapter.close()
            raise
        if execution_attempts:
            manifest = artifact_store.read_manifest()
            manifest["provenance"]["attempts"] = execution_attempts
            artifact_store.init_manifest(manifest)
        started = time.monotonic()
        def event(stage, completed):
            elapsed = time.monotonic() - started
            update = RuntimeProgress(stage, completed, len(inputs), elapsed, (completed - start_index) / elapsed if elapsed > 0 else 0)
            if event_callback:
                event_callback(update)
        try:
            _emit_status(status_callback, "Processing model batches...")
            for index in range(start_index, len(inputs), chunk_size):
                if control_callback is not None:
                    action = control_callback()
                    if action == "cancel":
                        raise RunCancelled("Embedding run cancelled.")
                    if action == "pause":
                        raise RunPaused("Embedding run paused.")
                batch = inputs[index : index + chunk_size]
                object_ids = np.asarray([item.ordinal for item in batch], dtype=np.int64)
                inference_size = max(1, config.batch_size or chunk_size)
                parts = []
                for start in range(0, len(batch), inference_size):
                    part = adapter.embed_batch(batch[start:start + inference_size]).astype(np.float32, copy=False)
                    parts.append(part)
                    event("embedding", index + start + len(part))
                embeddings = np.concatenate(parts, axis=0) if len(parts) > 1 else parts[0]
                identifiers = np.asarray([item.identifier or item.path.name for item in batch], dtype=np.str_)
                if phase_callback:
                    phase_callback("persistence")
                artifact_store.append_chunk(completed_chunks, object_ids, embeddings, identifiers)
                completed_chunks += 1
                next_index = min(len(inputs), index + len(batch))
                checkpoint_store.save(
                    CheckpointState(
                        total_items=len(inputs),
                        next_index=next_index,
                        completed_chunks=completed_chunks,
                        failed_items=0,
                    )
                )
                if progress_callback is not None:
                    # Reserve 100% for the consolidated artifact, which can take
                    # appreciable time for a large collection.
                    progress_callback(min(99, int(next_index / len(inputs) * 100)))
                event("embedding", next_index)

            if phase_callback:
                phase_callback("persistence")
            event("saving", len(inputs))
            _emit_status(status_callback, "Finalizing embedding artifact...")
            if execution_attempts:
                execution_attempts[-1]["outcome"] = "completed"
                manifest = artifact_store.read_manifest()
                manifest["provenance"]["attempts"] = execution_attempts
                artifact_store.init_manifest(manifest)
            artifact_path = artifact_store.finalize_npz(
                output_path,
                model_id=model_id,
                model_label=model_label,
                backend=adapter.descriptor.backend,
                content_types=tuple(sorted({item.content_type for item in inputs})),
                input_count=len(inputs),
            )
            if progress_callback is not None:
                progress_callback(100)
            event("complete", len(inputs))
            _emit_status(status_callback, "Embedding artifact saved.")
            return PipelineResult(
                processed_count=len(inputs),
                chunk_count=completed_chunks,
                artifact_path=artifact_path,
            )
        finally:
            _emit_status(status_callback, "Releasing model runtime...")
            adapter.close()


def _input_signature(inputs: list[RuntimeInput]) -> str:
    payload = "|".join(f"{item.ordinal}:{item.media_item_id}:{item.identifier}" for item in inputs)
    for item in inputs:
        for modality, part in sorted(item.components.items()):
            payload += f"|{item.ordinal}:{modality}:{part.media_item_id}:{part.identifier}:{part.path}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _emit_status(callback: StatusCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
