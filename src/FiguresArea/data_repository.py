"""Immutable, content-addressed figure datasets shared by saved plot cells."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import tempfile
import threading

import numpy as np

from FiguresArea.domain import PlotCell
from FiguresArea.compact_metadata import CompactMetadata, encode_metadata


class FigureDataRepository:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._references: dict[str, str] = {}
        self._lock = threading.Lock()

    def store(self, cell: PlotCell, *, shared: dict | None = None) -> str | None:
        if cell.data_ref:
            return cell.data_ref
        if len(cell.coordinates) <= 2000:
            return None
        with self._lock:
            sources = (id(cell.coordinates), id(cell.metadata_ids), id(cell.metadata))
            if shared is not None and sources in shared:
                if cell.data_key:
                    self._references[cell.data_key] = shared[sources]
                return shared[sources]
            if cell.data_key and cell.data_key in self._references:
                return self._references[cell.data_key]
            if isinstance(cell.coordinates, np.ndarray) or len(cell.coordinates) <= 16384:
                coordinates = np.asarray(cell.coordinates, dtype=np.float64)
            else:
                coordinates = np.empty((len(cell.coordinates), 2), dtype=np.float64)
                for start in range(0, len(coordinates), 16384):
                    coordinates[start:start + 16384] = cell.coordinates[start:start + 16384]
            ids = np.asarray(cell.metadata_ids, dtype=np.int64)
            arrays = {'coordinates': coordinates, 'metadata_ids': ids, **encode_metadata(cell.metadata)}
            digest = hashlib.sha256()
            for name, value in sorted(arrays.items()):
                digest.update(name.encode('utf-8'))
                raw = memoryview(np.ascontiguousarray(value)).cast('B')
                digest.update(len(raw).to_bytes(8, 'little'))
                digest.update(raw)
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / f"{digest.hexdigest()}.npz"
            if not path.exists():
                with tempfile.NamedTemporaryFile(dir=self.root, prefix=".figure-data-", suffix=".npz", delete=False) as stream:
                    temporary = Path(stream.name)
                try:
                    np.savez_compressed(temporary, **arrays)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            if cell.data_key:
                self._references[cell.data_key] = str(path)
            if shared is not None:
                shared[sources] = str(path)
            return str(path)


class FigureDataCache:
    """Bound decoded data in the render process; disk artifacts remain authoritative."""

    def __init__(self, max_bytes: int = 256 * 1024 * 1024) -> None:
        self.max_bytes = max_bytes
        self._entries = OrderedDict()
        self._bytes = 0

    def hydrate(self, cell: PlotCell, *, render_data: dict | None = None) -> None:
        if not cell.data_ref:
            return
        path = cell.data_ref
        if len(cell.coordinates):
            if render_data is not None:
                entry = render_data.setdefault(path, (cell.coordinates, cell.metadata_ids, cell.metadata, 0))
                self._assign(cell, entry)
            return
        if render_data is not None and path in render_data:
            self._assign(cell, render_data[path])
            return
        entry = self._entries.pop(path, None)
        if entry is None:
            with np.load(path, allow_pickle=False) as archive:
                coordinates = np.asarray(archive["coordinates"], dtype=np.float64)
                ids = np.asarray(archive["metadata_ids"], dtype=np.int64)
                if 'metadata_columns' in archive:
                    metadata = CompactMetadata(path, archive['metadata_row_ids'],
                                               json.loads(archive['metadata_columns'].tobytes()))
                    metadata_size = metadata.nbytes
                else:
                    raw = archive["metadata_json"].tobytes()
                    arrays = encode_metadata({int(key): values for key, values in json.loads(raw).items()})
                    metadata = CompactMetadata(None, arrays['metadata_row_ids'],
                                               json.loads(arrays['metadata_columns'].tobytes()), arrays)
                    metadata_size = metadata.nbytes
            if coordinates.ndim != 2 or coordinates.shape[1] != 2 or len(ids) != len(coordinates):
                raise ValueError("Invalid figure dataset dimensions")
            coordinates.setflags(write=False)
            ids.setflags(write=False)
            # Include Python mapping overhead in the decoded-cache budget.
            size = coordinates.nbytes + ids.nbytes + metadata_size
            entry = (coordinates, ids, metadata, size)
            self._bytes += size
        self._entries[path] = entry
        while self._bytes > self.max_bytes and self._entries:
            _, removed = self._entries.popitem(last=False)
            self._bytes -= removed[3]
        if render_data is not None:
            render_data[path] = entry
        self._assign(cell, entry)

    def trim(self):
        # Lazy column loads grow entries after hydration; account for them before
        # retaining datasets between frames.
        for key, entry in list(self._entries.items()):
            coordinates, ids, metadata, size = entry
            if isinstance(metadata, CompactMetadata):
                updated = coordinates.nbytes + ids.nbytes + metadata.nbytes
                self._bytes += updated - size
                self._entries[key] = (coordinates, ids, metadata, updated)
        while self._bytes > self.max_bytes and self._entries:
            _, entry = self._entries.popitem(last=False)
            self._bytes -= entry[3]

    @staticmethod
    def _assign(cell, entry):
        cell.coordinates, cell.metadata_ids, cell.metadata = entry[:3]
        cell.data_key = cell.data_ref
        cell.point_count = len(cell.coordinates)
        cell.data_sources = (cell.coordinates, cell.metadata_ids, cell.metadata)
