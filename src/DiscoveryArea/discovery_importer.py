from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

import numpy as np


@dataclass(frozen=True)
class DiscoveryImportPreview:
    path: Path
    format: str
    columns: list[str]
    rows: list[dict[str, object]]
    preview_rows: list[dict[str, object]]


@dataclass(frozen=True)
class DiscoveryImportValidationReport:
    total_rows: int
    matched_rows: int
    skipped_invalid_rows: int
    duplicate_metadata_ids: int
    unmatched_metadata_ids: int
    missing_project_metadata_ids: int
    messages: list[str]


@dataclass(frozen=True)
class DiscoveryImportResult:
    metadata_ids: np.ndarray
    coordinates: np.ndarray
    source_summary: dict[str, object]
    validation_report: DiscoveryImportValidationReport


def preview_discovery_import(path: Path) -> DiscoveryImportPreview:
    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == ".csv":
        rows = _read_delimited_rows(source, delimiter=",")
        return _preview(source, "csv", rows)
    if suffix == ".tsv":
        rows = _read_delimited_rows(source, delimiter="\t")
        return _preview(source, "tsv", rows)
    if suffix == ".json":
        rows = _read_json_records(source)
        return _preview(source, "json", rows)
    if suffix == ".xlsx":
        rows = _read_xlsx_rows(source)
        return _preview(source, "xlsx", rows)
    raise ValueError("Discovery import must be CSV, TSV, XLSX, or JSON.")


def import_discovery_coordinates(
    path: Path,
    *,
    metadata_ids: np.ndarray,
    id_column: str,
    x_column: str,
    y_column: str,
    metadata_key_values_by_id: dict[int, str] | None = None,
    metadata_column: str | None = None,
    key_is_path_like: bool | None = None,
) -> DiscoveryImportResult:
    preview = preview_discovery_import(Path(path))
    for column in (id_column, x_column, y_column):
        if column not in preview.columns:
            raise ValueError(f"Discovery import is missing column: {column}")
    if x_column == y_column:
        raise ValueError("Discovery import x and y columns must be different.")
    project_ids = {int(value) for value in np.asarray(metadata_ids, dtype=np.int64).tolist()}
    use_key_lookup = metadata_key_values_by_id is not None
    path_like = (
        _is_path_like_column(id_column) or _is_path_like_column(metadata_column or "")
        if key_is_path_like is None
        else bool(key_is_path_like)
    )
    metadata_id_by_key: dict[str, int] = {}
    if metadata_key_values_by_id is not None:
        for metadata_id, value in metadata_key_values_by_id.items():
            if int(metadata_id) not in project_ids:
                continue
            key = _match_key(value, path_like=path_like)
            if key and key not in metadata_id_by_key:
                metadata_id_by_key[key] = int(metadata_id)
    row_by_id: dict[int, tuple[float, float]] = {}
    skipped_invalid = 0
    duplicate_ids = 0
    unmatched_ids = 0
    for row in preview.rows:
        try:
            raw_identifier = str(row[id_column]).strip()
            metadata_id = (
                metadata_id_by_key.get(_match_key(raw_identifier, path_like=path_like))
                if use_key_lookup
                else int(raw_identifier)
            )
            x = float(row[x_column])
            y = float(row[y_column])
        except (KeyError, TypeError, ValueError):
            skipped_invalid += 1
            continue
        if metadata_id is None:
            unmatched_ids += 1
            continue
        if not np.isfinite([x, y]).all():
            skipped_invalid += 1
            continue
        if metadata_id not in project_ids:
            unmatched_ids += 1
            continue
        if metadata_id in row_by_id:
            duplicate_ids += 1
            continue
        row_by_id[metadata_id] = (x, y)

    aligned_ids: list[int] = []
    aligned_coordinates: list[tuple[float, float]] = []
    for metadata_id in np.asarray(metadata_ids, dtype=np.int64).tolist():
        coordinate = row_by_id.get(int(metadata_id))
        if coordinate is None:
            continue
        aligned_ids.append(int(metadata_id))
        aligned_coordinates.append(coordinate)

    missing_project_ids = int(np.asarray(metadata_ids).size - len(aligned_ids))
    messages = [
        f"Matched rows: {len(aligned_ids)}",
        f"Skipped invalid rows: {skipped_invalid}",
        f"Duplicate metadata IDs skipped: {duplicate_ids}",
        f"Rows outside project metadata skipped: {unmatched_ids}",
        f"Project metadata rows without imported coordinates: {missing_project_ids}",
    ]
    report = DiscoveryImportValidationReport(
        total_rows=len(preview.rows),
        matched_rows=len(aligned_ids),
        skipped_invalid_rows=skipped_invalid,
        duplicate_metadata_ids=duplicate_ids,
        unmatched_metadata_ids=unmatched_ids,
        missing_project_metadata_ids=missing_project_ids,
        messages=messages,
    )
    if len(aligned_ids) < 2:
        raise ValueError("Discovery import matched fewer than two metadata rows.")

    return DiscoveryImportResult(
        metadata_ids=np.asarray(aligned_ids, dtype=np.int64),
        coordinates=np.asarray(aligned_coordinates, dtype=np.float32),
        source_summary={
            "kind": "discovery_import",
            "file_name": preview.path.name,
            "format": preview.format,
            "id_column": id_column,
            "metadata_column": metadata_column or id_column,
            "x_column": x_column,
            "y_column": y_column,
            "total_rows": report.total_rows,
            "matched_rows": report.matched_rows,
            "skipped_invalid_rows": report.skipped_invalid_rows,
            "duplicate_metadata_ids": report.duplicate_metadata_ids,
            "unmatched_metadata_ids": report.unmatched_metadata_ids,
        },
        validation_report=report,
    )


def _preview(
    path: Path,
    format_name: str,
    rows: list[dict[str, object]],
) -> DiscoveryImportPreview:
    columns: list[str] = []
    for row in rows:
        for key in row.keys():
            text = str(key)
            if text and text not in columns:
                columns.append(text)
    return DiscoveryImportPreview(
        path=path,
        format=format_name,
        columns=columns,
        rows=rows,
        preview_rows=rows[:20],
    )


def _read_delimited_rows(path: Path, *, delimiter: str) -> list[dict[str, object]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        return [dict(row) for row in reader]


def _read_json_records(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Discovery JSON import must contain a list of records.")
    rows: list[dict[str, object]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("Discovery JSON import records must be objects.")
        rows.append(dict(item))
    return rows


def _read_xlsx_rows(path: Path) -> list[dict[str, object]]:
    with ZipFile(path) as archive:
        shared_strings = _xlsx_shared_strings(archive)
        sheet_path = _xlsx_sheet_path(archive)
        if not sheet_path:
            return []
        sheet = ElementTree.fromstring(archive.read(sheet_path))
    rows: list[list[str]] = []
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    for row in sheet.findall(".//x:sheetData/x:row", namespace):
        values: list[str] = []
        current_column = 0
        for cell in row.findall("x:c", namespace):
            column_index = _xlsx_column_index(cell.attrib.get("r", ""))
            while current_column < column_index:
                values.append("")
                current_column += 1
            values.append(_xlsx_cell_value(cell, shared_strings))
            current_column += 1
        rows.append(values)
    if not rows:
        return []
    headers = [str(value).strip() for value in rows[0]]
    return [
        {header: row[index] if index < len(row) else "" for index, header in enumerate(headers) if header}
        for row in rows[1:]
    ]


def _xlsx_sheet_path(archive: ZipFile) -> str:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    sheets = workbook.findall("x:sheets/x:sheet", namespace)
    selected = None
    for sheet in sheets:
        selected = sheet
        break
    if selected is None:
        return ""
    relationship_id = selected.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
    if not relationship_id:
        return ""
    rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rel_namespace = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
    for rel in rels.findall("r:Relationship", rel_namespace):
        if rel.attrib.get("Id") == relationship_id:
            return "xl/" + str(rel.attrib.get("Target", "")).lstrip("/")
    return ""


def _xlsx_shared_strings(archive: ZipFile) -> list[str]:
    try:
        payload = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ElementTree.fromstring(payload)
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    values: list[str] = []
    for item in root.findall("x:si", namespace):
        text_parts = [node.text or "" for node in item.findall(".//x:t", namespace)]
        values.append("".join(text_parts))
    return values


def _xlsx_cell_value(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    value = cell.find("x:v", namespace)
    raw = "" if value is None or value.text is None else value.text
    if cell.attrib.get("t") == "s":
        try:
            return shared_strings[int(raw)]
        except (ValueError, IndexError):
            return ""
    inline = cell.find("x:is/x:t", namespace)
    if inline is not None and inline.text is not None:
        return inline.text
    return raw


def _xlsx_column_index(cell_ref: str) -> int:
    letters = "".join(character for character in cell_ref if character.isalpha()).upper()
    if not letters:
        return 0
    value = 0
    for character in letters:
        value = value * 26 + (ord(character) - ord("A") + 1)
    return max(0, value - 1)


def _match_key(value: object, *, path_like: bool) -> str:
    text = str(value or "").strip()
    if path_like:
        text = text.replace("\\", "/").rsplit("/", 1)[-1].strip().casefold()
    return text


def _is_path_like_column(column: str) -> bool:
    normalized = str(column or "").strip().casefold()
    return normalized in {
        "file",
        "file_name",
        "filename",
        "filepath",
        "file_path",
        "image",
        "image_file",
        "image_name",
        "image_path",
        "name",
        "path",
        "relative_path",
        "uri",
        "url",
    }


ProjectionImportPreview = DiscoveryImportPreview
ProjectionImportValidationReport = DiscoveryImportValidationReport
ProjectionImportResult = DiscoveryImportResult
preview_projection_import = preview_discovery_import
import_projection_coordinates = import_discovery_coordinates
