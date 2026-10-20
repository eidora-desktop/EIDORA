from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


PROJECT_MANIFEST_FILENAME = "project.json"


@dataclass(frozen=True)
class Project:
    """A local EIDORA project workspace."""

    name: str
    root: Path
    schema_version: int

    @property
    def paths(self) -> ProjectStoragePaths:
        return ProjectStoragePaths(self.root)


@dataclass(frozen=True)
class RecentProjectEntry:
    project: Project
    created_at: datetime | None
    last_opened_at: datetime | None


@dataclass(frozen=True)
class ProjectManifest:
    """Project-level storage compatibility metadata."""

    schema_version: int
    name: str


@dataclass(frozen=True)
class ProjectStoragePaths:
    """Canonical project-local storage paths for the v1 project schema."""

    root: Path

    @property
    def manifest(self) -> Path:
        return self.root / PROJECT_MANIFEST_FILENAME

    @property
    def metadata(self) -> Path:
        return self.root / "metadata"

    @property
    def metadata_database(self) -> Path:
        return self.metadata / "metadata.db"

    @property
    def metadata_columns(self) -> Path:
        return self.metadata / "metadata_columns.json"

    @property
    def metadata_groups_database(self) -> Path:
        """Optional normalized metadata-group store, created on first use."""

        return self.metadata / "groups.db"

    @property
    def media(self) -> Path:
        return self.root / "media"

    @property
    def media_sources(self) -> Path:
        return self.media / "media_sources"

    @property
    def generated_text_sources(self) -> Path:
        """Optional materialized metadata text, created only when needed."""
        return self.media / "generated_text_sources"

    @property
    def embeddings(self) -> Path:
        return self.root / "embeddings"

    @property
    def discovery(self) -> Path:
        return self.root / "discovery"

    @property
    def discovery_arrangements(self) -> Path:
        return self.discovery / "arrangements"

    @property
    def presets(self) -> Path:
        return self.root / "presets"

    @property
    def discovery_presets(self) -> Path:
        return self.presets / "discovery"

    @property
    def appearance_presets(self) -> Path:
        return self.presets / "appearance"

    @property
    def filter_presets(self) -> Path:
        return self.presets / "filters"

    @property
    def slicer_presets(self) -> Path:
        return self.presets / "slicer"

    @property
    def camera_presets(self) -> Path:
        return self.presets / "camera"

    @property
    def transform_presets(self) -> Path:
        """Optional project-local transform recipes, created on first use."""

        return self.presets / "transform"

    @property
    def canvas_presets(self) -> Path:
        return self.presets / "canvas"

    @property
    def snapshot_presets(self) -> Path:
        return self.presets / "snapshots"

    @property
    def figure_plot_settings_presets(self) -> Path:
        return self.presets / "figure_plot_settings"

    @property
    def annotations(self) -> Path:
        return self.root / "annotations"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def figures(self) -> Path:
        return self.root / "figures"

    @property
    def static_figures(self) -> Path:
        return self.root / "static_figures"

    def required_directories(self) -> tuple[Path, ...]:
        return (
            self.metadata,
            self.media,
            self.media_sources,
            self.embeddings,
            self.discovery,
            self.presets,
            self.discovery_presets,
            self.appearance_presets,
            self.filter_presets,
            self.slicer_presets,
            self.camera_presets,
            self.canvas_presets,
            self.snapshot_presets,
            self.figure_plot_settings_presets,
            self.annotations,
            self.state,
            self.figures,
            self.static_figures,
        )
