from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class RuntimeInput:
    ordinal: int
    media_item_id: str
    identifier: str
    path: Path
    content_type: str
    components: dict[str, RuntimeInput] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeConfig:
    # Checkpoint targets never split an inference batch.
    chunk_size: int = 4096
    batch_size: int | None = None  # None selects automatic tuning.
    checkpoint_interval_seconds: float = 10.0
    batch_policy: str = 'auto'


@dataclass(frozen=True)
class PipelineResult:
    processed_count: int
    chunk_count: int
    artifact_path: Path


RunControlAction = Literal["continue", "pause", "cancel"]
RunControlCallback = Callable[[], RunControlAction]


class RunPaused(RuntimeError):
    """Raised when a run is paused at a safe chunk boundary."""


class RunCancelled(RuntimeError):
    """Raised when a run is cancelled at a safe chunk boundary."""


@dataclass(frozen=True)
class RuntimeProgress:
    stage: str
    completed: int
    total: int
    elapsed_seconds: float
    items_per_second: float = 0.0
    batch_size: int = 0
    batch_mode: str = ""
    batch_note: str = ""

    @property
    def message(self) -> str:
        label = {"embedding": "Embedding", "saving": "Saving embeddings", "complete": "Embeddings saved", "thumbnails": "Creating Preview Assets"}.get(self.stage, self.stage)
        text = f"{label}: {self.completed:,} / {self.total:,}"
        if self.batch_size and self.stage == "embedding":
            text += f" · Batch {self.batch_size} · {self.batch_mode}"
            if self.batch_note:
                text += f" · {self.batch_note}"
        text += f"\nElapsed {self.elapsed_seconds:.0f}s"
        if self.items_per_second > 0 and self.stage == "embedding":
            remaining = max(0, self.total - self.completed) / self.items_per_second
            text += f" · {self.items_per_second:,.1f} items/s · about {remaining:.0f}s remaining"
        return text


class ExecutionFailure(RuntimeError):
    """Session initialization/inference failed; an accelerator attempt may retry on CPU."""


class BatchMemoryError(ExecutionFailure):
    """A recoverable allocation failure; Auto may retry a smaller batch."""
