from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
import hashlib
from pathlib import Path
from typing import Any

import numpy as np


@dataclass(frozen=True)
class DiscoverySnapshotRefs:
    appearance: str | None = None
    filters: str | None = None
    camera: str | None = None
    slicer: str | None = None
    transform: str | None = None


@dataclass(frozen=True)
class DiscoverySnapshotProjection:
    coordinates_ref: str
    coordinate_space: str = "original"
    source_fingerprint: str = ""
    arrangement_identity: str = "original"
    transform: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "coordinates_ref": self.coordinates_ref,
            "coordinate_space": (
                "transformed" if self.coordinate_space == "transformed" else "original"
            ),
            "source_fingerprint": self.source_fingerprint,
            "arrangement_identity": self.arrangement_identity or "original",
            "transform": dict(self.transform),
        }

    @classmethod
    def from_dict(cls, payload: object) -> "DiscoverySnapshotProjection | None":
        if not isinstance(payload, dict):
            return None
        coordinates_ref = _safe_opt_str(payload.get("coordinates_ref"))
        if coordinates_ref is None:
            return None
        return cls(
            coordinates_ref=coordinates_ref,
            coordinate_space=(
                "transformed"
                if str(payload.get("coordinate_space") or "").lower() == "transformed"
                else "original"
            ),
            source_fingerprint=str(payload.get("source_fingerprint") or ""),
            arrangement_identity=str(payload.get("arrangement_identity") or "original"),
            transform=_safe_dict(payload.get("transform")),
        )


@dataclass(frozen=True)
class DiscoveryMetadataSignature:
    count: int
    checksum: str


@dataclass(frozen=True)
class DiscoverySnapshot:
    name: str
    created_at: str
    discovery_key: str
    discovery_ref: str | None = None
    arrangement_ref: str | None = None
    projection: DiscoverySnapshotProjection | None = None
    thumbnail: str | None = None
    refs: DiscoverySnapshotRefs = field(default_factory=DiscoverySnapshotRefs)
    appearance_state: dict[str, Any] = field(default_factory=dict)
    filters_state: dict[str, Any] = field(default_factory=dict)
    camera_state: dict[str, Any] = field(default_factory=dict)
    slicer_state: dict[str, Any] = field(default_factory=dict)
    layer_state: dict[str, Any] = field(default_factory=dict)
    pinboard_state: dict[str, Any] = field(default_factory=dict)
    transform_state: dict[str, Any] = field(default_factory=dict)
    methods_report: dict[str, Any] = field(default_factory=dict)
    metadata_signature: DiscoveryMetadataSignature = field(
        default_factory=lambda: DiscoveryMetadataSignature(count=0, checksum="")
    )

    def to_dict(self) -> dict[str, Any]:
        states = {
            "appearance": dict(self.appearance_state),
            "filters": dict(self.filters_state),
            "camera": dict(self.camera_state),
            "slicer": dict(self.slicer_state),
            "layers": dict(self.layer_state),
            "pinboard": dict(self.pinboard_state),
        }
        payload = {
            "methods_report": dict(self.methods_report),
            "name": self.name,
            "created_at": self.created_at,
            "discovery_key": self.discovery_key,
            "discovery_ref": self.discovery_ref,
            "thumbnail": self.thumbnail,
            "refs": {
                "appearance": self.refs.appearance,
                "filters": self.refs.filters,
                "camera": self.refs.camera,
                "slicer": self.refs.slicer,
                "transform": self.refs.transform,
            },
            "states": states,
            "metadata_signature": {
                "count": int(self.metadata_signature.count),
                "checksum": str(self.metadata_signature.checksum),
            },
        }
        if self.projection is not None:
            payload["projection"] = self.projection.to_dict()
        else:
            payload["arrangement_ref"] = self.arrangement_ref
            states["transform"] = dict(self.transform_state)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DiscoverySnapshot | None":
        if not isinstance(payload, dict):
            return None
        refs_raw = payload.get("refs")
        refs_dict = refs_raw if isinstance(refs_raw, dict) else {}
        states_raw = payload.get("states")
        states = states_raw if isinstance(states_raw, dict) else {}
        sig_raw = payload.get("metadata_signature")
        sig = sig_raw if isinstance(sig_raw, dict) else {}
        created = str(payload.get("created_at") or "").strip() or dt.datetime.now().isoformat(timespec="seconds")
        discovery_key = str(payload.get("discovery_key") or "").strip() or "random"
        return cls(
            name=str(payload.get("name") or "snapshot"),
            methods_report=_safe_dict(payload.get("methods_report")),
            created_at=created,
            discovery_key=discovery_key,
            discovery_ref=_safe_opt_str(payload.get("discovery_ref")),
            arrangement_ref=_safe_opt_str(payload.get("arrangement_ref")),
            projection=DiscoverySnapshotProjection.from_dict(payload.get("projection")),
            thumbnail=_safe_opt_str(payload.get("thumbnail")),
            refs=DiscoverySnapshotRefs(
                appearance=_safe_opt_str(refs_dict.get("appearance")),
                filters=_safe_opt_str(refs_dict.get("filters")),
                camera=_safe_opt_str(refs_dict.get("camera")),
                slicer=_safe_opt_str(refs_dict.get("slicer")),
                transform=_safe_opt_str(refs_dict.get("transform")),
            ),
            appearance_state=_safe_dict(states.get("appearance")),
            filters_state=_safe_dict(states.get("filters")),
            camera_state=_safe_dict(states.get("camera")),
            slicer_state=_safe_dict(states.get("slicer")),
            layer_state=_safe_dict(states.get("layers")),
            pinboard_state=_safe_dict(states.get("pinboard")),
            transform_state=(
                _safe_dict(states.get("transform"))
                or _safe_dict(
                    payload.get("projection", {}).get("transform")
                    if isinstance(payload.get("projection"), dict)
                    else None
                )
            ),
            metadata_signature=DiscoveryMetadataSignature(
                count=int(sig.get("count") or 0),
                checksum=str(sig.get("checksum") or ""),
            ),
        )


def metadata_signature_for_ids(metadata_ids: np.ndarray) -> DiscoveryMetadataSignature:
    ids = np.asarray(metadata_ids, dtype=np.int64).reshape(-1)
    if ids.size == 0:
        return DiscoveryMetadataSignature(count=0, checksum="")
    checksum = hashlib.sha1(np.ascontiguousarray(ids).tobytes()).hexdigest()
    return DiscoveryMetadataSignature(count=int(ids.size), checksum=checksum)


def relative_path_or_none(path: Path | None, project_root: Path) -> str | None:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _safe_dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _safe_opt_str(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


ProjectionSnapshotRefs = DiscoverySnapshotRefs
ProjectionMetadataSignature = DiscoveryMetadataSignature
ProjectionSnapshot = DiscoverySnapshot
metadata_signature_for_projection_ids = metadata_signature_for_ids
