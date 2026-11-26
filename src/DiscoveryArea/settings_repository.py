from __future__ import annotations

from pathlib import Path

from shared.persistence import json_store


class DiscoverySettingsRepository:
    """Current exploration settings, independent of pin records and presets."""

    def __init__(self, state_directory: Path) -> None:
        self._path = state_directory / "discovery_settings.json"

    def load(self) -> dict:
        payload = json_store.read_json(self._path, default={})
        if not isinstance(payload, dict):
            raise ValueError("Invalid saved Discovery settings.")
        return {
            key: value for key, value in payload.items()
            if (key in {"tooltip", "pins", "slicer", "filters", "canvas"} and isinstance(value, dict))
            or (key == "tooltips_visible" and isinstance(value, bool))
        }

    def save(self, payload: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        json_store.write_json(self._path, payload)
