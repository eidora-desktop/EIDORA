from __future__ import annotations

from EmbeddingsArea.runtime.adapters.base import RuntimeAdapterDescriptor, RuntimeEmbeddingAdapter
from EmbeddingsArea.runtime.adapters.fake import DeterministicFakeAdapter
from EmbeddingsArea.runtime.adapters.onnx import OnnxAdapter


_ADAPTER_TYPES = {
    "fake": DeterministicFakeAdapter,
    "onnx": OnnxAdapter,
}


def register_runtime_adapter(adapter_id: str, adapter_type: type[RuntimeEmbeddingAdapter]) -> None:
    _ADAPTER_TYPES[str(adapter_id).strip()] = adapter_type


def list_runtime_adapter_descriptors() -> tuple[RuntimeAdapterDescriptor, ...]:
    return tuple(adapter_type.descriptor for adapter_type in _ADAPTER_TYPES.values())


def resolve_runtime_adapter_id(adapter_id: object) -> str:
    raw = str(adapter_id or "").strip()
    if not raw:
        raise ValueError("A runtime adapter id is required.")
    if raw in _ADAPTER_TYPES:
        return raw
    for descriptor in list_runtime_adapter_descriptors():
        if raw in descriptor.aliases:
            return descriptor.adapter_id
    raise ValueError(f"Unsupported runtime adapter: {raw}")


def get_runtime_adapter(adapter_id: object) -> RuntimeEmbeddingAdapter:
    resolved = resolve_runtime_adapter_id(adapter_id)
    return _ADAPTER_TYPES[resolved]()


def get_runtime_adapter_descriptor(adapter_id: object) -> RuntimeAdapterDescriptor:
    return get_runtime_adapter(adapter_id).descriptor
