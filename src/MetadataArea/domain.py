from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from MetadataArea.values import display_value, scalar_values

MetadataValue = str | int | float | bool | None | list["MetadataValue"]


class MetadataGroupSetMode(str, Enum):
    EXCLUSIVE = "exclusive"
    OVERLAPPING = "overlapping"


@dataclass(frozen=True)
class MetadataGroupSet:
    group_set_id: str
    name: str
    mode: MetadataGroupSetMode


@dataclass(frozen=True)
class MetadataGroup:
    group_id: str
    group_set_id: str
    name: str
    member_count: int = 0


class MetadataGroupExportDestination(str, Enum):
    FILE = "file"
    METADATA = "metadata"


class MetadataGroupExportLayout(str, Enum):
    MEMBERSHIP_TABLE = "membership_table"
    GROUP_COLUMNS = "group_columns"
    CATEGORY_COLUMN = "category_column"


class MetadataGroupExportStructure(str, Enum):
    TABLE = "table"
    GROUPS_WITH_MEMBERS = "groups_with_members"
    ITEMS_WITH_GROUPS = "items_with_groups"
    GROUP_FILES = "group_files"


class MetadataGroupOverlapPolicy(str, Enum):
    ERROR = "error"
    MULTIPLE = "multiple"
    FIRST = "first"
    EXCLUDE = "exclude"


class MetadataGroupColumnConflictPolicy(str, Enum):
    ERROR = "error"
    UNIQUE = "unique"
    REPLACE = "replace"


@dataclass(frozen=True)
class MetadataGroupExportRequest:
    group_ids: tuple[str, ...]
    destination: MetadataGroupExportDestination
    layout: MetadataGroupExportLayout
    structure: MetadataGroupExportStructure = MetadataGroupExportStructure.TABLE
    destination_path: str = ""
    export_format: str = "csv"
    include_all_metadata_rows: bool = False
    identifier_column: str = "id"
    metadata_columns: tuple[str, ...] = ()
    category_column_name: str = "group"
    overlap_policy: MetadataGroupOverlapPolicy = MetadataGroupOverlapPolicy.ERROR
    priority_group_ids: tuple[str, ...] = ()
    metadata_column_prefix: str = "group_"
    membership_value_style: str = "true_false"
    unassigned_value: str = ""
    column_conflict_policy: MetadataGroupColumnConflictPolicy = (
        MetadataGroupColumnConflictPolicy.ERROR
    )


@dataclass(frozen=True)
class MetadataGroupExportPreview:
    valid: bool
    messages: tuple[str, ...]
    selected_group_count: int
    membership_count: int
    distinct_member_count: int
    overlap_count: int
    output_row_count: int
    output_columns: tuple[str, ...]
    conflicting_columns: tuple[str, ...] = ()
    rows: tuple[tuple[object, ...], ...] = ()
    text: str = ""


@dataclass(frozen=True)
class MetadataGroupExportResult:
    destination: MetadataGroupExportDestination
    output_row_count: int
    output_columns: tuple[str, ...]
    output_path: Path | None = None


@dataclass(frozen=True)
class MetadataColumn:
    name: str
    value_type: str = "text"
    unique_values: int | None = None
    display_name: str | None = None
    source_tab: str | None = None

    @property
    def label(self) -> str:
        return self.display_name or self.name


@dataclass(frozen=True)
class ResolvedValue:
    value: MetadataValue
    source_table: str
    record_index: int
    column: str

    @property
    def record_reference(self) -> str:
        return f'item:{self.record_index}' if self.source_table == 'metadata' else f'record:{self.source_table}:{self.record_index}'


@dataclass(frozen=True)
class ResolvedCell:
    records: tuple[ResolvedValue, ...] = ()

    @property
    def values(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(value for record in self.records for value in scalar_values(record.value)))

    @property
    def is_list(self) -> bool:
        return any(isinstance(record.value, list) for record in self.records)

    @property
    def value(self) -> MetadataValue:
        if self.is_list:
            return [entry for record in self.records
                    for entry in (record.value if isinstance(record.value, list) else [record.value])]
        return '; '.join(self.values)

    @property
    def scalar(self) -> str:
        values = self.values
        return values[0] if len(values) == 1 else ''

    @property
    def display(self) -> str:
        return display_value(self.value)


@dataclass(frozen=True)
class MetadataRow:
    id: int
    values: dict[str, MetadataValue]
    resolved: dict[str, ResolvedCell] = field(default_factory=dict)


@dataclass(frozen=True)
class MetadataSearchMatch:
    row_offset: int
    column_indexes: tuple[int, ...]


class MissingColumnPolicy(str, Enum):
    FILL = "fill"
    REMOVE = "remove"


@dataclass(frozen=True)
class MetadataMergePreview:
    valid: bool
    messages: list[str]
    existing_row_count: int
    incoming_row_count: int
    matched_row_count: int
    unmatched_existing_row_count: int
    unmatched_incoming_row_count: int
    columns_to_add: list[str]
    columns_to_update: list[str]


@dataclass(frozen=True)
class MetadataAddRowsMergeRequest:
    incoming_rows: list[dict[str, str]]
    existing_identifier_column: str
    incoming_identifier_column: str
    fill_value: str = ""
    missing_column_policy: MissingColumnPolicy = MissingColumnPolicy.FILL


@dataclass(frozen=True)
class MetadataTable:
    id: str
    name: str


@dataclass(frozen=True)
class TableLinkRequest:
    source_table: str
    source_column: str
    target_table: str
    target_column: str
    name: str
    accept_multiple: bool = False


@dataclass(frozen=True)
class TableLinkPreview:
    request: TableLinkRequest
    revision: int
    matched_count: int
    missing_count: int
    ambiguous_count: int
    existing_count: int
    linkable_count: int
    rows: tuple[tuple[int, str, int | None, str], ...]
    target_values: tuple[str, ...] = ()
    match_counts: tuple[int, ...] = ()


@dataclass(frozen=True)
class TableDeletionPlan:
    table_id: str
    row_count: int
    descriptions: tuple[str, ...]
    statements: tuple[tuple[Path, str, tuple], ...]
    files: tuple[Path, ...]
    fingerprints: tuple[tuple[Path, int, int], ...]
