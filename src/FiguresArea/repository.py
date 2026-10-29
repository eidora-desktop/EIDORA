from __future__ import annotations

import copy
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from FiguresArea.domain import (
    FigureComposition,
    FigureElement,
    FigurePlotSettings,
    PlotCell,
)
from FiguresArea.template_settings import default_plot_settings
from project.portable_paths import ProjectPaths
from shared.persistence import json_store


class FigureTabsRepository:
    """Project-local persistence boundary for saved FiguresArea compositions."""

    _FILENAME = "figures_area_state.json"
    _VERSION = 5
    _KINDS = {"journal", "presentation", "snapshot_animation", "animation"}

    def __init__(self, state_root: Path) -> None:
        self._path = state_root / self._FILENAME
        self._paths = ProjectPaths(state_root.parent)

    def save(self, tabs: list[dict[str, Any]], current_index: int | None) -> None:
        json_store.write_json(
            self._path,
            {"version": self._VERSION, "tabs": [self._composition_paths(tab, encode=True) for tab in tabs], "current_index": current_index},
        )

    def load(self) -> tuple[list[dict[str, Any]], int | None]:
        try:
            payload = json_store.read_json(self._path, default={})
        except (OSError, ValueError):
            return [], None
        if not isinstance(payload, dict) or payload.get("version") not in {1, 2, 3, 4, self._VERSION}:
            return [], None
        raw_tabs = payload.get("tabs")
        if not isinstance(raw_tabs, list):
            return [], None
        tabs = [tab for tab in (self._validated_tab(item) for item in raw_tabs) if tab is not None]
        current_index = payload.get("current_index")
        valid_index = current_index if isinstance(current_index, int) and not isinstance(current_index, bool) else None
        return [self._composition_paths(tab, encode=False) for tab in tabs], valid_index

    def _composition_paths(self, tab: dict[str, Any], *, encode: bool) -> dict[str, Any]:
        """Keep static figure preset references portable, like animation references."""
        tab = copy.deepcopy(tab)
        convert = self._paths.encode if encode else self._paths.decode
        composition = tab.get('composition')
        cells = composition.get('plot_cells', []) if isinstance(composition, dict) else []
        for cell in cells if isinstance(cells, list) else []:
            if not isinstance(cell, dict):
                continue
            if isinstance(cell.get('snapshot_path'), str) and cell['snapshot_path']:
                cell['snapshot_path'] = convert(cell['snapshot_path'])
            state = cell.get('render_state')
            paths = state.get('preset_paths') if isinstance(state, dict) else None
            if isinstance(paths, dict):
                for key in ('snapshot', 'discovery', 'transform', 'camera', 'appearance', 'filter'):
                    if isinstance(paths.get(key), str) and paths[key]:
                        paths[key] = convert(paths[key])
        return tab

    @classmethod
    def settings_payload(cls, settings: FigurePlotSettings) -> dict[str, Any]:
        return asdict(settings)

    @staticmethod
    def composition_payload(composition: FigureComposition) -> dict[str, Any]:
        return {
            "plot_cells": [
                {
                    "row": cell.row,
                    "column": cell.column,
                    "snapshot_path": cell.snapshot_path,
                    "snapshot_label": cell.snapshot_label,
                    "coordinates": [list(point) for point in cell.coordinates],
                    "metadata_ids": list(cell.metadata_ids),
                    "metadata": {
                        str(metadata_id): copy.deepcopy(values)
                        for metadata_id, values in cell.metadata.items()
                    },
                    "render_state": copy.deepcopy(cell.render_state),
                    "element_ids": list(cell.element_ids),
                }
                for cell in sorted(
                    composition.plot_cells.values(),
                    key=lambda item: (item.row, item.column),
                )
            ],
            "figure_elements": [
                asdict(element)
                for element in sorted(
                    composition.figure_elements.values(),
                    key=lambda item: item.order,
                )
            ],
            "selected_element_id": composition.selected_element_id,
            "next_element_number": composition.next_element_number,
        }

    @classmethod
    def composition_from_payload(
        cls,
        payload: object,
        *,
        settings: FigurePlotSettings,
    ) -> FigureComposition | None:
        if not isinstance(payload, dict):
            return None
        raw_cells = payload.get("plot_cells")
        raw_elements = payload.get("figure_elements")
        if not isinstance(raw_cells, list) or not isinstance(raw_elements, list):
            return None

        composition = FigureComposition(settings=settings)
        for raw_cell in raw_cells:
            cell = cls._plot_cell_from_payload(raw_cell)
            if cell is not None:
                composition.plot_cells[(cell.row, cell.column)] = cell
        if not composition.plot_cells:
            return None

        for raw_element in raw_elements:
            element = cls._figure_element_from_payload(raw_element)
            if element is not None:
                composition.figure_elements[element.element_id] = element
        known_elements = set(composition.figure_elements)
        for cell in composition.plot_cells.values():
            cell.element_ids = [
                element_id for element_id in cell.element_ids if element_id in known_elements
            ]

        selected = payload.get("selected_element_id")
        composition.selected_element_id = (
            selected if isinstance(selected, str) and selected in known_elements else None
        )
        next_number = payload.get("next_element_number")
        if isinstance(next_number, int) and not isinstance(next_number, bool):
            composition.next_element_number = max(next_number, 1)
        return composition

    @classmethod
    def settings_from_payload(cls, payload: object, *, figure_kind: str) -> FigurePlotSettings:
        defaults = default_plot_settings(figure_kind)
        if not isinstance(payload, dict):
            return defaults
        payload = dict(payload)
        if "export_dpi" not in payload and "preview_dpi" in payload:
            payload["export_dpi"] = payload["preview_dpi"]
        allowed = {field.name for field in fields(FigurePlotSettings)}
        default_values = asdict(defaults)
        values = {
            name: value
            for name, value in payload.items()
            if name in allowed and cls._matches_setting_type(value, default_values[name])
        }
        for name, choices in {
            "grid_mode": {"off", "lines", "map"},
            "main_title_location": {"top", "left", "right", "bottom"},
            "main_title_alignment": {"left", "center", "right"},
            "subtitle_location": {"top", "left", "right", "bottom"},
            "subtitle_alignment": {"left", "center", "right"},
            "legend_scope": {"global", "per_plot"},
            "legend_location": {
                "upper right",
                "upper left",
                "lower right",
                "lower left",
                "center right",
                "center left",
                "upper center",
                "lower center",
            },
            "subplot_caption_alignment": {"left", "center", "right", "justify"},
        }.items():
            if name in values and values[name] not in choices:
                values.pop(name)
        try:
            return FigurePlotSettings(**{**asdict(defaults), **values})
        except (TypeError, ValueError):
            return defaults

    @staticmethod
    def _matches_setting_type(value: object, default: object) -> bool:
        if isinstance(default, bool):
            return isinstance(value, bool)
        if isinstance(default, int):
            return isinstance(value, int) and not isinstance(value, bool)
        if isinstance(default, float):
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        return isinstance(value, type(default))

    @classmethod
    def _validated_tab(cls, value: object) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        kind = value.get("kind")
        title = value.get("title")
        if kind not in cls._KINDS or not isinstance(title, str) or not title.strip():
            return None
        normalized_kind = "animation" if kind == "snapshot_animation" else kind
        return {
            "kind": normalized_kind,
            "title": title.strip(),
            "settings": value.get("settings"),
            "composition": value.get("composition"),
        }

    @staticmethod
    def _plot_cell_from_payload(payload: object) -> PlotCell | None:
        if not isinstance(payload, dict):
            return None
        row = payload.get("row")
        column = payload.get("column")
        if not isinstance(row, int) or isinstance(row, bool):
            return None
        if not isinstance(column, int) or isinstance(column, bool):
            return None
        coordinates: list[tuple[float, float]] = []
        for point in payload.get("coordinates", []):
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            try:
                coordinates.append((float(point[0]), float(point[1])))
            except (TypeError, ValueError):
                continue
        metadata_ids = [
            int(value)
            for value in payload.get("metadata_ids", [])
            if isinstance(value, int) and not isinstance(value, bool)
        ]
        metadata: dict[int, dict[str, str]] = {}
        raw_metadata = payload.get("metadata")
        if isinstance(raw_metadata, dict):
            for raw_id, raw_values in raw_metadata.items():
                if not isinstance(raw_values, dict):
                    continue
                try:
                    metadata_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                metadata[metadata_id] = {
                    str(key): "" if value is None else str(value)
                    for key, value in raw_values.items()
                }
        render_state = payload.get("render_state")
        element_ids = payload.get("element_ids")
        return PlotCell(
            row=row,
            column=column,
            snapshot_path=(
                str(payload["snapshot_path"])
                if payload.get("snapshot_path") is not None
                else None
            ),
            snapshot_label=(
                str(payload["snapshot_label"])
                if payload.get("snapshot_label") is not None
                else None
            ),
            coordinates=coordinates,
            metadata_ids=metadata_ids,
            metadata=metadata,
            render_state=copy.deepcopy(render_state) if isinstance(render_state, dict) else {},
            element_ids=[
                str(value) for value in element_ids
                if isinstance(value, str)
            ] if isinstance(element_ids, list) else [],
        )

    @staticmethod
    def _figure_element_from_payload(payload: object) -> FigureElement | None:
        if not isinstance(payload, dict):
            return None
        element_id = payload.get("element_id")
        kind = payload.get("kind")
        direction = payload.get("direction")
        if not isinstance(element_id, str) or not element_id:
            return None
        if kind not in {"main_title", "plot_title", "legend", "caption"}:
            return None
        if direction not in {"top", "left", "right", "bottom"}:
            return None
        source_cell = payload.get("source_cell")
        source = None
        if isinstance(source_cell, (list, tuple)) and len(source_cell) == 2:
            if all(isinstance(value, int) and not isinstance(value, bool) for value in source_cell):
                source = (int(source_cell[0]), int(source_cell[1]))
        try:
            return FigureElement(
                element_id=element_id,
                kind=kind,
                direction=direction,
                text=str(payload.get("text") or ""),
                source_cell=source,
                visible=bool(payload.get("visible", True)),
                font_family=str(payload.get("font_family") or "DejaVu Sans"),
                font_size=float(payload.get("font_size", 10.0)),
                alignment=str(payload.get("alignment") or "center"),
                line_spacing=float(payload.get("line_spacing", 1.25)),
                order=int(payload.get("order", 0)),
                auto_text=bool(payload.get("auto_text", True)),
                rich_text=(
                    str(payload["rich_text"])
                    if payload.get("rich_text") is not None
                    else None
                ),
            )
        except (TypeError, ValueError):
            return None
