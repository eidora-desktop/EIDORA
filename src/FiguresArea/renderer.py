from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
import math
from pathlib import Path
import re
import tempfile
from typing import Any

from matplotlib.colors import to_rgba
from matplotlib.figure import Figure
from matplotlib import font_manager
from matplotlib import image as mpimg
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.lines import Line2D
from matplotlib.offsetbox import AnnotationBbox, HPacker, OffsetImage, TextArea, VPacker
from matplotlib.ticker import FixedFormatter, FixedLocator, NullFormatter
from matplotlib.textpath import TextToPath

from FiguresArea.appearance_units import diameter_pixels_to_matplotlib_area, pixels_to_points, signed_percent_to_scale
from FiguresArea.domain import FigureComposition, FigureElement, FigurePlotSettings, PlotCell


@dataclass(frozen=True)
class FigurePinGeometry:
    annotation_id: str
    cell: tuple[int, int]
    point_data: tuple[float, float]
    box_anchor_data: tuple[float, float]
    bbox_px: tuple[int, int, int, int]
    connector_start_px: tuple[float, float]
    connector_end_px: tuple[float, float]
    connector_color: Any
    connector_width: float
    xlim: tuple[float, float]
    ylim: tuple[float, float]


@dataclass(frozen=True)
class FigureLayoutMap:
    canvas_size_px: tuple[int, int]
    plot_rects_px: dict[tuple[int, int], tuple[int, int, int, int]] = field(default_factory=dict)
    element_rects_px: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    pin_rects_px: dict[tuple[int, int], dict[str, tuple[int, int, int, int]]] = field(default_factory=dict)
    pin_anchor_points_px: dict[tuple[int, int], dict[str, tuple[float, float]]] = field(default_factory=dict)
    pin_box_anchor_points_px: dict[tuple[int, int], dict[str, tuple[float, float]]] = field(default_factory=dict)
    pin_geometries: dict[tuple[int, int], dict[str, FigurePinGeometry]] = field(default_factory=dict)


@dataclass(frozen=True)
class FigureRenderResult:
    figure: Figure
    layout_map: FigureLayoutMap


@dataclass(frozen=True)
class _CaptionRun:
    text: str
    bold: bool = False
    italic: bool = False


@dataclass(frozen=True)
class _LegendEntry:
    group: str
    label: str
    face_color: Any
    edge_color: Any
    marker: str
    edge_width: float = 0.0


class FigureMatplotlibRenderer:
    """Compile a FiguresArea composition into one authoritative Matplotlib figure."""

    _ELEMENT_BAND_MM = 12.0
    _LEGEND_BAND_MM = 22.0

    def export(
        self,
        composition: FigureComposition,
        path: Path,
        *,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> None:
        if progress_callback is not None:
            progress_callback("Building figure for export…", 10)
        figure = self.build(composition).figure
        try:
            if progress_callback is not None:
                progress_callback("Writing figure export…", 55)
            # Keep a previous export intact until the new file is fully written.
            with tempfile.TemporaryDirectory(prefix=".eidora-export-", dir=path.parent) as directory:
                staged = Path(directory) / path.name
                figure.savefig(str(staged), dpi=max(int(composition.settings.export_dpi), 36))
                if progress_callback is not None:
                    progress_callback("Finalizing figure export…", 95)
                staged.replace(path)
        finally:
            figure.clear()
        if progress_callback is not None:
            progress_callback("Figure export complete", 100)

    def build(self, composition: FigureComposition) -> FigureRenderResult:
        settings = composition.settings
        cells = composition.plot_cells or {(0, 0): PlotCell(row=0, column=0)}
        min_row, max_row, min_column, max_column = self._bounds(cells)
        rows = max_row - min_row + 1
        columns = max_column - min_column + 1
        subplot_width_in = self._mm_to_inches(settings.width_mm)
        subplot_height_in = self._mm_to_inches(settings.height_mm)
        subplot_gap_in = self._subplot_margin_inches(settings)
        cell_element_bands_in = self._cell_element_bands_inches(
            composition,
            caption_width_in=subplot_width_in,
        )
        axis_bands_in = self._axis_decoration_bands_inches(cells, settings)
        cell_width_in = (
            cell_element_bands_in["left"]
            + axis_bands_in["left"]
            + subplot_width_in
            + cell_element_bands_in["right"]
            + axis_bands_in["right"]
        )
        cell_height_in = (
            cell_element_bands_in["top"]
            + axis_bands_in["top"]
            + subplot_height_in
            + cell_element_bands_in["bottom"]
            + axis_bands_in["bottom"]
        )
        margins_in = self._reserved_margins_inches(
            composition,
            caption_width_in=max(columns * subplot_width_in, subplot_width_in),
        )
        outer_margin_in = self._margin_mm_to_inches(getattr(settings, "margin_mm", 0.0))
        for side in margins_in:
            margins_in[side] += outer_margin_in
        figure_width_in = (columns * cell_width_in) + (max(columns - 1, 0) * subplot_gap_in) + margins_in["left"] + margins_in["right"]
        figure_height_in = (rows * cell_height_in) + (max(rows - 1, 0) * subplot_gap_in) + margins_in["top"] + margins_in["bottom"]
        dpi = max(int(settings.export_dpi), 36)
        figure = Figure(figsize=(figure_width_in, figure_height_in), dpi=dpi)
        figure.subplots_adjust(left=0, right=1, top=1, bottom=0, wspace=0, hspace=0)
        canvas_width_px = max(int(figure.bbox.width), 1)
        canvas_height_px = max(int(figure.bbox.height), 1)
        plot_rects: dict[tuple[int, int], tuple[int, int, int, int]] = {}
        pin_specs: dict[tuple[int, int], dict[str, dict[str, Any]]] = {}
        for (row, column), cell in sorted(cells.items()):
            cell_left_in = margins_in["left"] + ((column - min_column) * (cell_width_in + subplot_gap_in))
            cell_bottom_in = margins_in["bottom"] + ((max_row - row) * (cell_height_in + subplot_gap_in))
            left_in = cell_left_in + cell_element_bands_in["left"] + axis_bands_in["left"]
            bottom_in = cell_bottom_in + cell_element_bands_in["bottom"] + axis_bands_in["bottom"]
            rect = [
                left_in / figure_width_in,
                bottom_in / figure_height_in,
                subplot_width_in / figure_width_in,
                subplot_height_in / figure_height_in,
            ]
            axes = figure.add_axes(rect)
            axes.set_label(f"plot:{row},{column}")
            cell_pin_specs = self._render_plot_cell(axes, cell, settings)
            if cell_pin_specs:
                pin_specs[(row, column)] = cell_pin_specs
            left_px = int(round(rect[0] * canvas_width_px))
            right_px = int(round((rect[0] + rect[2]) * canvas_width_px))
            bottom_px = int(round(rect[1] * canvas_height_px))
            top_px = int(round((rect[1] + rect[3]) * canvas_height_px))
            plot_rects[(row, column)] = (
                left_px,
                canvas_height_px - top_px,
                max(right_px - left_px, 1),
                max(top_px - bottom_px, 1),
            )
        element_rects = self._render_elements(
            figure,
            composition,
            plot_rects,
            canvas_size_px=(canvas_width_px, canvas_height_px),
        )
        pin_geometries = self._pin_geometries_from_artists(
            figure,
            pin_specs,
            canvas_size_px=(canvas_width_px, canvas_height_px),
        )
        self._render_pin_connectors_from_geometry(figure, pin_geometries)
        pin_rects = {
            cell: {annotation_id: geometry.bbox_px for annotation_id, geometry in annotations.items()}
            for cell, annotations in pin_geometries.items()
        }
        pin_anchor_points = {
            cell: {annotation_id: geometry.connector_start_px for annotation_id, geometry in annotations.items()}
            for cell, annotations in pin_geometries.items()
        }
        pin_box_anchor_points = {
            cell: {
                annotation_id: self._data_point_to_canvas_px(figure, geometry.box_anchor_data, geometry.cell, plot_rects)
                for annotation_id, geometry in annotations.items()
            }
            for cell, annotations in pin_geometries.items()
        }
        return FigureRenderResult(
            figure=figure,
            layout_map=FigureLayoutMap(
                canvas_size_px=(canvas_width_px, canvas_height_px),
                plot_rects_px=plot_rects,
                element_rects_px=element_rects,
                pin_rects_px=pin_rects,
                pin_anchor_points_px=pin_anchor_points,
                pin_box_anchor_points_px=pin_box_anchor_points,
                pin_geometries=pin_geometries,
            ),
        )

    def _render_plot_cell(
        self,
        axes: Any,
        cell: PlotCell,
        settings: FigurePlotSettings,
    ) -> dict[str, dict[str, Any]]:
        if cell.coordinates:
            xs = [point[0] for point in cell.coordinates]
            ys = [point[1] for point in cell.coordinates]
            colors = self._point_colors_from_state(cell.render_state, cell.metadata_ids, cell.metadata)
            edge_colors, border_width = self._point_border_from_state(cell.render_state, cell.metadata_ids, cell.metadata, settings, axes.figure.dpi)
            markers = self._point_markers_from_state(cell.render_state, cell.metadata_ids, cell.metadata)
            point_size = self._point_size_from_state(cell.render_state, settings, axes.figure.dpi)
            if markers is None:
                axes.scatter(
                    xs,
                    ys,
                    s=point_size,
                    c=colors,
                    linewidths=border_width,
                    edgecolors=edge_colors if border_width > 0 else "none",
                )
            else:
                for marker in dict.fromkeys(markers):
                    indices = [index for index, value in enumerate(markers) if value == marker]
                    axes.scatter(
                        [xs[index] for index in indices],
                        [ys[index] for index in indices],
                        s=point_size,
                        c=[colors[index] for index in indices],
                        marker=marker,
                        linewidths=border_width,
                        edgecolors=[edge_colors[index] for index in indices] if border_width > 0 else "none",
                    )
            camera = self._safe_dict(cell.render_state.get("camera"))
            overview_xlim = self._axis_limits(xs)
            overview_ylim = self._axis_limits(ys)
            axes.set_xlim(*self._camera_or_axis_limits(camera, "x", xs))
            axes.set_ylim(*self._camera_or_axis_limits(camera, "y", ys))
            axes._eidora_map_grid_origin = (min(overview_xlim), min(overview_ylim))
        # Figure cells use Matplotlib's standard fill behavior. The camera
        # bounds choose the visible data; they do not letterbox the plot cell.
        axes.set_aspect("auto")
        axes.margins(0)
        self._apply_grid_mode(axes, settings)
        return self._render_pin_annotations(axes, cell, settings)

    @staticmethod
    def _subplot_margin_inches(settings: FigurePlotSettings) -> float:
        return FigureMatplotlibRenderer._margin_mm_to_inches(getattr(settings, "subplot_margin_mm", 0.0))

    def _apply_grid_mode(self, axes: Any, settings: FigurePlotSettings) -> None:
        grid_mode = str(getattr(settings, "grid_mode", "off") or "off").lower()
        if grid_mode not in {"off", "lines", "map"}:
            grid_mode = "off"
        axis_box_line_width = max(float(getattr(settings, "axis_box_line_width", 0.0)), 0.0)
        show_axis_box = axis_box_line_width > 0.0
        grid_line_width = max(float(getattr(settings, "grid_line_width", 0.8)), 0.0)
        if grid_mode == "off":
            axes.set_xticks([])
            axes.set_yticks([])
            axes.grid(False)
            if show_axis_box:
                axes.set_axis_on()
                axes.tick_params(
                    axis="both",
                    which="both",
                    bottom=False,
                    left=False,
                    labelbottom=False,
                    labelleft=False,
                    length=0.0,
                    width=0.0,
                )
            else:
                axes.set_axis_off()
            self._apply_axis_box(axes, visible=show_axis_box, line_width=axis_box_line_width)
            return

        xlim = axes.get_xlim()
        ylim = axes.get_ylim()
        grid_resolution = max(float(getattr(settings, "grid_resolution", 1.0)), 0.01)
        origin = getattr(axes, "_eidora_map_grid_origin", (min(xlim), min(ylim)))
        x_origin, y_origin = self._map_grid_origin_tuple(origin, xlim, ylim)
        x_major = self._grid_boundaries_for_limits(xlim, grid_resolution, origin=x_origin)
        y_major = self._grid_boundaries_for_limits(ylim, grid_resolution, origin=y_origin)
        x_centers = self._grid_cell_centers_for_limits(xlim, grid_resolution, origin=x_origin)
        y_centers = self._grid_cell_centers_for_limits(ylim, grid_resolution, origin=y_origin)
        axes.xaxis.set_major_locator(FixedLocator(x_major))
        axes.yaxis.set_major_locator(FixedLocator(y_major))
        axes.xaxis.set_major_formatter(NullFormatter())
        axes.yaxis.set_major_formatter(NullFormatter())
        axes.xaxis.set_minor_locator(FixedLocator(x_centers))
        axes.yaxis.set_minor_locator(FixedLocator(y_centers))
        if grid_mode == "map":
            axes.xaxis.set_minor_formatter(FixedFormatter(self._numeric_labels_for_centers(x_centers, grid_resolution, origin=x_origin)))
            axes.yaxis.set_minor_formatter(FixedFormatter(self._alpha_labels_for_centers(y_centers, grid_resolution, origin=y_origin)))
        else:
            axes.xaxis.set_minor_formatter(NullFormatter())
            axes.yaxis.set_minor_formatter(NullFormatter())
        axes.set_xlim(*xlim)
        axes.set_ylim(*ylim)
        axes.autoscale(enable=False)
        axes.set_axis_on()
        axes.set_axisbelow(True)
        self._apply_axis_box(axes, visible=show_axis_box, line_width=axis_box_line_width)
        axes.grid(True, which="major", alpha=0.4, linewidth=grid_line_width)
        tick_label_size = max(float(getattr(settings, "tick_label_size", 10)), 1.0)
        tick_label_margin = max(float(getattr(settings, "tick_label_margin_pt", 0.0)), 0.0)
        map_labels = grid_mode == "map"
        axes.tick_params(
            axis="both",
            which="major",
            bottom=True,
            left=True,
            labelbottom=False,
            labelleft=False,
            length=0.0,
            width=0.0,
        )
        axes.tick_params(
            axis="both",
            which="minor",
            bottom=True,
            left=True,
            labelbottom=map_labels,
            labelleft=map_labels,
            labelsize=tick_label_size,
            length=0.0,
            width=0.0,
            pad=tick_label_margin,
        )
        for tick_label in axes.get_xticklabels(minor=True):
            tick_label.set_horizontalalignment("center")
            tick_label.set_verticalalignment("top")
        for tick_label in axes.get_yticklabels(minor=True):
            tick_label.set_horizontalalignment("right")
            tick_label.set_verticalalignment("center")

    @staticmethod
    def _apply_axis_box(axes: Any, *, visible: bool, line_width: float) -> None:
        for spine in axes.spines.values():
            spine.set_visible(visible)
            spine.set_linewidth(line_width)

    def _render_elements(
        self,
        figure: Figure,
        composition: FigureComposition,
        plot_rects: dict[tuple[int, int], tuple[int, int, int, int]],
        *,
        canvas_size_px: tuple[int, int],
    ) -> dict[str, tuple[int, int, int, int]]:
        element_rects: dict[str, tuple[int, int, int, int]] = {}
        for element in sorted(composition.figure_elements.values(), key=lambda item: item.order):
            if not element.visible:
                continue
            rect = self._element_rect(element, plot_rects, canvas_size_px, composition.settings)
            element_rects[element.element_id] = rect
            center_x, center_y, horizontal_alignment, vertical_alignment = self._text_anchor_for_element(
                element,
                rect,
                canvas_size_px,
            )
            if element.kind == "legend":
                self._render_legend_element(figure, element, composition, rect, canvas_size_px)
            elif element.kind == "caption":
                self._render_caption_element(figure, element, rect, canvas_size_px)
            else:
                figure.text(
                    center_x,
                    center_y,
                    element.text,
                    ha=horizontal_alignment,
                    va=vertical_alignment,
                    fontsize=max(float(element.font_size), 1.0),
                    fontfamily=element.font_family,
                    rotation=self._text_rotation_for_element(element),
                )
        return element_rects

    def _render_caption_element(
        self,
        figure: Figure,
        element: FigureElement,
        rect: tuple[int, int, int, int],
        canvas_size_px: tuple[int, int],
    ) -> None:
        x, y, width, height = rect
        alignment = str(getattr(element, "alignment", "center") or "center")
        if alignment not in {"left", "center", "right", "justify"}:
            alignment = "center"
        text_alignment = "left" if alignment == "justify" else alignment
        font_size = max(float(element.font_size), 1.0)
        lines = self._wrapped_caption_text_lines(
            self._caption_plain_text(element),
            width,
            font_size,
            figure.dpi,
            element.font_family,
        )
        if not lines:
            return
        if alignment == "justify":
            lines = self._justified_caption_text_lines(lines, width, font_size, figure.dpi, element.font_family)
        line_height_px = self._caption_line_height_px_for_font_size(font_size, element, figure.dpi)
        text_height_px = len(lines) * line_height_px
        y_top = y + max((height - text_height_px) / 2.0, 0.0)
        if text_alignment == "left":
            x_anchor = float(x)
        elif text_alignment == "right":
            x_anchor = float(x + width)
        else:
            x_anchor = float(x) + (float(width) / 2.0)
        figure.text(
            x_anchor / canvas_size_px[0],
            1.0 - (y_top / canvas_size_px[1]),
            "\n".join(lines),
            ha=text_alignment,
            va="top",
            multialignment=text_alignment,
            fontsize=font_size,
            fontfamily=element.font_family,
            linespacing=max(float(getattr(element, "line_spacing", 1.25)), 0.1),
            transform=figure.transFigure,
        )

    def _render_legend_element(
        self,
        figure: Figure,
        element: FigureElement,
        composition: FigureComposition,
        rect: tuple[int, int, int, int],
        canvas_size_px: tuple[int, int],
    ) -> None:
        handles, labels = self._legend_handles_for_element(element, composition)
        if not handles:
            return
        x, y, width, height = rect
        canvas_width, canvas_height = canvas_size_px
        settings = composition.settings
        figure.legend(
            handles,
            labels,
            loc=self._legend_location(settings),
            bbox_to_anchor=(
                x / canvas_width,
                1.0 - ((y + height) / canvas_height),
                width / canvas_width,
                height / canvas_height,
            ),
            bbox_transform=figure.transFigure,
            frameon=bool(getattr(settings, "legend_frame", True)),
            fontsize=max(float(getattr(settings, "legend_font_size", 10)), 1.0),
            ncols=max(int(getattr(settings, "legend_columns", 1)), 1),
            borderaxespad=0.0,
            handlelength=1.2,
            handletextpad=0.5,
        )

    @staticmethod
    def _caption_plain_text(element: FigureElement) -> str:
        return "".join(run.text for run in FigureMatplotlibRenderer._caption_runs(element)).strip() or str(element.text)

    @staticmethod
    def _caption_runs(element: FigureElement) -> list[_CaptionRun]:
        rich_text = str(getattr(element, "rich_text", "") or "").strip()
        if rich_text:
            parser = _RichTextRunParser(only_body=bool(re.search(r"<body\b", rich_text, flags=re.IGNORECASE)))
            parser.feed(rich_text)
            runs = parser.runs()
            if runs:
                return runs
        return [_CaptionRun(str(element.text))]

    @classmethod
    def _caption_line_count(cls, element: FigureElement, width_px: int, dpi: float, *, font_size: float | None = None) -> int:
        lines = cls._wrapped_caption_text_lines(
            cls._caption_plain_text(element),
            width_px,
            float(element.font_size) if font_size is None else font_size,
            dpi,
            element.font_family,
        )
        return max(len(lines), 1)

    @classmethod
    def _wrapped_caption_text_lines(
        cls,
        text: str,
        width_px: int | float,
        font_size: float,
        dpi: float,
        font_family: str,
    ) -> list[str]:
        max_width = max(float(width_px), 1.0)
        normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        if not normalized.strip():
            return []
        lines: list[str] = []
        for paragraph in normalized.split("\n"):
            words = paragraph.strip().split()
            if not words:
                lines.append("")
                continue
            current = words[0]
            for word in words[1:]:
                candidate = f"{current} {word}"
                if cls._caption_text_width_px(candidate, font_size, dpi, font_family) > max_width:
                    lines.append(current)
                    current = word
                else:
                    current = candidate
            lines.append(current)
        return lines

    @staticmethod
    def _caption_text_width_px(text: str, font_size: float, dpi: float, font_family: str) -> float:
        if not text:
            return 0.0
        try:
            prop = font_manager.FontProperties(family=font_family, size=max(float(font_size), 0.1))
            width_pt, _height_pt, _descent_pt = TextToPath().get_text_width_height_descent(
                str(text),
                prop,
                ismath=False,
            )
            return max((float(width_pt) * max(float(dpi), 1.0)) / 72.0, 0.0)
        except Exception:
            return len(str(text)) * max(float(font_size), 0.1) * 0.52 * max(float(dpi), 1.0) / 72.0

    @classmethod
    def _justified_caption_text_lines(
        cls,
        lines: list[str],
        width_px: int | float,
        font_size: float,
        dpi: float,
        font_family: str,
    ) -> list[str]:
        justified: list[str] = []
        max_width = max(float(width_px), 1.0)
        space_width = max(cls._caption_text_width_px(" ", font_size, dpi, font_family), 0.1)
        for index, line in enumerate(lines):
            words = line.split()
            if index == len(lines) - 1 or len(words) < 2:
                justified.append(line)
                continue
            current_width = cls._caption_text_width_px(" ".join(words), font_size, dpi, font_family)
            extra_spaces = max(int(round((max_width - current_width) / space_width)), 0)
            gaps = len(words) - 1
            base_extra, remainder = divmod(extra_spaces, gaps)
            pieces: list[str] = []
            for word_index, word in enumerate(words[:-1]):
                pieces.append(word)
                pieces.append(" " * (1 + base_extra + (1 if word_index < remainder else 0)))
            pieces.append(words[-1])
            justified.append("".join(pieces))
        return justified

    @staticmethod
    def _caption_line_height_px_for_font_size(font_size: float, element: FigureElement, dpi: float) -> float:
        line_spacing = max(float(getattr(element, "line_spacing", 1.25)), 0.1)
        return max(float(font_size), 0.1) * line_spacing * max(float(dpi), 1.0) / 72.0

    @staticmethod
    def _text_anchor_for_element(
        element: FigureElement,
        rect: tuple[int, int, int, int],
        canvas_size_px: tuple[int, int],
    ) -> tuple[float, float, str, str]:
        x, y, width, height = rect
        alignment = str(getattr(element, "alignment", "center") or "center")
        if alignment not in {"left", "center", "right"}:
            alignment = "center"
        padding = 0.0
        if element.direction in {"left", "right"}:
            anchor_y = {
                "left": y + height - padding,
                "center": y + (height / 2.0),
                "right": y + padding,
            }[alignment]
            return (
                (x + (width / 2.0)) / canvas_size_px[0],
                1.0 - (anchor_y / canvas_size_px[1]),
                "center",
                {"left": "bottom", "center": "center", "right": "top"}[alignment],
            )
        anchor_x = {
            "left": x + padding,
            "center": x + (width / 2.0),
            "right": x + width - padding,
        }[alignment]
        return (
            anchor_x / canvas_size_px[0],
            1.0 - ((y + (height / 2.0)) / canvas_size_px[1]),
            alignment,
            "center",
        )

    def _element_rect(
        self,
        element: FigureElement,
        plot_rects: dict[tuple[int, int], tuple[int, int, int, int]],
        canvas_size_px: tuple[int, int],
        settings: FigurePlotSettings,
    ) -> tuple[int, int, int, int]:
        if element.source_cell is None:
            return self._global_element_rect(element, canvas_size_px, settings)
        source_rect = plot_rects[element.source_cell]
        x, y, width, height = source_rect
        band = self._element_band_px_for_width(element, settings, width_px=width if element.direction in {"top", "bottom"} else height)
        margin = self._element_margin_px(element, settings)
        if element.direction == "top":
            return x, max(y - margin - band, 0), width, band
        if element.direction == "bottom":
            return x, min(y + height + margin, canvas_size_px[1] - band), width, band
        if element.direction == "left":
            return max(x - margin - band, 0), y, band, height
        return min(x + width + margin, canvas_size_px[0] - band), y, band, height

    @staticmethod
    def _global_element_rect(
        element: FigureElement,
        canvas_size_px: tuple[int, int],
        settings: FigurePlotSettings,
    ) -> tuple[int, int, int, int]:
        width, height = canvas_size_px
        band = FigureMatplotlibRenderer._element_band_px_for_width(
            element,
            settings,
            width_px=width if element.direction in {"top", "bottom"} else height,
        )
        outer_margin = FigureMatplotlibRenderer._figure_margin_px(settings)
        if element.direction == "top":
            return 0, min(outer_margin, max(height - band, 0)), width, band
        if element.direction == "bottom":
            return 0, max(height - band - outer_margin, 0), width, band
        if element.direction == "left":
            return min(outer_margin, max(width - band, 0)), 0, band, height
        return max(width - band - outer_margin, 0), 0, band, height

    def _reserved_margins_inches(
        self,
        composition: FigureComposition,
        *,
        caption_width_in: float,
    ) -> dict[str, float]:
        margins = {"top": 0.0, "left": 0.0, "right": 0.0, "bottom": 0.0}
        for element in composition.figure_elements.values():
            if not element.visible or element.source_cell is not None:
                continue
            element_margin_mm = self._element_margin_mm(element, composition.settings)
            margins[element.direction] = max(
                margins[element.direction],
                self._element_band_inches_for_width(
                    element,
                    composition.settings,
                    width_in=caption_width_in,
                )
                + (element_margin_mm / 25.4),
            )
        return margins

    def _cell_element_bands_inches(
        self,
        composition: FigureComposition,
        *,
        caption_width_in: float,
    ) -> dict[str, float]:
        bands = {"top": 0.0, "left": 0.0, "right": 0.0, "bottom": 0.0}
        for element in composition.figure_elements.values():
            if not element.visible or element.source_cell is None:
                continue
            element_margin_mm = self._element_margin_mm(element, composition.settings)
            bands[element.direction] = max(
                bands[element.direction],
                self._element_band_inches_for_width(
                    element,
                    composition.settings,
                    width_in=caption_width_in,
                )
                + (element_margin_mm / 25.4),
            )
        return bands

    def _axis_decoration_bands_inches(
        self,
        cells: dict[tuple[int, int], PlotCell],
        settings: FigurePlotSettings,
    ) -> dict[str, float]:
        grid_mode = str(getattr(settings, "grid_mode", "off") or "off").lower()
        if grid_mode != "map":
            return {"top": 0.0, "left": 0.0, "right": 0.0, "bottom": 0.0}
        tick_label_size = max(float(getattr(settings, "tick_label_size", 10)), 1.0)
        tick_label_margin = max(float(getattr(settings, "tick_label_margin_pt", 0.0)), 0.0)
        max_x_chars = 1
        max_y_chars = 1
        grid_resolution = max(float(getattr(settings, "grid_resolution", 1.0)), 0.01)
        for cell in cells.values():
            xs = [point[0] for point in cell.coordinates]
            ys = [point[1] for point in cell.coordinates]
            camera = self._safe_dict(cell.render_state.get("camera"))
            xlim = self._camera_or_axis_limits(camera, "x", xs) if xs else (0.0, 1.0)
            ylim = self._camera_or_axis_limits(camera, "y", ys) if ys else (0.0, 1.0)
            x_count = len(self._grid_cell_centers_for_limits(xlim, grid_resolution))
            y_count = len(self._grid_cell_centers_for_limits(ylim, grid_resolution))
            max_x_chars = max(max_x_chars, len(str(max(x_count, 1))))
            max_y_chars = max(max_y_chars, len(self._index_to_alpha_label(max(y_count - 1, 0))))
        pad_in = tick_label_margin / 72.0
        safety_in = 2.0 / 72.0
        bottom_in = (tick_label_size / 72.0) + pad_in + safety_in
        left_in = ((tick_label_size * 0.62 * max_y_chars) / 72.0) + pad_in + safety_in
        return {"top": 0.0, "left": left_in, "right": 0.0, "bottom": bottom_in}

    @staticmethod
    def _element_margin_mm(element: FigureElement, settings: FigurePlotSettings) -> float:
        if element.kind == "main_title":
            return float(getattr(settings, "main_title_margin_mm", 0.0))
        if element.kind == "plot_title":
            return float(getattr(settings, "subtitle_margin_mm", 0.0))
        if element.kind == "legend":
            return max(float(getattr(settings, "legend_margin_mm", 0.0)), 0.0)
        if element.kind == "caption":
            return max(float(getattr(settings, "caption_margin_mm", 0.0)), 0.0)
        return 0.0

    @staticmethod
    def _text_rotation_for_element(element: FigureElement) -> float:
        if element.kind == "plot_title" and element.direction == "right":
            return 270.0
        if element.direction in {"left", "right"}:
            return 90.0
        return 0.0

    @staticmethod
    def _element_margin_px(
        element: FigureElement,
        settings: FigurePlotSettings,
    ) -> int:
        margin_in = FigureMatplotlibRenderer._element_margin_mm(element, settings) / 25.4
        dpi = max(float(settings.export_dpi), 1.0)
        return int(round(margin_in * dpi))

    @staticmethod
    def _element_band_px(element: FigureElement, settings: FigurePlotSettings) -> int:
        dpi = max(float(settings.export_dpi), 1.0)
        return max(int(round(FigureMatplotlibRenderer._element_band_inches(element, settings) * dpi)), 1)

    @staticmethod
    def _element_band_inches(element: FigureElement, settings: FigurePlotSettings) -> float:
        if element.kind in {"main_title", "plot_title"}:
            return max(float(element.font_size), 1.0) / 72.0
        band_mm = FigureMatplotlibRenderer._LEGEND_BAND_MM if element.kind == "legend" else FigureMatplotlibRenderer._ELEMENT_BAND_MM
        return band_mm / 25.4

    @staticmethod
    def _element_band_px_for_width(
        element: FigureElement,
        settings: FigurePlotSettings,
        *,
        width_px: int,
    ) -> int:
        if element.kind != "caption":
            return FigureMatplotlibRenderer._element_band_px(element, settings)
        dpi = max(float(settings.export_dpi), 1.0)
        base_font_size = max(float(element.font_size), 1.0)
        base_width_px = max(int(round(float(width_px))), 1)
        line_count = FigureMatplotlibRenderer._caption_line_count(element, base_width_px, dpi, font_size=base_font_size)
        height_px = line_count * FigureMatplotlibRenderer._caption_line_height_px_for_font_size(base_font_size, element, dpi)
        return max(int(math.ceil(height_px)), 1)

    @staticmethod
    def _element_band_inches_for_width(
        element: FigureElement,
        settings: FigurePlotSettings,
        *,
        width_in: float,
    ) -> float:
        if element.kind != "caption":
            return FigureMatplotlibRenderer._element_band_inches(element, settings)
        dpi = max(float(settings.export_dpi), 1.0)
        base_width_in = max(float(width_in), 0.01)
        base_width_px = max(int(round(base_width_in * dpi)), 1)
        base_font_size = max(float(element.font_size), 1.0)
        line_count = FigureMatplotlibRenderer._caption_line_count(element, base_width_px, dpi, font_size=base_font_size)
        base_height_px = line_count * FigureMatplotlibRenderer._caption_line_height_px_for_font_size(base_font_size, element, dpi)
        return max(base_height_px / dpi, 1.0 / dpi)

    @staticmethod
    def _figure_margin_px(settings: FigurePlotSettings) -> int:
        margin_in = FigureMatplotlibRenderer._margin_mm_to_inches(getattr(settings, "margin_mm", 0.0))
        dpi = max(float(settings.export_dpi), 1.0)
        return max(int(round(margin_in * dpi)), 0)

    @staticmethod
    def _bounds(cells: dict[tuple[int, int], PlotCell]) -> tuple[int, int, int, int]:
        return (
            min(row for row, _column in cells),
            max(row for row, _column in cells),
            min(column for _row, column in cells),
            max(column for _row, column in cells),
        )

    def _legend_handles_for_element(
        self,
        element: FigureElement,
        composition: FigureComposition,
    ) -> tuple[list[Line2D], list[str]]:
        entries = self._legend_entries_for_element(element, composition)
        marker_size = max(float(getattr(composition.settings, "legend_marker_size", 8.0)), 0.1)
        handles = [
            Line2D(
                [0],
                [0],
                marker=entry.marker,
                linestyle="none",
                label=entry.label,
                markerfacecolor=entry.face_color,
                markeredgecolor=entry.edge_color,
                markeredgewidth=max(float(entry.edge_width), 0.0),
                markersize=marker_size,
            )
            for entry in entries
        ]
        return handles, [entry.label for entry in entries]

    def _legend_entries_for_element(self, element: FigureElement, composition: FigureComposition) -> list[_LegendEntry]:
        if element.source_cell is None:
            cells = [composition.plot_cells[key] for key in sorted(composition.plot_cells)]
        else:
            cell = composition.plot_cells.get(element.source_cell)
            cells = [] if cell is None else [cell]
        entries: list[_LegendEntry] = []
        for cell in cells:
            entries.extend(self._legend_entries_for_cell(cell, composition.settings))
        deduped: list[_LegendEntry] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for entry in entries:
            key = (entry.group, entry.label, str(entry.face_color), str(entry.edge_color), entry.marker)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(entry)
        return deduped

    def _legend_entries_for_cell(self, cell: PlotCell, settings: FigurePlotSettings) -> list[_LegendEntry]:
        appearance = self._safe_dict(cell.render_state.get("appearance"))
        entries: list[_LegendEntry] = []
        if getattr(settings, "legend_include_point_color", True):
            entries.extend(self._color_legend_entries(appearance, "point_colors", "point color", fill=True))
        if getattr(settings, "legend_include_border_color", True):
            border = self._safe_dict(appearance.get("border"))
            try:
                edge_width = max(float(border.get("width", 1.0)), 0.6)
            except (TypeError, ValueError):
                edge_width = 1.0
            entries.extend(self._color_legend_entries(appearance, "border", "border color", fill=False, edge_width=edge_width))
        if getattr(settings, "legend_include_shape", True):
            entries.extend(self._shape_legend_entries(appearance))
        return entries

    def _color_legend_entries(
        self,
        appearance: dict[str, Any],
        key: str,
        group: str,
        *,
        fill: bool,
        edge_width: float = 0.0,
    ) -> list[_LegendEntry]:
        style = self._safe_dict(appearance.get(key))
        mapping = self._categorical_mapping_items(style)
        if not mapping:
            return []
        opacity_source = style if key == "border" else self._safe_dict(appearance.get("point"))
        opacity = self._clamped_float(opacity_source.get("opacity", 1.0), 0.0, 1.0, 1.0)
        entries: list[_LegendEntry] = []
        for label, color in mapping:
            rgba = to_rgba(str(color), opacity)
            entries.append(
                _LegendEntry(
                    group=group,
                    label=str(label),
                    face_color=rgba if fill else (1.0, 1.0, 1.0, 0.25),
                    edge_color=rgba,
                    marker="o",
                    edge_width=edge_width,
                )
            )
        return entries

    def _shape_legend_entries(self, appearance: dict[str, Any]) -> list[_LegendEntry]:
        style = self._safe_dict(appearance.get("shape"))
        mapping = self._categorical_mapping_items(style)
        if not mapping:
            return []
        point_color = self._mapped_color(self._safe_dict(appearance.get("point_colors")), None, {}, "#2f6fed")
        opacity = self._clamped_float(self._safe_dict(appearance.get("point")).get("opacity", 1.0), 0.0, 1.0, 1.0)
        face_color = to_rgba(point_color, opacity)
        return [
            _LegendEntry(
                group="shape",
                label=str(label),
                face_color=face_color,
                edge_color=face_color,
                marker=self._matplotlib_marker(shape),
            )
            for label, shape in mapping
        ]

    @staticmethod
    def _categorical_mapping_items(style: dict[str, Any]) -> list[tuple[str, object]]:
        mapping = style.get("mapping")
        if not isinstance(mapping, dict) or not mapping:
            return []
        items = [(str(label), value) for label, value in mapping.items()]
        non_global = [(label, value) for label, value in items if label.lower() != "global"]
        return non_global or items

    @staticmethod
    def _legend_location(settings: FigurePlotSettings) -> str:
        location = str(getattr(settings, "legend_location", "upper right") or "upper right")
        if location not in {
            "upper right",
            "upper left",
            "lower right",
            "lower left",
            "center right",
            "center left",
            "upper center",
            "lower center",
        }:
            return "upper right"
        return location

    @staticmethod
    def _mm_to_inches(value_mm: float) -> float:
        return max(float(value_mm), 0.1) / 25.4

    @staticmethod
    def _margin_mm_to_inches(value_mm: float) -> float:
        return max(float(value_mm), 0.0) / 25.4

    @staticmethod
    def _map_grid_origin_tuple(
        origin: object,
        xlim: tuple[float, float],
        ylim: tuple[float, float],
    ) -> tuple[float, float]:
        if isinstance(origin, tuple) and len(origin) == 2:
            try:
                return float(origin[0]), float(origin[1])
            except (TypeError, ValueError):
                pass
        return min(float(xlim[0]), float(xlim[1])), min(float(ylim[0]), float(ylim[1]))

    @staticmethod
    def _grid_boundaries_for_limits(limits: tuple[float, float], resolution: float, *, origin: float | None = None) -> list[float]:
        start = min(float(limits[0]), float(limits[1]))
        end = max(float(limits[0]), float(limits[1]))
        step = max(float(resolution), 1e-9)
        span = end - start
        if span <= 1e-12:
            return []
        if origin is not None:
            base = float(origin)
            first_index = max(math.floor(((start - base) / step) + 1e-9), 0)
            last_index = max(math.ceil(((end - base) / step) - 1e-9), first_index)
            max_intervals = 2000
            if last_index - first_index > max_intervals:
                last_index = first_index + max_intervals
            return [base + (step * float(index)) for index in range(first_index, last_index + 1)]
        max_intervals = 2000
        full_intervals = min(int(math.floor(span / step)), max_intervals)
        boundaries = [start + (step * float(index)) for index in range(full_intervals + 1)]
        if not boundaries:
            boundaries = [start]
        epsilon = step * 1e-6
        if (end - boundaries[-1]) > epsilon and len(boundaries) < (max_intervals + 2):
            boundaries.append(end)
        else:
            boundaries[-1] = end
        boundaries[0] = start
        return boundaries

    @classmethod
    def _grid_cell_centers_for_limits(cls, limits: tuple[float, float], resolution: float, *, origin: float | None = None) -> list[float]:
        boundaries = cls._grid_boundaries_for_limits(limits, resolution, origin=origin)
        if len(boundaries) < 2:
            return []
        step = max(float(resolution), 1e-9)
        epsilon = step * 1e-9
        visible_start = min(float(limits[0]), float(limits[1]))
        visible_end = max(float(limits[0]), float(limits[1]))
        centers: list[float] = []
        for index in range(len(boundaries) - 1):
            left = float(boundaries[index])
            right = float(boundaries[index + 1])
            visible_left = max(left, visible_start)
            visible_right = min(right, visible_end)
            if visible_right - visible_left <= epsilon:
                continue
            centers.append((visible_left + visible_right) * 0.5)
        return centers

    @staticmethod
    def _index_to_alpha_label(index: int) -> str:
        value = int(index)
        if value < 0:
            return "A"
        label = ""
        while True:
            value, remainder = divmod(value, 26)
            label = chr(ord("A") + remainder) + label
            if value == 0:
                break
            value -= 1
        return label

    @classmethod
    def _numeric_labels_for_centers(cls, centers: list[float], resolution: float, *, origin: float = 0.0) -> list[str]:
        return [str(cls._global_grid_index(center, resolution, origin=origin) + 1) for center in centers]

    @classmethod
    def _alpha_labels_for_centers(cls, centers: list[float], resolution: float, *, origin: float = 0.0) -> list[str]:
        return [cls._index_to_alpha_label(cls._global_grid_index(center, resolution, origin=origin)) for center in centers]

    @staticmethod
    def _global_grid_index(center: float, resolution: float, *, origin: float = 0.0) -> int:
        step = max(float(resolution), 1e-9)
        epsilon = step * 1e-9
        return max(int(math.floor(((float(center) - float(origin)) / step) + epsilon)), 0)

    def _point_size_from_state(self, state: dict[str, Any], settings: FigurePlotSettings, dpi: float) -> float:
        appearance = self._safe_dict(state.get("appearance"))
        point = self._safe_dict(appearance.get("point"))
        scale = signed_percent_to_scale(getattr(settings, "point_size_factor_percent", 0.0))
        try:
            size_px = max(float(point.get("size", settings.point_size)), 0.1)
        except (TypeError, ValueError):
            size_px = max(float(settings.point_size), 0.1)
        return diameter_pixels_to_matplotlib_area(size_px * scale, dpi)

    def _point_border_from_state(
        self,
        state: dict[str, Any],
        metadata_ids: list[int],
        metadata: dict[int, dict[str, str]],
        settings: FigurePlotSettings,
        dpi: float,
    ) -> tuple[list[tuple[float, float, float, float]], float]:
        from matplotlib.colors import to_rgba

        appearance = self._safe_dict(state.get("appearance"))
        border = self._safe_dict(appearance.get("border"))
        scale = signed_percent_to_scale(getattr(settings, "point_size_factor_percent", 0.0))
        try:
            width = pixels_to_points(max(float(border.get("width", 0.0)), 0.0) * scale, dpi)
        except (TypeError, ValueError):
            width = 0.0
        opacity = self._clamped_float(border.get("opacity", 1.0), 0.0, 1.0, 1.0)
        fallback = "#666666"
        base_color = self._mapped_color(border, metadata_ids[0] if metadata_ids else None, metadata, fallback)
        edge_colors = [to_rgba(base_color, opacity) for _ in metadata_ids]
        for index, metadata_id in enumerate(metadata_ids):
            edge_colors[index] = to_rgba(self._mapped_color(border, metadata_id, metadata, base_color), opacity)
        return edge_colors, width

    def _point_colors_from_state(
        self,
        state: dict[str, Any],
        metadata_ids: list[int],
        metadata: dict[int, dict[str, str]],
    ) -> list[tuple[float, float, float, float]]:
        from matplotlib.colors import to_rgba

        appearance = self._safe_dict(state.get("appearance"))
        point = self._safe_dict(appearance.get("point"))
        point_colors = self._safe_dict(appearance.get("point_colors"))
        opacity = self._clamped_float(point.get("opacity", 1.0), 0.0, 1.0, 1.0)
        base_color = self._mapped_color(point_colors, metadata_ids[0] if metadata_ids else None, metadata, "#2f6fed")
        colors = [to_rgba(base_color, opacity) for _ in metadata_ids]
        if str(point_colors.get("mode") or "categorical").lower() == "continuous":
            colors = self._continuous_point_colors(point_colors, metadata_ids, metadata, colors, opacity)
        else:
            for index, metadata_id in enumerate(metadata_ids):
                colors[index] = to_rgba(self._mapped_color(point_colors, metadata_id, metadata, base_color), opacity)
        filter_mask = self._filter_mask_from_state(state, metadata_ids, metadata)
        if filter_mask is None:
            return colors
        filtered_opacity = self._clamped_float(
            self._safe_dict(state.get("filters")).get("filtered_opacity", 1.0),
            0.0,
            1.0,
            1.0,
        )
        return [
            color if bool(filter_mask[index]) else (color[0], color[1], color[2], filtered_opacity)
            for index, color in enumerate(colors)
        ]

    @staticmethod
    def _continuous_point_colors(
        point_colors: dict[str, Any],
        metadata_ids: list[int],
        metadata: dict[int, dict[str, str]],
        fallback_colors: list[tuple[float, float, float, float]],
        opacity: float,
    ) -> list[tuple[float, float, float, float]]:
        """Match Discovery's continuous snapshot gradient for numeric metadata."""
        column = str(point_colors.get("column") or "").strip()
        if not column:
            return fallback_colors
        values: list[float | None] = []
        for metadata_id in metadata_ids:
            try:
                value = float(metadata.get(metadata_id, {}).get(column, ""))
            except (TypeError, ValueError):
                value = None
            values.append(value if value is not None and math.isfinite(value) else None)
        finite_values = [value for value in values if value is not None]
        if not finite_values:
            return fallback_colors
        minimum, maximum = min(finite_values), max(finite_values)
        span = max(maximum - minimum, 1e-9)
        start = to_rgba(str(point_colors.get("gradient_start") or "#3b82f6"), opacity)
        end = to_rgba(str(point_colors.get("gradient_end") or "#ef4444"), opacity)
        colors = list(fallback_colors)
        for index, value in enumerate(values):
            if value is None:
                continue
            ratio = (value - minimum) / span
            colors[index] = tuple(start[channel] + (ratio * (end[channel] - start[channel])) for channel in range(4))
        return colors

    def _point_markers_from_state(
        self,
        state: dict[str, Any],
        metadata_ids: list[int],
        metadata: dict[int, dict[str, str]],
    ) -> list[str] | None:
        appearance = self._safe_dict(state.get("appearance"))
        shape = self._safe_dict(appearance.get("shape"))
        mapping = shape.get("mapping") if isinstance(shape.get("mapping"), dict) else {}
        if not mapping:
            return None
        column = str(shape.get("column") or "Global")
        default_shape = str(shape.get("default") or "circle")
        if column == "Global":
            marker = self._matplotlib_marker(mapping.get("Global") or mapping.get("global") or default_shape)
            return [marker for _ in metadata_ids]
        markers: list[str] = []
        for metadata_id in metadata_ids:
            value = metadata.get(metadata_id, {}).get(column, "")
            markers.append(self._matplotlib_marker(mapping.get(value) or mapping.get(str(value)) or mapping.get("(missing)") or default_shape))
        return markers

    def _render_pin_annotations(
        self,
        axes: Any,
        cell: PlotCell,
        settings: FigurePlotSettings,
    ) -> dict[str, dict[str, Any]]:
        pin_specs: dict[str, dict[str, Any]] = {}
        if not bool(getattr(settings, "pins_visible", True)):
            return pin_specs
        options = self._safe_dict(cell.render_state.get("figure_options"))
        if "pins_visible" in options and not bool(options.get("pins_visible")):
            return pin_specs
        pinboard = self._safe_dict(cell.render_state.get("pinboard"))
        annotations = pinboard.get("annotations")
        if not isinstance(annotations, list) or not annotations:
            return pin_specs
        style = self._pin_style_for_settings(self._normalized_pin_style(self._safe_dict(pinboard.get("style"))), settings)
        metadata_ids = {int(value) for value in cell.metadata_ids}
        for annotation in annotations:
            if not isinstance(annotation, dict) or not bool(annotation.get("visible", True)):
                continue
            try:
                metadata_id = int(annotation.get("metadata_id"))
            except (TypeError, ValueError):
                continue
            if metadata_id not in metadata_ids:
                continue
            point = self._point_pair(annotation.get("point"))
            offset = self._point_pair(annotation.get("screen_offset"), fallback=(48.0, -36.0))
            if point is None or offset is None or not all(math.isfinite(value) for value in (*point, *offset)):
                continue
            annotation_id = str(annotation.get("annotation_id") or "").strip()
            box_anchor = self._box_anchor_data_for_pin(axes, annotation, point=point, offset=offset)
            if not annotation_id or box_anchor is None:
                continue
            artist = self._render_single_pin_annotation(
                axes,
                annotation,
                point=point,
                box_anchor=box_anchor,
                style=style,
                settings=settings,
            )
            if artist is not None:
                pin_specs[annotation_id] = {
                    "artist": artist,
                    "axes": axes,
                    "annotation": annotation,
                    "point": point,
                    "box_anchor": box_anchor,
                    "style": style,
                    "settings": settings,
                }
        return pin_specs

    def _render_single_pin_annotation(
        self,
        axes: Any,
        annotation: dict[str, Any],
        *,
        point: tuple[float, float],
        box_anchor: tuple[float, float],
        style: dict[str, Any],
        settings: FigurePlotSettings,
    ) -> Any | None:
        dpi = max(float(settings.export_dpi), 1.0)
        box = self._pin_content_box(axes, annotation, style=style, settings=settings)
        offset_points = (0.0, 0.0)
        box_xy = box_anchor
        box_xycoords = "data"
        boxcoords = "offset points"
        box_alignment = (0.0, 1.0)
        connector_visible = bool(style.get("connector_visible", True))
        connector_color = str(style.get("connector_color") or "#333333")
        connector_width = pixels_to_points(self._clamped_float(style.get("connector_width", 1.5), 0.0, 100.0, 1.5), dpi)
        if box is not None:
            box_visible = bool(style.get("box_visible", True))
            box_alpha = self._clamped_float(style.get("box_opacity", 0.96), 0.0, 1.0, 0.96) if box_visible else 0.0
            border_width = (
                pixels_to_points(self._clamped_float(style.get("border_width", 1.0), 0.0, 100.0, 1.0), dpi)
                if box_visible
                else 0.0
            )
            ab = AnnotationBbox(
                box,
                box_xy,
                xybox=offset_points,
                xycoords=box_xycoords,
                boxcoords=boxcoords,
                box_alignment=box_alignment,
                frameon=box_visible,
                pad=0.35,
                bboxprops={
                    "facecolor": str(style.get("box_color") or "#f0f0f0"),
                    "edgecolor": str(style.get("border_color") or "#666666"),
                    "linewidth": border_width,
                    "alpha": box_alpha,
                }
                if box_visible
                else None,
                annotation_clip=False,
                zorder=20,
            )
            axes.add_artist(ab)
            ab._eidora_connector_visible = connector_visible
            ab._eidora_connector_color = connector_color
            ab._eidora_connector_width = connector_width
            return ab
        elif connector_visible and connector_width > 0:
            annotation_artist = axes.annotate(
                "",
                xy=point,
                xycoords="data",
                xytext=box_anchor,
                textcoords="data",
                annotation_clip=False,
                arrowprops={
                    "arrowstyle": "-",
                    "color": connector_color,
                    "linewidth": connector_width,
                    "shrinkA": 0.0,
                    "shrinkB": 0.0,
                },
                zorder=20,
            )
            return annotation_artist
        return None

    def _pin_content_box(
        self,
        axes: Any,
        annotation: dict[str, Any],
        *,
        style: dict[str, Any],
        settings: FigurePlotSettings,
    ) -> Any | None:
        dpi = max(float(settings.export_dpi), 1.0)
        font_size = self._scaled_pin_font_size(axes, style, settings)
        text = self._pin_text(annotation, style) if bool(style.get("text_visible", True)) else ""
        thumbnail = self._pin_thumbnail_box(axes, annotation, style=style, settings=settings)
        text_box = None
        pieces: list[Any] = []
        if thumbnail is not None:
            pieces.append(thumbnail)
        if text:
            text_box = TextArea(
                text,
                textprops={
                    "fontsize": font_size,
                    "fontfamily": str(style.get("font_family") or "Arial"),
                    "color": str(style.get("text_color") or "#222222"),
                },
            )
            pieces.append(text_box)
        if not pieces:
            return None
        if len(pieces) == 1:
            return pieces[0]
        position = str(style.get("thumbnail_position") or "left").lower()
        gap = pixels_to_points(6.0, dpi)
        if position == "right":
            return HPacker(children=[text_box, thumbnail], align="top", pad=0, sep=gap)
        if position == "top":
            return VPacker(children=[thumbnail, text_box], align="left", pad=0, sep=gap)
        if position == "bottom":
            return VPacker(children=[text_box, thumbnail], align="left", pad=0, sep=gap)
        return HPacker(children=[thumbnail, text_box], align="top", pad=0, sep=gap)

    def _pin_thumbnail_box(
        self,
        axes: Any,
        annotation: dict[str, Any],
        *,
        style: dict[str, Any],
        settings: FigurePlotSettings,
    ) -> OffsetImage | None:
        if not bool(style.get("thumbnail_visible", False)):
            return None
        path_text = str(annotation.get("thumbnail_path") or "").strip()
        if not path_text:
            return None
        path = Path(path_text)
        if not path.exists():
            return None
        try:
            image = mpimg.imread(path)
        except (OSError, ValueError):
            return None
        thumbnail_size = self._scaled_pin_thumbnail_size_px(axes, style, settings)
        height = float(getattr(image, "shape", [thumbnail_size])[0] or thumbnail_size)
        width = float(getattr(image, "shape", [thumbnail_size, thumbnail_size])[1] or thumbnail_size)
        zoom = min(thumbnail_size / max(height, width, 1.0), 1.0)
        imagebox = OffsetImage(image, zoom=zoom)
        return imagebox

    def _scaled_pin_thumbnail_size_px(self, axes: Any, style: dict[str, Any], settings: FigurePlotSettings) -> float:
        base_size = self._clamped_float(style.get("thumbnail_size", 96), 1.0, 1000.0, 96.0)
        base_size *= self._clamped_float(style.get("size_scale", 1.0), 0.05, 6.0, 1.0)
        axes_width_px, axes_height_px = self._axes_size_px(axes)
        max_size = max(min(axes_width_px, axes_height_px) * 0.32, 1.0)
        return min(base_size, max_size)

    def _scaled_pin_font_size(self, axes: Any, style: dict[str, Any], settings: FigurePlotSettings) -> float:
        base_size = self._clamped_float(style.get("font_size", 11), 1.0, 200.0, 11.0)
        base_size *= self._clamped_float(style.get("size_scale", 1.0), 0.05, 6.0, 1.0)
        axes_width_px, axes_height_px = self._axes_size_px(axes)
        scale = min(max(min(axes_width_px, axes_height_px) / 220.0, 0.55), 1.0)
        return max(base_size * scale, 5.0)

    def _pin_geometries_from_artists(
        self,
        figure: Figure,
        pin_specs: dict[tuple[int, int], dict[str, dict[str, Any]]],
        *,
        canvas_size_px: tuple[int, int],
    ) -> dict[tuple[int, int], dict[str, FigurePinGeometry]]:
        if not pin_specs:
            return {}
        canvas = FigureCanvasAgg(figure)
        canvas.draw()
        renderer = canvas.get_renderer()
        canvas_width, canvas_height = canvas_size_px
        output: dict[tuple[int, int], dict[str, FigurePinGeometry]] = {}
        for cell, annotations in pin_specs.items():
            cell_geometries: dict[str, FigurePinGeometry] = {}
            for annotation_id, spec in annotations.items():
                artist = spec.get("artist")
                axes = spec.get("axes")
                point = self._point_pair(spec.get("point"))
                box_anchor = self._point_pair(spec.get("box_anchor"))
                if artist is None or axes is None or point is None or box_anchor is None:
                    continue
                try:
                    bbox = artist.get_window_extent(renderer)
                    point_display = axes.transData.transform(point)
                except Exception:
                    continue
                if bbox is None or not all(math.isfinite(float(value)) for value in bbox.bounds):
                    continue
                x0 = max(int(math.floor(float(bbox.x0))), 0)
                y0 = max(int(math.floor(float(canvas_height) - float(bbox.y1))), 0)
                x1 = min(int(math.ceil(float(bbox.x1))), int(canvas_width))
                y1 = min(int(math.ceil(float(canvas_height) - float(bbox.y0))), int(canvas_height))
                width = max(x1 - x0, 1)
                height = max(y1 - y0, 1)
                connector_start = (float(point_display[0]), float(canvas_height) - float(point_display[1]))
                style = self._safe_dict(spec.get("style"))
                settings = spec.get("settings")
                connector_end_display = self._pin_connector_target_display(
                    bounds=bbox,
                    anchor_x=float(point_display[0]),
                    anchor_y=float(point_display[1]),
                    mode=str(style.get("connector_target") or "box"),
                    snap=True,
                    thumb_position=str(style.get("thumbnail_position") or "none"),
                    thumb_size=self._scaled_pin_thumbnail_size_px(axes, style, settings),
                )
                connector_end = (float(connector_end_display[0]), float(canvas_height) - float(connector_end_display[1]))
                try:
                    xlim = tuple(float(value) for value in axes.get_xlim())
                    ylim = tuple(float(value) for value in axes.get_ylim())
                except Exception:
                    xlim = (0.0, 1.0)
                    ylim = (0.0, 1.0)
                cell_geometries[annotation_id] = FigurePinGeometry(
                    annotation_id=annotation_id,
                    cell=cell,
                    point_data=point,
                    box_anchor_data=box_anchor,
                    bbox_px=(x0, y0, width, height),
                    connector_start_px=connector_start,
                    connector_end_px=connector_end,
                    connector_color=str(style.get("connector_color") or "#333333"),
                    connector_width=pixels_to_points(
                        self._clamped_float(style.get("connector_width", 1.5), 0.0, 100.0, 1.5),
                        max(float(settings.export_dpi), 1.0),
                    ),
                    xlim=(float(xlim[0]), float(xlim[1])),
                    ylim=(float(ylim[0]), float(ylim[1])),
                )
            if cell_geometries:
                output[cell] = cell_geometries
        return output

    def _render_pin_connectors_from_geometry(
        self,
        figure: Figure,
        pin_geometries: dict[tuple[int, int], dict[str, FigurePinGeometry]],
    ) -> None:
        axes_by_cell = {
            self._axes_cell_from_label(str(axes.get_label())): axes
            for axes in figure.axes
            if str(axes.get_label()).startswith("plot:")
        }
        for cell, annotations in pin_geometries.items():
            axes = axes_by_cell.get(cell)
            if axes is None:
                continue
            for geometry in annotations.values():
                target_data = axes.transData.inverted().transform(
                    (geometry.connector_end_px[0], figure.bbox.height - geometry.connector_end_px[1])
                )
                axes.add_line(
                    Line2D(
                        [geometry.point_data[0], float(target_data[0])],
                        [geometry.point_data[1], float(target_data[1])],
                        transform=axes.transData,
                        color=geometry.connector_color,
                        linewidth=geometry.connector_width,
                        zorder=19,
                        clip_on=False,
                    )
                )

    @staticmethod
    def _axes_cell_from_label(label: str) -> tuple[int, int] | None:
        if not label.startswith("plot:"):
            return None
        try:
            row, column = label.split(":", 1)[1].split(",", 1)
            return int(row), int(column)
        except (TypeError, ValueError, IndexError):
            return None

    @staticmethod
    def _pin_connector_target_display(
        *,
        bounds: Any,
        anchor_x: float,
        anchor_y: float,
        mode: str,
        snap: bool,
        thumb_position: str,
        thumb_size: float,
    ) -> tuple[float, float]:
        _ = snap
        left = float(getattr(bounds, "x0", 0.0))
        right = float(getattr(bounds, "x1", left))
        bottom = float(getattr(bounds, "y0", 0.0))
        top = float(getattr(bounds, "y1", bottom))
        mid_x = (left + right) * 0.5
        mid_y = (bottom + top) * 0.5
        target_x = mid_x
        target_y = mid_y
        normalized_mode = str(mode or "box").strip().lower()
        normalized_thumb = str(thumb_position or "none").strip().lower()
        if normalized_mode == "thumbnail" and normalized_thumb in {"left", "right", "top", "bottom"}:
            thumb_radius = min(max(float(thumb_size), 1.0) * 0.5, max(right - left, top - bottom) * 0.5)
            if normalized_thumb == "left":
                target_x, target_y = left + thumb_radius, mid_y
            elif normalized_thumb == "right":
                target_x, target_y = right - thumb_radius, mid_y
            elif normalized_thumb == "top":
                target_x, target_y = mid_x, top - thumb_radius
            elif normalized_thumb == "bottom":
                target_x, target_y = mid_x, bottom + thumb_radius
        dx = float(target_x) - float(anchor_x)
        dy = float(target_y) - float(anchor_y)
        if abs(dx) <= 1e-9 and abs(dy) <= 1e-9:
            return float(target_x), float(target_y)
        candidates: list[tuple[float, float, float]] = []
        if abs(dx) > 1e-9:
            for edge_x in (left, right):
                t = (edge_x - float(anchor_x)) / dx
                if t <= 0.0 or t > 1.0:
                    continue
                y_value = float(anchor_y) + (t * dy)
                if bottom - 1e-6 <= y_value <= top + 1e-6:
                    candidates.append((t, edge_x, y_value))
        if abs(dy) > 1e-9:
            for edge_y in (bottom, top):
                t = (edge_y - float(anchor_y)) / dy
                if t <= 0.0 or t > 1.0:
                    continue
                x_value = float(anchor_x) + (t * dx)
                if left - 1e-6 <= x_value <= right + 1e-6:
                    candidates.append((t, x_value, edge_y))
        if candidates:
            _t, hit_x, hit_y = min(candidates, key=lambda item: item[0])
            return float(hit_x), float(hit_y)
        return float(target_x), float(target_y)

    def _box_anchor_data_for_pin(
        self,
        axes: Any,
        annotation: dict[str, Any],
        *,
        point: tuple[float, float],
        offset: tuple[float, float],
    ) -> tuple[float, float] | None:
        box_anchor = self._point_pair(annotation.get("box_anchor"))
        if box_anchor is not None:
            return box_anchor
        point_display = axes.transData.transform(point)
        anchor_display = (float(point_display[0]) + float(offset[0]), float(point_display[1]) - float(offset[1]))
        try:
            anchor_data = axes.transData.inverted().transform(anchor_display)
        except Exception:
            return None
        if not all(math.isfinite(float(value)) for value in anchor_data):
            return None
        return float(anchor_data[0]), float(anchor_data[1])

    @staticmethod
    def _data_point_to_canvas_px(
        figure: Figure,
        point: tuple[float, float],
        cell: tuple[int, int],
        plot_rects: dict[tuple[int, int], tuple[int, int, int, int]],
    ) -> tuple[float, float]:
        axes = next((item for item in figure.axes if item.get_label() == f"plot:{cell[0]},{cell[1]}"), None)
        if axes is None:
            rect = plot_rects.get(cell, (0, 0, 1, 1))
            return float(rect[0]), float(rect[1])
        display = axes.transData.transform(point)
        return float(display[0]), float(figure.bbox.height) - float(display[1])

    @staticmethod
    def _axes_size_px(axes: Any) -> tuple[float, float]:
        bbox = axes.get_position()
        figure = axes.figure
        width_px = max(float(bbox.width) * float(figure.bbox.width), 1.0)
        height_px = max(float(bbox.height) * float(figure.bbox.height), 1.0)
        return width_px, height_px

    @staticmethod
    def _pin_text(annotation: dict[str, Any], style: dict[str, Any]) -> str:
        lines = annotation.get("lines")
        if not isinstance(lines, list):
            lines = []
        show_label = bool(style.get("show_label", True))
        show_value = bool(style.get("show_value", True))
        output: list[str] = []
        for line in lines:
            label, separator, value = str(line).partition("\t")
            if separator:
                pieces: list[str] = []
                if show_label and label:
                    pieces.append(label)
                if show_value and value:
                    pieces.append(value)
                text = " ".join(pieces)
            else:
                text = label if show_value or show_label else ""
            if text:
                output.append(text)
        return "\n".join(output)

    @staticmethod
    def _normalized_pin_style(style: dict[str, Any]) -> dict[str, Any]:
        defaults = {
            "thumbnail_visible": False,
            "box_visible": True,
            "connector_visible": True,
            "text_visible": True,
            "box_color": "#f0f0f0",
            "border_color": "#666666",
            "connector_color": "#333333",
            "text_color": "#222222",
            "box_opacity": 0.96,
            "border_width": 1.0,
            "connector_width": 1.5,
            "font_family": "Arial",
            "font_size": 11,
            "show_label": True,
            "show_value": True,
            "thumbnail_size": 96,
            "thumbnail_position": "left",
        }
        defaults.update(style)
        return defaults

    @staticmethod
    def _pin_style_for_settings(style: dict[str, Any], settings: FigurePlotSettings) -> dict[str, Any]:
        result = dict(style)
        result["size_scale"] = signed_percent_to_scale(getattr(settings, "pin_size_factor_percent", 0.0))
        result["thumbnail_visible"] = bool(getattr(settings, "pin_thumbnail_visible", True))
        result["connector_width"] = getattr(settings, "pin_line_width", result.get("connector_width", 1.5))
        return result

    @staticmethod
    def _point_pair(value: object, *, fallback: tuple[float, float] | None = None) -> tuple[float, float] | None:
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            try:
                return float(value[0]), float(value[1])
            except (TypeError, ValueError):
                return fallback
        return fallback

    @staticmethod
    def _matplotlib_marker(shape: object) -> str:
        return {
            "circle": "o",
            "square": "s",
            "diamond": "D",
            "triangle": "^",
            "pentagon": "p",
            "hexagon": "h",
            "octagon": "8",
        }.get(str(shape or "").strip().lower(), "o")

    def _mapped_color(
        self,
        point_colors: dict[str, Any],
        metadata_id: int | None,
        metadata: dict[int, dict[str, str]],
        fallback: str,
    ) -> str:
        mapping = point_colors.get("mapping") if isinstance(point_colors.get("mapping"), dict) else {}
        column = str(point_colors.get("column") or "Global")
        if column == "Global":
            return str(mapping.get("Global") or mapping.get("global") or self._palette_first(point_colors) or fallback)
        if metadata_id is None:
            return fallback
        value = metadata.get(metadata_id, {}).get(column, "")
        if value:
            return str(mapping.get(value) or self._palette_first(point_colors) or fallback)
        return str(mapping.get("(missing)") or self._palette_first(point_colors) or fallback)

    @staticmethod
    def _palette_first(point_colors: dict[str, Any]) -> str | None:
        palette = point_colors.get("palette")
        if isinstance(palette, list) and palette:
            return str(palette[0])
        return None

    def _filter_mask_from_state(
        self,
        state: dict[str, Any],
        metadata_ids: list[int],
        metadata: dict[int, dict[str, str]],
    ) -> list[bool] | None:
        filters = self._safe_dict(state.get("filters"))
        rules = filters.get("rules")
        active = (
            [
                rule
                for rule in rules
                if isinstance(rule, dict) and bool(rule.get("active", False))
            ]
            if isinstance(rules, list)
            else []
        )
        mask: list[bool] | None = [True for _ in metadata_ids] if active else None
        for rule in active:
            column = str(rule.get("column") or "")
            if not column:
                continue
            next_mask = [
                self._metadata_value_matches_rule(
                    metadata.get(metadata_id, {}).get(column, ""), rule
                )
                for metadata_id in metadata_ids
            ]
            if mask is not None:
                mask = [
                    current and next_value
                    for current, next_value in zip(mask, next_mask, strict=True)
                ]
        slicer = self._safe_dict(state.get("slicer_selection"))
        slicer_column = str(slicer.get("column") or "").strip()
        slicer_values = slicer.get("values")
        if slicer_column and isinstance(slicer_values, list):
            active_values = {str(value) for value in slicer_values}
            slicer_mask = [
                metadata.get(metadata_id, {}).get(slicer_column, "") in active_values
                for metadata_id in metadata_ids
            ]
            mask = (
                slicer_mask
                if mask is None
                else [
                    current and selected
                    for current, selected in zip(mask, slicer_mask, strict=True)
                ]
            )
        return mask

    def _metadata_value_matches_rule(self, value: str, rule: dict[str, Any]) -> bool:
        kind = str(rule.get("kind") or "").lower()
        operator = str(rule.get("operator") or "").lower()
        if kind == "slider":
            threshold = self._clamped_float(rule.get("threshold", 0.0), float("-inf"), float("inf"), 0.0)
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                return False
            mode = str(rule.get("mode") or "max").lower()
            if mode == "min":
                return numeric >= threshold
            if mode == "exact":
                return numeric == threshold
            return numeric <= threshold
        expected = str(rule.get("value") if kind == "selection" else rule.get("query") or "")
        if operator == "is not":
            return value != expected
        if operator == "contains":
            return expected in value
        if operator == "does not contain":
            return expected not in value
        return value == expected

    @staticmethod
    def _axis_limits(values: list[float]) -> tuple[float, float]:
        if not values:
            return -0.5, 0.5
        low = min(values)
        high = max(values)
        if low == high:
            return low - 0.5, high + 0.5
        return low, high

    def _camera_or_axis_limits(self, camera: dict[str, Any], axis: str, values: list[float]) -> tuple[float, float]:
        try:
            low = float(camera.get(f"{axis}_min"))
            high = float(camera.get(f"{axis}_max"))
        except (TypeError, ValueError):
            return self._axis_limits(values)
        if high <= low:
            return self._axis_limits(values)
        return low, high

    @staticmethod
    def _safe_dict(value: object) -> dict[str, Any]:
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _clamped_float(value: object, low: float, high: float, fallback: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return fallback
        return min(max(number, low), high)


class _RichTextRunParser(HTMLParser):
    _IGNORED_CONTAINER_TAGS = {"style", "script", "head", "title"}
    _IGNORED_EMPTY_TAGS = {"meta", "link"}

    def __init__(self, *, only_body: bool = False) -> None:
        super().__init__()
        self._only_body = only_body
        self._inside_body = not only_body
        self._runs: list[_CaptionRun] = []
        self._style_stack: list[tuple[bool, bool]] = [(False, False)]
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_name = tag.lower()
        if tag_name == "body":
            self._inside_body = True
            self._style_stack.append(self._style_stack[-1])
            return
        if tag_name in self._IGNORED_CONTAINER_TAGS:
            self._ignored_depth += 1
            self._style_stack.append(self._style_stack[-1])
            return
        if tag_name in self._IGNORED_EMPTY_TAGS:
            return
        if self._ignored_depth or not self._inside_body:
            self._style_stack.append(self._style_stack[-1])
            return
        if tag_name == "br":
            self._append_run("\n")
            return
        if tag_name in {"p", "div"}:
            self._append_run("\n")
        bold, italic = self._style_stack[-1]
        if tag_name in {"b", "strong"}:
            bold = True
        if tag_name in {"i", "em"}:
            italic = True
        style = dict(attrs).get("style") or ""
        if re.search(r"font-weight\s*:\s*(bold|[6-9]00)", style, flags=re.IGNORECASE):
            bold = True
        if re.search(r"font-style\s*:\s*(italic|oblique)", style, flags=re.IGNORECASE):
            italic = True
        self._style_stack.append((bold, italic))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_name = tag.lower()
        if self._ignored_depth or not self._inside_body or tag_name in self._IGNORED_EMPTY_TAGS:
            return
        if tag_name == "br":
            self._append_run("\n")

    def handle_endtag(self, tag: str) -> None:
        tag_name = tag.lower()
        if len(self._style_stack) > 1:
            self._style_stack.pop()
        if tag_name == "body":
            self._inside_body = False
            return
        if self._ignored_depth:
            if tag_name in self._IGNORED_CONTAINER_TAGS:
                self._ignored_depth = max(self._ignored_depth - 1, 0)
            return
        if tag_name in {"p", "div"}:
            self._append_run("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth or not self._inside_body:
            return
        self._append_run(data)

    def runs(self) -> list[_CaptionRun]:
        compacted: list[_CaptionRun] = []
        previous_newline = False
        for run in self._runs:
            text = re.sub(r"[ \t\r\f\v]+", " ", run.text)
            if not text:
                continue
            if text == "\n":
                if previous_newline:
                    continue
                previous_newline = True
            else:
                previous_newline = False
            if compacted and compacted[-1].bold == run.bold and compacted[-1].italic == run.italic and compacted[-1].text != "\n" and text != "\n":
                compacted[-1] = _CaptionRun(compacted[-1].text + text, run.bold, run.italic)
            else:
                compacted.append(_CaptionRun(text, run.bold, run.italic))
        while compacted and compacted[0].text == "\n":
            compacted.pop(0)
        while compacted and compacted[-1].text == "\n":
            compacted.pop()
        return compacted

    def _append_run(self, text: str) -> None:
        if not text:
            return
        bold, italic = self._style_stack[-1]
        self._runs.append(_CaptionRun(text, bold, italic))
