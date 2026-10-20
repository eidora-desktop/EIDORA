from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import tempfile
import uuid

import numpy as np

from DiscoveryArea.domain import DiscoveryDataset
from DiscoveryArea.presets_repository import PresetEntry, PresetRepository
from DiscoveryArea.snapshots import DiscoverySnapshot, DiscoverySnapshotProjection
from shared.persistence import json_store
from project.portable_paths import ProjectPaths


class SnapshotRepository:
    """Own snapshot manifests and their single frozen coordinate payload."""

    def __init__(self, project_root: Path, snapshot_root: Path) -> None:
        self._project_root = Path(project_root)
        self._paths = ProjectPaths(project_root)
        self._root = Path(snapshot_root)
        self._root.mkdir(parents=True, exist_ok=True)

    def list_entries(self) -> list[PresetEntry]:
        return PresetRepository(self._root).list_entries()

    def save(
        self,
        label: str,
        snapshot: DiscoverySnapshot,
        dataset: DiscoveryDataset,
        projection: DiscoverySnapshotProjection,
        *,
        allow_overwrite: bool,
    ) -> Path:
        name = PresetRepository.normalize_label(label)
        if not name:
            raise ValueError("Snapshot label cannot be empty.")
        preset_repository = PresetRepository(self._root)
        manifest_path = preset_repository.path_for_label(name)
        filename = manifest_path.stem
        if manifest_path.exists() and not allow_overwrite:
            raise FileExistsError(manifest_path)

        coordinates = np.asarray(dataset.coordinates, dtype=np.float32)
        metadata_ids = np.asarray(dataset.metadata_ids, dtype=np.int64).reshape(-1)
        if coordinates.ndim != 2 or coordinates.shape[1] != 2:
            raise ValueError("Snapshot coordinates must have shape (n, 2).")
        if coordinates.shape[0] != metadata_ids.shape[0]:
            raise ValueError("Snapshot coordinates and metadata IDs must be aligned.")
        if not np.isfinite(coordinates).all():
            raise ValueError("Snapshot coordinates must be finite.")

        old_projection = None
        if manifest_path.exists():
            previous = self.load(manifest_path)
            old_projection = previous.projection if previous is not None else None

        coordinate_path = self._root / f"{filename}.{uuid.uuid4().hex}.coordinates.npz"
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{filename}.", suffix=".coordinates.npz", dir=self._root
        )
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            with temporary.open("wb") as handle:
                np.savez_compressed(
                    handle,
                    coordinates=np.ascontiguousarray(coordinates),
                    metadata_ids=np.ascontiguousarray(metadata_ids),
                )
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(coordinate_path)
            stored_projection = replace(
                projection,
                coordinates_ref=self._relative_ref(coordinate_path),
            )
            pinboard_state = dict(snapshot.pinboard_state)
            if "last_discovery_key" in pinboard_state:
                pinboard_state["last_discovery_key"] = self._paths.key(
                    pinboard_state["last_discovery_key"], encode=True
                )
            json_store.write_json(
                manifest_path,
                replace(snapshot, projection=stored_projection,
                        discovery_key=self._paths.key(snapshot.discovery_key, encode=True),
                        discovery_ref=self._paths.encode(snapshot.discovery_ref) or None,
                        pinboard_state=pinboard_state).to_dict(),
            )
        except Exception:
            coordinate_path.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)

        if old_projection is not None:
            old_path = self._resolve_coordinate_ref(old_projection.coordinates_ref)
            if old_path is not None and old_path != coordinate_path:
                try:
                    old_path.unlink(missing_ok=True)
                except OSError:
                    pass
        return manifest_path

    def load(self, path: Path) -> DiscoverySnapshot | None:
        try:
            payload = json_store.read_json(Path(path))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        payload["discovery_key"] = self._paths.key(str(payload.get("discovery_key") or ""))
        if payload.get("discovery_ref"):
            payload["discovery_ref"] = self._paths.decode(payload["discovery_ref"])
        state = payload.get("states", {}).get("pinboard", {})
        if "last_discovery_key" in state:
            state["last_discovery_key"] = self._paths.key(state["last_discovery_key"])
        return DiscoverySnapshot.from_dict(payload)

    def load_coordinates(self, snapshot: DiscoverySnapshot) -> DiscoveryDataset | None:
        projection = snapshot.projection
        if projection is None:
            return None
        path = self._resolve_coordinate_ref(projection.coordinates_ref)
        if path is None or not path.exists():
            return None
        try:
            with np.load(path, allow_pickle=False) as payload:
                coordinates = np.asarray(payload["coordinates"], dtype=np.float32)
                metadata_ids = np.asarray(payload["metadata_ids"], dtype=np.int64).reshape(-1)
        except (OSError, ValueError, KeyError):
            return None
        if coordinates.ndim != 2 or coordinates.shape[1] != 2:
            return None
        if coordinates.shape[0] != metadata_ids.shape[0] or not np.isfinite(coordinates).all():
            return None
        return DiscoveryDataset(
            coordinates=np.ascontiguousarray(coordinates),
            metadata_ids=np.ascontiguousarray(metadata_ids),
        )

    def delete(self, path: Path) -> bool:
        manifest_path = Path(path).resolve()
        try:
            manifest_path.relative_to(self._root.resolve())
        except ValueError:
            return False
        if manifest_path.suffix.lower() != ".json" or not manifest_path.exists():
            return False
        snapshot = self.load(manifest_path)
        manifest_path.unlink()
        manifest_path.with_suffix(".png").unlink(missing_ok=True)
        if snapshot is not None and snapshot.projection is not None:
            coordinate_path = self._resolve_coordinate_ref(snapshot.projection.coordinates_ref)
            if coordinate_path is not None:
                coordinate_path.unlink(missing_ok=True)
        return True

    def _relative_ref(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self._project_root.resolve()).as_posix()
        except ValueError:
            return str(path.resolve())

    def _resolve_coordinate_ref(self, value: str) -> Path | None:
        path = Path(value)
        if not path.is_absolute():
            path = self._project_root / path
        resolved = path.resolve()
        try:
            resolved.relative_to(self._root.resolve())
        except ValueError:
            return None
        return resolved
