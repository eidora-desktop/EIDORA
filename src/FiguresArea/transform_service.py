"""Background preparation of figure transformations and original projections."""
import copy
from pathlib import Path

import numpy as np

from FiguresArea.cache_budget import ByteCache
from FiguresArea.domain import PlotCell
from DiscoveryArea.method_catalog import DiscoveryMethodCatalog
from DiscoveryArea.transforms import TransformCancelled, TransformSettings, source_fingerprint, transform_coordinates


class FigureTransformService:
    def __init__(self, loader, max_bytes=128 * 1024 * 1024, *, cache=None):
        self.loader = loader
        self._cache = cache if cache is not None else ByteCache(max_bytes)

    @property
    def _bytes(self):
        return self._cache.retained_bytes

    def method_id(self, path):
        root = self.loader._project_root
        if root is None:
            return ''
        target = Path(path).resolve()
        for preset in sorted((root / 'presets' / 'discovery').glob('*.json')):
            payload = self.loader.payload_from_path(preset) or {}
            if not payload.get('discovery_file'):
                continue
            candidate = Path(payload['discovery_file'])
            if not candidate.is_absolute():
                candidate = root / candidate
            if candidate.resolve() == target:
                return str(payload.get('method_id') or payload.get('type') or payload.get('algorithm') or '').strip().lower()
        return 'random' if target.name == 'random_discovery.sqlite' else ''

    def supports_transform(self, path):
        method = self.method_id(path)
        definition = DiscoveryMethodCatalog().get('tsne' if method == 't-sne' else method)
        return bool(definition and definition.supports_arrangement_transform)

    def prepare(self, state, paths, preset=None, *, recipe=None, cancelled=lambda: False,
                load_metadata=True, source_projection=None):
        def check():
            if cancelled():
                raise TransformCancelled
        check()
        source = paths.get('discovery')
        if not source:
            raise ValueError('Select a Projection before applying a Transformation.')
        settings = None
        if preset is not None:
            payload = self.loader.payload_from_path(Path(preset))
            if payload is None or int(payload.get('preset_version', 1)) not in (1, 2):
                raise ValueError('Missing or unsupported transformation preset.')
            if not self.supports_transform(source):
                raise ValueError('Transformations are unavailable for this coordinate-truthful projection.')
            settings = TransformSettings.from_preset_dict(payload.get('transform'))
        elif recipe is not None and self.supports_transform(source):
            settings = TransformSettings.from_preset_dict(recipe)
        if source_projection is not None and Path(source_projection[0]).resolve() == Path(source).resolve():
            coordinates, ids = source_projection[1:]
        else:
            coordinates, ids = self.loader.load_discovery_coordinates(Path(source), cancelled=cancelled)
        if not len(coordinates):
            raise ValueError('The selected Projection has no plottable coordinates.')
        coordinates = np.asarray(coordinates, dtype=np.float64)
        ids = np.asarray(ids, dtype=np.int64)
        coordinates.setflags(write=False)
        ids.setflags(write=False)
        prepared = PlotCell(0, 0, coordinates=coordinates, metadata_ids=ids)
        check()
        if settings is not None:
            coordinates = np.asarray(prepared.coordinates, dtype=np.float32)
            ids = np.asarray(prepared.metadata_ids, dtype=np.int64)
            key = (source_fingerprint(coordinates, ids), settings.snap_percent,
                   settings.grid_aspect_preset, round(settings.minimum_spacing, 8))
            entry = self._cache.get(key)
            if entry is None:
                result = transform_coordinates(coordinates, ids, settings, cancelled=cancelled)
                arrays = (result.coordinates, result.metadata_ids)
                size = sum(array.nbytes for array in arrays)
                for array in arrays:
                    array.setflags(write=False)
                entry = (*arrays, size)
                self._cache.put(key, entry, size)
            prepared.coordinates, prepared.metadata_ids = entry[:2]
        check()
        if load_metadata:
            prepared.metadata = self.loader.metadata_for_ids(prepared.metadata_ids, cancelled=cancelled)
        prepared.render_state = copy.deepcopy(state)
        prepared.render_state.pop('snapshot_projection', None)
        paths = dict(paths, snapshot=None, transform=str(preset) if preset else None)
        prepared.render_state['preset_paths'] = paths
        if settings is None:
            prepared.render_state.pop('transform', None)
        else:
            prepared.render_state['transform'] = {'preset_version': 2, 'transform': settings.to_preset_dict()}
        reanchor_pins(prepared, cancelled=cancelled)
        check()
        return prepared

    def apply_overrides(
        self, plot_cell, overrides, *, cancelled=lambda: False, load_metadata=True, source_projection=None
    ) -> None:
        if not isinstance(overrides, dict):
            return
        state_overrides = overrides.get("state")
        if isinstance(state_overrides, dict):
            for key, value in state_overrides.items():
                if key != "pinboard":
                    plot_cell.render_state[str(key)] = copy.deepcopy(value)
            if "transform" in state_overrides:
                self.apply_state(plot_cell, cancelled=cancelled, load_metadata=load_metadata,
                                 source_projection=source_projection)
        pin_overrides = overrides.get("pins")
        if not isinstance(pin_overrides, dict):
            return
        pinboard = plot_cell.render_state.get("pinboard")
        if not isinstance(pinboard, dict):
            return
        annotations = pinboard.get("annotations")
        if not isinstance(annotations, list):
            return
        updated = []
        for annotation in annotations:
            if not isinstance(annotation, dict):
                continue
            annotation_id = str(annotation.get("annotation_id") or "")
            pin_override = pin_overrides.get(annotation_id)
            if not isinstance(pin_override, dict):
                updated.append(annotation)
                continue
            if bool(pin_override.get("removed", False)):
                continue
            merged = dict(annotation)
            for key in ("box_anchor", "screen_offset"):
                value = pin_override.get(key)
                if isinstance(value, list) and len(value) == 2:
                    merged[key] = [float(value[0]), float(value[1])]
            updated.append(merged)
        pinboard["annotations"] = updated

    def apply_state(self, cell, *, cancelled=lambda: False, load_metadata=True, source_projection=None):
        paths = cell.render_state.get('preset_paths', {})
        if not paths.get('discovery'):
            return False
        state = cell.render_state.get('transform')
        recipe = state.get('transform', state) if isinstance(state, dict) else None
        try:
            prepared = self.prepare(cell.render_state, paths, recipe=recipe, cancelled=cancelled,
                                    load_metadata=load_metadata, source_projection=source_projection)
        except (ValueError, TypeError, OverflowError):
            return False
        cell.coordinates, cell.metadata_ids, cell.metadata = prepared.coordinates, prepared.metadata_ids, prepared.metadata
        cell.render_state.pop('snapshot_projection', None)
        if 'transform' not in prepared.render_state:
            cell.render_state.pop('transform', None)
        if 'pinboard' in prepared.render_state:
            cell.render_state['pinboard'] = prepared.render_state['pinboard']
        return True


def reanchor_pins(cell, *, cancelled=lambda: False):
    pinboard = cell.render_state.get('pinboard')
    if not isinstance(pinboard, dict):
        return
    annotations = pinboard.get('annotations')
    if not isinstance(annotations, list):
        return
    wanted = set()
    for annotation in annotations:
        if isinstance(annotation, dict):
            try:
                wanted.add(int(annotation.get('metadata_id')))
            except (TypeError, ValueError):
                pass
    if not wanted:
        return
    points = {}
    # Resolve only pinned IDs; never allocate a Python point entry per row.
    ids = np.asarray(cell.metadata_ids, dtype=np.int64)
    for start in range(0, len(ids), 16384):
        if cancelled():
            raise TransformCancelled
        part = ids[start:start + 16384]
        indices = np.flatnonzero(np.isin(part, list(wanted))) + start
        for index in indices:
            points[int(ids[index])] = list(map(float, cell.coordinates[index]))
    for annotation in annotations:
        if not isinstance(annotation, dict):
            continue
        try:
            point = points[int(annotation.get('metadata_id'))]
        except (KeyError, TypeError, ValueError):
            continue
        annotation['point'] = point
        annotation.pop('box_anchor', None)
