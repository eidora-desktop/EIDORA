from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class MetadataImportPreset(str, Enum):
    INITIAL = "initial"
    APPEND = "append"
    UPDATE = "update"
    REPLACE_TABLE = "replace_table"

    @property
    def label(self) -> str:
        return {
            self.INITIAL: "Import Metadata",
            self.APPEND: "Append Items",
            self.UPDATE: "Update Metadata",
            self.REPLACE_TABLE: "Replace Metadata Table",
        }[self]


@dataclass(frozen=True)
class MetadataColumnSchema:
    name: str
    value_type: str = "text"
    value_format: str = ""
    nullable: bool = True
    unique_values: int = 0


@dataclass(frozen=True)
class MetadataImportProbe:
    source_path: Path
    format_key: str
    format_label: str
    size_bytes: int
    source_fingerprint: str
    row_count_hint: int | None = None
    column_names: tuple[str, ...] = ()
    table_names: tuple[str, ...] = ()
    default_table: str = ""
    default_record_path: str = ""
    is_tei: bool = False
    default_encoding: str = "utf-8-sig"
    encoding_ambiguous: bool = False
    default_delimiter: str = ""
    delimiter_ambiguous: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class MetadataImportInterpretation:
    table_name: str = ""
    record_path: str = ""
    delimiter: str = ""
    encoding: str = "utf-8-sig"
    header_row: int = 1
    flatten_nested: bool = True
    repeated_value_separator: str = "; "


@dataclass(frozen=True)
class MetadataColumnMapping:
    source_name: str
    target_name: str
    value_type: str = "text"
    value_format: str = ""
    enabled: bool = True
    example: str = ""


@dataclass(frozen=True)
class MetadataImportInspection:
    probe: MetadataImportProbe
    interpretation: MetadataImportInterpretation
    staging_path: Path
    row_count: int
    columns: tuple[str, ...]
    schemas: tuple[MetadataColumnSchema, ...]
    samples: tuple[dict[str, str], ...]
    source_fingerprint: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class MetadataImportImpact:
    existing_rows: int = 0
    incoming_rows: int = 0
    matched_rows: int = 0
    appended_rows: int = 0
    removed_rows: int = 0
    unchanged_rows: int = 0
    duplicate_incoming: int = 0
    duplicate_existing: int = 0
    empty_incoming_identifiers: int = 0
    empty_existing_identifiers: int = 0
    columns_added: tuple[str, ...] = ()
    columns_updated: tuple[str, ...] = ()
    columns_removed: tuple[str, ...] = ()
    affected_groups: int = 0
    affected_media_links: int = 0
    affected_embedding_inputs: int = 0
    affected_discovery_items: int = 0
    affected_thumbnails: int = 0
    affected_pins: int = 0
    affected_marked_areas: int = 0

    @property
    def affected_links(self) -> int:
        return sum(
            (
                self.affected_groups,
                self.affected_media_links,
                self.affected_embedding_inputs,
                self.affected_discovery_items,
                self.affected_thumbnails,
                self.affected_pins,
                self.affected_marked_areas,
            )
        )


@dataclass(frozen=True)
class MetadataDependencySummary:
    groups: int = 0
    media_links: int = 0
    embedding_inputs: int = 0
    discovery_datasets: int = 0
    thumbnails: int = 0
    pins: int = 0
    marked_areas: int = 0

    @property
    def total(self) -> int:
        return sum(
            (
                self.groups,
                self.media_links,
                self.embedding_inputs,
                self.discovery_datasets,
                self.thumbnails,
                self.pins,
                self.marked_areas,
            )
        )


@dataclass(frozen=True)
class MetadataImportPlan:
    inspection: MetadataImportInspection
    preset: MetadataImportPreset
    mappings: tuple[MetadataColumnMapping, ...]
    source_identifier_column: str = ""
    project_identifier_column: str = ""
    metadata_fingerprint: str = ""
    preview_token: str = ""
    impact: MetadataImportImpact = field(default_factory=MetadataImportImpact)
    can_import: bool = False
    messages: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class MetadataImportResult:
    preset: MetadataImportPreset
    row_count: int
    column_count: int
    impact: MetadataImportImpact
    source_path: Path | None = None
    warnings: tuple[str, ...] = ()
