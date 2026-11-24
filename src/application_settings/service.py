"""Settings workflow decisions; storage retains its existing persistence owner."""
from pathlib import Path
import sqlite3

from application_settings.repository import ApplicationPreferences
from application_storage.service import StorageService


class SettingsService:
    def __init__(self, projects, *, preferences=None, storage=None):
        self.projects = projects
        self.preferences = preferences or ApplicationPreferences()
        self.storage = storage or StorageService(projects.workspace_root)

    def projects_for_removal(self):
        """Inspect only project folders directly inside the displayed location."""
        workspace = self.projects.workspace_root
        if workspace.is_symlink() or getattr(workspace, "is_junction", lambda: False)():
            raise ValueError("Project removal cannot follow a linked project location.")
        return tuple(self._validate_project_removal(path) for path in sorted(workspace.iterdir())
                     if (path / "project.json").is_file())

    def _validate_project_removal(self, path):
        workspace = self.projects.workspace_root
        if (workspace.is_symlink() or getattr(workspace, "is_junction", lambda: False)()
                or path.is_symlink() or getattr(path, "is_junction", lambda: False)()
                or path.resolve().parent != workspace.resolve()):
            raise ValueError("Only projects inside the current project location can be removed.")
        return self.projects.inspect_project(path)

    def remove_projects(self, projects):
        # Validate the confirmed set before removing anything. Projects created
        # since confirmation and recent projects elsewhere are never included.
        for project in projects:
            self._validate_project_removal(project.root)
        removed, failures = 0, []
        for project in projects:
            try:
                self._validate_project_removal(project.root)
                self.projects.delete_project(project)
            except (OSError, ValueError, sqlite3.Error) as exc:
                if not project.root.exists():
                    removed += 1
                failures.append(f"{project.name}: {exc}")
            else:
                removed += 1
        return removed, failures

    def has_contents(self, kind: str) -> bool:
        root = self.storage.workspace if kind == "workspace" else self.storage.models
        if not root.exists():
            if self.storage.preferences.read().get(f"{kind}_root"):
                raise FileNotFoundError(f"The current folder is unavailable. Reconnect its drive first:\n{root}")
            return False
        return next(root.iterdir(), None) is not None

    def relocation_destination(self, kind: str, destination: Path) -> Path:
        source = self.storage.workspace if kind == "workspace" else self.storage.models
        destination = destination.expanduser().resolve()
        if destination == source.resolve() or source.resolve() in destination.parents or destination in source.resolve().parents:
            raise ValueError("Choose a destination outside the current folder.")
        destination = self.storage.validate_destination(kind, destination)
        if any(destination.iterdir()):
            raise ValueError("Choose a new or empty destination folder.")
        return destination

    def change_destination(self, kind: str, destination: Path) -> None:
        destination = self.storage.validate_destination(kind, destination)
        previous = self.projects.workspace_root
        # Preserve recent entries before publishing the new default. A failed
        # preference write restores the active workspace; project files never move.
        if kind == "workspace":
            try:
                self.projects.use_workspace(destination, preserve_recent=True)
                self.storage.set_destination(kind, destination)
            except Exception:
                self.projects.use_workspace(previous)
                raise
        else:
            self.storage.set_destination(kind, destination)
