from __future__ import annotations

import sys
from collections import Counter, OrderedDict
from dataclasses import dataclass, field, replace
from threading import RLock
from typing import TYPE_CHECKING

import numpy as np

from MetadataArea.domain import MetadataColumn
from MetadataArea.group_repository import MetadataGroupRepository
from MetadataArea.repository import MetadataRepository
from project.item_repository import ItemRepository

if TYPE_CHECKING:
    from project.domain import Project


@dataclass(frozen=True)
class MetadataColumnData:
    """One immutable metadata column aligned to sorted canonical IDs."""

    metadata_ids: np.ndarray
    values: np.ndarray
    multiple_values: dict[int, tuple[str, ...]] = field(default_factory=dict)
    numeric_values: np.ndarray | None = None


class DiscoveryMetadataAccess:
    """Shared, column-oriented metadata access for Discovery features.

    The cache intentionally stores only columns that an active visual mapping,
    filter, slicer, tooltip, or import workflow requests.
    """

    MAX_CACHED_COLUMNS = 8
    MAX_CACHE_BYTES = 64 * 1024 * 1024

    def __init__(self, project: "Project") -> None:
        self._repository = MetadataRepository(
            project.paths.metadata_database,
            project.paths.metadata_columns,
        )
        self._items = ItemRepository(project.paths.metadata_database)
        self._group_repository = MetadataGroupRepository(project.paths.metadata_groups_database)
        self._lock = RLock()
        self._columns: tuple[MetadataColumn, ...] | None = None
        self._metadata_ids: np.ndarray | None = None
        self._revision = self._repository.revision()
        self._column_data: OrderedDict[str, MetadataColumnData] = OrderedDict()
        self._distinct_values: dict[str, tuple[str, ...]] = {}
        self._value_counts: dict[str, tuple[tuple[str, int], ...]] = {}
        self._summary_sizes: dict[tuple[str, str], int] = {}
        self._group_fields: dict[str, tuple[str, str]] = {}
        self._group_field_aliases: dict[str, str] = {}
        self._related_field_aliases: dict[str, str] = {}

    def _check_revision(self):
        revision = self._repository.revision()
        with self._lock:
            if revision != self._revision:
                self._revision = revision
                self._columns = None
                self._metadata_ids = None
                self._column_data.clear()
                self._distinct_values.clear()
                self._value_counts.clear()
                self._summary_sizes.clear()
                self._group_fields.clear()
                self._group_field_aliases.clear()
                self._related_field_aliases.clear()
        return revision

    def columns(self) -> tuple[MetadataColumn, ...]:
        revision = self._check_revision()
        with self._lock:
            cached = self._columns
        if cached is not None:
            return cached
        from MetadataArea.related_fields import discovery_columns
        physical, related_aliases = discovery_columns(self._repository.database_path)
        label_counts = Counter(column.label for column in physical)
        canonical_names = {column.name for column in physical}
        for column in physical:
            if label_counts[column.label] == 1 and column.label not in canonical_names:
                related_aliases[column.label] = column.name
        used_labels = {column.label.casefold() for column in physical}
        virtual: list[MetadataColumn] = []
        fields: dict[str, tuple[str, str]] = {}
        aliases: dict[str, str] = {}
        for group_set in self._group_repository.list_group_sets():
            groups = self._group_repository.list_groups(group_set.group_set_id)
            if group_set.mode.value == "exclusive":
                key = f"group-set:{group_set.group_set_id}"
                label = self._unique_group_field_label(group_set.name, used_labels)
                fields[key] = ("exclusive", group_set.group_set_id)
                aliases[label] = key
                virtual.append(MetadataColumn(
                    name=key,
                    display_name=label,
                    value_type="categorical",
                    unique_values=len(groups) + 1,
                ))
            else:
                for group in groups:
                    key = f"group:{group.group_id}"
                    label = self._unique_group_field_label(
                        f"{group_set.name} / {group.name}", used_labels
                    )
                    fields[key] = ("overlapping", group.group_id)
                    aliases[label] = key
                    virtual.append(MetadataColumn(
                        name=key,
                        display_name=label,
                        value_type="categorical",
                        unique_values=2,
                    ))
        loaded = tuple([*physical, *virtual])
        if self._check_revision() != revision:
            return self.columns()
        with self._lock:
            if self._columns is None:
                self._columns = loaded
                self._group_fields = fields
                self._group_field_aliases = aliases
                self._related_field_aliases = related_aliases
            return self._columns

    def field_aliases(self) -> dict[str, str]:
        self.columns()
        return dict(self._related_field_aliases)

    def canonical_column_name(self, name: str) -> str:
        self.columns()
        name = str(name).strip()
        return self._related_field_aliases.get(name, self._group_field_aliases.get(name, name))

    def canonical_field_payload(self, payload):
        """Migrate saved field references without changing category values or labels."""
        if isinstance(payload, dict):
            return {key: (self.canonical_column_name(value)
                          if key in {'column', 'field', 'size_column', 'width_column'} and isinstance(value, str)
                          else self.canonical_field_payload(value)) for key, value in payload.items()}
        if isinstance(payload, list):
            return [self.canonical_field_payload(value) for value in payload]
        return payload

    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns())

    def metadata_ids(self) -> np.ndarray:
        revision = self._check_revision()
        with self._lock:
            cached = self._metadata_ids
        if cached is not None:
            return cached
        loaded = np.ascontiguousarray(
            np.asarray(self._items.active_ids(), dtype=np.int64)
        )
        loaded.setflags(write=False)
        if self._check_revision() != revision:
            return self.metadata_ids()
        with self._lock:
            if self._metadata_ids is None:
                self._metadata_ids = loaded
            return self._metadata_ids

    def column_data(self, column_name: str) -> MetadataColumnData:
        name = self.canonical_column_name(column_name)
        revision = self._revision
        with self._lock:
            cached = self._column_data.get(name)
            if cached is not None:
                self._column_data.move_to_end(name)
                return cached
        group_field = self._group_fields.get(name)
        if group_field is not None:
            loaded = self._group_column_data(*group_field)
        elif name == 'id' or name.startswith(('group:', 'group-set:')):
            metadata_ids = self.metadata_ids()
            values = metadata_ids.astype(str) if name == 'id' else np.full(metadata_ids.shape, '', dtype=np.str_)
            values.setflags(write=False)
            loaded = MetadataColumnData(metadata_ids, values)
        else:
            from MetadataArea.resolution import scalar_column_values
            items, multiple = scalar_column_values(self._repository.database_path, name, self._related_field_aliases)
            metadata_ids = np.fromiter((item[0] for item in items), dtype=np.int64, count=len(items))
            values = np.asarray([item[1] for item in items], dtype=np.str_)
            metadata_ids.setflags(write=False)
            values.setflags(write=False)
            loaded = MetadataColumnData(metadata_ids, values, multiple)
        # A worker finishing after publication must not repopulate the new cache
        # with old values. Its database read itself uses a consistent snapshot.
        if self._check_revision() != revision:
            return self.column_data(column_name)
        with self._lock:
            self._column_data[name] = loaded
            self._trim_column_cache()
        return loaded

    def _trim_column_cache(self):
        while (len(self._column_data) > self.MAX_CACHED_COLUMNS
               or (sum(self._column_bytes(data) for data in self._column_data.values())
                   + sum(self._summary_sizes.values())) > self.MAX_CACHE_BYTES):
            evicted, _ = self._column_data.popitem(last=False)
            self._distinct_values.pop(evicted, None)
            self._value_counts.pop(evicted, None)
            self._summary_sizes.pop((evicted, "distinct"), None)
            self._summary_sizes.pop((evicted, "counts"), None)

    @staticmethod
    def _summary_bytes(values):
        # Conservatively include Python tuple, string and integer storage.
        return sys.getsizeof(values) + sum(
            DiscoveryMetadataAccess._summary_bytes(value) if isinstance(value, tuple)
            else sys.getsizeof(value) for value in values)

    @staticmethod
    def _column_bytes(data):
        import sys
        numeric_bytes = 0 if data.numeric_values is None else data.numeric_values.nbytes
        return data.metadata_ids.nbytes + data.values.nbytes + numeric_bytes + sys.getsizeof(data.multiple_values) + sum(
            sys.getsizeof(item) + sys.getsizeof(values) + sum(sys.getsizeof(value) for value in values)
            for item, values in data.multiple_values.items())

    def resolved_cells(self, column_name, ids=None):
        """Uncached provenance for a requested page or explicit full-column read."""
        name = self.canonical_column_name(column_name)
        from MetadataArea.resolution import resolve_overview_field
        return resolve_overview_field(self._repository.database_path, name, self._related_field_aliases, ids)

    def multiple_values_for_ids(self, metadata_ids, *, columns):
        result = {}
        for column in columns:
            if column == 'id' or self.is_group_field(column):
                continue
            data = self.column_data(column)
            if data.multiple_values:
                targets = np.asarray(metadata_ids, dtype=np.int64)
                aligned = _align_column(data, targets)
                positions = np.searchsorted(data.metadata_ids, targets)
                matched = positions < data.metadata_ids.size
                if data.metadata_ids.size:
                    matched &= data.metadata_ids[np.minimum(positions, data.metadata_ids.size - 1)] == targets
                result[column] = tuple(data.multiple_values.get(int(item), (str(value),) if found else ())
                                       for item, value, found in zip(targets, aligned, matched, strict=True))
        return result

    def values_for_ids(
        self,
        metadata_ids: np.ndarray,
        *,
        columns: set[str],
        numeric_columns: set[str] | None = None,
        display: bool = False,
    ) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
        revision = self._check_revision()
        target_ids = np.asarray(metadata_ids, dtype=np.int64).reshape(-1)
        requested = {str(column).strip() for column in columns if str(column).strip()}
        requested.discard("id")
        numeric = (
            set(requested)
            if numeric_columns is None
            else {str(column).strip() for column in numeric_columns}
        )
        numeric &= requested
        text_values: dict[str, np.ndarray] = {}
        numeric_values: dict[str, np.ndarray] = {}
        for column in sorted(requested):
            data = None
            if display and not self.is_group_field(column):
                cells = self.resolved_cells(column, target_ids.tolist())
                aligned = np.asarray([cells[int(item)].display if int(item) in cells else '' for item in target_ids], dtype=np.str_)
                aligned.setflags(write=False)
            else:
                data = self.column_data(column)
                aligned = _align_column(data, target_ids)
            text_values[column] = aligned
            if column in numeric:
                if data is None:
                    numeric_values[column] = _numeric_values(aligned)
                else:
                    if data.numeric_values is None:
                        loaded = replace(data, numeric_values=_numeric_values(data.values))
                        name = self.canonical_column_name(column)
                        with self._lock:
                            if self._column_data.get(name) is data:
                                self._column_data[name] = loaded
                                self._trim_column_cache()
                        data = loaded
                    numeric_values[column] = _align_values(data.metadata_ids, data.numeric_values, target_ids, np.nan)
        if self._check_revision() != revision:
            return self.values_for_ids(target_ids, columns=columns, numeric_columns=numeric_columns, display=display)
        return text_values, numeric_values

    def values_by_id(self, column_name: str) -> dict[int, str]:
        data = self.column_data(column_name)
        return {
            int(metadata_id): str(value)
            for metadata_id, value in zip(
                data.metadata_ids.tolist(),
                data.values.tolist(),
                strict=True,
            )
        }

    def distinct_values(self, column_name: str) -> tuple[str, ...]:
        name = str(column_name).strip()
        self.columns()
        name = self.canonical_column_name(name)
        with self._lock:
            cached = self._distinct_values.get(name)
        if cached is not None:
            return cached
        revision = self._revision
        values = self._all_values(name)
        loaded = (tuple(sorted({str(value) for value in values})) if name in self._group_fields
                  else tuple(sorted({str(value).strip() for value in values if str(value).strip()})))
        size = self._summary_bytes(loaded)
        if self._check_revision() != revision:
            return self.distinct_values(column_name)
        with self._lock:
            if name in self._column_data:
                self._distinct_values[name] = loaded
                self._summary_sizes[name, "distinct"] = size
                self._trim_column_cache()
            return loaded

    def value_counts(self, column_name: str) -> tuple[tuple[str, int], ...]:
        name = str(column_name).strip()
        self.columns()
        name = self.canonical_column_name(name)
        with self._lock:
            cached = self._value_counts.get(name)
        if cached is not None:
            return cached
        revision = self._revision
        all_values = self._all_values(name)
        values, counts = np.unique(all_values, return_counts=True)
        loaded = tuple(
            sorted(
                ((str(value), int(count)) for value, count in zip(values.tolist(), counts.tolist(), strict=True)),
                key=lambda item: (-item[1], item[0]),
            )
        )
        size = self._summary_bytes(loaded)
        if self._check_revision() != revision:
            return self.value_counts(column_name)
        with self._lock:
            if name in self._column_data:
                self._value_counts[name] = loaded
                self._summary_sizes[name, "counts"] = size
                self._trim_column_cache()
            return loaded

    def _all_values(self, name):
        data = self.column_data(name)
        if not data.multiple_values:
            return data.values
        return [value for item, scalar in zip(data.metadata_ids, data.values, strict=True)
                for value in data.multiple_values.get(int(item), (str(scalar),))]

    def is_group_field(self, column_name: str) -> bool:
        self.columns()
        name = self._group_field_aliases.get(str(column_name), str(column_name))
        return name in self._group_fields

    def _group_column_data(self, kind: str, identifier: str) -> MetadataColumnData:
        metadata_ids = self.metadata_ids()
        positions = {int(value): index for index, value in enumerate(metadata_ids.tolist())}
        if kind == "exclusive":
            values = np.full(metadata_ids.shape, "", dtype=object)
            for group in self._group_repository.list_groups(identifier):
                for metadata_id in self._group_repository.member_ids(group.group_id):
                    index = positions.get(metadata_id)
                    if index is not None:
                        values[index] = group.name
        else:
            values = np.full(metadata_ids.shape, "Not member", dtype=object)
            for metadata_id in self._group_repository.member_ids(identifier):
                index = positions.get(metadata_id)
                if index is not None:
                    values[index] = "Member"
        output = np.ascontiguousarray(values, dtype=np.str_)
        output.setflags(write=False)
        return MetadataColumnData(metadata_ids=metadata_ids, values=output)

    @staticmethod
    def _unique_group_field_label(base: str, used: set[str]) -> str:
        candidate = base
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{base} ({suffix})"
            suffix += 1
        used.add(candidate.casefold())
        return candidate


def _align_column(data: MetadataColumnData, target_ids: np.ndarray) -> np.ndarray:
    return _align_values(data.metadata_ids, data.values, target_ids, '')


def _align_values(source_ids, values, target_ids, missing):
    if np.array_equal(source_ids, target_ids):
        return values
    if target_ids.size == 0:
        empty = np.empty((0,), dtype=values.dtype)
        empty.setflags(write=False)
        return empty
    if source_ids.size == 0:
        output = np.full(target_ids.shape, missing, dtype=values.dtype)
        output.setflags(write=False)
        return output
    positions = np.searchsorted(source_ids, target_ids)
    clipped = np.minimum(positions, source_ids.size - 1)
    matched = (positions < source_ids.size) & (source_ids[clipped] == target_ids)
    output = np.full(target_ids.shape, missing, dtype=values.dtype)
    output[matched] = values[clipped[matched]]
    output = np.ascontiguousarray(output)
    output.setflags(write=False)
    return output


def _numeric_values(values: np.ndarray) -> np.ndarray:
    try:
        output = np.asarray(values, dtype=np.float32)
    except (TypeError, ValueError):
        output = np.full(values.shape[0], np.nan, dtype=np.float32)
        for index, value in enumerate(values.tolist()):
            try:
                output[index] = float(value)
            except (TypeError, ValueError):
                pass
    output = np.ascontiguousarray(output, dtype=np.float32)
    output.setflags(write=False)
    return output


def numeric_value_range(values):
    """Numeric domain and integer/float control choice, computed off the GUI thread."""
    minimum = maximum = None
    use_double = False
    for raw in values:
        text = str(raw).strip()
        if not text:
            continue
        try:
            value = float(text)
        except (TypeError, ValueError):
            return None
        if np.isfinite(value):
            minimum = value if minimum is None else min(minimum, value)
            maximum = value if maximum is None else max(maximum, value)
            try:
                int(text)
            except ValueError:
                use_double = True
    return (minimum, maximum, use_double) if minimum is not None else None


def filter_options(access, column):
    values = list(access.distinct_values(column))
    return values, numeric_value_range(values)
