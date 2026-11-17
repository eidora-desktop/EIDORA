"""Resolve saved Discovery snapshots into plain figure data, without Qt widgets."""
from __future__ import annotations

import copy
from collections.abc import Mapping
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any
import zipfile

import numpy as np

from FiguresArea.domain import PlotCell
from DiscoveryArea.thumbnails.resolver import ThumbnailResolver


class FigureSnapshotService:
    def __init__(self, project=None, project_root: Path | None = None, *, transform_cache=None):
        self._transform_cache = transform_cache
        self._project = project
        self._project_root = project_root

    @staticmethod
    def _safe_dict(value):
        return dict(value) if isinstance(value, dict) else {}

    def needs_background(self, path: Path) -> bool:
        payload = self.snapshot_payload(path)
        refs = [payload.get("discovery_ref"), payload.get("arrangement_ref")]
        refs.append(self._safe_dict(payload.get("projection")).get("coordinates_ref"))
        if self._project_root:
            refs.append(str(self._project_root / "metadata" / "metadata.db"))
        for ref in refs:
            if not ref:
                continue
            source = Path(self.resolved_path_text(ref))
            try:
                if source.stat().st_size > 128 * 1024:
                    return True
                if source.suffix == ".npz":
                    with zipfile.ZipFile(source) as archive:
                        if sum(entry.file_size for entry in archive.infolist()) > 128 * 1024:
                            return True
            except (OSError, zipfile.BadZipFile):
                continue
        return False

    def prepare(self, snapshot_path: Path, *, cancelled=lambda: False) -> PlotCell:
        """Resolve one snapshot into renderer-ready data for preview or export."""

        payload = self.snapshot_payload(snapshot_path)
        return self._prepare_payload(snapshot_path, payload, cancelled=cancelled)

    def _prepare_payload(self, snapshot_path, payload, *, cancelled, load_metadata=True):
        coordinates, metadata_ids = self.coordinates_for_snapshot_payload(payload, cancelled=cancelled)
        if not len(coordinates):
            raise ValueError(
                f"Snapshot '{snapshot_path.stem}' has no plottable coordinates."
            )
        states = payload.get("states") if isinstance(payload.get("states"), dict) else {}
        state = self.snapshot_render_state(payload)
        if self._project is not None:
            from DiscoveryArea.snapshot_provenance import SnapshotMethodsService
            state["methods_report"] = SnapshotMethodsService().load(self._project, snapshot_path).to_payload()
        state["pinboard"] = self.snapshot_pinboard_state(
            states, metadata_ids=metadata_ids
        )
        state["figure_options"] = {
            "pins_visible": self.default_snapshot_pins_visible(
                self._safe_dict(state.get("layers")),
                self._safe_dict(state.get("pinboard")),
            )
        }
        state["preset_paths"] = self.snapshot_preset_paths(payload, snapshot_path)
        return PlotCell(
            row=0,
            column=0,
            snapshot_path=str(snapshot_path),
            snapshot_label=self.snapshot_label(payload, snapshot_path),
            coordinates=coordinates,
            metadata_ids=metadata_ids,
            metadata=self.metadata_for_ids(metadata_ids, cancelled=cancelled) if load_metadata else {},
            render_state=state,
        )

    def projection_needs_background(self, path: Path) -> bool:
        paths = [path]
        if self._project_root:
            paths.append(self._project_root / "metadata" / "metadata.db")
        for source in paths:
            try:
                if source.stat().st_size > 128 * 1024:
                    return True
            except OSError:
                continue
        return False

    def prepare_projection(self, path: Path, *, cancelled=lambda: False) -> PlotCell:
        coordinates, metadata_ids = self.load_discovery_coordinates(path, cancelled=cancelled)
        if not len(coordinates):
            raise ValueError(f"Discovery '{path.stem}' has no plottable coordinates.")
        if len(coordinates) > 2000:
            coordinates = np.asarray(coordinates, dtype=np.float64)
            coordinates.setflags(write=False)
        if len(metadata_ids) > 2000:
            metadata_ids = np.asarray(metadata_ids, dtype=np.int64)
            metadata_ids.setflags(write=False)
        return PlotCell(0, 0, coordinates=coordinates, metadata_ids=metadata_ids,
                        metadata=self.metadata_for_ids(metadata_ids, cancelled=cancelled))

    def prepare_with_overrides(self, path, overrides, *, cancelled=lambda: False):
        from FiguresArea.transform_service import FigureTransformService
        payload = self.snapshot_payload(path)
        prepared = self._prepare_payload(path, payload, cancelled=cancelled, load_metadata=False)
        source_projection = None
        if not payload.get('arrangement_ref') and not self._safe_dict(payload.get('projection')).get('coordinates_ref'):
            source_projection = (self.resolved_path_text(payload.get('discovery_ref')),
                                 prepared.coordinates, prepared.metadata_ids)
        if not hasattr(self, '_override_transforms'):
            self._override_transforms = FigureTransformService(self, cache=self._transform_cache)
        self._override_transforms.apply_overrides(prepared, overrides, cancelled=cancelled,
                                                load_metadata=False, source_projection=source_projection)
        prepared.metadata = self.metadata_for_ids(prepared.metadata_ids, cancelled=cancelled)
        return prepared

    @staticmethod
    def snapshot_label(payload: dict[str, Any], snapshot_path: Path) -> str:
        for key in ("label", "name"):
            value = payload.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return snapshot_path.stem

    def snapshot_payload(self, snapshot_path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def coordinates_for_snapshot_payload(self, payload: dict[str, Any], *, cancelled=lambda: False) -> tuple[list[tuple[float, float]], list[int]]:
        projection = payload.get("projection")
        if isinstance(projection, dict):
            coordinate_ref = str(projection.get("coordinates_ref") or "").strip()
            if coordinate_ref:
                coordinate_path = Path(coordinate_ref)
                if not coordinate_path.is_absolute() and self._project_root is not None:
                    coordinate_path = (self._project_root / coordinate_path).resolve()
                coordinates, metadata_ids = self.load_arrangement_coordinates(coordinate_path)
                if len(coordinates):
                    return coordinates, metadata_ids
        arrangement_ref = str(payload.get("arrangement_ref") or "").strip()
        if arrangement_ref:
            arrangement_path = Path(arrangement_ref)
            if not arrangement_path.is_absolute() and self._project_root is not None:
                arrangement_path = (self._project_root / arrangement_path).resolve()
            coordinates, metadata_ids = self.load_arrangement_coordinates(arrangement_path)
            if len(coordinates):
                return coordinates, metadata_ids
        discovery_ref = str(payload.get("discovery_ref") or "").strip()
        if not discovery_ref:
            return [], []
        discovery_path = Path(discovery_ref)
        if not discovery_path.is_absolute() and self._project_root is not None:
            discovery_path = (self._project_root / discovery_path).resolve()
        return self.load_discovery_coordinates(discovery_path, cancelled=cancelled)

    @staticmethod
    def load_arrangement_coordinates(path: Path) -> tuple[list[tuple[float, float]], list[int]]:
        try:
            with np.load(path, allow_pickle=False) as payload:
                coordinates = np.asarray(payload["coordinates"], dtype=np.float32)
                metadata_ids = np.asarray(payload["metadata_ids"], dtype=np.int64).reshape(-1)
        except (OSError, ValueError, KeyError):
            return [], []
        if coordinates.ndim != 2 or coordinates.shape[1] < 2 or coordinates.shape[0] != metadata_ids.shape[0]:
            return [], []
        if len(coordinates) > 2000:
            coordinates.setflags(write=False)
            metadata_ids.setflags(write=False)
            return coordinates[:, :2], metadata_ids
        return [tuple(map(float, point[:2])) for point in coordinates], [int(value) for value in metadata_ids]

    @staticmethod
    def load_discovery_coordinates(discovery_path: Path, *, cancelled=lambda: False):
        from DiscoveryArea.transforms import TransformCancelled
        if not discovery_path.exists():
            return [], []
        point_chunks, id_chunks = [], []
        try:
            with closing(sqlite3.connect(discovery_path)) as connection:
                connection.set_progress_handler(lambda: int(cancelled()), 1000)
                try:
                    cursor = connection.execute('SELECT metadata_id, x, y FROM coordinates ORDER BY metadata_id')
                    has_ids = True
                except sqlite3.Error:
                    cursor = connection.execute('SELECT x, y FROM coordinates ORDER BY rowid')
                    has_ids = False
                count = 0
                while rows := cursor.fetchmany(16384):
                    if cancelled():
                        raise TransformCancelled
                    points, ids = [], []
                    for row in rows:
                        try:
                            identity = int(row[0]) if has_ids else count + len(ids)
                            point = (float(row[-2]), float(row[-1]))
                        except (ValueError, TypeError, IndexError):
                            continue
                        ids.append(identity)
                        points.append(point)
                    count += len(ids)
                    point_chunks.append(np.asarray(points, dtype=np.float64).reshape(-1, 2))
                    id_chunks.append(np.asarray(ids, dtype=np.int64))
        except sqlite3.Error:
            if cancelled():
                raise TransformCancelled
            return [], []
        if not point_chunks:
            return [], []
        coordinates, ids = np.concatenate(point_chunks), np.concatenate(id_chunks)
        if len(ids) <= 2000:
            return [tuple(point) for point in coordinates.tolist()], ids.tolist()
        coordinates.setflags(write=False)
        ids.setflags(write=False)
        return coordinates, ids

    def snapshot_render_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        states = payload.get("states") if isinstance(payload.get("states"), dict) else {}
        refs = payload.get("refs") if isinstance(payload.get("refs"), dict) else {}
        appearance = self.payload_from_ref(refs.get("appearance")) or self._safe_dict(states.get("appearance"))
        filters = self.payload_from_ref(refs.get("filters")) or self._safe_dict(states.get("filters"))
        camera_payload = self.payload_from_ref(refs.get("camera"))
        camera = (
            self._safe_dict(camera_payload.get("camera"))
            if isinstance(camera_payload, dict)
            else self._safe_dict(states.get("camera"))
        )
        layers = self._safe_dict(states.get("layers"))
        pinboard = self.snapshot_pinboard_state(states, metadata_ids=[])
        state = {
            "appearance": appearance,
            "filters": filters,
            "camera": camera,
            "layers": layers,
            "pinboard": pinboard,
            "figure_options": {"pins_visible": self.default_snapshot_pins_visible(layers, pinboard)},
        }
        projection = payload.get("projection")
        if isinstance(projection, dict):
            state["snapshot_projection"] = copy.deepcopy(projection)
        return state

    def snapshot_pinboard_state(self, states: dict[str, Any], *, metadata_ids: list[int]) -> dict[str, Any]:
        pinboard = self._safe_dict(states.get("pinboard"))
        annotations = self.normalized_snapshot_pin_annotations(pinboard.get("annotations"))
        if len(metadata_ids):
            annotations = self.pins_with_resolved_thumbnails(
                annotations,
                pinboard=pinboard,
                metadata_ids=metadata_ids,
            )
        pinboard["annotations"] = annotations
        return pinboard

    @staticmethod
    def default_snapshot_pins_visible(layers: dict[str, Any], pinboard: dict[str, Any]) -> bool:
        if "pins_visible" in layers:
            return bool(layers.get("pins_visible"))
        layer_state = pinboard.get("layer_state")
        if isinstance(layer_state, dict) and "pins_visible" in layer_state:
            return bool(layer_state.get("pins_visible"))
        return True

    @staticmethod
    def normalized_snapshot_pin_annotations(value: object) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        output: list[dict[str, Any]] = []
        for entry in value:
            if not isinstance(entry, dict) or not bool(entry.get("visible", True)):
                continue
            annotation_id = str(entry.get("annotation_id") or "").strip()
            if not annotation_id:
                continue
            try:
                metadata_id = int(entry.get("metadata_id"))
            except (TypeError, ValueError):
                continue
            point = entry.get("point")
            box_anchor = entry.get("box_anchor")
            offset = entry.get("screen_offset", [48.0, -36.0])
            if not (isinstance(point, (list, tuple)) and len(point) >= 2):
                continue
            if not (isinstance(offset, (list, tuple)) and len(offset) >= 2):
                offset = [48.0, -36.0]
            try:
                point_values = [float(point[0]), float(point[1])]
                offset_values = [float(offset[0]), float(offset[1])]
            except (TypeError, ValueError):
                continue
            box_anchor_values = None
            if isinstance(box_anchor, (list, tuple)) and len(box_anchor) >= 2:
                try:
                    box_anchor_values = [float(box_anchor[0]), float(box_anchor[1])]
                except (TypeError, ValueError):
                    box_anchor_values = None
            lines = entry.get("lines")
            payload = {
                "annotation_id": annotation_id,
                "metadata_id": metadata_id,
                "point": point_values,
                "screen_offset": offset_values,
                "visible": True,
                "created_at": str(entry.get("created_at") or ""),
                "lines": [str(line) for line in lines] if isinstance(lines, list) else [],
            }
            if box_anchor_values is not None:
                payload["box_anchor"] = box_anchor_values
            output.append(payload)
        return output

    def pins_with_resolved_thumbnails(
        self,
        annotations: list[dict[str, Any]],
        *,
        pinboard: dict[str, Any],
        metadata_ids: list[int],
    ) -> list[dict[str, Any]]:
        if self._project is None or not annotations:
            return annotations
        style = self._safe_dict(pinboard.get("style"))
        if not bool(style.get("thumbnail_visible", False)):
            return annotations
        variant_id = str(pinboard.get("thumbnail_variant_id") or "none").strip()
        if not variant_id or variant_id == "none":
            return annotations
        try:
            thumbnail_size = int(style.get("thumbnail_size", 96))
        except (TypeError, ValueError):
            thumbnail_size = 96
        resolver = ThumbnailResolver(self._project)
        wanted = {int(value) for value in metadata_ids}
        resolved: list[dict[str, Any]] = []
        for annotation in annotations:
            if int(annotation["metadata_id"]) not in wanted:
                resolved.append(annotation)
                continue
            ref = resolver.resolve(int(annotation["metadata_id"]), variant_id=variant_id, target_px=thumbnail_size)
            if ref is not None:
                annotation = dict(annotation)
                annotation["thumbnail_path"] = str(ref.path)
            resolved.append(annotation)
        return resolved

    def payload_from_ref(self, value: object) -> dict[str, Any] | None:
        text = str(value or "").strip()
        if not text:
            return None
        path = Path(text)
        if not path.is_absolute() and self._project_root is not None:
            path = (self._project_root / path).resolve()
        return self.payload_from_path(path)

    def payload_from_path(self, path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def snapshot_preset_paths(self, payload: dict[str, Any], snapshot_path: Path) -> dict[str, str | None]:
        refs = payload.get("refs") if isinstance(payload.get("refs"), dict) else {}
        return {
            "snapshot": str(snapshot_path),
            "discovery": self.resolved_path_text(payload.get("discovery_ref")),
            "transform": self.resolved_path_text(refs.get("transform")),
            "camera": self.resolved_path_text(refs.get("camera")),
            "appearance": self.resolved_path_text(refs.get("appearance")),
            "filter": self.resolved_path_text(refs.get("filters")),
        }

    def resolved_path_text(self, value: object) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        path = Path(text)
        if not path.is_absolute() and self._project_root is not None:
            path = (self._project_root / path).resolve()
        return str(path)

    def metadata_for_ids(self, metadata_ids: list[int], *, cancelled=lambda: False) -> Mapping[int, Mapping[str, str]]:
        if self._project_root is None or not len(metadata_ids):
            return {}
        database = self._project_root / "metadata" / "metadata.db"
        if not database.exists():
            return {}
        from FiguresArea.compact_metadata import CompactMetadata, MetadataColumnBuilder
        from DiscoveryArea.transforms import TransformCancelled
        ordered_ids = np.unique(np.asarray(metadata_ids, dtype=np.int64))
        present = np.zeros(len(ordered_ids), dtype=bool)
        builders = None
        try:
            with closing(sqlite3.connect(database)) as connection:
                connection.set_progress_handler(lambda: int(cancelled()), 1000)
                for offset in range(0, len(ordered_ids), 500):
                    if cancelled():
                        raise TransformCancelled
                    batch = ordered_ids[offset:offset + 500].tolist()
                    placeholders = ','.join('?' for _ in batch)
                    cursor = connection.execute(f'SELECT * FROM metadata WHERE id IN ({placeholders})', batch)
                    if builders is None:
                        columns = [column[0] for column in cursor.description]
                        id_column = next(index for index, name in enumerate(columns) if name.casefold() == 'id')
                        names = sorted(columns)
                        indices = [columns.index(name) for name in names]
                        builders = [MetadataColumnBuilder(len(ordered_ids)) for _ in names]
                    rows, identities = [], []
                    for row in cursor:
                        try:
                            identity = int(row[id_column])
                        except (KeyError, TypeError, ValueError, IndexError):
                            continue
                        rows.append(row)
                        identities.append(identity)
                    positions = np.searchsorted(ordered_ids, identities)
                    present[positions] = True
                    for builder, column in zip(builders, indices):
                        builder.add_batch(positions, (row[column] for row in rows))
        except sqlite3.Error:
            if cancelled():
                raise TransformCancelled
            return {}
        if not present.any():
            return {}
        arrays = {'metadata_row_ids': ordered_ids if present.all() else ordered_ids[present],
                  'metadata_columns': np.frombuffer(json.dumps(names, ensure_ascii=False).encode('utf-8'), np.uint8)}
        for index, builder in enumerate(builders):
            if cancelled():
                raise TransformCancelled
            codes, offsets, values = builder.finish(present)
            arrays.update({f'meta_{index}_codes': codes, f'meta_{index}_offsets': offsets,
                           f'meta_{index}_values': values})
        for array in arrays.values():
            array.setflags(write=False)
        return CompactMetadata(None, arrays['metadata_row_ids'], names, arrays)
