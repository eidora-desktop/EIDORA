"""Installation-local file locations and window layout, separate from projects."""
from pathlib import Path
import logging

from shared.persistence import json_store


def default_session_path() -> Path:
    from shared.paths import application_data_root

    return application_data_root() / "session.json"


class SessionPreferences:
    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else default_session_path()

    def read(self, section: str) -> dict:
        value = self._read().get(section)
        return value if isinstance(value, dict) else {}

    def update(self, section: str, **changes) -> None:
        payload = self._read()
        previous = payload.get(section)
        payload[section] = {**(previous if isinstance(previous, dict) else {}), **changes}
        try:
            json_store.write_json(self.path, payload)
        except OSError:
            logging.getLogger(__name__).warning("Could not save session preferences", exc_info=True)

    def _read(self) -> dict:
        try:
            payload = json_store.read_json(self.path, {})
        except (OSError, ValueError):
            logging.getLogger(__name__).warning("Could not read session preferences", exc_info=True)
            return {}
        return payload if isinstance(payload, dict) else {}
