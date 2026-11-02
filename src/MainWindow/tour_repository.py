"""Installation-local onboarding state, independent of collection contents."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile


def _current_installation() -> dict:
    from eidora_version import __version__

    identity = {"version": __version__}
    if not getattr(sys, "frozen", False):
        return {**identity, "source": True}
    # AppImage's mounted executable changes location on every launch.
    appimage = os.environ.get("APPIMAGE") if sys.platform == "linux" else None
    executable = Path(appimage or sys.executable).resolve()
    identity["executable"] = str(executable)
    try:
        info = executable.stat()
    except OSError:
        return identity
    # File identity/timestamps also detect replacing an install at the same version.
    identity.update(file_id=info.st_ino, changed_ns=info.st_ctime_ns,
                    modified_ns=info.st_mtime_ns)
    return identity


class TourPreferences:
    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            from shared.paths import application_data_root

            path = application_data_root() / "tour.json"
        self.path = path
        self._installation = _current_installation()

    def was_shown(self) -> bool:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return (
            isinstance(value, dict)
            and value.get("welcome_tour_shown") is True
            and value.get("installation") == self._installation
        )

    def mark_shown(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(dir=self.path.parent, prefix=".tour-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"welcome_tour_shown": True, "installation": self._installation}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)
