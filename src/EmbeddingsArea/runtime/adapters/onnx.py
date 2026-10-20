from __future__ import annotations

import platform
from pathlib import Path
from dataclasses import replace
from typing import Any

import numpy as np

from EmbeddingsArea.runtime.adapters.base import AdapterRunConfig, RuntimeAdapterDescriptor
from EmbeddingsArea.runtime.contracts import RuntimeInput, ExecutionFailure
from EmbeddingsArea.preparation_contract import validate_providers, resolve_contract
from EmbeddingsArea.runtime.preparers import prepare_batch


_DEVICE_PROVIDERS = {
    "cpu": "CPUExecutionProvider",
    "cuda": "CUDAExecutionProvider",
    "coreml": "CoreMLExecutionProvider",
}
_AUTO_PROVIDER_PRIORITY = (
    "CUDAExecutionProvider",
    "CoreMLExecutionProvider",
    "CPUExecutionProvider",
)


def _platform_execution_providers() -> set[str]:
    """Return providers EIDORA permits on the current hardware platform."""
    if platform.system().casefold() != "darwin":
        return {"CPUExecutionProvider", "CUDAExecutionProvider"}
    if platform.machine().casefold() in {"arm64", "aarch64"}:
        return {"CPUExecutionProvider", "CoreMLExecutionProvider"}
    return {"CPUExecutionProvider"}


def execution_device_availability(declared=None) -> dict[str, bool]:
    try:
        import onnxruntime as ort
    except Exception:
        return {"auto": False, "cpu": False, "cuda": False, "coreml": False}
    available = set(ort.get_available_providers())
    available &= _platform_execution_providers()
    available &= set(declared if declared is not None else _DEVICE_PROVIDERS.values())
    result = {device: provider in available for device, provider in _DEVICE_PROVIDERS.items()}
    result["auto"] = any(result.values())
    return result


def unavailable_device_reason(device: object) -> str:
    normalized = str(device or "auto").strip().casefold()
    if normalized == "cuda":
        return "CUDA is unavailable. It requires an NVIDIA GPU and a CUDA-enabled ONNX Runtime build."
    if normalized == "coreml":
        return "CoreML is unavailable. It requires an Apple Silicon Mac and the ONNX Runtime CoreML provider."
    if normalized == "cpu":
        return "The ONNX Runtime CPU provider is unavailable."
    return "No supported ONNX Runtime execution provider is available."


def execution_providers_for_device(ort: Any, requested_device: object, declared=None) -> list[str]:
    device = str(requested_device or "auto").strip().casefold()
    available = set(ort.get_available_providers())
    available &= _platform_execution_providers()
    available &= set(declared if declared is not None else _DEVICE_PROVIDERS.values())
    if "CPUExecutionProvider" not in available:
        raise RuntimeError("The required CPUExecutionProvider is unavailable.")
    if device == "auto":
        provider = next((value for value in _AUTO_PROVIDER_PRIORITY if value in available), None)
        if provider is None:
            raise RuntimeError(
                "No supported ONNX execution provider is available. "
                f"Available providers: {', '.join(sorted(available)) or 'none'}."
            )
        return _provider_chain(provider, available)
    provider = _DEVICE_PROVIDERS.get(device)
    if provider is None:
        raise RuntimeError(f"Unknown execution device '{device}'. Choose auto, cpu, cuda, or coreml.")
    if provider not in available:
        return ["CPUExecutionProvider"]
    return _provider_chain(provider, available)


def _provider_chain(provider: str, available: set[str]) -> list[str]:
    if provider == "CPUExecutionProvider":
        return [provider]
    return [provider, "CPUExecutionProvider"] if "CPUExecutionProvider" in available else [provider]


class OnnxAdapter:
    """One strict executor for all package-declared ONNX embedding modalities."""

    descriptor = RuntimeAdapterDescriptor(
        adapter_id="onnx",
        backend="onnx",
        supported_inputs=("image", "text", "audio", "video"),
        primary_input="media",
    )

    def __init__(self) -> None:
        self._session: Any | None = None
        self._contract: dict[str, object] = {}
        self._package_directory: Path | None = None
        self._output_name = ""
        self._budget = 256 * 1024 * 1024
        self._components: dict[str, OnnxAdapter] = {}
        self._phase_callback = None

    def setup(self, config: AdapterRunConfig) -> None:
        self._phase_callback = config.phase_callback
        contract = config.runtime_contract
        if contract.get("schema_version") == 3 and not contract.get("preparation_version") and ("preparation" in contract or contract.get("components")):
            config = replace(config, runtime_contract=resolve_contract(contract, config.preprocess_values))
        if config.runtime_contract.get("components"):
            self._setup_components(config)
            return
        if config.model_path is None or not config.model_path.is_file():
            raise RuntimeError("ONNX adapter requires an existing model file.")
        if not config.runtime_contract or config.runtime_contract.get("schema_version") != 3:
            raise RuntimeError("ONNX execution requires an EIDORA schema v3 runtime contract.")
        declared = validate_providers(config.runtime_contract.get("execution_providers"))
        import onnxruntime as ort
        providers = execution_providers_for_device(ort, config.requested_device, declared)
        self._phase("setup")
        try:
            self._session = ort.InferenceSession(
                str(config.model_path),
                providers=providers,
            )
            if hasattr(self._session, "disable_fallback"):
                self._session.disable_fallback()
            if self._session.get_providers() != providers:
                raise RuntimeError(f"Requested session providers {providers}, received {self._session.get_providers()}.")
        except Exception as exc:
            raise ExecutionFailure(f"Failed to load ONNX model at {config.model_path}: {exc}") from exc
        self._contract = dict(config.runtime_contract)
        self._package_directory = config.package_directory or config.model_path.parent
        self._budget = max(1, int(config.prepared_batch_budget_bytes))
        self._phase("validation")
        self._validate_graph_contract()
        self._phase("idle")
        self._output_name = str(dict(self._contract["output"])["name"])

    def _validate_graph_contract(self) -> None:
        assert self._session is not None
        graph_inputs = {str(value.name): value for value in self._session.get_inputs()}
        declared = list(self._contract["inputs"])
        declared_names = {str(value["name"]) for value in declared}
        if set(graph_inputs) != declared_names:
            raise RuntimeError(
                f"ONNX graph inputs do not exactly match config: graph={sorted(graph_inputs)}, "
                f"config={sorted(declared_names)}."
            )
        expected_types = {"float32": "tensor(float)", "int32": "tensor(int32)", "int64": "tensor(int64)"}
        for item in declared:
            meta = graph_inputs[str(item["name"])]
            if str(getattr(meta, "type", "")) != expected_types[str(item["dtype"])]:
                raise RuntimeError(f"ONNX input '{item['name']}' dtype does not match config.")
            _validate_meta_shape(getattr(meta, "shape", ()), item["shape"], f"input '{item['name']}'")
        outputs = {str(value.name): value for value in self._session.get_outputs()}
        output = dict(self._contract["output"])
        meta = outputs.get(str(output["name"]))
        if meta is None:
            raise RuntimeError(f"ONNX graph does not define output '{output['name']}'.")
        if str(getattr(meta, "type", "")) != "tensor(float)":
            raise RuntimeError("ONNX embedding output must be float32.")
        _validate_meta_shape(getattr(meta, "shape", ()), output["shape"], "output")

    def embed_batch(self, batch: list[RuntimeInput]) -> np.ndarray:
        if self._components:
            return self._embed_components(batch)
        if self._session is None or self._package_directory is None:
            raise RuntimeError("ONNX adapter session was not initialized.")
        limit = _contract_batch_limit(self._contract, self._budget)
        parts: list[np.ndarray] = []
        for start in range(0, len(batch), limit):
            items = batch[start : start + limit]
            self._phase("preparation")
            try:
                feed = prepare_batch(items, self._contract, package_directory=self._package_directory)
            except Exception as exc:
                identifiers = ", ".join(item.identifier or item.path.name for item in items[:3])
                raise RuntimeError(f"Could not prepare model input ({identifiers}): {exc}") from exc
            prepared_bytes = sum(value.nbytes for value in feed.values())
            if prepared_bytes > self._budget:
                if len(items) == 1:
                    raise RuntimeError(
                        f"One prepared model input requires {prepared_bytes / 1024**2:.1f} MiB, "
                        f"above EIDORA's {self._budget / 1024**2:.0f} MiB limit."
                    )
                midpoint = max(1, len(items) // 2)
                parts.extend((self.embed_batch(items[:midpoint]), self.embed_batch(items[midpoint:])))
                continue
            self._phase("inference")
            try:
                raw = self._session.run([self._output_name], feed)[0]
            except Exception as exc:
                raise ExecutionFailure(f"ONNX inference failed: {exc}") from exc
            vectors = np.asarray(raw)
            dimensions = int(dict(self._contract["embedding"])["dimensions"])
            if vectors.dtype != np.float32 or vectors.shape != (len(items), dimensions):
                raise ExecutionFailure(
                    f"ONNX output must be float32 [{len(items)}, {dimensions}], got {vectors.dtype} {vectors.shape}."
                )
            if not np.isfinite(vectors).all():
                raise ExecutionFailure("ONNX model returned nonfinite embeddings.")
            if bool(dict(self._contract["embedding"])["normalized"]) and not np.allclose(
                np.linalg.norm(vectors, axis=1), 1.0, rtol=1e-4, atol=1e-5
            ):
                raise ExecutionFailure("ONNX output contradicts embedding.normalized: true.")
            parts.append(vectors)
            self._phase("idle")
        return np.concatenate(parts, axis=0) if len(parts) > 1 else parts[0]

    def _setup_components(self, config: AdapterRunConfig) -> None:
        from EmbeddingsArea.model_package import resolve_package_path

        contract = config.runtime_contract
        if contract.get("schema_version") != 3 or contract.get("fusion") != "normalized_mean":
            raise RuntimeError("Unsupported multimodal execution contract.")
        if config.package_directory is None:
            raise RuntimeError("Multimodal execution requires a package directory.")
        selected = contract.get("selected_modalities", list(contract["components"]))
        if not selected or len(set(selected)) != len(selected):
            raise RuntimeError("Select at least one distinct model input.")
        try:
            for modality in selected:
                component = contract["components"][modality]
                adapter = OnnxAdapter()
                self._components[modality] = adapter
                adapter.setup(replace(
                    config, runtime_contract=component,
                    model_path=resolve_package_path(config.package_directory, component["graph"]),
                ))
        except Exception:
            self.close()
            raise

    def _embed_components(self, batch: list[RuntimeInput]) -> np.ndarray:
        combined = None
        for modality, adapter in self._components.items():
            if any(set(item.components) != set(self._components) for item in batch):
                raise RuntimeError("Each item must contain every selected modality exactly once.")
            vectors = adapter.embed_batch([item.components[modality] for item in batch])
            self._phase("inference")
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            if np.any(norms <= 1e-8):
                raise ExecutionFailure("Cannot combine a zero-length encoder embedding.")
            vectors = vectors / norms
            combined = vectors if combined is None else combined + vectors
        norms = np.linalg.norm(combined, axis=1, keepdims=True)
        if np.any(norms <= 1e-8):
            raise ExecutionFailure("Selected modality embeddings cancel out; a combined embedding is undefined.")
        result = (combined / norms).astype(np.float32, copy=False)
        self._phase("idle")
        return result

    def _phase(self, value):
        if self._phase_callback:
            self._phase_callback(value)

    def close(self) -> None:
        for adapter in self._components.values():
            adapter.close()
        self._components.clear()
        self._session = None
        self._contract = {}
        self._package_directory = None

    def execution_providers(self) -> list[str]:
        if self._components:
            return list(dict.fromkeys(
                provider for adapter in self._components.values()
                for provider in adapter.execution_providers()
            ))
        return self._session.get_providers() if self._session is not None else []


def _validate_meta_shape(actual: Any, declared: Any, label: str) -> None:
    actual_values, declared_values = list(actual or []), list(declared or [])
    if len(actual_values) != len(declared_values):
        raise RuntimeError(f"ONNX {label} rank does not match config.")
    for index, (found, expected) in enumerate(zip(actual_values, declared_values)):
        if index == 0 and expected in {"batch", "dynamic"} and isinstance(found, int):
            raise RuntimeError(f"ONNX {label} has fixed batch size but config declares dynamic batch.")
        if isinstance(expected, int) and isinstance(found, int) and found != expected:
            raise RuntimeError(f"ONNX {label} shape does not match config.")


def _contract_batch_limit(contract: dict[str, object], budget: int) -> int:
    tensors = [*contract["inputs"], contract["output"]]
    if any(list(item.get("shape", []))[:1] == [1] for item in tensors):
        return 1
    total = 0
    sizes = {"float32": 4, "int32": 4, "int64": 8}
    for item in contract["inputs"]:
        count = 1
        for dimension in list(item["shape"])[1:]:
            if not isinstance(dimension, int):
                count = 0
                break
            count *= dimension
        total += count * sizes[str(item["dtype"])]
    return max(1, budget // total) if total else 1
