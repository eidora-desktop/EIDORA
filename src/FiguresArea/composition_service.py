"""Capture edits without copying immutable point datasets on the GUI thread."""
from __future__ import annotations

import copy
from dataclasses import replace
from uuid import uuid4

from FiguresArea.domain import FigureComposition


def snapshot_composition(composition: FigureComposition) -> FigureComposition:
    cells = {}
    for key, cell in composition.plot_cells.items():
        sources = (cell.coordinates, cell.metadata_ids, cell.metadata)
        if not cell.data_sources or any(a is not b for a, b in zip(sources, cell.data_sources)):
            if cell.data_sources:
                cell.data_ref = None
            cell.data_key = cell.data_ref or uuid4().hex
            cell.data_sources = sources
            cell.point_count = len(cell.coordinates) if not cell.data_ref else cell.point_count
        cells[key] = replace(
            cell, render_state=copy.deepcopy(cell.render_state), element_ids=list(cell.element_ids),
            size_overrides=dict(cell.size_overrides),
        )
    return replace(composition, plot_cells=cells, figure_elements=copy.deepcopy(composition.figure_elements))


def preview_needs_worker(composition: FigureComposition) -> bool:
    # Small forms stay immediate. Dataset loading and substantial rendering
    # always go to the isolated renderer, including restored lazy documents.
    return (
        max(len(composition.plot_cells), composition.settings.rows * composition.settings.columns) > 16
        or sum(max(len(c.coordinates), c.point_count) for c in composition.plot_cells.values()) > 2000
        or any(c.data_ref and not len(c.coordinates) for c in composition.plot_cells.values())
        or composition.settings.width_mm * composition.settings.height_mm
        * (composition.settings.export_dpi / 25.4) ** 2
        * (len(composition.plot_cells) if composition.settings.layout_version < 2 else 1) > 32_000_000
        or sum(len(c.render_state.get("pinboard", {}).get("annotations", [])) for c in composition.plot_cells.values()) > 50
    )


def reserve_grid(composition: FigureComposition) -> FigureComposition:
    """Expand a fixed page's reserved grid to include an adjacent plot."""
    cells = composition.plot_cells
    min_row = min(0, min((key[0] for key in cells), default=0))
    min_column = min(0, min((key[1] for key in cells), default=0))
    max_row = max(composition.settings.rows - 1, max((key[0] for key in cells), default=0))
    max_column = max(composition.settings.columns - 1, max((key[1] for key in cells), default=0))
    shifted = shift_grid(composition, -min_row, -min_column)
    return replace(shifted, settings=replace(composition.settings, rows=max_row - min_row + 1, columns=max_column - min_column + 1))


def shift_grid(composition, row_offset, column_offset):
    """Rekey positions and generated annotations, retaining pending jobs' cells."""
    cells = composition.plot_cells
    shifted_cells = {}
    for (row, column), cell in cells.items():
        cell.row, cell.column = row + row_offset, column + column_offset
        shifted_cells[cell.row, cell.column] = cell
    shifted_elements = {}
    renamed = {}
    for key, element in composition.figure_elements.items():
        source = element.source_cell
        if source is not None:
            source = (source[0] + row_offset, source[1] + column_offset)
            for prefix in ('plot_title_settings_', 'legend_settings_', 'subplot_caption_settings_'):
                if key == f'{prefix}{element.source_cell[0]}_{element.source_cell[1]}':
                    renamed[key] = f'{prefix}{source[0]}_{source[1]}'
        name = renamed.get(key, key)
        shifted_elements[name] = replace(element, element_id=name, source_cell=source)
    for cell in cells.values():
        cell.element_ids = [renamed.get(key, key) for key in cell.element_ids]
    return replace(
        composition,
        plot_cells=shifted_cells,
        figure_elements=shifted_elements,
        selected_element_id=renamed.get(composition.selected_element_id, composition.selected_element_id),
    )


def clear_inherited_bands(previous, settings):
    """Editing decorations replaces their converted legacy spacing constraints."""
    from dataclasses import fields
    changed = {field.name for field in fields(settings) if getattr(previous, field.name) != getattr(settings, field.name)}
    local = any(name.startswith(('subtitle_', 'subplot_caption_', 'legend_', 'grid_', 'tick_', 'axis_'))
                or name == 'caption_margin_mm' for name in changed)
    shared = any(name.startswith(('main_title_', 'main_caption_', 'legend_'))
                 or name in {'margin_mm', 'caption_margin_mm'} for name in changed)
    return replace(settings,
        cell_padding_mm=(0.,) * 4 if local else settings.cell_padding_mm,
        cell_content_padding_mm=(0.,) * 4 if local else settings.cell_content_padding_mm,
        page_padding_mm=(0.,) * 4 if shared else settings.page_padding_mm)
