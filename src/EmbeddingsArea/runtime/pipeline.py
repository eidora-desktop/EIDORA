from __future__ import annotations

from collections.abc import Callable
from collections import deque
import hashlib
import time
from pathlib import Path

import numpy as np

from EmbeddingsArea.runtime.adapters import AdapterRunConfig, get_runtime_adapter, resolve_runtime_adapter_id
from EmbeddingsArea.runtime.artifacts import ArtifactStore
from EmbeddingsArea.runtime.batching import AutomaticBatchSizer, BatchThrottle, BATCH_POLICIES
from EmbeddingsArea.runtime.checkpoints import CheckpointState, CheckpointStore
from EmbeddingsArea.runtime.contracts import (
    BatchMemoryError,
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
        monitor = profile = None
        policy = BATCH_POLICIES[config.batch_policy]
        initial_batch = config.batch_size or policy.candidates[0]
        setup_batch = initial_batch
        try:
            model_fingerprint = _file_digest(model_path) if model_path else ""
            external_data = model_path.with_name(model_path.name + ".data") if model_path else None
            if external_data is not None and external_data.exists():
                model_fingerprint += ":" + _file_digest(external_data)
            if runtime_adapter_id.startswith('onnx'):
                from EmbeddingsArea.runtime.resources import ResourceMonitor
                from EmbeddingsArea.runtime.batch_profiles import BatchProfile
                from filelock import Timeout

                monitor = ResourceMonitor(output_path.parent, requested_device, config.batch_policy)
                state = monitor.sample()
                if state.stop_reason:
                    raise RunPaused(state.stop_reason)
                try:
                    profile = BatchProfile({
                        'model': model_fingerprint, 'contract': runtime_contract or {},
                        'preparation': preprocess_values or {}, 'device': requested_device,
                        'batch_policy': config.batch_policy,
                    })
                    if config.batch_size is None:
                        initial_batch = profile.read_size() or initial_batch
                except (OSError, Timeout):
                    # Caches are optional; read-only/full cache storage must not prevent a run.
                    profile = None
                if config.batch_size is None:
                    # Start the comparison before compiling CoreML's first fixed shape.
                    initial_batch = AutomaticBatchSizer(len(inputs), policy.candidates[-1], initial_batch, config.batch_policy).size
                setup_batch = min(initial_batch, monitor.batch_limit(initial_batch), len(inputs))
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
                    batch_size=setup_batch,
                    batch_profile=profile,
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
            if profile is not None:
                profile.close()
            raise
        if execution_attempts:
            manifest = artifact_store.read_manifest()
            manifest["provenance"]["attempts"] = execution_attempts
            artifact_store.init_manifest(manifest)
        started = time.monotonic()
        last_event_at = float("-inf")
        last_event_stage = ""
        batch_mode = config.batch_policy.capitalize() if config.batch_size is None else "Manual"
        actual_batch_size = 0
        batch_note = ''

        def event(stage, completed):
            nonlocal last_event_at, last_event_stage
            now = time.monotonic()
            if stage == last_event_stage and completed < len(inputs) and now - last_event_at < 0.25:
                return
            last_event_at, last_event_stage = now, stage
            elapsed = now - started
            update = RuntimeProgress(
                stage, completed, len(inputs), elapsed,
                (completed - start_index) / elapsed if elapsed > 0 else 0,
                actual_batch_size, batch_mode, batch_note,
            )
            if event_callback:
                event_callback(update)

        pending_inputs = []
        pending_vectors = []
        pending_bytes = 0
        next_index = start_index
        last_checkpoint_at = started

        def checkpoint():
            nonlocal completed_chunks, last_checkpoint_at, pending_bytes
            if not pending_inputs:
                return
            if phase_callback:
                phase_callback("persistence")
            object_ids = np.asarray([item.ordinal for item in pending_inputs], dtype=np.int64)
            identifiers = np.asarray([item.identifier or item.path.name for item in pending_inputs], dtype=np.str_)
            embeddings = np.concatenate(pending_vectors, axis=0) if len(pending_vectors) > 1 else pending_vectors[0]
            if monitor is not None:
                monitor.require_disk_space(embeddings.nbytes + identifiers.nbytes + object_ids.nbytes)
            artifact_store.append_chunk(completed_chunks, object_ids, embeddings, identifiers)
            completed_chunks += 1
            checkpoint_store.save(CheckpointState(
                total_items=len(inputs), next_index=next_index,
                completed_chunks=completed_chunks, failed_items=0,
            ))
            pending_inputs.clear()
            pending_vectors.clear()
            pending_bytes = 0
            last_checkpoint_at = time.monotonic()
            if progress_callback is not None:
                # This callback records durable progress, independently of live telemetry.
                progress_callback(min(99, int(next_index / len(inputs) * 100)))

        def check_control():
            action = control_callback() if control_callback is not None else "continue"
            if action == "cancel":
                raise RunCancelled("Embedding run cancelled.")
            if action == "pause":
                checkpoint()
                raise RunPaused("Embedding run paused.")
            if monitor is not None:
                reason = monitor.sample().stop_reason
                if reason:
                    if 'disk' not in reason:
                        checkpoint()
                    raise RunPaused(reason)

        try:
            _emit_status(status_callback, "Processing model batches...")
            adapter_limit = max(1, int(getattr(adapter, "batch_size_limit", 512)))
            tuner = AutomaticBatchSizer(len(inputs) - start_index, adapter_limit, initial_batch, config.batch_policy) if config.batch_size is None else None
            inference_size = tuner.size if tuner else min(max(1, config.batch_size), adapter_limit)
            throttle = BatchThrottle(inference_size)
            throttle.select(inference_size, min(setup_batch, adapter_limit), started)
            resource_report = 'resource limit during model setup'
            resource_transitions = deque(maxlen=32)
            successful_batches: dict[int, int] = {}
            provider_label = 'CoreML' if 'CoreMLExecutionProvider' in providers else 'CUDA' if 'CUDAExecutionProvider' in providers else 'CPU'
            _emit_status(status_callback, f"Batch size: {batch_mode} · starting at {inference_size} · {provider_label}.")
            last_auto_report = ''

            def selection_report():
                report = tuner.report if tuner is not None else f'Manual batch {config.batch_size}.'
                if throttle.active:
                    report += f' Temporarily running batch {throttle.size}: {resource_report}.'
                return report

            def report_auto_choice():
                nonlocal last_auto_report
                if tuner is not None and selection_report() != last_auto_report:
                    last_auto_report = selection_report()
                    _emit_status(status_callback, last_auto_report)

            report_auto_choice()
            previous_resource = None
            while next_index < len(inputs):
                check_control()
                preferred_size = tuner.size if tuner else min(max(1, config.batch_size), adapter_limit)
                batch_note = provider_label
                if adapter_limit == 1:
                    batch_note += ' · model limit'
                if monitor is not None:
                    state = monitor.sample()
                    limit = min(adapter_limit, monitor.batch_limit(throttle.size))
                    old_size, was_throttled = throttle.size, throttle.active
                    inference_size = throttle.select(preferred_size, limit, time.monotonic(), healthy=not state.reduce)
                    resource_report = state.pressure_reason or (
                        'limited hardware monitoring' if state.thermal is None or
                        (requested_device == 'cuda' and state.gpu_free is None) else 'memory headroom')
                    if throttle.active and limit > inference_size and not state.reduce:
                        resource_report = 'recovering after stable headroom; checking every 10 seconds'
                    resource_key = (inference_size, throttle.active, resource_report if throttle.active else '')
                    if resource_key != previous_resource and (throttle.active or was_throttled):
                        details = dict(elapsed_seconds=round(time.monotonic() - started, 3),
                                       previous_size=old_size, size=inference_size, preferred_size=preferred_size,
                                       reason=resource_report if throttle.active else 'preferred batch restored',
                                       thermal=state.thermal, available_ram_bytes=state.available,
                                       worker_rss_bytes=state.resident, gpu_free_bytes=state.gpu_free)
                        resource_transitions.append(details)
                        _emit_status(status_callback,
                                     f'Batch {inference_size}: {details["reason"]} '
                                     f'(thermal={state.thermal}, available RAM={state.available // 1024**2} MiB, '
                                     f'worker RSS={state.resident // 1024**2} MiB).')
                    previous_resource = resource_key
                    if tuner is not None and (throttle.active or state.reduce):
                        tuner.suspend()
                    report_auto_choice()
                    if throttle.active:
                        batch_note += ' · ' + resource_report
                    if state.thermal is None or (requested_device == 'cuda' and state.gpu_free is None):
                        batch_note += ' · limited monitoring'
                    if state.reduce:
                        batch_note += ' · reduced workload'
                else:
                    inference_size = throttle.select(preferred_size, adapter_limit, time.monotonic())
                if tuner is not None and tuner.tuning and not throttle.active:
                    batch_note += ' · comparing batch sizes'
                if hasattr(adapter, 'set_batch_size'):
                    adapter.set_batch_size(inference_size)
                batch = inputs[next_index : next_index + inference_size]
                batch_started = time.monotonic()
                try:
                    vectors = adapter.embed_batch(batch).astype(np.float32, copy=False)
                except (BatchMemoryError, MemoryError):
                    if tuner is None or not tuner.reduce_after_memory_failure(len(batch)):
                        raise
                    inference_size = tuner.size
                    report_auto_choice()
                    continue
                batch_seconds = time.monotonic() - batch_started
                actual_batch_size = len(batch)
                if actual_batch_size == inference_size:
                    successful_batches[inference_size] = successful_batches.get(inference_size, 0) + 1
                pending_inputs.extend(batch)
                pending_vectors.append(vectors)
                pending_bytes += vectors.nbytes
                next_index += len(batch)
                if tuner is not None and tuner.tuning and not throttle.active and (monitor is None or not state.reduce):
                    tuner.observe(len(batch), batch_seconds, len(inputs) - next_index)
                    report_auto_choice()
                event("embedding", next_index)
                if (len(pending_inputs) >= chunk_size
                        or pending_bytes >= 64 * 1024 ** 2
                        or time.monotonic() - last_checkpoint_at >= config.checkpoint_interval_seconds):
                    checkpoint()

            check_control()
            checkpoint()

            if monitor is not None:
                dimensions = int(vectors.shape[1]) if actual_batch_size else int((runtime_contract or {}).get('embedding', {}).get('dimensions', 0))
                monitor.require_disk_space(len(inputs) * dimensions * 4 * 2)

            if phase_callback:
                phase_callback("persistence")
            event("saving", len(inputs))
            _emit_status(status_callback, "Finalizing embedding artifact...")
            manifest = artifact_store.read_manifest()
            manifest["provenance"]["batching"] = {
                "mode": batch_mode.casefold(), "requested_size": config.batch_size,
                "policy": config.batch_policy,
                "selected_size": min(tuner.size, throttle.size) if tuner else inference_size,
                "preferred_size": tuner.size if tuner else min(max(1, config.batch_size), adapter_limit),
                "adapter_limit": adapter_limit,
                "resource_transitions": list(resource_transitions),
                "pacing_seconds": 0.0,
            }
            if tuner is not None and not throttle.active:
                manifest["provenance"]["batching"]["selected_size"] = tuner.size
            if tuner is not None:
                manifest["provenance"]["batching"].update(
                    selection_report=selection_report(),
                    measured_items_per_second={str(size): rate for size, rate in tuner.measurements.items()},
                )
            if execution_attempts:
                execution_attempts[-1]["outcome"] = "completed"
                manifest["provenance"]["attempts"] = execution_attempts
            artifact_store.init_manifest(manifest)
            artifact_path = artifact_store.finalize_npz(
                output_path,
                model_id=model_id,
                model_label=model_label,
                backend=adapter.descriptor.backend,
                content_types=tuple(sorted({item.content_type for item in inputs})),
                input_count=len(inputs),
                check_cancelled=check_control,
            )
            if progress_callback is not None:
                progress_callback(100)
            event("complete", len(inputs))
            _emit_status(status_callback, "Embedding artifact saved.")
            if (profile is not None and tuner is not None and tuner.profile_size is not None
                    and successful_batches.get(tuner.profile_size, 0) >= 4):
                try:
                    profile.save_size(tuner.profile_size)
                except OSError:
                    pass
            return PipelineResult(
                processed_count=len(inputs),
                chunk_count=completed_chunks,
                artifact_path=artifact_path,
            )
        finally:
            _emit_status(status_callback, "Releasing model runtime...")
            adapter.close()
            if profile is not None:
                profile.close()


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
