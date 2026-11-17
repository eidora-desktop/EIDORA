"""Lossless dictionary-encoded columns with lazy field access for figure data."""
from collections.abc import Mapping
import json
import sys

import numpy as np


def encode_metadata(metadata):
    if isinstance(metadata, CompactMetadata):
        return metadata.encoded_arrays()
    row_ids = np.asarray(sorted(metadata), dtype=np.int64)
    names = sorted({name for row in metadata.values() for name in row})
    arrays = {'metadata_row_ids': row_ids,
              'metadata_columns': np.frombuffer(json.dumps(names, ensure_ascii=False).encode('utf-8'), np.uint8)}
    for column, name in enumerate(names):
        lookup, data, offsets = {}, bytearray(), [0]
        codes = np.full(len(row_ids), -1, dtype=np.int32)
        for index, row_id in enumerate(row_ids):
            row = metadata[int(row_id)]
            if name not in row:
                continue
            value = row[name]
            key = (type(value), value) if isinstance(value, (str, int, float, bool, type(None))) else json.dumps(value, sort_keys=True)
            code = lookup.get(key)
            if code is None:
                code = len(lookup)
                lookup[key] = code
                data.extend(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
                offsets.append(len(data))
            codes[index] = code
        arrays[f'meta_{column}_codes'] = codes
        arrays[f'meta_{column}_offsets'] = np.asarray(offsets, dtype=np.int64)
        arrays[f'meta_{column}_values'] = np.frombuffer(data, np.uint8)
    return arrays


class MetadataColumnBuilder:
    """Encode normalized database strings without retaining row dictionaries.

    Codes are preallocated for the requested IDs. Distinct-value lookup and
    byte/offset staging stay bounded, including high-cardinality text fields.
    Repeated palette values after a lookup reset are lossless.
    """

    def __init__(self, count):
        self.codes = np.full(count, -1, dtype=np.int32)
        self._lookup = {}
        self._values = []
        self._buffer = bytearray()
        self._offsets = []
        self._offset_buffer = np.empty(4096, dtype=np.int64)
        self._offset_count = 0
        self._value_count = 0
        self._byte_count = 0
        self._append_offset(0)

    def _append_offset(self, value):
        if self._offset_count == len(self._offset_buffer):
            self._offsets.append(self._offset_buffer)
            self._offset_buffer = np.empty(4096, dtype=np.int64)
            self._offset_count = 0
        self._offset_buffer[self._offset_count] = value
        self._offset_count += 1

    def add(self, position, value):
        self.codes[position] = self._value_code(value)

    def add_batch(self, positions, values):
        self.codes[positions] = [self._value_code(value) for value in values]

    def _value_code(self, value):
        text = '' if value is None else str(value)
        code = self._lookup.get(text)
        if code is None:
            code = self._value_count
            if len(self._lookup) >= 4096:
                self._lookup.clear()
            self._lookup[text] = code
            encoded = json.encoder.encode_basestring(text).encode('utf-8')
            if len(self._buffer) + len(encoded) > 256 * 1024:
                self._values.append(bytes(self._buffer))
                self._buffer.clear()
            if len(encoded) > 256 * 1024:
                self._values.append(encoded)
            else:
                self._buffer.extend(encoded)
            self._byte_count += len(encoded)
            self._append_offset(self._byte_count)
            self._value_count += 1
        return code

    def finish(self, present):
        self._lookup.clear()
        self._values.append(bytes(self._buffer))
        raw = np.frombuffer(b''.join(self._values), dtype=np.uint8)
        offsets = np.concatenate(self._offsets + [self._offset_buffer[:self._offset_count]])
        codes = self.codes if present.all() else self.codes[present]
        self._values.clear()
        self._buffer.clear()
        self._offsets.clear()
        self.codes = None
        return codes, offsets, raw


class CompactMetadata(Mapping):
    def __init__(self, path, row_ids, names, arrays=None):
        self.path = path
        self.row_ids = row_ids
        self.row_ids.setflags(write=False)
        self.names = names
        self._arrays = arrays
        if arrays is not None:
            for array in arrays.values():
                array.setflags(write=False)
        self._columns = {}
        self.nbytes = sum(a.nbytes for a in arrays.values()) if arrays is not None else row_ids.nbytes

    def encoded_arrays(self):
        if self._arrays is not None:
            return dict(self._arrays)
        with np.load(self.path, allow_pickle=False) as archive:
            return {name: archive[name] for name in archive.files
                    if name.startswith(('metadata_', 'meta_'))}

    def __deepcopy__(self, memo):
        # Published metadata is immutable; lazy decoded columns are caches only.
        return self

    def encoded_column(self, name):
        if name not in self.names:
            return None
        if name not in self._columns:
            index = self.names.index(name)
            from contextlib import nullcontext
            source = nullcontext(self._arrays) if self._arrays is not None else np.load(self.path, allow_pickle=False)
            with source as archive:
                codes = archive[f'meta_{index}_codes']
                offsets = archive[f'meta_{index}_offsets']
                raw = archive[f'meta_{index}_values'].tobytes()
            if len(codes) != len(self.row_ids) or codes.min(initial=-1) < -1 or codes.max(initial=-1) >= len(offsets)-1:
                raise ValueError('Invalid figure metadata codes')
            values = tuple(json.loads(raw[int(a):int(b)]) for a, b in zip(offsets[:-1], offsets[1:]))
            codes.setflags(write=False)
            self._columns[name] = (codes, values)
            self.nbytes += (codes.nbytes if self._arrays is None else 0) + sys.getsizeof(values) + sum(sys.getsizeof(v) for v in values)
        return self._columns[name]

    def map_values(self, name, ids, function):
        """Apply a style once per distinct value, retaining the plotted ID order."""
        column = self.encoded_column(name)
        if column is None:
            return np.asarray([function('')])[np.zeros(len(ids), dtype=np.intp)]
        codes, values = column
        positions = np.searchsorted(self.row_ids, ids)
        present = positions < len(self.row_ids)
        safe = np.minimum(positions, max(len(self.row_ids)-1, 0))
        if len(self.row_ids):
            present &= self.row_ids[safe] == ids
        selected = np.full(len(ids), -1, dtype=np.int32)
        selected[present] = codes[safe[present]]
        palette = np.asarray([function('')] + [function(value) for value in values])
        return palette[selected + 1]

    def __getitem__(self, key):
        position = int(np.searchsorted(self.row_ids, key))
        if position == len(self.row_ids) or self.row_ids[position] != key:
            raise KeyError(key)
        return _Row(self, position)

    def __iter__(self):
        return (int(value) for value in self.row_ids)

    def __len__(self):
        return len(self.row_ids)


class _Row(Mapping):
    def __init__(self, table, position):
        self.table, self.position = table, position

    def __getitem__(self, name):
        column = self.table.encoded_column(name)
        if column is None or column[0][self.position] < 0:
            raise KeyError(name)
        return column[1][column[0][self.position]]

    def __iter__(self):
        return (name for name in self.table.names if self.table.encoded_column(name)[0][self.position] >= 0)

    def __len__(self):
        return sum(1 for _ in self)
