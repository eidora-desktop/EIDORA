"""Isolated inference, with one explicit full-run CPU recovery attempt."""
from __future__ import annotations

import json
import multiprocessing
import shutil
import time

from EmbeddingsArea.runtime.contracts import ExecutionFailure, RunCancelled, RunPaused, RuntimeProgress
from EmbeddingsArea.runtime.checkpoints import _atomic_write_text


class WorkerFailure(RuntimeError):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def _child(connection, pause, arguments):
    from EmbeddingsArea.runtime.pipeline import EmbeddingPipeline
    try:
        result = EmbeddingPipeline().run(
            **arguments,
            control_callback=lambda: "pause" if pause.is_set() else "continue",
            progress_callback=lambda value: connection.send(("progress", value)),
            status_callback=lambda value: connection.send(("status", value)),
            event_callback=lambda value: connection.send(("event", value)),
            phase_callback=lambda value: connection.send(("phase", value)),
        )
        connection.send(("result", result))
    except RunPaused:
        connection.send(("paused", None))
    except BaseException as exc:
        connection.send(("error", dict(message=f"{type(exc).__name__}: {exc}", retryable=isinstance(exc, ExecutionFailure))))
    finally:
        connection.close()


def _run_attempt(arguments, *, progress_callback=None, status_callback=None, control_callback=None, heartbeat_callback=None, event_callback=None):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    pause = context.Event()
    process = context.Process(target=_child, args=(sender, pause, arguments), name="eidora-embedding", daemon=True)
    process.start()
    sender.close()
    heartbeat = time.monotonic()
    phase = "starting"
    try:
        while True:
            action = control_callback() if control_callback else "continue"
            if action == "cancel":
                raise RunCancelled("Embedding run cancelled.")
            if action == "pause":
                pause.set()
            if heartbeat_callback and time.monotonic() - heartbeat >= 1:
                heartbeat_callback()
                heartbeat = time.monotonic()
            if receiver.poll(0.1):
                try:
                    kind, value = receiver.recv()
                except EOFError as exc:
                    process.join(timeout=0.1)
                    raise WorkerFailure(f"Embedding process exited during {phase} (code {process.exitcode}).", phase in {"setup", "inference"}) from exc
                if kind == "result":
                    return value
                if kind == "paused":
                    raise RunPaused("Embedding run paused at a checkpoint.")
                if kind == "error":
                    raise WorkerFailure(value["message"], value["retryable"])
                if kind == "phase":
                    phase = value
                if kind == "progress" and progress_callback:
                    progress_callback(value)
                if kind == "event" and event_callback:
                    event_callback(value)
                if kind == "status" and status_callback:
                    status_callback(value)
            elif not process.is_alive():
                raise WorkerFailure(f"Embedding process exited during {phase} (code {process.exitcode}).", phase in {"setup", "inference"})
    finally:
        if process.is_alive():
            process.terminate()
        process.join(timeout=3)
        if process.is_alive():
            process.kill()
            process.join()
        receiver.close()


def _clear_attempt(run_dir):
    # All children are unpublished runtime artifacts; the recovery journal is
    # retained so interruption during deletion still resumes on CPU from zero.
    for path in run_dir.iterdir():
        if path.name in {"execution.json", "prepared.json"}:
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def run_isolated(*, progress_callback=None, status_callback=None, control_callback=None, heartbeat_callback=None, event_callback=None, **arguments):
    callbacks = dict(progress_callback=progress_callback, status_callback=status_callback,
                     control_callback=control_callback, heartbeat_callback=heartbeat_callback, event_callback=event_callback)
    if arguments.get("runtime_adapter_id", "fake") != "onnx":
        return _run_attempt(arguments, **callbacks)
    import onnxruntime as ort
    from EmbeddingsArea.runtime.adapters.onnx import execution_providers_for_device, _DEVICE_PROVIDERS
    from EmbeddingsArea.preparation_contract import validate_providers

    contract = arguments["runtime_contract"]
    declared = validate_providers(contract.get("execution_providers"))
    requested = arguments.get("requested_device", "auto")
    selected = execution_providers_for_device(ort, requested, declared)
    device = next(key for key, value in _DEVICE_PROVIDERS.items() if value == selected[0])
    run_dir = arguments["run_dir"]
    run_dir.mkdir(parents=True, exist_ok=True)
    journal = run_dir / "execution.json"
    signature = arguments.get("prepared_signature", "")
    state = json.loads(journal.read_text()) if journal.exists() else dict(signature=signature, attempts=[], cpu_only=False)
    if state["signature"] != signature:
        raise RuntimeError("Execution history does not match the prepared run.")

    def save():
        _atomic_write_text(journal, json.dumps(state, indent=2))

    def status(message):
        if status_callback:
            status_callback(message)

    if state["cpu_only"]:
        device = "cpu"
    elif requested not in {"auto", "cpu"} and device == "cpu":
        state.update(cpu_only=True, fallback_reason=f"Requested {requested} is not declared or available; using CPU.")
        status(state["fallback_reason"])
        # A resumed run must not mix saved accelerator results with CPU results.
        if (run_dir / "manifest.json").exists():
            state["restart_required"] = True
        save()
    if (run_dir / "manifest.json").exists() and not state.get("restart_required"):
        manifest = json.loads((run_dir / "manifest.json").read_text())
        previous = manifest.get("provenance", {}).get("execution_providers", [])
        expected = [_DEVICE_PROVIDERS[device]] + (["CPUExecutionProvider"] if device != "cpu" else [])
        if previous and previous != expected:
            state.update(cpu_only=True, restart_required=True, fallback_reason="Execution environment changed; restarting the entire run on CPU.")
            device = "cpu"
            save()
            status(state["fallback_reason"])
    while True:
        action = control_callback() if control_callback else "continue"
        if action == "cancel":
            raise RunCancelled("Embedding run cancelled.")
        if action == "pause":
            raise RunPaused("Embedding run paused before execution.")
        if state.get("restart_required"):
            _clear_attempt(run_dir)
            state["restart_required"] = False
            save()
            if progress_callback:
                progress_callback(0)
            if event_callback:
                event_callback(RuntimeProgress("embedding", 0, len(arguments["inputs"]), 0))
        attempt = dict(device=device, session_providers=[_DEVICE_PROVIDERS[device]] + (["CPUExecutionProvider"] if device != "cpu" else []))
        if state.get("fallback_reason"):
            attempt["fallback_reason"] = state["fallback_reason"]
        state["attempts"].append(attempt)
        save()
        try:
            result = _run_attempt({**arguments, "requested_device": device,
                                   "original_requested_device": requested,
                                   "execution_attempts": state["attempts"]}, **callbacks)
            attempt["outcome"] = "completed"
            save()
            if state.get("fallback_reason"):
                status("Completed on CPU. " + state["fallback_reason"])
            return result
        except (RunPaused, RunCancelled) as exc:
            attempt["outcome"] = "paused" if isinstance(exc, RunPaused) else "cancelled"
            save()
            raise
        except WorkerFailure as exc:
            attempt.update(outcome="failed", error=str(exc))
            if device == "cpu" or not exc.retryable:
                save()
                previous = state.get("fallback_reason", "")
                raise RuntimeError((previous + "\n" if previous else "") + str(exc)) from exc
            state.update(cpu_only=True, restart_required=True,
                         fallback_reason=f"{device.upper()} execution failed: {exc}. Restarting the entire run on CPU.")
            save()
            status(state["fallback_reason"])
            device = "cpu"
