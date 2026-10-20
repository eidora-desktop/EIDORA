"""Small, atomic storage preferences; no project contents live here."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


class StoragePreferences:
    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            from shared.paths import application_data_root

            path = application_data_root() / "storage.json"
        self.path = path

    def read(self) -> dict:
        if not self.path.exists():
            return {}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"Invalid storage preferences: {self.path}")
        return value

    def update(self, **changes) -> None:
        value = self.read()
        value.update(changes)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=self.path.parent, prefix=".storage-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)
