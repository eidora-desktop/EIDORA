"""Resolve saved Discovery snapshots into plain figure data, without Qt widgets."""
from __future__ import annotations

import copy
from dataclasses import replace
from collections.abc import Mapping
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any
import zipfile

import numpy as np

from FiguresArea.domain import PlotCell
from FiguresArea.data_repository import FigureDataRepository, FigureDataCache
from DiscoveryArea.snapshot_settings import snapshot_component_states
from DiscoveryArea.slicing import slicer_selection
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
        if "figure_snapshot_copy" in payload:
            return True
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
        if isinstance(payload.get("figure_snapshot_copy"), dict):
            return self._load_snapshot_copy(snapshot_path, payload["figure_snapshot_copy"])
        prepared = self._prepare_payload(snapshot_path, payload, cancelled=cancelled)
        self._prepare_style_metadata(prepared, cancelled=cancelled)
        self._resolve_slicer_membership(prepared, cancelled=cancelled)
        self.capture_snapshot_base(prepared, payload)
        return prepared

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
        frozen = isinstance(payload.get("figure_snapshot_copy"), dict)
        transformed = "transform" in (overrides or {}).get("state", {})
        source_projection = None
        if frozen:
            prepared = self._load_snapshot_copy(path, payload["figure_snapshot_copy"])
        elif not transformed:
            prepared = self.prepare(path, cancelled=cancelled)
        else:
            # Legacy animations prepare only their final metadata population.
            prepared = self._prepare_payload(path, payload, cancelled=cancelled, load_metadata=False)
            if not payload.get('arrangement_ref') and not self._safe_dict(payload.get('projection')).get('coordinates_ref'):
                source_projection = (self.resolved_path_text(payload.get('discovery_ref')),
                                     prepared.coordinates, prepared.metadata_ids)
        if not hasattr(self, '_override_transforms'):
            self._override_transforms = FigureTransformService(self, cache=self._transform_cache)
        before_ids = prepared.metadata_ids
        self._override_transforms.apply_overrides(prepared, overrides, cancelled=cancelled,
                                                load_metadata=False, source_projection=source_projection)
        if (transformed and not frozen) or prepared.metadata_ids is not before_ids:
            prepared.metadata = self.metadata_for_ids(prepared.metadata_ids, cancelled=cancelled)
        metadata_override = prepared.render_state.get("preset_metadata")
        frozen_metadata = prepared.metadata if frozen else None
        if isinstance(metadata_override, dict):
            saved = PlotCell(0, 0, data_ref=metadata_override.get("data_ref"),
                             metadata={int(key): row for key, row in metadata_override.get("metadata", {}).items()})
            FigureDataCache().hydrate(saved)
            frozen_metadata = saved.metadata
        self._prepare_style_metadata(prepared, cancelled=cancelled, frozen_metadata=frozen_metadata)
        selection = prepared.render_state.get("slicer_selection", {})
        if "matched_metadata_ids" not in selection:
            self._resolve_slicer_membership(prepared, cancelled=cancelled)
        return prepared

    def archive_snapshot(self, path: Path, directory: Path) -> Path:
        """Import an immutable animation source on a worker, including frozen metadata."""
        from FiguresArea.repository import FigureTabsRepository
        prepared = self.prepare(path)
        repository = FigureTabsRepository(self._project_root / "state" if self._project_root else directory.parent)
        return repository.save_snapshot_copy(path, prepared.render_state["snapshot_base"], directory,
                                             portable=self._project_root is not None)

    def _load_snapshot_copy(self, path, base):
        from FiguresArea.repository import FigureTabsRepository
        from project.portable_paths import ProjectPaths
        base = copy.deepcopy(base)
        if self._project_root is not None:
            FigureTabsRepository.render_state_paths({"snapshot_base": base}, ProjectPaths(self._project_root).decode)
        prepared = PlotCell(0, 0, snapshot_path=str(path), snapshot_label=base.get("name"),
                            coordinates=base.get("coordinates", []), metadata_ids=base.get("metadata_ids", []),
                            metadata={int(key): row for key, row in base.get("metadata", {}).items()},
                            data_ref=base.get("data_ref"), point_count=base.get("point_count", 0),
                            render_state=copy.deepcopy(base["state"]))
        FigureDataCache().hydrate(prepared)
        prepared.render_state["snapshot_base"] = base
        prepared.render_state["component_sources"] = {}
        return prepared

    def _resolve_slicer_membership(self, prepared: PlotCell, *, cancelled) -> None:
        from DiscoveryArea.metadata_access import DiscoveryMetadataAccess
        from DiscoveryArea.multiple_values import membership_mask
        from DiscoveryArea.transforms import TransformCancelled
        from project.domain import Project

        if cancelled():
            raise TransformCancelled
        selection = prepared.render_state.get("slicer_selection")
        recipe = self._safe_dict(prepared.render_state.get("slicer"))
        column = str((selection if isinstance(selection, dict) else recipe).get("column") or "")
        if not column or (selection is None and not recipe.get("enabled", bool(column))):
            prepared.render_state["slicer_selection"] = {}
            return
        access = None
        if self._project_root is not None:
            project = self._project or Project(name=self._project_root.name, root=self._project_root, schema_version=1)
            access = DiscoveryMetadataAccess(project)
            column = access.canonical_column_name(column)
            if column not in access.column_names():
                raise ValueError(f"Slicer column is unavailable: {column}")
        if not isinstance(selection, dict):
            values = access.distinct_values(column) if access else [row.get(column, "") for row in prepared.metadata.values()]
            selection = slicer_selection(dict(recipe, column=column), values)
        selection = copy.deepcopy(selection)
        selection["column"] = column
        ids = np.asarray(prepared.metadata_ids, dtype=np.int64)
        if access:
            values, _ = access.values_for_ids(ids, columns={column}, numeric_columns=set())
            multiple = access.multiple_values_for_ids(ids, columns={column})
            scalars = ids.astype(str) if column == "id" else values[column]
            mask = membership_mask(scalars, selection.get("values", []), multiple.get(column))
        else:
            scalars = ids.astype(str) if column == "id" else np.asarray([prepared.metadata.get(int(key), {}).get(column, "") for key in ids])
            mask = membership_mask(scalars, selection.get("values", []))
        if cancelled():
            raise TransformCancelled
        selection["matched_metadata_ids"] = ids[mask].tolist()
        prepared.render_state["slicer_selection"] = selection

    def _prepare_style_metadata(self, cell: PlotCell, *, cancelled, kind=None, frozen_metadata=None) -> None:
        """Freeze resolved Discovery fields used by a style, including group fields."""
        from DiscoveryArea.canvas.style_preparation import color_style_metadata_columns, custom_color_labels
        from DiscoveryArea.filters import filters_from_state_payload, required_filter_columns
        from DiscoveryArea.metadata_access import DiscoveryMetadataAccess
        from DiscoveryArea.transforms import TransformCancelled
        from FiguresArea.compact_metadata import CompactMetadata, MetadataColumnBuilder, encode_metadata
        from project.domain import Project

        appearance = self._safe_dict(cell.render_state.get("appearance"))
        styles = [appearance.setdefault(key, {}) for key in ("point_colors", "border")] if kind != "filter" else []
        columns, _ = required_filter_columns(filters_from_state_payload(self._safe_dict(cell.render_state.get("filters")))) if kind != "appearance" else (set(), set())
        for style in styles:
            columns.update(color_style_metadata_columns(style)[0])
        if not columns:
            return
        FigureDataCache().hydrate(cell)
        existing_names = (set(cell.metadata.names) if isinstance(cell.metadata, CompactMetadata)
                          else {key for row in cell.metadata.values() for key in row})
        if columns <= existing_names and not any(style.get("custom") for style in styles) and frozen_metadata is None:
            return
        ids = np.unique(np.asarray(cell.metadata_ids, dtype=np.int64))
        access = None
        if frozen_metadata is None and self._project_root is not None and (self._project_root / "metadata" / "metadata.db").exists():
            project = self._project or Project(name=self._project_root.name, root=self._project_root, schema_version=1)
            access = DiscoveryMetadataAccess(project)
        resolved = {}
        if access:
            available = set(access.column_names())
            requested = {column for column in columns if access.canonical_column_name(column) in available}
            resolved, _ = access.values_for_ids(ids, columns=requested, numeric_columns=set())
            if "id" in columns:
                resolved["id"] = ids.astype(str)
        source = frozen_metadata if frozen_metadata is not None else cell.metadata
        for column in columns - resolved.keys():
            resolved[column] = (source.map_values(column, ids, str) if isinstance(source, CompactMetadata)
                                else np.asarray([str(source.get(int(key), {}).get(column, "")) for key in ids]))
        for channel, style in zip(("point_colors", "border"), styles):
            if style.get("custom") and style.get("column"):
                style["resolved_column"] = f"__figure_{channel}__"
                resolved[style["resolved_column"]] = custom_color_labels(len(ids), style, resolved)
        cell.render_state["appearance"] = appearance
        if cancelled():
            raise TransformCancelled
        # Retain unmodified columns in their encoded representation. Only new or
        # replaced fields are encoded, without materializing one dict per item.
        previous = encode_metadata(cell.metadata)
        previous_ids = previous["metadata_row_ids"]
        names = json.loads(previous["metadata_columns"].tobytes())
        names = sorted(set(names) | resolved.keys())
        arrays = {"metadata_row_ids": ids,
                  "metadata_columns": np.frombuffer(json.dumps(names).encode("utf-8"), np.uint8)}
        previous_names = json.loads(previous["metadata_columns"].tobytes())
        positions = np.searchsorted(ids, previous_ids)
        present = positions < len(ids)
        if len(ids):
            present &= ids[np.minimum(positions, len(ids) - 1)] == previous_ids
        for index, name in enumerate(names):
            if cancelled():
                raise TransformCancelled
            if name in resolved:
                builder = MetadataColumnBuilder(len(ids))
                builder.add_batch(np.arange(len(ids)), resolved[name])
                codes, offsets, values = builder.finish(np.ones(len(ids), dtype=bool))
            else:
                old_index = previous_names.index(name)
                codes = np.full(len(ids), -1, dtype=np.int32)
                codes[positions[present]] = previous[f"meta_{old_index}_codes"][present]
                offsets, values = previous[f"meta_{old_index}_offsets"], previous[f"meta_{old_index}_values"]
            arrays.update({f"meta_{index}_codes": codes, f"meta_{index}_offsets": offsets,
                           f"meta_{index}_values": values})
        cell.metadata = CompactMetadata(None, ids, names, arrays)
        cell.data_ref, cell.data_key, cell.data_sources = None, "", ()

    def capture_snapshot_base(self, cell: PlotCell, payload: dict) -> None:
        """Keep reset data owned by the figure, independent of the snapshot file."""
        reference = (FigureDataRepository(self._project_root / "state" / "figure_data").store(cell)
                     if self._project_root is not None else None)
        base = {
            "name": cell.snapshot_label,
            "created_at": payload.get("created_at", ""),
            "state": copy.deepcopy(cell.render_state),
            "data_ref": reference,
            "point_count": len(cell.coordinates),
        }
        if reference is None:
            base.update(coordinates=np.asarray(cell.coordinates).tolist(),
                        metadata_ids=np.asarray(cell.metadata_ids).tolist(),
                        metadata={str(key): dict(value) for key, value in cell.metadata.items()})
        cell.render_state["snapshot_base"] = base
        cell.render_state["component_sources"] = {}
        if reference:
            cell.data_ref = reference

    def prepare_component(self, cell: PlotCell, kind: str, value: str, *, cancelled=lambda: False) -> PlotCell:
        """Apply a copied recipe, or restore the figure's captured snapshot component."""
        prepared = replace(cell, render_state=copy.deepcopy(cell.render_state))
        state = prepared.render_state
        key = {"filter": "filters"}.get(kind, kind)
        base = state.get("snapshot_base", {})
        if value == "__snapshot__":
            if not base:
                raise ValueError("This older plot has no captured reset state. Drop its snapshot onto the plot again.")
            baseline = base["state"]
            state[key] = copy.deepcopy(baseline.get(key, {}))
            if kind == "slicer":
                state["slicer_selection"] = copy.deepcopy(baseline.get("slicer_selection", {}))
            if kind in ("transform", "discovery"):
                state["transform"] = copy.deepcopy(baseline.get("transform", {}))
                state.setdefault("component_sources", {}).pop("discovery", None)
                state["component_sources"].pop("transform", None)
                prepared.coordinates = base.get("coordinates", [])
                prepared.metadata_ids = base.get("metadata_ids", [])
                prepared.metadata = {int(key): row for key, row in base.get("metadata", {}).items()}
                prepared.data_ref = base.get("data_ref")
                prepared.point_count = base.get("point_count", 0)
                prepared.data_key, prepared.data_sources = "", ()
                state["snapshot_projection"] = copy.deepcopy(baseline.get("snapshot_projection", {}))
                state["preset_paths"]["discovery"] = baseline.get("preset_paths", {}).get("discovery")
                state["preset_paths"]["transform"] = None
                FigureDataCache().hydrate(prepared)
                metadata_override = state.get("preset_metadata")
                if isinstance(metadata_override, dict):
                    saved = PlotCell(0, 0, data_ref=metadata_override.get("data_ref"),
                                     metadata={int(key): row for key, row in metadata_override.get("metadata", {}).items()})
                    FigureDataCache().hydrate(saved)
                    self._prepare_style_metadata(prepared, cancelled=cancelled, frozen_metadata=saved.metadata)
            state.setdefault("component_sources", {}).pop(kind, None)
            path = baseline.get("preset_paths", {}).get("discovery") if kind == "discovery" else None
        else:
            payload = {} if value == "__none__" else self.payload_from_path(Path(value))
            if payload is None:
                raise ValueError(f"Could not load {kind} preset '{Path(value).stem}'.")
            state[key] = copy.deepcopy(payload.get("camera", payload) if kind == "camera" else payload)
            path = None if value == "__none__" else value
            state.setdefault("component_sources", {})[kind] = "none" if value == "__none__" else "preset"
            if kind == "slicer":
                state.pop("slicer_selection", None)
                self._resolve_slicer_membership(prepared, cancelled=cancelled)
        state.setdefault("preset_paths", {})[kind] = path
        if kind in ("appearance", "filter"):
            frozen_metadata = None
            if value == "__snapshot__":
                frozen = PlotCell(0, 0, data_ref=base.get("data_ref"),
                                  metadata={int(key): row for key, row in base.get("metadata", {}).items()})
                FigureDataCache().hydrate(frozen)
                frozen_metadata = frozen.metadata
            self._prepare_style_metadata(prepared, cancelled=cancelled, kind=kind, frozen_metadata=frozen_metadata)
            reference = (FigureDataRepository(self._project_root / "state" / "figure_data").store(prepared)
                         if self._project_root is not None else None)
            state["preset_metadata"] = ({"data_ref": reference} if reference else
                {"metadata": {str(key): dict(row) for key, row in prepared.metadata.items()}})
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
        states = snapshot_component_states(payload, self.payload_from_ref)
        appearance = self._safe_dict(states.get("appearance"))
        filters = self._safe_dict(states.get("filters"))
        camera = self._safe_dict(states.get("camera"))
        layers = self._safe_dict(states.get("layers"))
        pinboard = self.snapshot_pinboard_state(states, metadata_ids=[])
        state = {
            "appearance": appearance,
            "slicer": self._safe_dict(states.get("slicer")),
            "transform": {"preset_version": 2, "transform": self._safe_dict(states.get("transform"))},
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
        return {
            "snapshot": str(snapshot_path),
            "discovery": self.resolved_path_text(payload.get("discovery_ref")),
            "transform": None,
            "camera": None,
            "appearance": None,
            "filter": None,
            "slicer": None,
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
