from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any


@dataclass(frozen=True)
class PresetEntry:
    label: str
    path: Path
    modified_at: float
    created_at: float | None = None


class PresetRepository:
    """JSON preset repository for one project-level preset directory."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def list_entries(self) -> list[PresetEntry]:
        entries: list[PresetEntry] = []
        for path in self._root.glob("*.json"):
            try:
                stat = path.stat()
                modified = float(stat.st_mtime)
                created = float(getattr(stat, "st_birthtime", stat.st_ctime))
            except OSError:
                continue
            label = path.stem
            payload = self.load_payload_from_path(path)
            if isinstance(payload, dict):
                saved_name = str(payload.get("name") or "").strip()
                if saved_name:
                    label = saved_name
            entries.append(PresetEntry(label=label, path=path, modified_at=modified, created_at=created))
        entries.sort(key=lambda item: item.modified_at, reverse=True)
        return entries

    def has_entries(self) -> bool:
        """Check availability without reading or sorting preset payloads."""
        return any(path.is_file() for path in self._root.glob("*.json"))

    def exists_label(self, label: str) -> bool:
        return self._path_for_label(label).exists()

    def path_for_label(self, label: str) -> Path:
        """Return the normalized JSON path for a preset label."""

        return self._path_for_label(label)

    def load_payload(self, label: str) -> dict[str, Any] | None:
        path = self._path_for_label(label)
        return self.load_payload_from_path(path)

    def load_payload_from_path(self, path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def save_payload(self, label: str, payload: dict[str, Any], *, allow_overwrite: bool) -> Path:
        safe_label = self.normalize_label(label)
        if not safe_label:
            raise ValueError("Preset label cannot be empty.")
        if not isinstance(payload, dict):
            raise ValueError("Preset payload must be a dictionary.")
        path = self._path_for_label(safe_label)
        if path.exists() and not allow_overwrite:
            raise FileExistsError(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def ensure_payload(
        self,
        label: str,
        payload: dict[str, Any],
        *,
        ignored_keys: tuple[str, ...] = (),
    ) -> Path:
        """Return an equivalent preset, or save a new one without overwriting."""

        comparable = self._comparable_payload(payload, ignored_keys)
        for entry in self.list_entries():
            stored = self.load_payload_from_path(entry.path)
            if stored is not None and self._comparable_payload(stored, ignored_keys) == comparable:
                return entry.path

        unique_label = self.normalize_label(label)
        if not unique_label:
            raise ValueError("Preset label cannot be empty.")
        base_label = unique_label
        suffix = 2
        while self.exists_label(unique_label):
            unique_label = f"{base_label} {suffix}"
            suffix += 1

        stored_payload = dict(payload)
        if "name" in ignored_keys and "name" in stored_payload:
            stored_payload["name"] = unique_label
        return self.save_payload(unique_label, stored_payload, allow_overwrite=False)

    @staticmethod
    def normalize_label(label: str) -> str:
        text = str(label or "").strip()
        text = re.sub(r"\s+", " ", text)
        return text

    def _path_for_label(self, label: str) -> Path:
        safe = self._filename_for_label(label)
        return self._root / f"{safe}.json"

    @staticmethod
    def _comparable_payload(payload: dict[str, Any], ignored_keys: tuple[str, ...]) -> dict[str, Any]:
        return {key: value for key, value in payload.items() if key not in ignored_keys}

    @staticmethod
    def _filename_for_label(label: str) -> str:
        text = PresetRepository.normalize_label(label).lower()
        text = re.sub(r"[^a-z0-9._-]+", "_", text).strip("._-")
        return text or "preset"
