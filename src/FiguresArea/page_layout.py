"""Measure page geometry without loading points or allocating a page bitmap."""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

from matplotlib.backends.backend_agg import RendererAgg
from matplotlib.figure import Figure

from FiguresArea.domain import FigureComposition, FigurePlotSettings, PlotCell
from FiguresArea.measurements import plot_settings, plot_element

SIDES = ("left", "right", "top", "bottom")


@dataclass(frozen=True)
class PlotRenderSettings(FigurePlotSettings):
    # Render-only scale: never written back into the document's style settings.
    content_scale: float = 1.0


@dataclass
class PageLayout:
    width: float
    height: float
    plots: dict
    legends: dict
    legend_origins: dict
    margins: dict
    bands: dict
    content_bands: dict | None = None


def legacy_page_layout(renderer, composition, figure):
    settings = composition.settings
    cells = composition.plot_cells or {(0, 0): PlotCell(row=0, column=0)}
    min_row, max_row, min_column, max_column = renderer._bounds(cells)
    rows, columns = max_row - min_row + 1, max_column - min_column + 1
    dpi = max(int(settings.export_dpi), 36)
    subplot_width_in = renderer._mm_to_inches(settings.width_mm)
    subplot_height_in = renderer._mm_to_inches(settings.height_mm)
    subplot_gap_in = renderer._subplot_margin_inches(settings)
    cell_element_bands_in = renderer._cell_element_bands_inches(
        composition,
        caption_width_in=subplot_width_in,
    )
    axis_bands_in = renderer._axis_decoration_bands_inches(settings)
    cell_bands_in = {side: band + axis_bands_in[side] for side, band in cell_element_bands_in.items()}
    cell_bands_in = {side: max(cell_bands_in[side], saved / 25.4)
                     for side, saved in zip(SIDES, settings.cell_content_padding_mm)}
    content_bands = cell_bands_in.copy()
    # Text metrics do not require allocating an Agg buffer for the old page
    # at a potentially new DPI. Use a tiny measurement surface.
    legend_renderer = RendererAgg(1, 1, dpi)
    legends = {}
    for element in composition.figure_elements.values():
        if not element.visible or element.kind != "legend":
            continue
        if element.source_cell is not None and element.source_cell not in cells:
            continue
        width_in = subplot_width_in if element.source_cell is not None else columns * subplot_width_in
        legend = renderer._create_legend(figure, element, composition, width_in)
        if legend is not None:
            bbox = legend.get_window_extent(legend_renderer)
            legends[element.element_id] = (element, legend, bbox.width / dpi, bbox.height / dpi)
    local_legends = [entry for entry in legends.values() if entry[0].source_cell is not None]
    global_legends = [entry for entry in legends.values() if entry[0].source_cell is None]
    cell_bands_in, legend_rects = renderer._layout_legends(
        local_legends, settings, subplot_width_in, subplot_height_in, cell_bands_in, dpi,
    )
    cell_bands_in = {side: max(cell_bands_in[side], saved / 25.4)
                     for side, saved in zip(SIDES, settings.cell_padding_mm)}
    cell_width_in = cell_bands_in["left"] + subplot_width_in + cell_bands_in["right"]
    cell_height_in = cell_bands_in["top"] + subplot_height_in + cell_bands_in["bottom"]
    grid_width_in = columns * cell_width_in + (columns - 1) * subplot_gap_in
    grid_height_in = rows * cell_height_in + (rows - 1) * subplot_gap_in
    margins_in = renderer._reserved_margins_inches(
        composition,
        caption_width_in=max(columns * subplot_width_in, subplot_width_in),
    )
    global_bands_in, global_legend_rects = renderer._layout_legends(
        global_legends,
        settings,
        grid_width_in - cell_bands_in["left"] - cell_bands_in["right"],
        grid_height_in - cell_bands_in["top"] - cell_bands_in["bottom"],
        cell_bands_in,
        dpi,
    )
    legend_rects.update(global_legend_rects)
    outer_margin_in = renderer._margin_mm_to_inches(settings.margin_mm)
    margins_in = {
        side: margin + global_bands_in[side] - cell_bands_in[side] + outer_margin_in
        for side, margin in margins_in.items()
    }
    figure_width_in = grid_width_in + margins_in["left"] + margins_in["right"]
    figure_height_in = grid_height_in + margins_in["top"] + margins_in["bottom"]

    plots = {}
    for row, column in cells:
        left = margins_in["left"] + (column - min_column) * (cell_width_in + subplot_gap_in) + cell_bands_in["left"]
        bottom = margins_in["bottom"] + (max_row - row) * (cell_height_in + subplot_gap_in) + cell_bands_in["bottom"]
        plots[row, column] = (left, bottom, subplot_width_in, subplot_height_in, settings)
    origins = {None: (margins_in["left"] + cell_bands_in["left"], margins_in["bottom"] + cell_bands_in["bottom"])}
    origins.update({key: rect[:2] for key, rect in plots.items()})
    legend_origins = {key: (x + origins[legends[key][0].source_cell][0], y + origins[legends[key][0].source_cell][1])
                      for key, (x, y) in legend_rects.items()}
    return PageLayout(figure_width_in, figure_height_in, plots, legends, legend_origins, margins_in, cell_bands_in, content_bands)


def measure_page(renderer, composition, figure):
    settings = composition.settings
    if settings.layout_version < 2:
        return legacy_page_layout(renderer, composition, figure)
    width = renderer._mm_to_inches(settings.width_mm)
    height = renderer._mm_to_inches(settings.height_mm)
    dpi = max(settings.export_dpi, 36)
    cells = dict(composition.plot_cells)
    min_row = min(0, min((key[0] for key in cells), default=0))
    min_column = min(0, min((key[1] for key in cells), default=0))
    max_row = max(settings.rows - 1, max((key[0] for key in cells), default=0))
    max_column = max(settings.columns - 1, max((key[1] for key in cells), default=0))
    rows, columns = max_row - min_row + 1, max_column - min_column + 1
    for row in range(min_row, max_row + 1):
        for column in range(min_column, max_column + 1):
            cells.setdefault((row, column), PlotCell(row, column))

    global_elements = {key: element for key, element in composition.figure_elements.items() if element.source_cell is None}
    global_composition = replace(composition, figure_elements=global_elements)
    outer = renderer._margin_mm_to_inches(settings.margin_mm)
    margins = renderer._reserved_margins_inches(global_composition, caption_width_in=max(width - 2 * outer, .01))
    global_legends = {}
    metrics = RendererAgg(1, 1, dpi)
    for element in global_elements.values():
        if element.visible and element.kind == 'legend':
            legend_width = min(width - 2 * outer, settings.plot_width_mm * columns / 25.4)
            legend = renderer._create_legend(figure, element, composition, max(legend_width, .01))
            if legend is not None:
                box = legend.get_window_extent(metrics)
                global_legends[element.element_id] = (element, legend, box.width / dpi, box.height / dpi)
    legend_bands, _ = renderer._layout_legends(list(global_legends.values()), settings,
                                             width - 2 * outer, height - 2 * outer,
                                             dict.fromkeys(SIDES, 0.0), dpi)
    margins = {side: max(margins[side] + legend_bands[side] + outer, saved / 25.4)
               for side, saved in zip(SIDES, settings.page_padding_mm)}
    gap = renderer._subplot_margin_inches(settings)
    grid_width = width - margins['left'] - margins['right']
    grid_height = height - margins['top'] - margins['bottom']
    slot_width = (grid_width - (columns - 1) * gap) / columns
    slot_height = (grid_height - (rows - 1) * gap) / rows
    if min(slot_width, slot_height) <= 0:
        raise ValueError('The figure is too small for its margins, spacing and figure-level text. Increase the figure size or reduce these settings.')

    reference_settings = replace(settings, width_mm=settings.plot_width_mm, height_mm=settings.plot_height_mm,
                                 margin_mm=0.0, subplot_margin_mm=0.0, page_padding_mm=(0.,) * 4, layout_version=1)
    plots, legends, origins = {}, dict(global_legends), {}
    for key, cell in sorted(cells.items()):
        local_settings = plot_settings(reference_settings, cell)
        if settings.aspect_ratio == 'snapshot':
            local_settings = snapshot_dimensions(local_settings, cell)
        elements = {name: plot_element(element, local_settings) for name, element in composition.figure_elements.items() if element.source_cell == key}
        local = replace(composition, settings=local_settings, plot_cells={key: cell}, figure_elements=elements)
        local_settings, reference, fitted = fit_plot(renderer, local, figure, slot_width, slot_height)
        local_width, local_height, local_bands, local_legends, local_origins, scale = fitted
        render_settings = PlotRenderSettings(**vars(local_settings), content_scale=scale)
        x = margins['left'] + (key[1] - min_column) * (slot_width + gap) + (slot_width - local_width) / 2
        y = margins['bottom'] + (max_row - key[0]) * (slot_height + gap) + (slot_height - local_height) / 2
        plot_width, plot_height = reference.plots[key][2:4]
        plots[key] = (x + local_bands['left'], y + local_bands['bottom'], plot_width * scale, plot_height * scale, render_settings)
        for name, entry in local_legends.items():
            legends[name] = entry
            lx, ly = local_origins[name]
            origins[name] = (x + local_bands['left'] + lx, y + local_bands['bottom'] + ly)

    # Shared legends surround the complete grid, including empty reserved cells.
    left = min(rect[0] for rect in plots.values())
    bottom = min(rect[1] for rect in plots.values())
    right = max(rect[0] + rect[2] for rect in plots.values())
    top = max(rect[1] + rect[3] for rect in plots.values())
    bands = dict(left=left - margins['left'], right=width - margins['right'] - right,
                 bottom=bottom - margins['bottom'], top=height - margins['top'] - top)
    _, global_origins = renderer._layout_legends(list(global_legends.values()), settings, right - left, top - bottom, bands, dpi)
    for key, (x, y) in global_origins.items():
        _, _, legend_width, legend_height = global_legends[key]
        padding = max(1 / dpi, 1 / 72)
        if legend_width + 2 * (outer + padding) > width or legend_height + 2 * (outer + padding) > height:
            raise ValueError('The shared legend does not fit on the figure. Increase the figure size or reduce the legend font size or columns.')
        origins[key] = (min(max(left + x, outer + padding), width - outer - padding - legend_width),
                        min(max(bottom + y, outer + padding), height - outer - padding - legend_height))
    return PageLayout(width, height, plots, legends, origins, margins, bands)


def snapshot_dimensions(settings, cell):
    """Use captured camera spans without loading points or following live sources."""
    base = cell.render_state.get('snapshot_base', {})
    state = base.get('state', {}) if isinstance(base, dict) else {}
    camera = state.get('camera') if isinstance(state, dict) else None
    if not isinstance(camera, dict):
        camera = cell.render_state.get('camera', {})
    if not isinstance(camera, dict):
        return settings
    try:
        spans = [float(camera[f'{axis}_max']) - float(camera[f'{axis}_min']) for axis in ('x', 'y')]
    except (KeyError, TypeError, ValueError, OverflowError):
        return settings
    if not all(math.isfinite(span) and span > 1e-9 for span in spans):
        return settings
    ratio = spans[0] / spans[1]
    if not math.isfinite(ratio) or ratio <= 0:
        return settings
    width = min(settings.width_mm, settings.height_mm * ratio)
    return replace(settings, width_mm=width, height_mm=width / ratio)


def fit_plot(renderer, composition, figure, slot_width, slot_height):
    """Fit styles uniformly; optionally expand the plot into the remaining space."""
    reference = legacy_page_layout(renderer, composition, figure)
    scale = min(slot_width / reference.width, slot_height / reference.height)
    # Avoid microscopic rounding differences during saved-layout conversion.
    if abs(scale - 1.0) < 1e-12:
        scale = 1.0
    for attempt in range(12):
        fitted = fit_cell(renderer, composition, figure, reference, scale, slot_width, slot_height)
        width, height, bands, legends, _origins, scale = fitted
        if composition.settings.aspect_ratio != 'fit' or max(abs(slot_width - width), abs(slot_height - height)) < 1e-8:
            return composition.settings, reference, fitted
        # Remeasure after expansion: captions can wrap differently and map
        # labels can need more room. Only transient render dimensions change.
        settings = replace(composition.settings,
            width_mm=(slot_width - bands['left'] - bands['right']) / scale * 25.4,
            height_mm=(slot_height - bands['top'] - bands['bottom']) / scale * 25.4)
        for _, legend, _, _ in legends.values():
            legend.remove()
        composition = replace(composition, settings=settings)
        reference = legacy_page_layout(renderer, composition, figure)
    raise ValueError('The plot annotations do not fit in their grid cell. Increase the figure size or reduce annotation sizes.')


def migrate_page_dimensions(composition):
    """Convert old plot dimensions using measured geometry, without reading points."""
    if composition.settings.layout_version >= 2:
        return composition
    from FiguresArea.renderer import FigureMatplotlibRenderer
    settings = composition.settings
    figure = Figure(figsize=(1, 1), dpi=max(settings.export_dpi, 36))
    try:
        page = legacy_page_layout(FigureMatplotlibRenderer(), composition, figure)
    finally:
        figure.clear()
    cells = composition.plot_cells
    min_row, max_row, min_column, max_column = FigureMatplotlibRenderer._bounds(cells)
    # Old adjacent-plot controls allowed negative cell coordinates. Normalize
    # them once so later settings edits cannot silently remove those plots.
    from FiguresArea.composition_service import shift_grid
    composition = shift_grid(replace(composition, plot_cells={key: replace(cell) for key, cell in cells.items()}),
                             -min_row, -min_column)
    settings = replace(settings, layout_version=2, width_mm=page.width * 25.4, height_mm=page.height * 25.4,
                       plot_width_mm=settings.width_mm, plot_height_mm=settings.height_mm,
                       rows=max_row - min_row + 1, columns=max_column - min_column + 1,
                       cell_padding_mm=tuple(page.bands[side] * 25.4 for side in SIDES),
                       cell_content_padding_mm=tuple(page.content_bands[side] * 25.4 for side in SIDES),
                       page_padding_mm=tuple(page.margins[side] * 25.4 for side in SIDES))
    for row in range(settings.rows):
        for column in range(settings.columns):
            composition.plot_cells.setdefault((row, column), PlotCell(row, column, reserved_only=True))
    return replace(composition, settings=settings)


def fit_cell(renderer, composition, figure, reference, scale, slot_width, slot_height):
    """Fit measured artists, including font hinting at the final output scale."""
    settings = composition.settings
    plot_width, plot_height = next(iter(reference.plots.values()))[2:4]
    dpi = max(settings.export_dpi, 36)
    metrics = RendererAgg(1, 1, dpi)
    if scale == 1.0 and reference.width <= slot_width + 1e-10 and reference.height <= slot_height + 1e-10:
        origins = {key: (x - reference.bands['left'], y - reference.bands['bottom'])
                   for key, (x, y) in reference.legend_origins.items()}
        return reference.width, reference.height, reference.bands, reference.legends, origins, scale
    for _, legend, _, _ in reference.legends.values():
        legend.remove()
    for attempt in range(8):
        legends = {}
        scaled = replace(settings, legend_font_size=settings.legend_font_size * scale,
                         legend_marker_size=settings.legend_marker_size * scale,
                         legend_margin_mm=settings.legend_margin_mm * scale)
        scaled = PlotRenderSettings(**vars(scaled), content_scale=scale)
        for key, (element, _, _, _) in reference.legends.items():
            legend = renderer._create_legend(figure, element, replace(composition, settings=scaled), plot_width * scale)
            legend.get_frame().set_linewidth(legend.get_frame().get_linewidth() * scale)
            box = legend.get_window_extent(metrics)
            legends[key] = (element, legend, box.width / dpi, box.height / dpi)
        bands, origins = renderer._layout_legends(list(legends.values()), scaled, plot_width * scale, plot_height * scale,
                                                 {side: reference.content_bands[side] * scale for side in SIDES}, dpi)
        bands = {side: max(bands[side], settings.cell_padding_mm[index] / 25.4 * scale)
                 for index, side in enumerate(SIDES)}
        width = bands['left'] + plot_width * scale + bands['right']
        height = bands['bottom'] + plot_height * scale + bands['top']
        if width <= slot_width + 1e-10 and height <= slot_height + 1e-10:
            return width, height, bands, legends, origins, scale
        for _, legend, _, _ in legends.values():
            legend.remove()
        scale *= min(slot_width / width, slot_height / height) * .995
    raise ValueError('The plot annotations do not fit in their grid cell. Increase the figure size or reduce annotation sizes.')
