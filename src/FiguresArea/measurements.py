"""Translate displayed output measurements to immutable plot design units."""
from __future__ import annotations

from dataclasses import replace
import math

# Values rendered in points except the explicitly named millimetre fields.
LOCAL_SIZES = frozenset({
    'axis_box_line_width', 'grid_line_width', 'grid_cell_size_mm',
    'tick_label_size', 'tick_label_margin_pt', 'subtitle_size', 'subtitle_margin_mm',
    'subplot_caption_size', 'caption_margin_mm', 'legend_font_size',
    'legend_marker_size', 'legend_margin_mm', 'pin_line_width',
})
LEGEND_SIZES = frozenset({'legend_font_size', 'legend_marker_size', 'legend_margin_mm'})
FONT_SIZES = frozenset({'tick_label_size', 'subtitle_size', 'subplot_caption_size',
                        'legend_font_size', 'main_title_size', 'main_caption_size'})


MM_SIZES = frozenset({'width_mm', 'height_mm', 'margin_mm', 'subplot_margin_mm',
                      'main_title_margin_mm', 'subtitle_margin_mm', 'legend_margin_mm',
                      'caption_margin_mm', 'grid_cell_size_mm'})
POINT_SIZES = FONT_SIZES | {'axis_box_line_width', 'grid_line_width',
                           'tick_label_margin_pt', 'legend_marker_size'}
PIXEL_SIZES = MM_SIZES | POINT_SIZES | {'pin_line_width'}


def pixels_per_unit(settings, key):
    """Keep the saved rendering contract; all editor measurements are pixels."""
    dpi = max(settings.export_dpi, 36)
    if key in MM_SIZES:
        return dpi / 25.4
    if key in POINT_SIZES:
        return dpi / 72.0
    return 1.0


def page_inches_for_pixels(inches, dpi):
    # Agg truncates its buffer dimensions. Protect an integer pixel request
    # against the sub-pixel error introduced by mm/inch conversion.
    pixels = inches * dpi
    nearest = round(pixels)
    return math.nextafter(nearest / dpi, math.inf) if abs(pixels - nearest) < 1e-9 else inches


def scaled_sizes(settings):
    return LOCAL_SIZES - LEGEND_SIZES if settings.legend_scope == 'global' else LOCAL_SIZES


def validated_sizes(values):
    if not isinstance(values, dict):
        return {}
    return {key: float(value) for key, value in values.items()
            if key in LOCAL_SIZES and isinstance(value, (float, int)) and not isinstance(value, bool)
            and math.isfinite(value) and (value >= 0 or key == 'subtitle_margin_mm')}


def plot_settings(settings, cell):
    overrides = {key: value for key, value in validated_sizes(cell.size_overrides).items() if key in scaled_sizes(settings)}
    if not overrides:
        return settings
    from FiguresArea.composition_service import clear_inherited_bands
    return clear_inherited_bands(settings, replace(settings, **overrides))


def plot_element(element, settings):
    # Free text elements retain the sizes chosen in their own editor.
    for prefix, key in (('plot_title_settings_', 'subtitle_size'), ('subplot_caption_settings_', 'subplot_caption_size')):
        if element.element_id.startswith(prefix):
            return replace(element, font_size=getattr(settings, key))
    return element


def output_measurements(settings):
    scale = getattr(settings, 'content_scale', 1.0)
    values = {key: float(getattr(settings, key)) * scale * pixels_per_unit(settings, key)
              for key in LOCAL_SIZES}
    # Matplotlib enforces a one-point minimum for rendered text.
    for key in FONT_SIZES & values.keys():
        values[key] = max(pixels_per_unit(settings, key), values[key])
    return values


def measurement_snapshot(composition):
    """Text/style-only layout request: never copy, serialize or load point data."""
    import copy
    return replace(composition,
        plot_cells={key: replace(cell, coordinates=[], metadata_ids=[], metadata={}, data_ref=None,
                                 point_count=0, data_key='', data_sources=(),
                                 size_overrides=dict(cell.size_overrides), render_state=copy.deepcopy(cell.render_state))
                    for key, cell in composition.plot_cells.items()},
        figure_elements=copy.deepcopy(composition.figure_elements))


def prepare_measurements(composition, values):
    """Solve the requested pixel sizes for all plots against their resulting layout.

    Runs in the isolated rendering process. Only design overrides are returned;
    subsequent page edits scale those values normally, with no rounding drift.
    """
    from matplotlib.figure import Figure
    from FiguresArea.renderer import FigureMatplotlibRenderer
    from FiguresArea.page_layout import measure_page
    values = validated_sizes(values)
    values = {key: value for key, value in values.items() if key in scaled_sizes(composition.settings)}
    minimum_font = pixels_per_unit(composition.settings, 'subtitle_size')
    if any(values[key] < minimum_font - .005 for key in FONT_SIZES & values.keys()):
        raise ValueError(f'Text sizes must be at least {minimum_font:.2f} px.')
    if values.get('grid_cell_size_mm', 1) <= 0:
        raise ValueError('Grid cell size must be greater than zero.')
    renderer = FigureMatplotlibRenderer()
    figure = Figure(figsize=(1, 1), dpi=max(composition.settings.export_dpi, 36))
    try:
        for attempt in range(40):
            figure.clear()
            page = measure_page(renderer, composition, figure)
            error = 0.0
            for cell_key, cell in composition.plot_cells.items():
                effective = page.plots[cell_key][4]
                measured = output_measurements(effective)
                scale = effective.content_scale
                for key, desired in values.items():
                    error = max(error, abs(measured[key] - desired))
                    factor = scale * pixels_per_unit(effective, key)
                    cell.size_overrides[key] = desired / factor
            if error < .002:
                return {key: dict(cell.size_overrides) for key, cell in composition.plot_cells.items()}
        raise ValueError('These measurements cannot fit in the current plot cells. Increase the figure size or reduce the requested sizes.')
    finally:
        figure.clear()


def size_override_payload(composition):
    return [{'row': row, 'column': column, 'values': dict(cell.size_overrides)}
            for (row, column), cell in sorted(composition.plot_cells.items()) if cell.size_overrides]


def restore_size_overrides(composition, payload):
    values = {}
    for entry in payload if isinstance(payload, list) else []:
        if not isinstance(entry, dict):
            continue
        row, column = entry.get('row'), entry.get('column')
        if type(row) is int and type(column) is int:
            values[row, column] = validated_sizes(entry.get('values'))
    for key, cell in composition.plot_cells.items():
        cell.size_overrides = values.get(key, {})
