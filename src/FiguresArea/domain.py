from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True)
class Figure:
    name: str
    figure_type: str


@dataclass(frozen=True)
class FigurePlotSettings:
    width_mm: float = 70.0
    height_mm: float = 70.0
    rows: int = 1
    columns: int = 1
    margin_mm: float = 0.0
    subplot_margin_mm: float = 0.0
    main_title_margin_mm: float = 0.0
    subtitle_margin_mm: float = 0.0
    legend_margin_mm: float = 0.0
    caption_margin_mm: float = 0.0
    export_dpi: int = 144
    show_axis: bool = False
    show_ticks: bool = False
    show_grid: bool = False
    axis_box_line_width: float = 0.0
    grid_mode: Literal["off", "lines", "map"] = "off"
    grid_line_width: float = 0.8
    grid_resolution: float = 0.5
    tick_label_size: int = 10
    tick_label_margin_pt: float = 0.0
    point_size_factor_percent: float = 0.0
    point_size: float = 9.0
    point_border_width: float = 0.0
    point_size_override: bool = False
    main_title_show: bool = False
    main_title_location: Literal["top", "left", "right", "bottom"] = "top"
    main_title_alignment: Literal["left", "center", "right"] = "center"
    main_title_font: str = "DejaVu Sans"
    main_title_size: int = 14
    subtitle_show: bool = False
    subtitle_location: Literal["top", "left", "right", "bottom"] = "top"
    subtitle_alignment: Literal["left", "center", "right"] = "center"
    subtitle_font: str = "DejaVu Sans"
    subtitle_size: int = 10
    legend_show: bool = False
    legend_scope: Literal["global", "per_plot"] = "per_plot"
    legend_location: Literal[
        "upper right",
        "upper left",
        "lower right",
        "lower left",
        "center right",
        "center left",
        "upper center",
        "lower center",
    ] = "upper right"
    legend_include_point_color: bool = True
    legend_include_border_color: bool = True
    legend_include_shape: bool = True
    legend_font_size: int = 10
    legend_marker_size: float = 8.0
    legend_columns: int = 1
    legend_frame: bool = True
    pins_visible: bool = True
    pin_size_factor_percent: float = 0.0
    pin_thumbnail_visible: bool = True
    pin_line_width: float = 1.5
    main_caption_show: bool = False
    main_caption_font: str = "DejaVu Sans"
    main_caption_size: int = 10
    main_caption_justify: bool = False
    main_caption_line_spacing: float = 1.25
    subplot_caption_show: bool = False
    subplot_caption_alignment: Literal["left", "center", "right", "justify"] = "center"
    subplot_caption_font: str = "DejaVu Sans"
    subplot_caption_size: int = 9
    subplot_caption_line_spacing: float = 1.25

    @property
    def subplot_size_mm(self) -> float:
        return self.width_mm


FigureElementKind = Literal["main_title", "plot_title", "legend", "caption"]
FigureElementDirection = Literal["top", "left", "right", "bottom"]


@dataclass
class FigureElement:
    element_id: str
    kind: FigureElementKind
    direction: FigureElementDirection
    text: str
    source_cell: tuple[int, int] | None = None
    visible: bool = True
    font_family: str = "DejaVu Sans"
    font_size: float = 10.0
    alignment: str = "center"
    line_spacing: float = 1.25
    order: int = 0
    auto_text: bool = True
    rich_text: str | None = None


@dataclass
class PlotCell:
    row: int
    column: int
    snapshot_path: str | None = None
    snapshot_label: str | None = None
    coordinates: list[tuple[float, float]] = field(default_factory=list)
    metadata_ids: list[int] = field(default_factory=list)
    metadata: dict[int, dict[str, str]] = field(default_factory=dict)
    render_state: dict[str, Any] = field(default_factory=dict)
    element_ids: list[str] = field(default_factory=list)


@dataclass
class FigureComposition:
    settings: FigurePlotSettings = field(default_factory=FigurePlotSettings)
    plot_cells: dict[tuple[int, int], PlotCell] = field(default_factory=dict)
    figure_elements: dict[str, FigureElement] = field(default_factory=dict)
    selected_element_id: str | None = None
    next_element_number: int = 1

    def add_plot_cell(self, row: int, column: int) -> PlotCell:
        cell = PlotCell(row=row, column=column)
        self.plot_cells[(row, column)] = cell
        return cell

    def next_element_id(self, kind: FigureElementKind) -> str:
        element_id = f"{kind}_{self.next_element_number}"
        self.next_element_number += 1
        return element_id


@dataclass(frozen=True)
class SnapshotAnimationFrame:
    """One ordered snapshot frame in a manual animation."""

    frame_id: str
    label: str
    snapshot_path: str
    plot_overrides: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SlicerAnimationFrame:
    """One generated slicer window rendered from the animation base snapshot."""

    frame_id: str
    label: str
    slicer_preset_path: str
    column: str
    active_values: tuple[str, ...]
    range_start: str
    range_end: str
    plot_overrides: dict[str, Any] = field(default_factory=dict)


AnimationFrame = SnapshotAnimationFrame | SlicerAnimationFrame


AnimationRepeatStyle = Literal["from_start", "ping_pong", "play_once"]
AnimationExportQuality = Literal["highest", "high", "medium", "low"]


@dataclass(frozen=True)
class AnimationExportSettings:
    """Validated user choices for one animated GIF export."""

    destination: Path
    quality: AnimationExportQuality = "high"
    width: int = 1920
    height: int = 1080
    gif_dithering: bool = True


@dataclass(frozen=True)
class AnimationExportResult:
    """Completed animation export returned to the UI."""

    destination: Path
    bytes_written: int


@dataclass(frozen=True)
class AnimationPlaybackAdvance:
    """Result of advancing one animation timer step."""

    index: int
    direction: int
    finished: bool = False


@dataclass
class AnimationTimelineState:
    """Project-owned state for one unified animation tab."""

    frames: list[AnimationFrame] = field(default_factory=list)
    base_snapshot_path: str | None = None
    current_frame_id: str | None = None
    interval_ms: int = 150
    thumbnail_size: int = 48
    repeat_style: AnimationRepeatStyle = "from_start"
    thumbnails_dirty: bool = False
    pending_apply_frame_ids: set[str] = field(default_factory=set)
