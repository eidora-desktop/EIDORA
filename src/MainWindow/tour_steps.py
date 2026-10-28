"""Tour navigation targets; edit the user-facing copy in tour_text.toml."""

from dataclasses import dataclass
from importlib.resources import files
import tomllib


@dataclass(frozen=True)
class TourStep:
    workspace: int
    target: str | None
    title: str
    description: str


WORKSPACE_NAMES = ("Project", "Metadata", "Embeddings", "Discovery", "Figures")

# Text section, workspace index, panel object name. Order here owns navigation.
TOUR_TARGETS = (
    ("workspace_bar", 0, None),
    ("project_media_sources", 0, "panel_media_sources_host"),
    ("project_thumbnails", 0, "panel_thumbnails_host"),
    ("metadata_items", 1, "panel_metadata_table"),
    ("metadata_groups", 1, "panel_metadata_groups"),
    ("embeddings_models", 2, "panel_local_models"),
    ("embeddings_vectors", 2, "panel_vector_data_host"),
    ("discovery_canvas", 3, "panel_canvas_center"),
    ("discovery_projection", 3, "panel_projection_host"),
    ("discovery_transform", 3, "panel_transform_host"),
    ("discovery_filters", 3, "panel_filters_host"),
    ("discovery_slicer", 3, "panel_slicer_host"),
    ("discovery_snapshots", 3, "panel_snapshots_host"),
    ("discovery_appearance", 3, "panel_appearance_host"),
    ("figures_canvas", 4, "panel_canvas_center"),
    ("figures_snapshots", 4, "panel_snapshots_host"),
    ("figures_settings", 4, "panel_figure_settings_host"),
)


def load_tour_steps() -> tuple[TourStep, ...]:
    """Read fresh copy on launch so editing text does not require an app restart."""
    copy = tomllib.loads(files("MainWindow").joinpath("tour_text.toml").read_text(encoding="utf-8"))
    steps = []
    for key, workspace, target in TOUR_TARGETS:
        section = copy.get(key)
        if not isinstance(section, dict) or any(
            not isinstance(section.get(field), str) or not section[field].strip()
            for field in ("title", "description")
        ):
            raise ValueError(f"[{key}] needs a nonempty title and description.")
        steps.append(TourStep(workspace, target, section["title"].strip(), section["description"].strip()))
    return tuple(steps)
