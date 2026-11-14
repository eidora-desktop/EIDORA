from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ProcessInputKind(str, Enum):
    MEDIA_SOURCE = "media_source"
    ARTIFACT = "artifact"


class ProcessState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self in {
            self.COMPLETED,
            self.COMPLETED_WITH_WARNINGS,
            self.FAILED,
            self.CANCELLED,
            self.INTERRUPTED,
        }


@dataclass(frozen=True)
class ProcessInputReference:
    kind: ProcessInputKind
    id: str
    label: str
    media_type: str
    item_count: int


@dataclass(frozen=True)
class ProcessRequest:
    operation_id: str
    input: ProcessInputReference
    output_id: str
    output_name: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProcessRecord:
    id: str
    operation_id: str
    operation_label: str
    input: ProcessInputReference
    output_id: str
    output_name: str
    parameters: dict[str, Any]
    state: ProcessState
    progress: int = 0
    message: str = ""
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    generated_files: int = 0
    failed_files: int = 0
    missing_files: int = 0
    errors: tuple[str, ...] = ()


THUMBNAIL_RESIZE_MODES = (
    ("Preserve aspect ratio", "original_aspect"),
    ("Square cropped", "square_cropped"),
    ("Square squeezed", "square_squeezed"),
)

THUMBNAIL_VIDEO_FRAME_MODES = (
    ("First frame", "first"),
    ("Middle frame", "middle"),
    ("Last frame", "last"),
    ("Random frame", "random"),
)
