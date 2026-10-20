from __future__ import annotations

from datetime import datetime
from datetime import timezone
import json
import re
import shutil
import zipfile
from pathlib import Path
from pathlib import PurePosixPath
from typing import Iterable

from MetadataArea.repository import MetadataRepository
from MetadataArea.service import MetadataService
from project.domain import Project, RecentProjectEntry
from project.storage import (
    CURRENT_PROJECT_SCHEMA_VERSION,
    PROJECT_MANIFEST_FILENAME,
    ProjectStoragePaths,
    ensure_current_project_schema,
    write_project_manifest,
)
from shared.paths import default_workspace_root


class ProjectService:
    """Application-facing project operations.

    Project creation owns the workspace folder. Metadata persistence remains in
    MetadataRepository so the metadata database has one authoritative writer.
    """

    RECENT_PROJECT_LIMIT = 10

    def __init__(self, workspace_root: Path | str | None = None) -> None:
        self._workspace_root = (
            default_workspace_root() if workspace_root is None else Path(workspace_root).expanduser()
        )
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        self._recent_projects_file = self._workspace_root / ".recent_projects.json"

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    def use_workspace(self, root: Path) -> None:
        """Activate an explicitly selected, already prepared workspace."""
        self._workspace_root = Path(root)
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        self._recent_projects_file = self._workspace_root / ".recent_projects.json"

    def available_project_path(self, name: str, *, parent: Path | None = None) -> Path:
        return self._next_available_project_path(self._sanitize_name(name), parent=parent)

    def create_empty_project(self, name: str, *, parent: Path | None = None) -> Project:
        project = self._create_project_folder(name, parent=parent)
        self._metadata_service(project).initialize_empty()
        self._record_recent_project(project)
        return project

    def create_project_from_metadata(
        self,
        source_path: Path | str,
        name: str | None = None,
        id_column_strategy: str = "error",
    ) -> Project:
        source = Path(source_path).expanduser()
        project_name = name or source.stem
        project = self._create_project_folder(project_name)
        try:
            self._metadata_service(project).import_metadata(source, id_column_strategy=id_column_strategy)
        except Exception:
            # Leave no half-created project in the workspace when import fails before any user work exists.
            self._remove_empty_project_folder(project.root)
            raise
        self._record_recent_project(project)
        return project

    def open_project(self, root: Path | str) -> Project:
        path = Path(root).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            raise FileNotFoundError(path)
        project = self._project_from_path(path)
        metadata_repository = self._metadata_repository(project)
        if not metadata_repository.is_initialized():
            raise ValueError("Selected directory is not a valid EIDORA project: missing metadata/metadata.db.")
        self._record_recent_project(project)
        return project

    def load_last_project(self) -> Project | None:
        recent_projects = self.list_recent_projects()
        if recent_projects:
            try:
                return self.open_project(recent_projects[0].root)
            except (FileNotFoundError, ValueError):
                return None

        return None

    def list_recent_projects(self) -> list[Project]:
        return [entry.project for entry in self.list_recent_project_entries()]

    def list_recent_project_entries(self) -> list[RecentProjectEntry]:
        records = self._read_recent_project_records()
        projects: list[Project] = []
        kept_records: list[tuple[Path, datetime | None]] = []
        for path, last_opened_at in records:
            try:
                project = self._project_from_path(path)
                if not self._metadata_repository(project).is_initialized():
                    continue
            except (FileNotFoundError, ValueError, OSError):
                continue
            projects.append(project)
            kept_records.append((project.root.resolve(), last_opened_at))
        if [str(path) for path, _last_opened_at in kept_records] != [str(path) for path, _last_opened_at in records]:
            self._write_recent_project_records(kept_records)
        return [
            RecentProjectEntry(
                project=project,
                created_at=self._project_created_at(project.root),
                last_opened_at=last_opened_at,
            )
            for project, (_path, last_opened_at) in zip(projects, kept_records)
        ]

    def open_recent_project(self, path_or_index: Path | str | int) -> Project:
        if isinstance(path_or_index, int):
            recent_projects = self.list_recent_projects()
            try:
                project = recent_projects[path_or_index]
            except IndexError as exc:
                raise ValueError("Recent project index is out of range.") from exc
            return self.open_project(project.root)
        return self.open_project(path_or_index)

    def clear_recent_projects(self) -> None:
        self._write_recent_project_records([])

    def delete_project(self, project_or_path: Project | Path | str) -> Project:
        root = self._project_root_from_value(project_or_path)
        if not root.exists() or not root.is_dir():
            raise FileNotFoundError(root)
        if root == self._workspace_root.resolve():
            raise ValueError("Refusing to delete the project workspace root.")

        project = self._project_from_path(root)
        if not self._metadata_repository(project).is_initialized():
            raise ValueError("Selected directory is not a valid EIDORA project: missing metadata/metadata.db.")

        self._remove_project_folder(root)
        self._forget_project_path(root)
        return project

    def rename_project(self, project_or_path: Project | Path | str, name: str) -> Project:
        root = self._project_root_from_value(project_or_path)
        if not root.exists() or not root.is_dir():
            raise FileNotFoundError(root)
        if not name.strip():
            raise ValueError("Project name cannot be empty.")

        project = self._project_from_path(root)
        if not self._metadata_repository(project).is_initialized():
            raise ValueError("Selected directory is not a valid EIDORA project: missing metadata/metadata.db.")

        renamed_name = self._sanitize_name(name)
        write_project_manifest(root, renamed_name, project.schema_version)
        return Project(name=renamed_name, root=root, schema_version=project.schema_version)

    def export_project_bundle(self, project: Project, destination: Path | str) -> Path:
        destination_path = Path(destination).expanduser()
        if destination_path.suffix.lower() != ".zip":
            destination_path = destination_path.with_suffix(".zip")
        destination_path.parent.mkdir(parents=True, exist_ok=True)

        root = project.root.resolve()
        if not root.exists() or not root.is_dir():
            raise FileNotFoundError(root)
        self._project_from_path(root)
        if not self._metadata_repository(project).is_initialized():
            raise ValueError("Project cannot be exported because metadata/metadata.db is missing.")

        top_level = project.name
        with zipfile.ZipFile(destination_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(root)
                archive_name = PurePosixPath(top_level, *relative.parts).as_posix()
                if path.is_dir():
                    archive.write(path, archive_name + "/")
                else:
                    archive.write(path, archive_name)
        return destination_path

    def import_project_bundle(self, bundle_path: Path | str) -> Project:
        source = Path(bundle_path).expanduser()
        if not source.exists() or not source.is_file():
            raise FileNotFoundError(source)
        if not zipfile.is_zipfile(source):
            raise ValueError("Project bundle must be a valid ZIP file.")

        try:
            with zipfile.ZipFile(source) as archive:
                entries = archive.infolist()
                safe_entries = self._validated_bundle_entries(entries)
                top_level_names = {parts[0] for _entry, parts in safe_entries}
                if len(top_level_names) != 1:
                    raise ValueError("Project bundle must contain exactly one top-level project folder.")
                top_level = next(iter(top_level_names))
                if not any(len(parts) > 1 and parts[1] == PROJECT_MANIFEST_FILENAME for _entry, parts in safe_entries):
                    raise ValueError("Project bundle is missing project.json.")

                target_root = self._next_available_project_path(self._sanitize_name(top_level))
                target_root.mkdir(parents=True, exist_ok=False)
                try:
                    self._extract_project_bundle_entries(archive, safe_entries, target_root)
                    project = self.open_project(target_root)
                except Exception:
                    self._remove_project_folder(target_root)
                    raise
        except zipfile.BadZipFile as exc:
            raise ValueError("Project bundle must be a valid ZIP file.") from exc
        return project

    def _create_project_folder(self, name: str, *, parent: Path | None = None) -> Project:
        project_root = self._next_available_project_path(self._sanitize_name(name), parent=parent)
        self._ensure_project_directories(project_root)
        write_project_manifest(project_root, project_root.name)
        return Project(
            name=project_root.name,
            root=project_root,
            schema_version=CURRENT_PROJECT_SCHEMA_VERSION,
        )

    def _project_from_path(self, root: Path) -> Project:
        manifest = ensure_current_project_schema(root)
        return Project(name=manifest.name, root=root, schema_version=manifest.schema_version)

    def _ensure_project_directories(self, root: Path) -> None:
        for directory in ProjectStoragePaths(root).required_directories():
            directory.mkdir(parents=True, exist_ok=True)

    def _record_recent_project(self, project: Project) -> None:
        project_path = project.root.resolve()
        records = [(path, last_opened_at) for path, last_opened_at in self._read_recent_project_records() if path != project_path]
        records.insert(0, (project_path, datetime.now(timezone.utc)))
        self._write_recent_project_records(records[: self.RECENT_PROJECT_LIMIT])

    def _forget_project_path(self, root: Path) -> None:
        resolved = root.resolve()
        records = [(path, last_opened_at) for path, last_opened_at in self._read_recent_project_records() if path != resolved]
        self._write_recent_project_records(records)

    def _read_recent_project_records(self) -> list[tuple[Path, datetime | None]]:
        if not self._recent_projects_file.exists():
            return []
        try:
            payload = self._recent_projects_file.read_text(encoding="utf-8")
            values = json.loads(payload)
        except Exception:
            return []
        if not isinstance(values, list):
            return []
        records: list[tuple[Path, datetime | None]] = []
        seen: set[Path] = set()
        for value in values:
            raw_path: str | None = None
            last_opened_at: datetime | None = None
            if isinstance(value, dict):
                path_value = value.get("path")
                if isinstance(path_value, str):
                    raw_path = path_value
                last_opened_at = self._parse_recent_datetime(value.get("last_opened_at"))
            if raw_path is None or not raw_path.strip():
                continue
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                path = self._workspace_root / path
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            records.append((resolved, last_opened_at))
            if len(records) >= self.RECENT_PROJECT_LIMIT:
                break
        return records

    def _write_recent_project_records(self, records: Iterable[tuple[Path, datetime | None]]) -> None:
        self._recent_projects_file.parent.mkdir(parents=True, exist_ok=True)
        values = [
            {
                "path": str(Path(path).resolve()),
                "last_opened_at": last_opened_at.isoformat() if last_opened_at is not None else None,
            }
            for path, last_opened_at in records
        ][: self.RECENT_PROJECT_LIMIT]
        self._recent_projects_file.write_text(
            json.dumps(values, indent=2),
            encoding="utf-8",
        )

    def _metadata_repository(self, project: Project) -> MetadataRepository:
        paths = project.paths
        return MetadataRepository(paths.metadata_database, paths.metadata_columns)

    def _metadata_service(self, project: Project) -> MetadataService:
        return MetadataService(self._metadata_repository(project))

    def _next_available_project_path(self, base_name: str, *, parent: Path | None = None) -> Path:
        parent = self._workspace_root if parent is None else Path(parent).expanduser().resolve()
        candidate = parent / base_name
        suffix = 1
        while candidate.exists():
            candidate = parent / f"{base_name}_{suffix}"
            suffix += 1
        return candidate

    @staticmethod
    def _sanitize_name(name: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip()).strip("_")
        return cleaned or "Project"

    @staticmethod
    def _parse_recent_datetime(value: object) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _project_created_at(root: Path) -> datetime | None:
        try:
            stat = root.stat()
        except OSError:
            return None
        timestamp = getattr(stat, "st_birthtime", stat.st_ctime)
        return datetime.fromtimestamp(timestamp).astimezone()

    @staticmethod
    def _project_root_from_value(project_or_path: Project | Path | str) -> Path:
        if isinstance(project_or_path, Project):
            return project_or_path.root.expanduser().resolve()
        return Path(project_or_path).expanduser().resolve()

    @staticmethod
    def _remove_empty_project_folder(path: Path) -> None:
        for child in sorted(path.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        if path.exists():
            path.rmdir()

    @staticmethod
    def _remove_project_folder(path: Path) -> None:
        if path.exists():
            shutil.rmtree(path)

    @staticmethod
    def _validated_bundle_entries(entries: list[zipfile.ZipInfo]) -> list[tuple[zipfile.ZipInfo, tuple[str, ...]]]:
        safe_entries: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
        for entry in entries:
            normalized = entry.filename.replace("\\", "/")
            path = PurePosixPath(normalized)
            if not normalized.strip() or path.is_absolute():
                raise ValueError("Project bundle contains an unsafe archive path.")
            parts = tuple(part for part in path.parts if part not in {"", "."})
            if not parts:
                continue
            if any(part == ".." for part in parts):
                raise ValueError("Project bundle contains an unsafe archive path.")
            if ":" in parts[0]:
                raise ValueError("Project bundle contains an unsafe archive path.")
            safe_entries.append((entry, parts))
        if not safe_entries:
            raise ValueError("Project bundle is empty.")
        if not any(len(parts) > 1 for _entry, parts in safe_entries):
            raise ValueError("Project bundle must contain one top-level project folder.")
        return safe_entries

    @staticmethod
    def _extract_project_bundle_entries(
        archive: zipfile.ZipFile,
        entries: list[tuple[zipfile.ZipInfo, tuple[str, ...]]],
        target_root: Path,
    ) -> None:
        for entry, parts in entries:
            relative_parts = parts[1:]
            if not relative_parts:
                continue
            destination = target_root.joinpath(*relative_parts)
            if entry.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source_file, destination.open("wb") as target_file:
                shutil.copyfileobj(source_file, target_file)
