from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Callable

import numpy as np

from EmbeddingsArea.runtime.contracts import RuntimeInput


@dataclass(frozen=True)
class RuntimeAdapterDescriptor:
    adapter_id: str
    backend: str
    supported_inputs: tuple[str, ...]
    primary_input: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdapterRunConfig:
    model_path: Path | None = None
    tokenizer_path: Path | None = None
    input_names: tuple[str, ...] = ()
    output_name: str = ""
    max_length: int = 128
    preprocess_values: dict[str, str] = field(default_factory=dict)
    preprocessing_contract: dict[str, object] = field(default_factory=dict)
    requested_device: str = "auto"
    runtime_contract: dict[str, object] = field(default_factory=dict)
    package_directory: Path | None = None
    phase_callback: Callable[[str], None] | None = None
    prepared_batch_budget_bytes: int = 256 * 1024 * 1024


class RuntimeEmbeddingAdapter(Protocol):
    descriptor: RuntimeAdapterDescriptor

    def setup(self, config: AdapterRunConfig) -> None:
        """Initialize adapter resources for one run."""

    def embed_batch(self, batch: list[RuntimeInput]) -> np.ndarray:
        """Return a 2D float32 embedding matrix for the batch."""

    def close(self) -> None:
        """Release adapter resources after a run."""
