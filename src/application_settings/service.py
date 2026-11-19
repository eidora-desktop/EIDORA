"""Settings workflow decisions; storage retains its existing persistence owner."""
from pathlib import Path

from application_settings.repository import ApplicationPreferences
from application_storage.service import StorageService


class SettingsService:
    def __init__(self, projects, *, preferences=None, storage=None):
        self.projects = projects
        self.preferences = preferences or ApplicationPreferences()
        self.storage = storage or StorageService(projects.workspace_root)

    def has_workspace_projects(self) -> bool:
        return any(self.projects.workspace_root.glob("*/project.json"))

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
