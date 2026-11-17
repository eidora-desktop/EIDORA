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
        )
    return replace(composition, plot_cells=cells, figure_elements=copy.deepcopy(composition.figure_elements))


def preview_needs_worker(composition: FigureComposition) -> bool:
    # Small forms stay immediate. Dataset loading and substantial rendering
    # always go to the isolated renderer, including restored lazy documents.
    return (
        len(composition.plot_cells) > 16
        or sum(max(len(c.coordinates), c.point_count) for c in composition.plot_cells.values()) > 2000
        or any(c.data_ref and not len(c.coordinates) for c in composition.plot_cells.values())
        or composition.settings.width_mm * composition.settings.height_mm
        * (composition.settings.export_dpi / 25.4) ** 2 * len(composition.plot_cells) > 32_000_000
        or sum(len(c.render_state.get("pinboard", {}).get("annotations", [])) for c in composition.plot_cells.values()) > 50
    )
