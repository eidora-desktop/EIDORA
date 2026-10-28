"""Explicit maintenance of application-owned files, never automatic project deletion."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from collections.abc import Iterator

from application_storage.repository import StoragePreferences


def _check_cancelled(cancelled) -> None:
    if cancelled():
        raise InterruptedError("Storage operation cancelled.")


def _raise_walk_error(error: OSError) -> None:
    raise error


def _files(root: Path, cancelled) -> Iterator[Path]:
    """Reject links and special files rather than following them during maintenance."""
    if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
        raise ValueError(f"Storage maintenance cannot follow symbolic links: {root}")
    if not root.exists():
        return
    if not root.is_dir():
        raise ValueError(f"Storage location is not a directory: {root}")
    for directory, folders, names in os.walk(root, followlinks=False, onerror=_raise_walk_error):
        _check_cancelled(cancelled)
        for name in folders + names:
            _check_cancelled(cancelled)
            path = Path(directory) / name
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                raise ValueError(f"Storage maintenance cannot follow linked folders or files: {path}")
            if path.is_file():
                yield path
            elif not path.is_dir():
                raise ValueError(f"Unsupported storage entry: {path}")


def directory_size(root: Path, cancelled=lambda: False) -> int:
    return sum(path.stat().st_size for path in _files(root, cancelled))


def _digest(path: Path, cancelled) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            _check_cancelled(cancelled)
            digest.update(chunk)
    return digest.hexdigest()


def copy_verified(source: Path, destination: Path, *, cancelled=lambda: False,
                  progress=lambda text: None) -> None:
    """Publish a verified copy into a new/empty folder; never remove the source."""
    source, destination = source.expanduser().absolute(), destination.expanduser().absolute()
    if source.resolve() == destination.resolve() or source.resolve() in destination.resolve().parents:
        raise ValueError("Choose a destination outside the current folder.")
    if destination.resolve() in source.resolve().parents:
        raise ValueError("The destination cannot contain the current folder.")
    if destination.is_symlink() or (destination.exists() and any(destination.iterdir())):
        raise ValueError("Choose a new or empty destination folder.")
    files = list(_files(source, cancelled))
    sizes = {path.relative_to(source): (path.stat().st_size, path.stat().st_mtime_ns) for path in files}
    destination.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(destination.parent).free < sum(value[0] for value in sizes.values()) + 8 * 1024**2:
        raise OSError("There is not enough free space for a verified copy.")
    stage = Path(tempfile.mkdtemp(prefix=".eidora-copy-", dir=destination.parent))
    try:
        # Preserve empty project directories too.
        directories = os.walk(source, onerror=_raise_walk_error) if source.exists() else ()
        for directory, folders, _ in directories:
            _check_cancelled(cancelled)
            for folder in folders:
                (stage / Path(directory).relative_to(source) / folder).mkdir(parents=True, exist_ok=True)
        for index, path in enumerate(files):
            _check_cancelled(cancelled)
            relative = path.relative_to(source)
            progress(f"Copying {index + 1} of {len(files)} files: {relative}")
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with path.open("rb") as reader, target.open("wb") as writer:
                while chunk := reader.read(1024 * 1024):
                    _check_cancelled(cancelled)
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            if _digest(path, cancelled) != _digest(target, cancelled):
                raise OSError(f"Verification failed: {relative}")
        latest = {path.relative_to(source): (path.stat().st_size, path.stat().st_mtime_ns)
                  for path in _files(source, cancelled)}
        if latest != sizes:
            raise OSError("Source files changed during copying. Close other EIDORA windows and retry.")
        _check_cancelled(cancelled)
        if destination.exists():
            destination.rmdir()  # Fails if another process filled the folder.
        stage.rename(destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


class StorageService:
    def __init__(self, workspace: Path, *, preferences: StoragePreferences | None = None,
                 models: Path | None = None, cache: Path | None = None) -> None:
        from shared.paths import application_cache_root, models_root

        self.workspace = Path(workspace)
        self.preferences = preferences or StoragePreferences()
        self.models = models if models is not None else models_root()
        self.cache = cache if cache is not None else application_cache_root()

    def inspect(self, *, cancelled=lambda: False, progress=lambda text: None) -> dict:
        result = {}
        for key, path in (("projects", self.workspace), ("models", self.models), ("cache", self.cache)):
            progress(f"Measuring {key}…")
            result[key] = directory_size(path, cancelled)
        return result

    def relocate(self, kind: str, destination: Path, *, cancelled=lambda: False,
                 progress=lambda text: None) -> None:
        if kind not in ("workspace", "models"):
            raise ValueError("Unknown storage location.")
        source = self.workspace if kind == "workspace" else self.models
        if not source.is_dir() and (kind == "workspace" or self.preferences.read().get("models_root")):
            raise FileNotFoundError(f"The current folder is unavailable. Reconnect its drive first:\n{source}")
        destination = destination.expanduser().resolve()
        other = self.models if kind == "workspace" else self.workspace
        for protected in (other.resolve(), self.cache.resolve()):
            if destination == protected or protected in destination.parents or destination in protected.parents:
                raise ValueError("Projects, models, and caches must use separate folders.")
        copy_verified(source, destination, cancelled=cancelled, progress=progress)
        # Recent-project entries are absolute paths. Only entries in this workspace move.
        if kind == "workspace":
            recent = destination / ".recent_projects.json"
            if recent.exists():
                records = json.loads(recent.read_text(encoding="utf-8"))
                for index, record in enumerate(records):
                    value = record if isinstance(record, str) else record["path"]
                    path = Path(value)
                    path = path if path.is_absolute() else source / path
                    try:
                        relocated = destination / path.resolve().relative_to(source.resolve())
                    except ValueError:
                        continue
                    if isinstance(record, str):
                        records[index] = str(relocated)
                    else:
                        record["path"] = str(relocated)
                recent.write_text(json.dumps(records, indent=2), encoding="utf-8")
        # On a preferences-write failure the verified copy remains, and the original
        # location remains authoritative. No source data is ever removed.
        self.preferences.update(**{f"{kind}_root": str(destination), f"previous_{kind}_root": str(source)})
        if kind == "workspace":
            self.workspace = destination
        else:
            self.models = destination

    def clean(self, *, models: bool, cache: bool, cancelled=lambda: False,
              progress=lambda text: None) -> int:
        """Remove only recognized downloaded packages and named disposable caches."""
        candidates = []
        for root in (self.models, self.cache):
            if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
                raise ValueError(f"Cleanup cannot follow a linked storage root: {root}")
        if models and self.models.is_dir():
            for path in self.models.iterdir():
                if path.is_dir() and (path / ".eidora-download.json").is_file():
                    candidates.append(path)
            candidates.append(self.models / ".downloads")
        if cache:
            candidates.append(self.cache / "matplotlib")
        # Validate the whole plan before deleting anything.
        plans = []
        for path in candidates:
            if not path.exists():
                continue
            resolved = path.resolve()
            workspace = self.workspace.resolve()
            if resolved == workspace or resolved in workspace.parents or workspace in resolved.parents:
                raise ValueError("Cleanup cannot touch the project workspace.")
            size = 0
            for file in _files(path, cancelled):
                if file.name in ("project.json", "metadata.db"):
                    raise ValueError(f"Cleanup found project data in {path}; nothing was removed.")
                size += file.stat().st_size
            plans.append((path, size))
        removed = 0
        for path, size in plans:
            _check_cancelled(cancelled)
            progress(f"Removing {path.name}…")
            shutil.rmtree(path)
            removed += size
        return removed
