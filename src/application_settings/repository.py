"""Persist ordinary preferences without changing storage locations."""
from pathlib import Path
import logging

from shared.persistence import json_store


DEFAULTS = {"appearance": "system", "startup": "chooser", "pin_scope": "project", "clustering_cpu_usage": "75"}
CHOICES = {
    "appearance": {"system", "light", "dark"},
    "startup": {"chooser", "last_project"},
    "pin_scope": {"project", "projection"},
    "clustering_cpu_usage": {"25", "50", "75", "100"},
}


class ApplicationPreferences:
    def __init__(self, path: Path | None = None):
        if path is None:
            from shared.paths import application_data_root

            path = application_data_root() / "preferences.json"
        self.path = path

    def read(self) -> dict:
        try:
            value = json_store.read_json(self.path, {})
        except (OSError, ValueError):
            logging.getLogger(__name__).warning("Could not read application preferences", exc_info=True)
            value = {}
        if not isinstance(value, dict):
            value = {}
        return {key: value.get(key) if isinstance(value.get(key), str) and value.get(key) in choices else DEFAULTS[key]
                for key, choices in CHOICES.items()}

    def update(self, **changes) -> None:
        for key, value in changes.items():
            if key not in CHOICES or value not in CHOICES[key]:
                raise ValueError(f"Invalid application preference: {key}")
        json_store.write_json(self.path, {**self.read(), **changes})

    def reset(self) -> None:
        json_store.write_json(self.path, DEFAULTS)
