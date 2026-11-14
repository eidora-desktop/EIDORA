from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import json
import hashlib
import shutil
import zipfile

from EmbeddingsArea.domain import (
    MediaContentType, MediaSource, MediaSourceKind,
    MediaSourceStatus, MediaSourceSummary, MediaProgress, MediaAsset,
)
from EmbeddingsArea.media_sources import (
    MediaScanResult, MediaSourceInspection, build_metadata_text_assets,
    detect_content_type, iter_inspected_media_assets, iter_media_source_assets,
    label_for_path, scan_media_source, source_id_for_path, source_kind_for_path,
)
from EmbeddingsArea.repository import MediaRepository, MediaLinkRepository
from MetadataArea.repository import MetadataRepository
from project.domain import Project


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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

    def assets_for_items(self, item_ids, *, source_ids=()):
        return self._media_repository.assets_for_items(item_ids, source_ids=source_ids)

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
            if any(value.source.id == source_id for value in self.list_analysis_sources()):
                return self.register_metadata_text_source(column)
        return self.require_source(source_id)

    def list_analysis_sources(self) -> list[MediaSourceSummary]:
        """Offer metadata columns without materializing or scanning them in the GUI."""
        sources = self.list_source_summaries()
        existing = {value.source.id for value in sources}
        from MetadataArea.table_repository import MetadataTableRepository, list_tables
        store = MetadataTableRepository(self._metadata_repository.database_path)
        counts, labels = {}, {}
        for table in list_tables(store.database):
            repository = store.repository(table.id)
            row_count = repository.count_rows()
            fields = store.field_ids(table.id)
            for column in repository.list_columns():
                if column.name == 'id':
                    continue
                reference = column.name if table.id == 'metadata' else f'table:{table.id}:{fields[column.name]}'
                key = f'media_text_{reference}'
                counts[key] = row_count
                labels[key] = f"{table.name}: {column.label} (metadata text)"
                if row_count and key not in existing:
                    label = column.label if table.id == 'metadata' else f'{table.name}: {column.label}'
                    source = MediaSource(id=key, label=f'{label} (metadata text)',
                        path=self._metadata_text_path(reference), source_kind=MediaSourceKind.METADATA_COLUMN,
                        content_type=MediaContentType.TEXT)
                    sources.append(MediaSourceSummary(source, row_count, 0, row_count, 0, 0))
        return [replace(value, source=replace(value.source, label=labels.get(value.source.id, value.source.label)),
                        asset_count=counts.get(value.source.id, 0), missing_count=0, error_count=0)
                if value.source.source_kind == MediaSourceKind.METADATA_COLUMN else value for value in sources]

    def metadata_text_values(self, field):
        if not field.startswith('table:'):
            return self._metadata_repository.column_values_by_id(field)
        from MetadataArea.table_repository import MetadataTableRepository
        _, table, field_id = field.split(':', 2)
        store = MetadataTableRepository(self._metadata_repository.database_path)
        column = next((name for name, identity in store.field_ids(table).items() if identity == field_id), None)
        if column is None:
            raise ValueError('The selected metadata field no longer exists.')
        return store.repository(table).column_values_by_id(column)

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

    def relocate_source(self, source_id, path, *, progress=None, should_cancel=None):
        source = self.require_source(source_id)
        if source.source_kind == MediaSourceKind.METADATA_COLUMN:
            raise ValueError("Metadata text sources are managed by the project.")
        candidate = self.prepare_source(path, source.content_type, recursive=source.recursive, existing=source)
        candidate = replace(candidate, label=source.label)
        if candidate.source_kind != source.source_kind:
            raise ValueError("Choose the same kind of source (file, folder, or archive).")
        if source.source_kind == MediaSourceKind.FILE and candidate.path.name != source.path.name:
            raise ValueError("Choose the original filename at its new location.")
        registered = self._media_repository.get_source_for_path(candidate.path, candidate.content_type)
        if registered is not None and registered.id != source_id:
            raise ValueError("This location is already registered. Choose another location.")
        return self.scan_source(source_id, stage_callback=progress, should_cancel=should_cancel,
                                replacement_source=candidate)

    def scan_source(
        self,
        source_id: str,
        *,
        progress_callback: Callable[[int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        stage_callback: Callable[[MediaProgress], None] | None = None,
        inspection: MediaSourceInspection | None = None,
        replacement_source: MediaSource | None = None,
    ) -> MediaSourceSummary:
        source = replacement_source or self.require_source(source_id)
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
                replacement_path=source.path if replacement_source else None,
            )
        elif source.source_kind == MediaSourceKind.METADATA_COLUMN:
            column_name = source.id.removeprefix("media_text_")
            values = self.metadata_text_values(column_name)
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
                replacement_path=source.path if replacement_source else None,
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
                    replacement_path=source.path if replacement_source else None,
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
                    replacement_path=source.path if replacement_source else None,
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
                        replacement_path=source.path if replacement_source else None,
                    )
                except OSError as exc:
                    error_message = str(exc)
        if result is not None:
            error_message = error_message or result.error_message
        summaries = {summary.source.id: summary for summary in self._media_repository.list_source_summaries()}
        summary = summaries.get(source.id)
        missing_count = 0 if summary is None else summary.missing_count
        linked_count = 0 if summary is None else summary.linked_count
        if replacement_source and error_message:
            raise ValueError(error_message)
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
        if indexed_count == 0:
            return MediaSourceStatus.UNLINKED
        return MediaSourceStatus.READY
