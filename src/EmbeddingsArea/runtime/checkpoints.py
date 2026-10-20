from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile


@dataclass(frozen=True)
class CheckpointState:
    total_items: int
    next_index: int
    completed_chunks: int
    failed_items: int = 0


class CheckpointStore:
    """Persistence boundary for resumable embedding runs."""

    def __init__(self, run_dir: Path) -> None:
        self._path = run_dir / "checkpoint.json"

    def load(self) -> CheckpointState | None:
        if not self._path.exists():
            return None
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        return CheckpointState(
            total_items=int(payload.get("total_items", 0)),
            next_index=int(payload.get("next_index", 0)),
            completed_chunks=int(payload.get("completed_chunks", 0)),
            failed_items=int(payload.get("failed_items", 0)),
        )

    def save(self, state: CheckpointState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.__dict__, indent=2, sort_keys=True)
        _atomic_write_text(self._path, payload)

    def clear(self) -> None:
        if self._path.exists():
            self._path.unlink()


def _atomic_write_text(path: Path, content: str) -> None:
    tmp_fd: int | None = None
    tmp_path = ""
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=f".{path.stem}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            tmp_fd = None
            handle.write(content)
            handle.flush()
        os.replace(tmp_path, path)
    finally:
        if tmp_fd is not None:
            os.close(tmp_fd)
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
