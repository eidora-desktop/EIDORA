from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import csv
import json
import hashlib
import shutil
import xml.etree.ElementTree as ET
import zipfile

from EmbeddingsArea.domain import (
    MediaContentType, MediaMetadataMergePreview, MediaSource, MediaSourceKind,
    MediaSourceStatus, MediaSourceSummary, MediaProgress, MediaAsset,
)
from EmbeddingsArea.media_sources import (
    MediaScanResult, MediaSourceInspection, build_metadata_text_assets,
    detect_content_type, iter_inspected_media_assets, iter_media_source_assets,
    label_for_path, scan_media_source, source_id_for_path, source_kind_for_path,
)
from EmbeddingsArea.repository import MediaRepository, MediaLinkRepository
from MetadataArea.repository import MetadataRepository
from MetadataArea.service import MetadataService
from MetadataArea.domain import MetadataAddRowsMergeRequest
from project.domain import Project


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_metadata_column_name(value):
    value = str(value or "").strip()
    return "record_id" if value.lower() == "id" else value


class MediaLibraryService:
    def __init__(
        self,
        project: Project,
        media_repository: MediaRepository,
        metadata_repository: MetadataRepository,
        link_repository: MediaLinkRepository,
    ) -> None:
        self._project = project
        self._media_repository = media_repository
        self._metadata_repository = metadata_repository
        self._link_repository = link_repository

    def register_source(
        self,
        path: Path | str,
        content_type: MediaContentType | str | None = None,
        *,
        recursive: bool | None = None,
    ) -> MediaSource:
        candidate = self.prepare_source(path, content_type, recursive=recursive)
        existing = self._media_repository.get_source_for_path(
            candidate.path,
            candidate.content_type,
        )
        source = self.prepare_source(
            path,
            candidate.content_type,
            recursive=recursive,
            existing=existing,
        )
        self._media_repository.upsert_source(source)
        return source

    @staticmethod
    def prepare_source(path, content_type=None, *, recursive=None, existing=None):
        """Describe a source without database access or filesystem discovery."""
        source_path = Path(path).expanduser()
        if not source_path.exists():
            raise FileNotFoundError(source_path)
        if not source_path.is_file() and not source_path.is_dir():
            raise ValueError(f"Media source must be a file or directory: {source_path}")
        resolved_type = (MediaContentType(content_type) if content_type is not None else
                         MediaContentType.IMAGE if source_path.is_dir() or source_path.suffix.lower() == '.zip' else
                         detect_content_type(source_path))
        if existing is not None:
            # A source's media type defines the identity of its indexed assets.
            # Reimporting the same path must not silently invalidate links.
            resolved_type = existing.content_type
        now = _utc_now()
        should_recurse = bool(recursive) if recursive is not None else (existing.recursive if existing else source_path.is_dir())
        return MediaSource(
            id=(
                existing.id
                if existing is not None
                else source_id_for_path(source_path, resolved_type)
            ),
            label=label_for_path(source_path),
            path=source_path.resolve(),
            source_kind=source_kind_for_path(source_path),
            content_type=resolved_type,
            recursive=should_recurse,
            status=existing.status if existing is not None else MediaSourceStatus.UNLINKED,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
            last_scanned_at=existing.last_scanned_at if existing is not None else None,
            scan_cursor=existing.scan_cursor if existing is not None else "",
            source_revision=existing.source_revision if existing is not None else 0,
            error_message="",
        )

    def analysis_source(self, source_id: str) -> MediaSource:
        source = self._media_repository.get_source(source_id)
        if source is not None:
            return source
        prefix = "media_text_"
        if source_id.startswith(prefix):
            column = source_id[len(prefix):]
            if column in {value.name for value in self._metadata_repository.list_columns()} - {"id"}:
                return self.register_metadata_text_source(column)
        return self.require_source(source_id)

    def list_analysis_sources(self) -> list[MediaSourceSummary]:
        """Offer metadata columns without materializing or scanning them in the GUI."""
        sources = self.list_source_summaries()
        existing = {value.source.id for value in sources}
        row_count = self._metadata_repository.count_rows()
        if row_count:
            for column in self._metadata_repository.list_columns():
                key = f"media_text_{column.name}"
                if column.name == "id" or key in existing:
                    continue
                source = MediaSource(
                    id=key, label=f"{column.name} (metadata text)",
                    path=self._metadata_text_path(column.name),
                    source_kind=MediaSourceKind.METADATA_COLUMN, content_type=MediaContentType.TEXT,
                )
                sources.append(MediaSourceSummary(source, row_count, 0, row_count, 0, 0))
        # Metadata text gets a fresh snapshot in the worker at preparation time.
        return [replace(value, asset_count=row_count, missing_count=0, error_count=0)
                if value.source.source_kind == MediaSourceKind.METADATA_COLUMN else value
                for value in sources]

    def register_metadata_text_source(self, metadata_column: str) -> MediaSource:
        column = metadata_column.strip()
        if not column:
            raise ValueError("Choose a metadata column.")
        path = self._metadata_text_path(column)
        existing = self._media_repository.get_source(f"media_text_{column}")
        now = _utc_now()
        source = MediaSource(
            id=existing.id if existing is not None else f"media_text_{column}",
            label=f"{column} (metadata text)",
            path=path,
            source_kind=MediaSourceKind.METADATA_COLUMN,
            content_type=MediaContentType.TEXT,
            recursive=False,
            status=existing.status if existing is not None else MediaSourceStatus.UNLINKED,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
            last_scanned_at=existing.last_scanned_at if existing is not None else None,
            scan_cursor=existing.scan_cursor if existing is not None else "",
            source_revision=existing.source_revision if existing is not None else 0,
            error_message="",
        )
        self._media_repository.upsert_source(source)
        return source

    def scan_source(
        self,
        source_id: str,
        *,
        progress_callback: Callable[[int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        stage_callback: Callable[[MediaProgress], None] | None = None,
        inspection: MediaSourceInspection | None = None,
    ) -> MediaSourceSummary:
        source = self.require_source(source_id)
        started_at = _utc_now()
        indexed_count = 0
        scan_cursor = ""
        error_message = ""
        result: MediaScanResult | None = None
        if inspection is not None:
            indexed_count, scan_cursor = self._media_repository.synchronize_source_assets(
                source.id,
                iter_inspected_media_assets(
                    source,
                    inspection,
                    archive_extract_root=self._project.paths.media_sources / source.id,
                    scanned_at=started_at,
                    should_cancel=should_cancel,
                ),
                updated_at=started_at,
                progress_callback=progress_callback,
                should_cancel=should_cancel,
                stage_callback=stage_callback,
            )
        elif source.source_kind == MediaSourceKind.METADATA_COLUMN:
            column_name = source.id.removeprefix("media_text_")
            values = self._metadata_repository.column_values_by_id(column_name)
            materialized_path = self._materialize_metadata_text_source(column_name, values)
            source = replace(source, path=materialized_path)
            result = build_metadata_text_assets(
                source,
                materialized_path=materialized_path,
                values_by_row_id=values,
                scanned_at=started_at,
            )
            indexed_count, scan_cursor = self._media_repository.synchronize_source_assets(
                source.id,
                result.assets,
                updated_at=started_at,
                progress_callback=progress_callback,
                should_cancel=should_cancel,
                stage_callback=stage_callback,
            )
        elif source.content_type == MediaContentType.TEXT and source.path.suffix.lower() in {".jsonl", ".ndjson"}:
            result = scan_media_source(source, scanned_at=started_at)
            if not result.error_message:
                indexed_count, scan_cursor = self._media_repository.synchronize_source_assets(
                    source.id,
                    result.assets,
                    updated_at=started_at,
                    progress_callback=progress_callback,
                    should_cancel=should_cancel,
                    stage_callback=stage_callback,
                )
        elif source.path.suffix.lower() == ".zip":
            result = self._scan_archive_source(source, started_at)
            if not result.error_message:
                indexed_count, scan_cursor = self._media_repository.synchronize_source_assets(
                    source.id,
                    result.assets,
                    updated_at=started_at,
                    progress_callback=progress_callback,
                    should_cancel=should_cancel,
                    stage_callback=stage_callback,
                )
        else:
            path = source.path.expanduser()
            if not path.exists():
                error_message = "Source path does not exist."
            elif source.source_kind == MediaSourceKind.FILE and not path.is_file():
                error_message = "Source file does not exist."
            elif source.source_kind == MediaSourceKind.DIRECTORY and not path.is_dir():
                error_message = "Source directory does not exist."
            else:
                try:
                    indexed_count, scan_cursor = self._media_repository.synchronize_source_assets(
                        source.id,
                        iter_media_source_assets(source, started_at, should_cancel=should_cancel),
                        updated_at=started_at,
                        progress_callback=progress_callback,
                        should_cancel=should_cancel,
                        stage_callback=stage_callback,
                    )
                except OSError as exc:
                    error_message = str(exc)
        if result is not None:
            error_message = error_message or result.error_message
        summaries = {summary.source.id: summary for summary in self._media_repository.list_source_summaries()}
        summary = summaries.get(source.id)
        missing_count = 0 if summary is None else summary.missing_count
        linked_count = 0 if summary is None else summary.linked_count
        finished_at = _utc_now()
        updated_source = MediaSource(
            id=source.id,
            label=source.label,
            path=source.path,
            source_kind=source.source_kind,
            content_type=source.content_type,
            recursive=source.recursive,
            status=self._status_after_scan(error_message, indexed_count, linked_count),
            created_at=source.created_at,
            updated_at=finished_at,
            last_scanned_at=finished_at,
            scan_cursor=scan_cursor,
            source_revision=self.require_source(source.id).source_revision,
            error_message=error_message,
        )
        self._media_repository.upsert_source(updated_source)
        self._media_repository.record_scan_event(
            source.id,
            started_at=started_at,
            finished_at=finished_at,
            discovered_count=indexed_count,
            indexed_count=indexed_count,
            missing_count=missing_count,
            error_message=error_message,
        )
        refreshed = {summary.source.id: summary for summary in self._media_repository.list_source_summaries()}
        return refreshed[source.id]

    def list_source_summaries(self) -> list[MediaSourceSummary]:
        return self._media_repository.list_source_summaries()

    def list_source_assets(self, source_id: str, *, limit: int = 200, offset: int = 0) -> list[MediaAsset]:
        self.require_source(source_id)
        return self._media_repository.fetch_assets(source_id, limit=limit, offset=offset)

    def remove_source(self, source_id: str) -> None:
        source = self.require_source(source_id)
        remaining_paths = [
            value.path.resolve() for value in self._media_repository.list_sources()
            if value.id != source_id
        ]
        generated_root = self._project.paths.generated_text_sources
        candidates = [self._project.paths.media_sources / source.id]
        if source.path.resolve().is_relative_to(generated_root.resolve()):
            candidates.append(source.path)
        for path in candidates:
            resolved = path.resolve()
            # Only EIDORA-owned storage is disposable. Never follow an owned
            # folder's symlink into external originals or another project.
            if (
                not resolved.is_relative_to(self._project.root.resolve())
                or not resolved.is_relative_to(self._project.paths.media.resolve())
                or resolved == self._project.paths.media.resolve()
            ):
                continue
            if any(
                other.is_relative_to(resolved) or resolved.is_relative_to(other)
                for other in remaining_paths
            ):
                continue
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        if (
            generated_root.is_dir()
            and not generated_root.is_symlink()
            and not any(value.is_relative_to(generated_root.resolve()) for value in remaining_paths)
            and not any(generated_root.iterdir())
        ):
            generated_root.rmdir()
        self._media_repository.delete_source(source_id)

    def rename_source(self, source_id: str, label: str) -> MediaSource:
        source = self.require_source(source_id)
        new_label = label.strip()
        if not new_label:
            raise ValueError("Media source name cannot be empty.")
        renamed = replace(source, label=new_label, updated_at=_utc_now())
        self._media_repository.upsert_source(renamed)
        return renamed

    def preview_metadata_from_source(
        self,
        source_id: str,
        *,
        existing_identifier_column: str = "",
        incoming_identifier_column: str = "",
    ) -> MediaMetadataMergePreview:
        source = self.require_source(source_id)
        rows, columns = self._metadata_rows_for_source(source)
        if not rows or not columns:
            raise ValueError("This source does not expose structured metadata rows yet.")
        if not self._metadata_repository.is_initialized() or self._metadata_repository.count_rows() == 0:
            return MediaMetadataMergePreview(
                source_id=source.id,
                columns=tuple(columns),
                row_count=len(rows),
            )
        existing_identifier = existing_identifier_column.strip()
        incoming_identifier = incoming_identifier_column.strip()
        if not existing_identifier or not incoming_identifier:
            raise ValueError("Choose identifier columns before merging media-derived metadata into existing metadata.")

        request = MetadataAddRowsMergeRequest(
            incoming_rows=rows,
            existing_identifier_column=existing_identifier,
            incoming_identifier_column=incoming_identifier,
        )
        preview = MetadataService(self._metadata_repository).preview_add_rows_merge(request)
        return MediaMetadataMergePreview(
            source_id=source.id,
            columns=tuple(columns),
            row_count=len(rows),
            merge_preview=preview,
        )

    def create_metadata_from_source(
        self,
        source_id: str,
        *,
        existing_identifier_column: str = "",
        incoming_identifier_column: str = "",
    ) -> dict[str, object]:
        source = self.require_source(source_id)
        rows, columns = self._metadata_rows_for_source(source)
        if not rows or not columns:
            raise ValueError("This source does not expose structured metadata rows yet.")
        if not self._metadata_repository.is_initialized():
            rows, columns = self._bootstrap_metadata_shape(rows, columns)
            self._metadata_repository.initialize_from_rows(rows, columns)
            action = "initialized"
            merge_preview = None
        elif self._metadata_repository.count_rows() == 0:
            rows, columns = self._bootstrap_metadata_shape(rows, columns)
            self._metadata_repository.initialize_from_rows(rows, columns)
            action = "initialized"
            merge_preview = None
        else:
            existing_identifier = existing_identifier_column.strip()
            incoming_identifier = incoming_identifier_column.strip()
            if not existing_identifier or not incoming_identifier:
                raise ValueError("Choose identifier columns before merging media-derived metadata into existing metadata.")
            request = MetadataAddRowsMergeRequest(
                incoming_rows=rows,
                existing_identifier_column=existing_identifier,
                incoming_identifier_column=incoming_identifier,
            )
            merge_preview = MetadataService(self._metadata_repository).merge_add_rows(request)
            action = "merged"
        return {
            "action": action,
            "row_count": len(rows),
            "columns": tuple(columns),
            "merge_preview": merge_preview,
        }

    @staticmethod
    def _bootstrap_metadata_shape(
        rows: list[dict[str, str]],
        columns: list[str],
    ) -> tuple[list[dict[str, str]], list[str]]:
        # For metadata initialized from unlinked media assets, keep only filename + internal id.
        if "filename" not in columns:
            return rows, columns
        return ([{"filename": str(row.get("filename", "")).strip()} for row in rows], ["filename"])

    def require_source(self, source_id: str) -> MediaSource:
        source = self._media_repository.get_source(source_id)
        if source is None:
            raise ValueError(f"Unknown media source: {source_id}")
        return source

    def _scan_archive_source(self, source: MediaSource, scanned_at: str):
        extract_root = self._project.paths.media_sources / source.id
        if extract_root.exists():
            shutil.rmtree(extract_root, ignore_errors=True)
        extract_root.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(source.path) as archive:
                for member in archive.infolist():
                    member_name = str(member.filename or "")
                    if not member_name or member.is_dir():
                        continue
                    destination = (extract_root / member_name).resolve()
                    try:
                        destination.relative_to(extract_root.resolve())
                    except ValueError:
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as src, destination.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
        except (OSError, zipfile.BadZipFile) as exc:
            return MediaScanResult(assets=[], discovered_count=0, error_message=str(exc))
        extracted_source = MediaSource(
            id=source.id,
            label=source.label,
            path=extract_root,
            source_kind=MediaSourceKind.DIRECTORY,
            content_type=source.content_type,
            recursive=True,
            status=source.status,
            created_at=source.created_at,
            updated_at=source.updated_at,
            last_scanned_at=source.last_scanned_at,
            scan_cursor=source.scan_cursor,
            source_revision=source.source_revision,
            error_message=source.error_message,
        )
        return scan_media_source(extracted_source, scanned_at=scanned_at)

    def _metadata_rows_for_source(self, source: MediaSource) -> tuple[list[dict[str, str]], list[str]]:
        if source.source_kind == MediaSourceKind.METADATA_COLUMN:
            raise ValueError("This source already derives from metadata.")
        suffix = source.path.suffix.lower()
        if suffix in {".csv", ".tsv"}:
            return self._metadata_rows_from_delimited(source.path, delimiter="\t" if suffix == ".tsv" else ",")
        if suffix == ".json":
            return self._metadata_rows_from_json(source.path)
        if suffix in {".xml"}:
            return self._metadata_rows_from_xml(source.path)
        if suffix in {".jsonl", ".ndjson"}:
            return self._metadata_rows_from_jsonl(source.path)
        assets = self._media_repository.fetch_assets(source.id, limit=1_000_000, offset=0)
        if source.content_type == MediaContentType.TEXT and source.path.is_file():
            text = source.path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                return (
                    [{"source_key": source.path.name, "text": text}],
                    ["source_key", "text"],
                )
        rows = [
            {
                "filename": asset.display_name,
                "relative_path": asset.relative_path,
                "source_key": asset.source_key,
            }
            for asset in assets
        ]
        return rows, ["filename", "relative_path", "source_key"]

    @staticmethod
    def _metadata_rows_from_delimited(path: Path, *, delimiter: str) -> tuple[list[dict[str, str]], list[str]]:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=delimiter)
            if reader.fieldnames is None:
                raise ValueError("This file does not contain header columns.")
            columns = [_safe_metadata_column_name(str(name).strip()) for name in reader.fieldnames if str(name).strip()]
            rows = [
                {
                    _safe_metadata_column_name(column): str(row.get(column, "") or "").strip()
                    for column in reader.fieldnames or []
                    if str(column).strip()
                }
                for row in reader
            ]
        return rows, columns

    @staticmethod
    def _metadata_rows_from_json(path: Path) -> tuple[list[dict[str, str]], list[str]]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list) or not payload or not all(isinstance(item, dict) for item in payload):
            raise ValueError("JSON source must contain one object or a list of objects.")
        columns = sorted({_safe_metadata_column_name(str(key).strip()) for item in payload for key in item if str(key).strip()})
        rows = [
            {_safe_metadata_column_name(str(column).strip()): str(value or "").strip() for column, value in item.items() if str(column).strip()}
            for item in payload
        ]
        return rows, columns

    @staticmethod
    def _metadata_rows_from_jsonl(path: Path) -> tuple[list[dict[str, str]], list[str]]:
        rows: list[dict[str, str]] = []
        columns: set[str] = set()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = line.strip()
            if not raw:
                continue
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                continue
            row = {
                _safe_metadata_column_name(str(key).strip()): str(value or "").strip()
                for key, value in payload.items()
                if str(key).strip()
            }
            if row:
                rows.append(row)
                columns.update(row)
        return rows, sorted(columns)

    @staticmethod
    def _metadata_rows_from_xml(path: Path) -> tuple[list[dict[str, str]], list[str]]:
        root = ET.fromstring(path.read_text(encoding="utf-8", errors="replace"))
        rows: list[dict[str, str]] = []
        columns: set[str] = set()
        for child in list(root):
            row: dict[str, str] = {}
            for element in list(child):
                key = _safe_metadata_column_name(element.tag.strip())
                value = (element.text or "").strip()
                if key:
                    row[key] = value
                    columns.add(key)
            if row:
                rows.append(row)
        if not rows:
            raise ValueError("XML source did not contain row-like child elements.")
        return rows, sorted(columns)

    def _metadata_text_path(self, column: str) -> Path:
        key = hashlib.sha256(column.encode("utf-8")).hexdigest()[:24]
        return self._project.paths.generated_text_sources / f"column_{key}.jsonl"

    def _materialize_metadata_text_source(self, column: str, values_by_row_id: dict[int, str]) -> Path:
        output_path = self._metadata_text_path(column)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        for row_id, text in sorted(values_by_row_id.items()):
            normalized = str(text or "").strip()
            if not normalized:
                continue
            lines.append(json.dumps({"id": str(row_id), "text": normalized}, ensure_ascii=True))
        output_path.write_text("\n".join(lines), encoding="utf-8")
        return output_path

    @staticmethod
    def _content_type(value: MediaContentType | str) -> MediaContentType:
        normalized = value.value if isinstance(value, MediaContentType) else str(value).strip().lower()
        return MediaContentType(normalized)

    @staticmethod
    def _status_after_scan(error_message: str, indexed_count: int, linked_count: int) -> MediaSourceStatus:
        if error_message:
            return MediaSourceStatus.MISSING if "does not exist" in error_message else MediaSourceStatus.ERROR
        if indexed_count == 0 or linked_count == 0:
            return MediaSourceStatus.UNLINKED
        return MediaSourceStatus.READY
