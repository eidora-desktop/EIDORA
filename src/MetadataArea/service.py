from __future__ import annotations

import csv
from dataclasses import dataclass
import json
from pathlib import Path
from xml.etree import ElementTree

from MetadataArea.domain import (
    MetadataAddRowsMergeRequest,
    MetadataColumn,
    MetadataImportPreview,
    MetadataMergePreview,
    MetadataRow,
    MetadataSearchMatch,
    MissingColumnPolicy,
)
from MetadataArea.repository import MetadataRepository


class IdColumnConflictError(ValueError):
    """Raised when imported metadata tries to use the reserved internal id column."""

    def __init__(self, conflicting_columns: list[str]) -> None:
        self.conflicting_columns = conflicting_columns
        super().__init__("Imported metadata contains the reserved id column.")


class MetadataService:
    """Application behavior for metadata import and browsing."""

    def __init__(self, repository: MetadataRepository) -> None:
        self._repository = repository

    def initialize_empty(self) -> None:
        self._repository.initialize_empty()

    def import_metadata(self, source_path: Path, id_column_strategy: str = "error") -> None:
        metadata = self._prepare_import(source_path, id_column_strategy=id_column_strategy)
        self._repository.initialize_from_rows(metadata.rows, metadata.columns, row_ids=metadata.row_ids)

    def export_metadata(self, destination: Path, export_format: str) -> Path:
        normalized_format = str(export_format).strip().lower()
        if normalized_format not in {"csv", "tsv", "json"}:
            raise ValueError(f"Unsupported metadata export format: {export_format}")

        output = Path(destination).with_suffix(f".{normalized_format}")
        output.parent.mkdir(parents=True, exist_ok=True)
        columns = [column.name for column in self.list_columns()]
        rows = self.fetch_rows(order_by="id")
        export_rows = [
            {
                "id": row.id,
                **row.values,
            }
            for row in rows
        ]
        if normalized_format == "json":
            output.write_text(
                json.dumps(export_rows, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return output

        delimiter = "," if normalized_format == "csv" else "\t"
        with output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, delimiter=delimiter)
            writer.writeheader()
            writer.writerows(export_rows)
        return output

    def prepare_import_rows(
        self,
        source_path: Path,
        id_column_strategy: str = "error",
    ) -> tuple[list[dict[str, str]], list[str]]:
        metadata = self._prepare_import(source_path, id_column_strategy=id_column_strategy)
        return metadata.rows, metadata.columns

    def load_metadata_preview(
        self,
        source_path: Path,
        id_column_strategy: str = "error",
    ) -> MetadataImportPreview:
        metadata = self._prepare_import(source_path, id_column_strategy=id_column_strategy)
        return MetadataImportPreview(
            source_name=source_path.name,
            columns=metadata.columns,
            rows=metadata.rows,
        )

    def preview_add_rows_merge(self, request: MetadataAddRowsMergeRequest) -> MetadataMergePreview:
        existing_columns = [column.name for column in self.list_columns()]
        messages = self._validate_common_merge_request(
            request.incoming_rows,
            existing_columns,
            request.existing_identifier_column,
            request.incoming_identifier_column,
        )
        incoming_columns = [column for column in self._incoming_columns(request.incoming_rows) if column != "id"]
        if not incoming_columns:
            messages.append("Incoming metadata does not contain any importable columns.")
        match_data = self._merge_match_data(
            request.incoming_rows,
            request.existing_identifier_column,
            request.incoming_identifier_column,
        )
        messages.extend(match_data.messages)
        columns_to_add = [column for column in incoming_columns if column not in existing_columns]
        columns_to_update = [column for column in incoming_columns if column in existing_columns and column != "id"]
        return MetadataMergePreview(
            valid=not messages,
            messages=messages,
            existing_row_count=match_data.existing_row_count,
            incoming_row_count=len(request.incoming_rows),
            matched_row_count=len(match_data.matched_values),
            unmatched_existing_row_count=match_data.unmatched_existing_count,
            unmatched_incoming_row_count=match_data.unmatched_incoming_count,
            columns_to_add=columns_to_add,
            columns_to_update=columns_to_update,
        )

    def merge_add_rows(self, request: MetadataAddRowsMergeRequest) -> MetadataMergePreview:
        preview = self.preview_add_rows_merge(request)
        if not preview.valid:
            raise ValueError("\n".join(preview.messages))

        existing_columns = [column.name for column in self.list_columns()]
        incoming_columns = [column for column in self._incoming_columns(request.incoming_rows) if column != "id"]
        fill_value = str(request.fill_value).strip()
        match_data = self._merge_match_data(
            request.incoming_rows,
            request.existing_identifier_column,
            request.incoming_identifier_column,
        )
        updates_by_row_id: dict[int, dict[str, str]] = {}
        rows_to_insert: list[dict[str, str]] = []
        missing_existing_columns = [
            column
            for column in existing_columns
            if column != "id" and column not in incoming_columns
        ]
        columns_to_remove: list[str] = []
        if request.missing_column_policy == MissingColumnPolicy.REMOVE:
            columns_to_remove = [
                column
                for column in missing_existing_columns
                if column != request.existing_identifier_column
            ]

        for value in match_data.matched_values:
            row_id = match_data.existing_row_ids_by_value[value]
            incoming_row = match_data.incoming_rows_by_value[value]
            updates_by_row_id[row_id] = {
                column: str(incoming_row.get(column, "")).strip()
                for column in incoming_columns
                if column != "id" and column not in columns_to_remove
            }

        final_columns = [
            column
            for column in [*existing_columns, *preview.columns_to_add]
            if column != "id" and column not in columns_to_remove
        ]
        for value in match_data.unmatched_incoming_values:
            incoming_row = match_data.incoming_rows_by_value[value]
            row: dict[str, str] = {}
            for column in final_columns:
                if column in incoming_columns:
                    row[column] = str(incoming_row.get(column, "")).strip()
                elif column == request.existing_identifier_column:
                    row[column] = str(incoming_row.get(request.incoming_identifier_column, "")).strip()
                else:
                    row[column] = fill_value
            rows_to_insert.append(row)

        self._repository.merge_add_rows(
            updates_by_row_id,
            rows_to_insert,
            preview.columns_to_add,
            columns_to_remove=columns_to_remove,
        )
        return preview

    def _prepare_import(
        self,
        source_path: Path,
        id_column_strategy: str = "error",
    ) -> "_NormalizedMetadata":
        raw_table = self._load_tabular_table(source_path)
        if not raw_table.rows:
            raise ValueError("Metadata file does not contain any rows.")

        columns, kept_indices, row_ids = self._normalize_columns(
            raw_table.columns,
            raw_table.rows,
            id_column_strategy=id_column_strategy,
        )
        if not columns:
            raise ValueError("Metadata file does not contain any columns.")

        rows = [
            {
                column: raw_row[column_index]
                for column, column_index in zip(columns, kept_indices, strict=True)
            }
            for raw_row in raw_table.rows
        ]
        return _NormalizedMetadata(columns=columns, rows=rows, row_ids=row_ids)

    def list_columns(self) -> list[MetadataColumn]:
        return self._repository.list_columns()

    def count_rows(self) -> int:
        return self._repository.count_rows()

    def add_identifier_rows(self, column_name: str, values: list[str]) -> int:
        self._validate_editable_column(column_name.strip())
        return self._repository.add_identifier_rows(column_name, values)

    def fetch_rows(
        self,
        order_by: str = "id",
        descending: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[MetadataRow]:
        return self._repository.fetch_rows(
            order_by=order_by,
            descending=descending,
            limit=limit,
            offset=offset,
        )

    def update_cell(self, row_id: int, column_name: str, value: str) -> None:
        self._repository.update_cell(row_id, column_name, value)

    def rename_column(self, old_name: str, new_name: str) -> None:
        old_name = old_name.strip()
        new_name = new_name.strip()
        self._validate_editable_column(old_name)
        if not new_name:
            raise ValueError("Metadata column name cannot be empty.")
        if new_name == "id":
            raise ValueError("The internal metadata ID column cannot be renamed or replaced.")
        existing_columns = [column.name for column in self.list_columns()]
        if new_name in existing_columns:
            raise ValueError(f"Metadata column already exists: {new_name}")
        self._repository.rename_column(old_name, new_name)

    def delete_column(self, column_name: str) -> None:
        column_name = column_name.strip()
        self._validate_editable_column(column_name)
        self._repository.delete_column(column_name)

    def search_row_offsets(
        self,
        query: str,
        regex: bool = False,
        match_case: bool = False,
        match_word: bool = False,
        order_by: str = "id",
        descending: bool = False,
        search_column: str | None = None,
    ) -> list[int]:
        return self._repository.search_row_offsets(
            query,
            regex=regex,
            match_case=match_case,
            match_word=match_word,
            order_by=order_by,
            descending=descending,
            search_column=search_column,
        )

    def row_offset_for_id(
        self,
        metadata_id: int,
        *,
        order_by: str = "id",
        descending: bool = False,
    ) -> int | None:
        return self._repository.row_offset_for_id(
            metadata_id,
            order_by=order_by,
            descending=descending,
        )

    def search_matches(
        self,
        query: str,
        regex: bool = False,
        match_case: bool = False,
        match_word: bool = False,
        order_by: str = "id",
        descending: bool = False,
        search_column: str | None = None,
    ) -> list[MetadataSearchMatch]:
        return self._repository.search_matches(
            query,
            regex=regex,
            match_case=match_case,
            match_word=match_word,
            order_by=order_by,
            descending=descending,
            search_column=search_column,
        )

    def _validate_editable_column(self, column_name: str) -> None:
        if not column_name:
            raise ValueError("Choose a metadata column.")
        if column_name == "id":
            raise ValueError("The internal metadata ID column cannot be edited.")
        existing_columns = [column.name for column in self.list_columns()]
        if column_name not in existing_columns:
            raise ValueError(f"Unknown metadata column: {column_name}")

    def _validate_common_merge_request(
        self,
        incoming_rows: list[dict[str, str]],
        existing_columns: list[str],
        existing_identifier_column: str,
        incoming_identifier_column: str,
    ) -> list[str]:
        messages: list[str] = []
        if not incoming_rows:
            messages.append("Incoming metadata does not contain any rows.")
        if existing_identifier_column not in existing_columns:
            messages.append("Choose an existing identifier column.")
        if incoming_rows and incoming_identifier_column not in self._incoming_columns(incoming_rows):
            messages.append("Choose an incoming identifier column.")
        return messages

    def _merge_match_data(
        self,
        incoming_rows: list[dict[str, str]],
        existing_identifier_column: str,
        incoming_identifier_column: str,
    ) -> "_MergeMatchData":
        messages: list[str] = []
        existing_values_by_id: dict[int, str] = {}
        incoming_rows_by_value: dict[str, dict[str, str]] = {}
        existing_row_ids_by_value: dict[str, int] = {}
        duplicate_existing: set[str] = set()
        duplicate_incoming: set[str] = set()
        empty_existing = 0
        empty_incoming = 0

        try:
            existing_values_by_id = self._repository.column_values_by_id(existing_identifier_column)
        except ValueError:
            existing_values_by_id = {}

        for row_id, value in existing_values_by_id.items():
            normalized = str(value).strip()
            if not normalized:
                empty_existing += 1
                continue
            if normalized in existing_row_ids_by_value:
                duplicate_existing.add(normalized)
                continue
            existing_row_ids_by_value[normalized] = row_id

        for row in incoming_rows:
            normalized = str(row.get(incoming_identifier_column, "")).strip()
            if not normalized:
                empty_incoming += 1
                continue
            if normalized in incoming_rows_by_value:
                duplicate_incoming.add(normalized)
                continue
            incoming_rows_by_value[normalized] = row

        if empty_existing:
            messages.append("Existing identifier column contains empty values.")
        if duplicate_existing:
            messages.append("Existing identifier column contains duplicate values.")
        if empty_incoming:
            messages.append("Incoming identifier column contains empty values.")
        if duplicate_incoming:
            messages.append("Incoming identifier column contains duplicate values.")

        existing_values = set(existing_row_ids_by_value)
        incoming_values = set(incoming_rows_by_value)
        matched_values = sorted(existing_values & incoming_values)
        if existing_values_by_id and incoming_rows and not matched_values:
            messages.append("No matching identifier values were found.")
        unmatched_incoming_values = sorted(incoming_values - existing_values)
        return _MergeMatchData(
            messages=messages,
            existing_row_count=len(existing_values_by_id),
            existing_row_ids_by_value=existing_row_ids_by_value,
            incoming_rows_by_value=incoming_rows_by_value,
            matched_values=matched_values,
            unmatched_incoming_values=unmatched_incoming_values,
            unmatched_existing_count=max(0, len(existing_values) - len(matched_values)),
            unmatched_incoming_count=len(unmatched_incoming_values),
        )

    @staticmethod
    def _incoming_columns(incoming_rows: list[dict[str, str]]) -> list[str]:
        columns: list[str] = []
        seen: set[str] = set()
        for row in incoming_rows:
            for column in row:
                if column not in seen:
                    seen.add(column)
                    columns.append(column)
        return columns

    def _normalize_columns(
        self,
        raw_columns: list[str],
        raw_rows: list[list[str]],
        id_column_strategy: str,
    ) -> tuple[list[str], list[int], list[int] | None]:
        named_columns: list[str] = []
        kept_indices: list[int] = []
        row_ids: list[int] | None = None

        for column_index, raw_name in enumerate(raw_columns):
            name = raw_name.strip()
            values = [row[column_index].strip() for row in raw_rows]
            if not name:
                if self._column_is_empty(values):
                    continue
                if column_index == 0 and self._looks_like_index_values(values):
                    row_ids = self._row_ids_from_index_values(values)
                    continue
                raise ValueError(
                    f"Metadata column {column_index + 1} has no name but contains values. "
                    "Rename the column or remove it before import."
                )
            if column_index == 0 and self._looks_like_named_index_column(name, values):
                row_ids = self._row_ids_from_index_values(values)
                continue
            named_columns.append(name)
            kept_indices.append(column_index)

        named_columns, kept_indices = self._resolve_id_columns(
            named_columns,
            kept_indices,
            id_column_strategy,
        )
        return self._dedupe_column_names(named_columns), kept_indices, row_ids

    def _resolve_id_columns(
        self,
        columns: list[str],
        kept_indices: list[int],
        strategy: str,
    ) -> tuple[list[str], list[int]]:
        id_positions = [index for index, column in enumerate(columns) if column.strip().lower() == "id"]
        if not id_positions:
            return columns, kept_indices

        normalized_strategy = strategy.strip().lower()
        if normalized_strategy == "error":
            raise IdColumnConflictError([columns[index] for index in id_positions])
        if normalized_strategy == "ignore":
            ignored = set(id_positions)
            return (
                [column for index, column in enumerate(columns) if index not in ignored],
                [column_index for index, column_index in enumerate(kept_indices) if index not in ignored],
            )
        if normalized_strategy == "rename":
            renamed = list(columns)
            existing_lower = {column.lower() for index, column in enumerate(columns) if index not in id_positions}
            for index in id_positions:
                new_name = self._next_available_column_name("id_imported", existing_lower)
                existing_lower.add(new_name.lower())
                renamed[index] = new_name
            return renamed, kept_indices
        raise ValueError("Invalid id column strategy. Expected error, ignore, or rename.")

    def _load_tabular_table(self, source_path: Path) -> "_RawMetadataTable":
        if not source_path.exists():
            raise FileNotFoundError(source_path)

        suffix = source_path.suffix.lower()
        if suffix in {".csv", ".tsv", ".tab", ".txt"}:
            delimiter = "\t" if suffix in {".tsv", ".tab"} else None
            return self._load_delimited_table(source_path, delimiter=delimiter)
        if suffix == ".json":
            return self._load_json_table(source_path)
        if suffix in {".jsonl", ".ndjson"}:
            return self._load_jsonl_table(source_path)
        if suffix == ".xml":
            return self._load_xml_table(source_path)
        raise ValueError(
            "Unsupported metadata format. Supported extensions: .csv, .tsv, .tab, .txt, .json, .jsonl, .ndjson, .xml."
        )

    def _load_delimited_table(self, source_path: Path, delimiter: str | None) -> "_RawMetadataTable":
        with source_path.open(newline="", encoding="utf-8-sig") as handle:
            chosen_delimiter = delimiter
            if chosen_delimiter is None:
                sample = handle.read(4096)
                handle.seek(0)
                chosen_delimiter = ","
                if sample.strip():
                    try:
                        chosen_delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
                    except csv.Error:
                        chosen_delimiter = ","

            reader = csv.reader(handle, delimiter=chosen_delimiter)
            try:
                columns = next(reader)
            except StopIteration as exc:
                raise ValueError("Delimited metadata file is missing a header row.") from exc
            if not columns:
                raise ValueError("Delimited metadata file is missing a header row.")

            rows = [[str(value).strip() for value in row] for row in reader]
            return self._pad_table([str(column).strip() for column in columns], rows)

    def _load_json_table(self, source_path: Path) -> "_RawMetadataTable":
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON metadata file: {exc}") from exc

        if isinstance(payload, list):
            records = [self._normalize_flat_record(record, f"JSON record {index}") for index, record in enumerate(payload, 1)]
            return self._records_to_table(records)
        if isinstance(payload, dict):
            list_values = [value for value in payload.values() if isinstance(value, list)]
            object_lists = [value for value in list_values if all(isinstance(item, dict) for item in value)]
            if len(object_lists) == 1:
                records = [
                    self._normalize_flat_record(record, f"JSON record {index}")
                    for index, record in enumerate(object_lists[0], 1)
                ]
                return self._records_to_table(records)
            return self._records_to_table([self._normalize_flat_record(payload, "JSON object")])
        raise ValueError("JSON metadata must contain an object, an array of objects, or one object-array field.")

    def _load_jsonl_table(self, source_path: Path) -> "_RawMetadataTable":
        records: list[dict[str, str]] = []
        with source_path.open("r", encoding="utf-8-sig") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSONL record at line {line_number}: {exc}") from exc
                records.append(self._normalize_flat_record(record, f"JSONL line {line_number}"))
        return self._records_to_table(records)

    def _load_xml_table(self, source_path: Path) -> "_RawMetadataTable":
        try:
            root = ElementTree.fromstring(source_path.read_text(encoding="utf-8-sig"))
        except ElementTree.ParseError as exc:
            raise ValueError(f"Invalid XML metadata file: {exc}") from exc

        children = list(root)
        if not children:
            raise ValueError("XML metadata does not contain row elements.")
        if self._xml_children_look_like_fields(root):
            return self._records_to_table([self._normalize_xml_fields(root, "XML root")])
        if len(children) == 1 and not self._xml_children_look_like_fields(children[0]):
            children = list(children[0])
        records = [self._normalize_xml_fields(child, f"XML row {index}") for index, child in enumerate(children, 1)]
        return self._records_to_table(records)

    def _normalize_flat_record(self, record: object, context: str) -> dict[str, str]:
        if not isinstance(record, dict):
            raise ValueError(f"{context} must be an object.")
        normalized: dict[str, str] = {}
        for key, value in record.items():
            if isinstance(value, (dict, list)):
                raise ValueError(f"{context} contains nested data in field {key!r}.")
            normalized[str(key).strip()] = "" if value is None else str(value).strip()
        return normalized

    def _normalize_xml_fields(self, element: ElementTree.Element, context: str) -> dict[str, str]:
        row = {str(name).strip(): str(value).strip() for name, value in element.attrib.items()}
        for child in list(element):
            if list(child):
                raise ValueError(f"{context} contains nested XML field {child.tag!r}.")
            name = str(child.tag).strip()
            if name in row:
                raise ValueError(f"{context} contains duplicate field {name!r}.")
            row[name] = (child.text or "").strip()
        if not row and (element.text or "").strip():
            row["value"] = (element.text or "").strip()
        return row

    @staticmethod
    def _xml_children_look_like_fields(element: ElementTree.Element) -> bool:
        children = list(element)
        return bool(children) and all(not list(child) for child in children) and len({child.tag for child in children}) == len(children)

    @staticmethod
    def _records_to_table(records: list[dict[str, str]]) -> "_RawMetadataTable":
        columns: list[str] = []
        seen: set[str] = set()
        for record in records:
            for column in record:
                if column not in seen:
                    seen.add(column)
                    columns.append(column)
        rows = [[record.get(column, "") for column in columns] for record in records]
        return _RawMetadataTable(columns=columns, rows=rows)

    @staticmethod
    def _pad_table(columns: list[str], rows: list[list[str]]) -> "_RawMetadataTable":
        width = max([len(columns), *(len(row) for row in rows)], default=len(columns))
        padded_columns = [*columns, *([""] * (width - len(columns)))]
        padded_rows = [[*row, *([""] * (width - len(row)))] for row in rows]
        return _RawMetadataTable(columns=padded_columns, rows=padded_rows)

    @staticmethod
    def _column_is_empty(values: list[str]) -> bool:
        return all(not value.strip() for value in values)

    @staticmethod
    def _looks_like_index_values(values: list[str]) -> bool:
        if not values:
            return False
        try:
            integers = [int(value) for value in values]
        except ValueError:
            return False
        return len(set(integers)) == len(integers)

    @staticmethod
    def _row_ids_from_index_values(values: list[str]) -> list[int]:
        row_ids = [int(value) for value in values]
        if min(row_ids) < 0:
            raise ValueError("Metadata index values must not be negative.")
        if min(row_ids) == 0:
            return [row_id + 1 for row_id in row_ids]
        return row_ids

    @staticmethod
    def _looks_like_named_index_column(name: str, values: list[str]) -> bool:
        normalized = name.strip().lower()
        index_names = {"index", "row_index", "row id", "row_id", "rowid"}
        return normalized in index_names and MetadataService._looks_like_index_values(values)

    def _dedupe_column_names(self, columns: list[str]) -> list[str]:
        used_lower: set[str] = set()
        deduped: list[str] = []
        for column in columns:
            candidate = self._next_available_column_name(column, used_lower)
            used_lower.add(candidate.lower())
            deduped.append(candidate)
        return deduped

    @staticmethod
    def _next_available_column_name(base_name: str, existing_lower: set[str]) -> str:
        candidate = base_name
        suffix = 2
        while candidate.lower() in existing_lower:
            candidate = f"{base_name}_{suffix}"
            suffix += 1
        return candidate


@dataclass(frozen=True)
class _RawMetadataTable:
    columns: list[str]
    rows: list[list[str]]


@dataclass(frozen=True)
class _NormalizedMetadata:
    columns: list[str]
    rows: list[dict[str, str]]
    row_ids: list[int] | None = None


@dataclass(frozen=True)
class _MergeMatchData:
    messages: list[str]
    existing_row_count: int
    existing_row_ids_by_value: dict[str, int]
    incoming_rows_by_value: dict[str, dict[str, str]]
    matched_values: list[str]
    unmatched_incoming_values: list[str]
    unmatched_existing_count: int
    unmatched_incoming_count: int
