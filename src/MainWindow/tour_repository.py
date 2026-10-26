"""Installation-local onboarding state, independent of collection contents."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


class TourPreferences:
    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            from shared.paths import application_data_root

            path = application_data_root() / "tour.json"
        self.path = path

    def was_shown(self) -> bool:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(value, dict) and value.get("welcome_tour_shown") is True

    def mark_shown(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=self.path.parent, prefix=".tour-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"welcome_tour_shown": True}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)
