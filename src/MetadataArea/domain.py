from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


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

    @property
    def label(self) -> str:
        return self.display_name or self.name


@dataclass(frozen=True)
class MetadataRow:
    id: int
    values: dict[str, str]


@dataclass(frozen=True)
class MetadataSearchMatch:
    row_offset: int
    column_indexes: tuple[int, ...]


class MissingColumnPolicy(str, Enum):
    FILL = "fill"
    REMOVE = "remove"


@dataclass(frozen=True)
class MetadataImportPreview:
    source_name: str
    columns: list[str]
    rows: list[dict[str, str]]


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
