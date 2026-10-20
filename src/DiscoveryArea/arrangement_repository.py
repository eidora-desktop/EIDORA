from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

import numpy as np

from DiscoveryArea.transforms import TransformResult, TransformSettings


class ArrangementRepository:
    """Own immutable transformed-coordinate artifacts and current references."""

    def __init__(self, discovery_root: Path) -> None:
        self._root = Path(discovery_root) / "arrangements"

    def save(self, discovery_scope: str, result: TransformResult) -> TransformResult:
        scope_root = self._scope_root(discovery_scope)
        scope_root.mkdir(parents=True, exist_ok=True)
        artifact_id = uuid.uuid4().hex
        artifact = scope_root / f"{artifact_id}.npz"
        fd, temporary_name = tempfile.mkstemp(prefix=".arrangement-", suffix=".npz", dir=scope_root)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            with temporary.open("wb") as handle:
                np.savez_compressed(
                    handle,
                    metadata_ids=np.asarray(result.metadata_ids, dtype=np.int64),
                    grid_coordinates=np.asarray(result.grid_coordinates, dtype=np.float32),
                    coordinates=np.asarray(result.coordinates, dtype=np.float32),
                    settings_json=np.asarray(json.dumps(result.settings.to_dict())),
                    source_fingerprint=np.asarray(result.source_fingerprint),
                    grid_aspect_ratio=np.asarray(float(result.grid_aspect_ratio), dtype=np.float64),
                )
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(artifact)
        finally:
            if temporary.exists():
                temporary.unlink()
        return replace(result, arrangement_ref=str(artifact.resolve()))

    def set_current(self, discovery_scope: str, result: TransformResult) -> None:
        if not result.arrangement_ref:
            raise ValueError("Arrangement artifact reference is required.")
        artifact = Path(result.arrangement_ref).resolve()
        scope_root = self._scope_root(discovery_scope).resolve()
        if artifact.parent != scope_root or not artifact.exists():
            raise ValueError("Arrangement artifact does not belong to this projection.")
        self._write_current_atomic(scope_root, artifact.name)

    def load_current(self, discovery_scope: str, source_fingerprint: str) -> TransformResult | None:
        scope_root = self._scope_root(discovery_scope)
        try:
            payload = json.loads((scope_root / "current.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        artifact_name = str(payload.get("artifact") or "") if isinstance(payload, dict) else ""
        if not artifact_name or Path(artifact_name).name != artifact_name:
            return None
        result = self.load_artifact(scope_root / artifact_name)
        if result is None or result.source_fingerprint != str(source_fingerprint):
            self.clear_current(discovery_scope)
            return None
        return result

    def has_current(self, discovery_scope: str) -> bool:
        return (self._scope_root(discovery_scope) / "current.json").is_file()

    def load_artifact(self, path: Path) -> TransformResult | None:
        artifact = Path(path)
        if not artifact.exists():
            return None
        try:
            with np.load(artifact, allow_pickle=False) as payload:
                settings = TransformSettings.from_dict(json.loads(str(payload["settings_json"].item())))
                result = TransformResult(
                    metadata_ids=np.asarray(payload["metadata_ids"], dtype=np.int64),
                    grid_coordinates=np.asarray(payload["grid_coordinates"], dtype=np.float32),
                    coordinates=np.asarray(payload["coordinates"], dtype=np.float32),
                    settings=settings,
                    source_fingerprint=str(payload["source_fingerprint"].item()),
                    grid_aspect_ratio=(
                        float(payload["grid_aspect_ratio"].item())
                        if "grid_aspect_ratio" in payload.files
                        else _coordinate_aspect(payload["grid_coordinates"])
                    ),
                    arrangement_ref=str(artifact.resolve()),
                )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None
        if result.coordinates.shape != result.grid_coordinates.shape:
            return None
        if result.coordinates.ndim != 2 or result.coordinates.shape[1] != 2:
            return None
        if result.metadata_ids.shape[0] != result.coordinates.shape[0]:
            return None
        if not np.isfinite(result.coordinates).all() or not np.isfinite(result.grid_coordinates).all():
            return None
        return result

    def clear_current(self, discovery_scope: str) -> None:
        current = self._scope_root(discovery_scope) / "current.json"
        if current.exists():
            current.unlink()

    def relative_ref(self, project_root: Path, result: TransformResult | None) -> str | None:
        if result is None or not result.arrangement_ref:
            return None
        path = Path(result.arrangement_ref).resolve()
        try:
            return str(path.relative_to(Path(project_root).resolve()))
        except ValueError:
            return str(path)

    def resolve_ref(self, project_root: Path, arrangement_ref: str | None) -> Path | None:
        if not arrangement_ref:
            return None
        path = Path(arrangement_ref)
        if not path.is_absolute():
            path = Path(project_root) / path
        resolved = path.resolve()
        arrangements_root = self._root.resolve()
        try:
            resolved.relative_to(arrangements_root)
        except ValueError:
            return None
        return resolved

    def _scope_root(self, discovery_scope: str) -> Path:
        digest = hashlib.sha256(str(discovery_scope or "").encode("utf-8")).hexdigest()[:24]
        return self._root / digest

    @staticmethod
    def _write_current_atomic(scope_root: Path, artifact_name: str) -> None:
        fd, temporary_name = tempfile.mkstemp(prefix=".current-", suffix=".json", dir=scope_root)
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            temporary.write_text(json.dumps({"artifact": artifact_name}, indent=2), encoding="utf-8")
            temporary.replace(scope_root / "current.json")
        finally:
            if temporary.exists():
                temporary.unlink()


def _coordinate_aspect(coordinates: np.ndarray) -> float:
    points = np.asarray(coordinates, dtype=np.float64)
    if points.ndim != 2 or points.shape[0] <= 1 or points.shape[1] < 2:
        return 1.0
    span = np.ptp(points[:, :2], axis=0)
    if span[1] <= 1e-12:
        return float(max(1, points.shape[0]))
    return max(1e-12, float(span[0] / span[1]))
