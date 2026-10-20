from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import closing

from EmbeddingsArea.domain import (
    MediaAsset,
    MediaAssetStatus,
    MediaContentType,
    MediaProgress,
    MediaSource,
    MediaSourceKind,
)


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi",
    ".mpg", ".mpeg", ".ts", ".mts", ".m2ts",
}
AUDIO_EXTENSIONS = {
    ".wav", ".wave", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wma", ".aiff", ".aif",
}
TEXT_EXTENSIONS = {
    ".txt",
    ".text",
    ".md",
    ".markdown",
    ".json",
    ".jsonl",
    ".ndjson",
    ".csv",
    ".tsv",
    ".xml",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".log",
    ".rst",
    ".tex",
    ".srt",
    ".vtt",
    ".html",
    ".htm",
}
JSONL_EXTENSIONS = {".jsonl", ".ndjson"}
TEXT_KEYS = ("text", "content", "body", "prompt")
IDENTIFIER_KEYS = ("id", "identifier", "filename", "file", "name")


@dataclass(frozen=True)
class MediaScanResult:
    assets: list[MediaAsset]
    discovered_count: int
    scan_cursor: str = ""
    error_message: str = ""

    @property
    def items(self) -> list[MediaAsset]:
        return self.assets


@dataclass(frozen=True)
class MediaSourceInspection:
    """One background discovery pass retained for later catalog publication."""

    path: Path
    recursive: bool
    manifest_path: Path
    counts: tuple[tuple[MediaContentType, int], ...]

    @property
    def content_types(self) -> tuple[MediaContentType, ...]:
        return tuple(content_type for content_type, count in self.counts if count > 0)

    def cleanup(self) -> None:
        try:
            self.manifest_path.unlink(missing_ok=True)
        except OSError:
            # The worker may still be releasing a SQLite handle on Windows.
            pass


def inspect_media_source(
    path: Path,
    *,
    recursive: bool,
    should_cancel=None,
    stage_callback=None,
) -> MediaSourceInspection:
    """Discover supported files once and persist a bounded-memory manifest."""

    source_path = path.expanduser().resolve()
    handle, raw_manifest_path = tempfile.mkstemp(prefix="eidora-media-inspection-", suffix=".sqlite")
    os.close(handle)
    manifest_path = Path(raw_manifest_path)
    counts = {content_type: 0 for content_type in MediaContentType}
    completed = 0
    try:
        with closing(sqlite3.connect(manifest_path)) as connection, connection:
            connection.execute(
                """
                CREATE TABLE files (
                    relative_path TEXT PRIMARY KEY,
                    absolute_path TEXT NOT NULL,
                    archive_member TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    size_bytes INTEGER,
                    mtime_ns INTEGER
                )
                """
            )
            batch = []
            if source_path.is_file() and source_path.suffix.lower() == ".zip":
                with zipfile.ZipFile(source_path) as archive:
                    entries = (
                        (str(member.filename or ""), member)
                        for member in archive.infolist()
                        if not member.is_dir()
                    )
                    for member_name, member in entries:
                        if should_cancel and should_cancel():
                            raise RuntimeError("Media inspection cancelled.")
                        relative_member = Path(member_name)
                        content_type = _content_type_for_suffix(relative_member.suffix.lower())
                        if (
                            content_type is None
                            or not member_name
                            or relative_member.is_absolute()
                            or ".." in relative_member.parts
                        ):
                            continue
                        batch.append((member_name, "", member_name, content_type.value, int(member.file_size), None))
                        completed += 1
                        if len(batch) == 2000:
                            connection.executemany("INSERT OR IGNORE INTO files VALUES (?, ?, ?, ?, ?, ?)", batch)
                            batch.clear()
                            if stage_callback:
                                stage_callback(MediaProgress("Inspecting media", completed))
            else:
                root = source_path
                for file_path, stat in _candidate_file_entries(
                    source_path,
                    recursive=recursive,
                    should_cancel=should_cancel,
                ):
                    content_type = _content_type_for_file(file_path)
                    if content_type is None:
                        continue
                    relative_path = (
                        file_path.name if source_path.is_file()
                        else file_path.relative_to(root).as_posix()
                    )
                    batch.append((
                        relative_path,
                        str(file_path.resolve()),
                        "",
                        content_type.value,
                        int(stat.st_size),
                        _mtime_ns(stat),
                    ))
                    completed += 1
                    if len(batch) == 2000:
                        connection.executemany("INSERT OR IGNORE INTO files VALUES (?, ?, ?, ?, ?, ?)", batch)
                        batch.clear()
                        if stage_callback:
                            stage_callback(MediaProgress("Inspecting media", completed))
            if batch:
                connection.executemany("INSERT OR IGNORE INTO files VALUES (?, ?, ?, ?, ?, ?)", batch)
            for content_type_value, count in connection.execute(
                "SELECT content_type, COUNT(*) FROM files GROUP BY content_type"
            ):
                counts[MediaContentType(str(content_type_value))] = int(count)
            if stage_callback:
                stage_callback(MediaProgress("Inspecting media", completed, completed))
        return MediaSourceInspection(
            path=source_path,
            recursive=recursive,
            manifest_path=manifest_path,
            counts=tuple((content_type, counts[content_type]) for content_type in MediaContentType),
        )
    except BaseException:
        manifest_path.unlink(missing_ok=True)
        raise


def cleanup_media_source_inspection(inspection: MediaSourceInspection | None) -> None:
    if inspection is not None:
        inspection.cleanup()


def iter_inspected_media_assets(
    source: MediaSource,
    inspection: MediaSourceInspection,
    *,
    archive_extract_root: Path,
    scanned_at: str,
    should_cancel=None,
) -> Iterator[MediaAsset]:
    """Build selected assets from a saved discovery pass without traversing again."""

    if inspection.path != source.path.expanduser().resolve():
        raise ValueError("The media inspection does not belong to this source.")
    with closing(sqlite3.connect(inspection.manifest_path)) as connection:
        rows = connection.execute(
            """
            SELECT relative_path, absolute_path, archive_member, size_bytes, mtime_ns
            FROM files
            WHERE content_type = ?
            ORDER BY rowid
            """,
            (source.content_type.value,),
        )
        if source.path.suffix.lower() == ".zip":
            if archive_extract_root.exists():
                shutil.rmtree(archive_extract_root, ignore_errors=True)
            archive_extract_root.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(source.path) as archive:
                for relative_path, _absolute_path, member_name, _size, _mtime in rows:
                    if should_cancel and should_cancel():
                        raise RuntimeError("Media indexing cancelled.")
                    destination = (archive_extract_root / str(relative_path)).resolve()
                    try:
                        destination.relative_to(archive_extract_root.resolve())
                    except ValueError:
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(str(member_name)) as input_stream, destination.open("wb") as output_stream:
                        shutil.copyfileobj(input_stream, output_stream)
                    stat = destination.stat()
                    yield _inspected_media_asset(
                        source,
                        relative_path=str(relative_path),
                        absolute_path=destination,
                        size_bytes=int(stat.st_size),
                        mtime_ns=_mtime_ns(stat),
                        scanned_at=scanned_at,
                    )
            return
        for relative_path, absolute_path, _member_name, _recorded_size, _recorded_mtime in rows:
            if should_cancel and should_cancel():
                raise RuntimeError("Media indexing cancelled.")
            asset_path = Path(str(absolute_path))
            try:
                stat = asset_path.stat()
            except OSError:
                continue
            yield _inspected_media_asset(
                source,
                relative_path=str(relative_path),
                absolute_path=asset_path,
                size_bytes=int(stat.st_size),
                mtime_ns=_mtime_ns(stat),
                scanned_at=scanned_at,
            )


def _inspected_media_asset(
    source: MediaSource,
    *,
    relative_path: str,
    absolute_path: Path,
    size_bytes: int | None,
    mtime_ns: int | None,
    scanned_at: str,
) -> MediaAsset:
    return MediaAsset(
        id=media_asset_id(source.id, relative_path),
        source_id=source.id,
        source_key=relative_path,
        absolute_path=absolute_path,
        relative_path=relative_path,
        display_name=Path(relative_path).name,
        content_type=source.content_type,
        fingerprint=_fingerprint(str(absolute_path), size_bytes, mtime_ns),
        size_bytes=size_bytes,
        mtime_ns=mtime_ns,
        status=MediaAssetStatus.READY,
        created_at=scanned_at,
        updated_at=scanned_at,
    )


def source_id_for_path(
    path: Path,
    content_type: MediaContentType | str | None = None,
) -> str:
    resolved = _safe_resolve(path)
    identity = str(resolved)
    if content_type is not None:
        identity += "\n" + MediaContentType(content_type).value
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"media_{digest}"


def media_asset_id(source_id: str, source_key: str) -> str:
    digest = hashlib.sha256(f"{source_id}\n{source_key}".encode("utf-8")).hexdigest()
    return f"asset_{digest[:32]}"


def media_item_id(source_id: str, source_key: str) -> str:
    return media_asset_id(source_id, source_key)


def source_kind_for_path(path: Path) -> MediaSourceKind:
    return MediaSourceKind.FILE if path.is_file() else MediaSourceKind.DIRECTORY


def label_for_path(path: Path) -> str:
    return path.name or str(path)


def detect_content_type(path: Path) -> MediaContentType:
    if path.is_file() and path.suffix.lower() == ".zip":
        return _content_type_for_archive(path)
    text_count = 0
    video_count = 0
    audio_count = 0
    recognized_count = 0
    for file_path, _stat in _candidate_file_entries(path, recursive=path.is_dir()):
        suffix = file_path.suffix.lower()
        if suffix in TEXT_EXTENSIONS:
            text_count += 1
            recognized_count += 1
        elif suffix in IMAGE_EXTENSIONS:
            # Image is the mixed-folder default, so no complete discovery pass is
            # needed merely to infer the source type.
            return MediaContentType.IMAGE
        elif suffix in VIDEO_EXTENSIONS:
            video_count += 1
            recognized_count += 1
        elif suffix in AUDIO_EXTENSIONS:
            audio_count += 1
            recognized_count += 1
        if recognized_count >= 512:
            break
    if text_count > 0:
        return MediaContentType.TEXT
    if video_count > 0:
        return MediaContentType.VIDEO
    if audio_count > 0:
        return MediaContentType.AUDIO
    return MediaContentType.IMAGE


def media_content_types_for_path(path: Path) -> set[MediaContentType]:
    """Return recognized source types, stopping once every supported type is seen."""
    found: set[MediaContentType] = set()
    if path.is_file() and path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path) as archive:
                suffixes = (Path(member.filename).suffix.lower() for member in archive.infolist())
                for suffix in suffixes:
                    content_type = _content_type_for_suffix(suffix)
                    if content_type is not None:
                        found.add(content_type)
                    if len(found) == len(MediaContentType):
                        break
        except (OSError, zipfile.BadZipFile):
            return set()
        return found
    for file_path, _stat in _candidate_file_entries(path, recursive=path.is_dir()):
        content_type = _content_type_for_file(file_path)
        if content_type is not None:
            found.add(content_type)
        if len(found) == len(MediaContentType):
            break
    return found


def scan_media_source(source: MediaSource, scanned_at: str) -> MediaScanResult:
    if source.source_kind == MediaSourceKind.METADATA_COLUMN:
        return MediaScanResult(assets=[], discovered_count=0, scan_cursor="", error_message="")
    path = source.path.expanduser()
    if not path.exists():
        return MediaScanResult(assets=[], discovered_count=0, error_message="Source path does not exist.")
    if source.source_kind == MediaSourceKind.FILE and not path.is_file():
        return MediaScanResult(assets=[], discovered_count=0, error_message="Source file does not exist.")
    if source.source_kind == MediaSourceKind.DIRECTORY and not path.is_dir():
        return MediaScanResult(assets=[], discovered_count=0, error_message="Source directory does not exist.")

    try:
        if source.content_type == MediaContentType.TEXT and path.is_file() and path.suffix.lower() in JSONL_EXTENSIONS:
            return _scan_jsonl_text_file(source, path, scanned_at)
        return _scan_files(source, path, scanned_at)
    except OSError as exc:
        return MediaScanResult(assets=[], discovered_count=0, error_message=str(exc))


def iter_media_source_assets(source: MediaSource, scanned_at: str, *, should_cancel=None) -> Iterator[MediaAsset]:
    """Yield file-backed assets without retaining or globally sorting the source."""

    path = source.path.expanduser()
    root_path = path.resolve()
    for file_path, stat in _candidate_file_entries(path, recursive=source.recursive, should_cancel=should_cancel):
        if _content_type_for_file(file_path) == source.content_type:
            yield _media_asset_for_file(source, file_path, root_path, scanned_at, stat=stat)


def build_metadata_text_assets(
    source: MediaSource,
    *,
    materialized_path: Path,
    values_by_row_id: dict[int, str],
    scanned_at: str,
) -> MediaScanResult:
    assets: list[MediaAsset] = []
    for row_id, value in sorted(values_by_row_id.items()):
        text = str(value or "").strip()
        if not text:
            continue
        source_key = str(row_id)
        assets.append(
            MediaAsset(
                id=media_asset_id(source.id, source_key),
                source_id=source.id,
                source_key=source_key,
                absolute_path=materialized_path.resolve(),
                relative_path=f"{materialized_path.name}#{source_key}",
                display_name=source_key,
                content_type=MediaContentType.TEXT,
                fingerprint=_fingerprint(str(materialized_path.resolve()), len(text.encode("utf-8")), row_id),
                size_bytes=len(text.encode("utf-8")),
                mtime_ns=row_id,
                status=MediaAssetStatus.READY,
                created_at=scanned_at,
                updated_at=scanned_at,
            )
        )
    return MediaScanResult(
        assets=assets,
        discovered_count=len(assets),
        scan_cursor=str(max(values_by_row_id) if values_by_row_id else ""),
    )


def _scan_files(source: MediaSource, path: Path, scanned_at: str) -> MediaScanResult:
    assets = list(iter_media_source_assets(source, scanned_at))
    scan_cursor = assets[-1].relative_path if assets else ""
    return MediaScanResult(assets=assets, discovered_count=len(assets), scan_cursor=scan_cursor)


def _scan_jsonl_text_file(source: MediaSource, path: Path, scanned_at: str) -> MediaScanResult:
    assets: list[MediaAsset] = []
    discovered = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    stat = path.stat()
    for line_number, line in enumerate(lines, start=1):
        raw = line.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        text_value = _first_payload_value(payload, TEXT_KEYS)
        identifier = _first_payload_value(payload, IDENTIFIER_KEYS)
        if not text_value or not identifier:
            continue
        discovered += 1
        source_key = identifier
        assets.append(
            MediaAsset(
                id=media_asset_id(source.id, source_key),
                source_id=source.id,
                source_key=source_key,
                absolute_path=path.resolve(),
                relative_path=f"{path.name}#{line_number}",
                display_name=source_key,
                content_type=MediaContentType.TEXT,
                fingerprint=_fingerprint(str(path.resolve()), int(stat.st_size), line_number),
                size_bytes=int(stat.st_size),
                mtime_ns=_mtime_ns(stat),
                status=MediaAssetStatus.READY,
                created_at=scanned_at,
                updated_at=scanned_at,
            )
        )
    return MediaScanResult(assets=assets, discovered_count=discovered, scan_cursor=str(len(lines)))


def _media_asset_for_file(
    source: MediaSource,
    file_path: Path,
    root_path: Path,
    scanned_at: str,
    *,
    stat: os.stat_result | None = None,
) -> MediaAsset:
    stat = file_path.stat() if stat is None else stat
    relative_path = file_path.name if source.source_kind == MediaSourceKind.FILE else file_path.relative_to(root_path).as_posix()
    # Relative path is the immutable identity. A basename is not unique in a
    # recursive source and remains available separately as display_name.
    source_key = relative_path
    absolute_path = file_path if file_path.is_absolute() else root_path / relative_path
    return MediaAsset(
        id=media_asset_id(source.id, source_key),
        source_id=source.id,
        source_key=source_key,
        absolute_path=absolute_path,
        relative_path=relative_path,
        display_name=file_path.name,
        content_type=source.content_type,
        fingerprint=_fingerprint(str(absolute_path), int(stat.st_size), _mtime_ns(stat)),
        size_bytes=int(stat.st_size),
        mtime_ns=_mtime_ns(stat),
        status=MediaAssetStatus.READY,
        created_at=scanned_at,
        updated_at=scanned_at,
    )


def _candidate_file_entries(path: Path, *, recursive: bool, should_cancel=None) -> Iterator[tuple[Path, os.stat_result]]:
    """Stream visible files while reusing directory-entry stat information."""

    if path.is_file():
        if not path.name.startswith("."):
            yield path, path.stat()
        return
    if not path.is_dir():
        return

    pending = [path]
    while pending:
        if should_cancel and should_cancel():
            raise RuntimeError("Media indexing cancelled.")
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if should_cancel and should_cancel():
                    raise RuntimeError("Media indexing cancelled.")
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_file(follow_symlinks=False):
                        yield Path(entry.path), entry.stat(follow_symlinks=False)
                    elif recursive and entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                except OSError:
                    continue


def _content_type_for_file(path: Path) -> MediaContentType | None:
    return _content_type_for_suffix(path.suffix.lower())


def _content_type_for_suffix(suffix: str) -> MediaContentType | None:
    if suffix in IMAGE_EXTENSIONS:
        return MediaContentType.IMAGE
    if suffix in VIDEO_EXTENSIONS:
        return MediaContentType.VIDEO
    if suffix in AUDIO_EXTENSIONS:
        return MediaContentType.AUDIO
    if suffix in TEXT_EXTENSIONS:
        return MediaContentType.TEXT
    return None


def _content_type_for_archive(path: Path) -> MediaContentType:
    text_count = 0
    image_count = 0
    video_count = 0
    audio_count = 0
    try:
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                suffix = Path(member.filename).suffix.lower()
                if suffix in TEXT_EXTENSIONS:
                    text_count += 1
                elif suffix in IMAGE_EXTENSIONS:
                    image_count += 1
                elif suffix in VIDEO_EXTENSIONS:
                    video_count += 1
                elif suffix in AUDIO_EXTENSIONS:
                    audio_count += 1
    except (OSError, zipfile.BadZipFile):
        return MediaContentType.IMAGE
    if text_count > 0 and image_count == 0:
        return MediaContentType.TEXT
    if video_count > 0 and image_count == 0 and text_count == 0:
        return MediaContentType.VIDEO
    if audio_count > 0 and image_count == 0 and text_count == 0 and video_count == 0:
        return MediaContentType.AUDIO
    return MediaContentType.IMAGE


def _first_payload_value(payload: dict[object, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _safe_resolve(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser().absolute()


def _mtime_ns(stat_result: object) -> int:
    value = getattr(stat_result, "st_mtime_ns", None)
    if value is not None:
        return int(value)
    return int(float(getattr(stat_result, "st_mtime", 0.0)) * 1_000_000_000)


def _fingerprint(path: str, size_bytes: int | None, mtime_ns: int | None) -> str:
    payload = json.dumps(
        {"path": path, "size_bytes": size_bytes, "mtime_ns": mtime_ns},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
