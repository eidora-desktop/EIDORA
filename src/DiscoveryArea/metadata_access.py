from __future__ import annotations

from dataclasses import dataclass
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


class DiscoveryMetadataAccess:
    """Shared, column-oriented metadata access for Discovery features.

    The cache intentionally stores only columns that an active visual mapping,
    filter, slicer, tooltip, or import workflow requests.
    """

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
        self._column_data: dict[str, MetadataColumnData] = {}
        self._distinct_values: dict[str, tuple[str, ...]] = {}
        self._value_counts: dict[str, tuple[tuple[str, int], ...]] = {}
        self._group_fields: dict[str, tuple[str, str]] = {}
        self._group_field_aliases: dict[str, str] = {}

    def columns(self) -> tuple[MetadataColumn, ...]:
        with self._lock:
            cached = self._columns
        if cached is not None:
            return cached
        physical = list(self._repository.list_columns())
        used_labels = {column.name.casefold() for column in physical}
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
        with self._lock:
            if self._columns is None:
                self._columns = loaded
                self._group_fields = fields
                self._group_field_aliases = aliases
            return self._columns

    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self._repository.list_columns())

    def metadata_ids(self) -> np.ndarray:
        with self._lock:
            cached = self._metadata_ids
        if cached is not None:
            return cached
        loaded = np.ascontiguousarray(
            np.asarray(self._items.active_ids(), dtype=np.int64)
        )
        loaded.setflags(write=False)
        with self._lock:
            if self._metadata_ids is None:
                self._metadata_ids = loaded
            return self._metadata_ids

    def column_data(self, column_name: str) -> MetadataColumnData:
        name = str(column_name).strip()
        self.columns()
        name = self._group_field_aliases.get(name, name)
        with self._lock:
            cached = self._column_data.get(name)
        if cached is not None:
            return cached
        group_field = self._group_fields.get(name)
        if group_field is not None:
            loaded = self._group_column_data(*group_field)
            with self._lock:
                return self._column_data.setdefault(name, loaded)
        if name.startswith(("group:", "group-set:")):
            metadata_ids = self.metadata_ids()
            values = np.full(metadata_ids.shape, "", dtype=np.str_)
            values.setflags(write=False)
            loaded = MetadataColumnData(metadata_ids=metadata_ids, values=values)
            with self._lock:
                return self._column_data.setdefault(name, loaded)
        items = ([(int(value), str(value)) for value in self.metadata_ids()]
                 if name == "id" else self._repository.column_items(name))
        metadata_ids = np.ascontiguousarray(
            np.fromiter((item[0] for item in items), dtype=np.int64, count=len(items))
        )
        values = np.ascontiguousarray(
            np.asarray([item[1] for item in items], dtype=np.str_)
        )
        metadata_ids.setflags(write=False)
        values.setflags(write=False)
        loaded = MetadataColumnData(metadata_ids=metadata_ids, values=values)
        with self._lock:
            return self._column_data.setdefault(name, loaded)

    def values_for_ids(
        self,
        metadata_ids: np.ndarray,
        *,
        columns: set[str],
        numeric_columns: set[str] | None = None,
    ) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
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
            data = self.column_data(column)
            aligned = _align_column(data, target_ids)
            text_values[column] = aligned
            if column in numeric:
                numeric_values[column] = _numeric_values(aligned)
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
        name = self._group_field_aliases.get(name, name)
        with self._lock:
            cached = self._distinct_values.get(name)
        if cached is not None:
            return cached
        if name in self._group_fields:
            loaded = tuple(sorted({str(value) for value in self.column_data(name).values.tolist()}))
        else:
            loaded = tuple(self._repository.distinct_column_values(name))
        with self._lock:
            return self._distinct_values.setdefault(name, loaded)

    def value_counts(self, column_name: str) -> tuple[tuple[str, int], ...]:
        name = str(column_name).strip()
        self.columns()
        name = self._group_field_aliases.get(name, name)
        with self._lock:
            cached = self._value_counts.get(name)
        if cached is not None:
            return cached
        if name in self._group_fields:
            values, counts = np.unique(self.column_data(name).values, return_counts=True)
            loaded = tuple(
                sorted(
                    ((str(value), int(count)) for value, count in zip(values.tolist(), counts.tolist(), strict=True)),
                    key=lambda item: (-item[1], item[0]),
                )
            )
        else:
            loaded = tuple(self._repository.column_value_counts(name))
        with self._lock:
            return self._value_counts.setdefault(name, loaded)

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
    if target_ids.size == 0:
        empty = np.empty((0,), dtype=data.values.dtype)
        empty.setflags(write=False)
        return empty
    source_ids = data.metadata_ids
    if source_ids.size == 0:
        output = np.full(target_ids.shape, "", dtype=np.str_)
        output.setflags(write=False)
        return output
    positions = np.searchsorted(source_ids, target_ids)
    clipped = np.minimum(positions, source_ids.size - 1)
    matched = (positions < source_ids.size) & (source_ids[clipped] == target_ids)
    output = np.full(target_ids.shape, "", dtype=data.values.dtype)
    output[matched] = data.values[clipped[matched]]
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
