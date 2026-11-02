"""Exercise packaged inference in a child process, with file-based diagnostics."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import platform
import subprocess
import sys
import traceback


# ONNX IR 9, opset 17: Conv, Relu, AveragePool, GlobalAveragePool, Flatten, ReduceL2, Div.
# Conv exercises cuDNN engine loading as well as the CUDA provider itself.
# Omitted Conv/AveragePool pads exercise the MLProgram compatibility path.
# L2 normalization detects CoreML precision loss on real embedding contracts.
# An immutable synthetic graph keeps release checks independent of Model Zoo,
# networking, and the exporter/onnx development package.
_MODEL = "CAk6ugMKIQoGcGl4ZWxzCgZ3ZWlnaHQSCWNvbnZvbHZlZCIEQ29udgobCgljb252b2x2ZWQSCHBvc2l0aXZlIgRSZWx1CkoKCHBvc2l0aXZlEghhdmVyYWdlZCILQXZlcmFnZVBvb2wqFQoMa2VybmVsX3NoYXBlQAJAAqABByoQCgdzdHJpZGVzQAJAAqABBwolCghhdmVyYWdlZBIGcG9vbGVkIhFHbG9iYWxBdmVyYWdlUG9vbAobCgZwb29sZWQSCGZlYXR1cmVzIgdGbGF0dGVuCicKCGZlYXR1cmVzEgRub3JtIghSZWR1Y2VMMioLCgRheGVzQAGgAQcKIAoIZmVhdHVyZXMKBG5vcm0SCWVtYmVkZGluZyIDRGl2Ehpub3JtYWxpemVkLWVtYmVkZGluZy1hdWRpdCo4CAMIAwgBCAEQAUIGd2VpZ2h0SiQAAIA/AAAAAAAAAAAAAAAAAACAPwAAAAAAAAAAAAAAAAAAgD9aJQoGcGl4ZWxzEhsKGQgBEhUKBxIFYmF0Y2gKAggDCgIIBAoCCARiIAoJZW1iZWRkaW5nEhMKEQgBEg0KBxIFYmF0Y2gKAggDQgIQEQ=="


def probe(directory: Path, provider: str | None = None) -> dict:
    import ctypes
    import numpy as np
    import onnxruntime as ort
    from EmbeddingsArea.runtime.adapters.onnx import create_onnx_session

    directory.mkdir(parents=True, exist_ok=True)
    available = ort.get_available_providers()
    report = {"runtime": ort.__version__, "available_providers": available}
    if sys.platform in {"win32", "linux"} and getattr(sys, "frozen", False):
        if "CUDAExecutionProvider" not in available:
            raise RuntimeError("The packaged runtime is missing CUDAExecutionProvider.")
        from EmbeddingsArea.runtime.cuda import preload_cuda

        preload_cuda(ort)
        filename = "onnxruntime_providers_cuda.dll" if sys.platform == "win32" else "libonnxruntime_providers_cuda.so"
        # Loading the native plugin catches missing dependencies on GPU-less CI.
        ctypes.CDLL(str(Path(ort.__file__).parent / "capi" / filename))
        report["cuda_library_load"] = "passed"
        report["cuda_inference"] = "not tested; requires --provider cuda on NVIDIA hardware"
    if provider is None:
        provider = "coreml" if sys.platform == "darwin" and platform.machine().lower() in {"arm64", "aarch64"} else "cpu"
    name = {"cpu": "CPUExecutionProvider", "cuda": "CUDAExecutionProvider", "coreml": "CoreMLExecutionProvider"}[provider]
    if name not in available:
        raise RuntimeError(f"Required provider {name} is unavailable: {available}")
    if provider == "cuda":
        from EmbeddingsArea.runtime.cuda import preload_cuda

        preload_cuda(ort)
    providers = [name] if provider == "cpu" else [name, "CPUExecutionProvider"]
    options = ort.SessionOptions()
    options.enable_profiling = True
    options.profile_file_prefix = str(directory / "profile")
    model_path = directory / "model.onnx"
    model_path.write_bytes(base64.b64decode(_MODEL))
    session = create_onnx_session(ort, model_path, providers, sess_options=options)
    session.disable_fallback()
    if session.get_providers() != providers:
        raise RuntimeError(f"Requested {providers}, received {session.get_providers()}")
    for batch in (1, 2, 32):
        pixels = np.arange(batch * 48, dtype=np.float32).reshape(batch, 3, 4, 4)
        actual = session.run(None, {"pixels": pixels})[0]
        expected = pixels.mean(axis=(2, 3))
        expected /= np.linalg.norm(expected, axis=1, keepdims=True)
        np.testing.assert_allclose(actual, expected, rtol=1e-4, atol=1e-5)
        np.testing.assert_allclose(np.linalg.norm(actual, axis=1), 1.0, rtol=1e-4, atol=1e-5)
    profile = Path(session.end_profiling())
    events = json.loads(profile.read_text(encoding="utf-8"))
    executed = sorted({event.get("args", {}).get("provider") for event in events if event.get("args", {}).get("provider")})
    if name not in executed:
        raise RuntimeError(f"Inference did not execute any nodes on {name}: {executed}")
    report.update(provider=name, executed_providers=executed, batches=[1, 2, 32], normalized_vectors=True)
    if provider == "cuda":
        report["cuda_inference"] = "passed"
    return report


def audit_onnx_runtime(directory: Path, diagnostic_prefix: Path) -> dict:
    from shared.internal_workers import internal_worker_command, read_worker_result

    directory.mkdir(parents=True, exist_ok=True)
    result = diagnostic_prefix.with_suffix(".onnx.json")
    log = diagnostic_prefix.with_suffix(".onnx.log")
    result.unlink(missing_ok=True)
    with log.open("w", encoding="utf-8") as stream:
        completed = subprocess.run(internal_worker_command("onnx-audit", directory, result),
                                   stdout=stream, stderr=subprocess.STDOUT, timeout=120)
    try:
        reply = read_worker_result(result)
    except RuntimeError as exc:
        raise RuntimeError(f"ONNX worker returned no valid result (exit {completed.returncode}); see {log}") from exc
    if completed.returncode or not reply["ok"]:
        raise RuntimeError(f"ONNX audit failed (exit {completed.returncode}): {reply.get('error')}; see {log}")
    return reply


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("result", type=Path)
    parser.add_argument("--provider", choices=("cpu", "cuda", "coreml"))
    args = parser.parse_args(argv)
    try:
        result = {"ok": True, **probe(args.directory, args.provider)}
    except Exception:
        result = {"ok": False, "error": traceback.format_exc()}
    args.result.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1
