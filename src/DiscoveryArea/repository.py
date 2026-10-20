from __future__ import annotations

import csv
from contextlib import closing
import sqlite3
from pathlib import Path
import json
from typing import TYPE_CHECKING
import uuid
import os
import tempfile
from xml.etree import ElementTree
import datetime as dt
import re

import numpy as np

from DiscoveryArea.domain import (
    PinnedAnnotation,
    PolygonArea,
    DiscoveryDataset,
    DiscoveryOption,
    DiscoveryPinDisplaySettings,
)
from shared.persistence import json_store

if TYPE_CHECKING:
    from project.domain import Project


class DiscoveryRepository:
    """Persistence boundary for discovery coordinates and presets."""

    RANDOM_PRESET_BASENAME = "random_discovery"
    RANDOM_PRESET_FLAG = "_random_discovery"

    def list_discovery_options(self, project: "Project") -> list[DiscoveryOption]:
        presets_dir = project.paths.discovery_presets
        options: list[tuple[float, DiscoveryOption]] = []
        if not presets_dir.exists():
            return options
        for preset_file in sorted(presets_dir.glob("*.json")):
            try:
                payload = json.loads(preset_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            discovery_file_value = payload.get("discovery_file")
            if not discovery_file_value:
                continue
            discovery_file = Path(str(discovery_file_value))
            if not discovery_file.is_absolute():
                discovery_file = (project.root / discovery_file).resolve()
            parameters = payload.get("parameters")
            if not isinstance(parameters, dict):
                parameters = {}
            source = payload.get("source")
            if not isinstance(source, dict):
                source = {}
            provenance = payload.get("provenance")
            if not isinstance(provenance, dict):
                provenance = {}
            is_random = bool(parameters.get(self.RANDOM_PRESET_FLAG)) or preset_file.stem == self.RANDOM_PRESET_BASENAME
            label = str(payload.get("name") or preset_file.stem)
            option = DiscoveryOption(
                key=str(preset_file.resolve()),
                label=label,
                discovery_type=str(payload.get("method_id") or payload.get("type") or payload.get("algorithm") or "unknown"),
                discovery_file=discovery_file,
                parameters=parameters,
                source=source,
                provenance=provenance,
                embeddings_label=str(
                    source.get("label")
                    or payload.get("embeddings")
                    or payload.get("embeddings_label")
                    or ""
                )
                or None,
                is_random=is_random,
            )
            options.append((_preset_created_timestamp(preset_file, payload), option))
        options.sort(key=lambda entry: (entry[0], entry[1].label.casefold()), reverse=True)
        return [option for _created_at, option in options]

    def load_coordinates(self, discovery_file: Path, metadata_ids: np.ndarray) -> DiscoveryDataset:
        if not discovery_file.exists():
            return self.empty()
        try:
            with closing(sqlite3.connect(discovery_file)) as connection, connection:
                try:
                    cursor = connection.execute(
                        "SELECT metadata_id, x, y FROM coordinates ORDER BY metadata_id"
                    )
                    rows = cursor.fetchall()
                    if not rows:
                        return self.empty()
                    row_metadata = np.asarray([row[0] for row in rows], dtype=np.int64)
                    coordinates = np.asarray([[row[1], row[2]] for row in rows], dtype=np.float32)
                    return DiscoveryDataset(coordinates=coordinates, metadata_ids=row_metadata)
                except sqlite3.Error:
                    cursor = connection.execute("SELECT x, y FROM coordinates ORDER BY rowid")
                    rows = cursor.fetchall()
        except sqlite3.Error:
            return self.empty()

        if not rows:
            return self.empty()

        coordinates = np.asarray(rows, dtype=np.float32)
        coordinates = coordinates[:, :2] if coordinates.ndim == 2 else np.empty((0, 2), dtype=np.float32)
        if metadata_ids.size != len(coordinates):
            row_metadata = np.arange(len(coordinates), dtype=np.int64)
        else:
            row_metadata = metadata_ids[: len(coordinates)].copy()
        return DiscoveryDataset(coordinates=coordinates, metadata_ids=row_metadata)

    def save_discovery(
        self,
        project: "Project",
        *,
        label: str,
        method_id: str,
        method_name: str,
        coordinates: np.ndarray,
        metadata_ids: np.ndarray,
        parameters: dict[str, object],
        normalization: dict[str, object],
        source: dict[str, object],
        provenance: dict[str, object] | None = None,
        description: str = "",
        slug_override: str | None = None,
    ) -> DiscoveryOption:
        safe_label = str(label or "").strip() or "Discovery"
        is_internal_random = bool(parameters.get(self.RANDOM_PRESET_FLAG))
        if not is_internal_random and self._discovery_label_exists(project, safe_label):
            raise ValueError(f"Discovery name already exists: {safe_label}")
        slug = str(slug_override or "").strip() or _unique_slug(
            project.paths.discovery_presets,
            _slugify(safe_label),
            discovery_dir=project.paths.discovery,
        )
        discovery_file = project.paths.discovery / f"{slug}.sqlite"
        preset_file = project.paths.discovery_presets / f"{slug}.json"
        project.paths.discovery.mkdir(parents=True, exist_ok=True)
        project.paths.discovery_presets.mkdir(parents=True, exist_ok=True)
        coords = np.asarray(coordinates, dtype=np.float32)
        ids = np.asarray(metadata_ids, dtype=np.int64)
        if coords.ndim != 2 or coords.shape[1] < 2:
            raise ValueError("Discovery coordinates must be a two-dimensional array.")
        if coords.shape[0] != ids.shape[0]:
            raise ValueError("Discovery coordinates and metadata IDs must have the same length.")
        try:
            self._write_coordinates_atomic(discovery_file, coords[:, :2], ids)
            discovery_rel_path = self._relative_to_project(project, discovery_file)
            preset_payload = {
                "name": safe_label,
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds"),
                "type": method_name,
                "method_id": str(method_id),
                "discovery_file": discovery_rel_path,
                "parameters": parameters,
                "normalization": normalization,
                "source": source,
                **({"provenance": provenance} if provenance else {}),
                "item_count": int(ids.shape[0]),
                "metadata_checksum": _metadata_checksum(ids),
                "description": description,
            }
            self._write_json_atomic(preset_file, preset_payload)
        except Exception:
            discovery_file.unlink(missing_ok=True)
            preset_file.unlink(missing_ok=True)
            raise
        return DiscoveryOption(
            key=str(preset_file.resolve()),
            label=safe_label,
            discovery_type=str(method_id),
            discovery_file=discovery_file.resolve(),
            parameters=dict(parameters),
            source=dict(source),
            provenance=dict(provenance or {}),
            embeddings_label=str(source.get("label") or "") or None,
            is_random=False,
        )

    def delete_discovery(self, project: "Project", option: DiscoveryOption) -> bool:
        if option.is_random:
            return False
        preset_path = Path(option.key)
        discovery_path = option.discovery_file
        removed = False
        if self._is_inside_project(project, preset_path) and preset_path.exists():
            preset_path.unlink()
            removed = True
        if discovery_path is not None and self._is_inside_project(project, discovery_path) and discovery_path.exists():
            discovery_path.unlink()
            removed = True
        return removed

    def rename_discovery(self, project: "Project", option: DiscoveryOption, label: str) -> bool:
        if option.is_random:
            return False
        safe_label = str(label or "").strip()
        if not safe_label:
            raise ValueError("Discovery name is required.")
        preset_path = Path(option.key)
        if not self._is_inside_project(project, preset_path) or not preset_path.exists():
            return False
        current_label = str(option.label or "").strip()
        if safe_label.casefold() != current_label.casefold() and self._discovery_label_exists(project, safe_label):
            raise ValueError(f"Discovery name already exists: {safe_label}")
        try:
            payload = json.loads(preset_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Discovery preset is not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Discovery preset must be a JSON object.")
        if not str(payload.get("created_at") or "").strip():
            created = dt.datetime.fromtimestamp(
                _preset_created_timestamp(preset_path, payload),
                tz=dt.timezone.utc,
            )
            payload["created_at"] = created.isoformat(timespec="microseconds")
        payload["name"] = safe_label
        self._write_json_atomic(preset_path, payload)
        return True

    def export_coordinates(
        self,
        *,
        destination: Path,
        export_format: str,
        rows: list[dict[str, object]],
    ) -> Path:
        normalized_format = str(export_format or "").strip().lower()
        if normalized_format not in {"csv", "tsv", "json", "xml"}:
            raise ValueError(f"Unsupported discovery export format: {export_format}")
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{target.stem}.", suffix=".tmp", dir=str(target.parent))
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            if normalized_format in {"csv", "tsv"}:
                delimiter = "," if normalized_format == "csv" else "\t"
                self._write_delimited_export(
                    tmp_path,
                    rows=rows,
                    delimiter=delimiter,
                )
            elif normalized_format == "json":
                self._write_json_export(tmp_path, rows=rows)
            else:
                self._write_xml_export(tmp_path, rows=rows)
            tmp_path.replace(target)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
        return target

    @staticmethod
    def _write_delimited_export(
        path: Path,
        *,
        rows: list[dict[str, object]],
        delimiter: str,
    ) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["id", "x", "y"],
                delimiter=delimiter,
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        "id": row["id"],
                        "x": row["x"],
                        "y": row["y"],
                    }
                )

    @staticmethod
    def _write_json_export(path: Path, *, rows: list[dict[str, object]]) -> None:
        path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    @staticmethod
    def _write_xml_export(path: Path, *, rows: list[dict[str, object]]) -> None:
        root = ElementTree.Element("discovery_coordinates")
        for row in rows:
            ElementTree.SubElement(
                root,
                "point",
                {
                    "id": str(row["id"]),
                    "x": str(row["x"]),
                    "y": str(row["y"]),
                },
            )
        tree = ElementTree.ElementTree(root)
        tree.write(path, encoding="utf-8", xml_declaration=True)

    def _write_coordinates_atomic(self, destination: Path, coordinates: np.ndarray, metadata_ids: np.ndarray) -> None:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{destination.stem}.", suffix=".tmp", dir=str(destination.parent))
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            with closing(sqlite3.connect(tmp_path)) as connection, connection:
                connection.execute(
                    "CREATE TABLE coordinates (id INTEGER PRIMARY KEY, metadata_id INTEGER NOT NULL, x REAL NOT NULL, y REAL NOT NULL)"
                )
                connection.executemany(
                    "INSERT INTO coordinates (metadata_id, x, y) VALUES (?, ?, ?)",
                    (
                        (int(metadata_id), float(point[0]), float(point[1]))
                        for metadata_id, point in zip(metadata_ids, coordinates, strict=True)
                    ),
                )
                connection.commit()
            tmp_path.replace(destination)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    @staticmethod
    def _write_json_atomic(destination: Path, payload: dict[str, object]) -> None:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{destination.stem}.", suffix=".tmp", dir=str(destination.parent))
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp_path.replace(destination)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    @staticmethod
    def _relative_to_project(project: "Project", path: Path) -> str:
        try:
            return path.resolve().relative_to(project.root.resolve()).as_posix()
        except ValueError:
            return str(path.resolve())

    @staticmethod
    def _is_inside_project(project: "Project", path: Path) -> bool:
        try:
            path.resolve().relative_to(project.root.resolve())
        except ValueError:
            return False
        return True

    @staticmethod
    def empty() -> DiscoveryDataset:
        return DiscoveryDataset(
            coordinates=np.empty((0, 2), dtype=np.float32),
            metadata_ids=np.empty((0,), dtype=np.int64),
        )

    def _discovery_label_exists(self, project: "Project", label: str) -> bool:
        normalized = label.strip().casefold()
        if not normalized:
            return False
        return any(
            option.label.strip().casefold() == normalized
            for option in self.list_discovery_options(project)
            if not option.is_random
        )


def _slugify(value: str) -> str:
    text = "".join(character.lower() if character.isalnum() else "_" for character in value)
    return "_".join(part for part in text.split("_") if part) or "discovery"


def _preset_created_timestamp(path: Path, payload: dict[str, object]) -> float:
    created_at = str(payload.get("created_at") or "").strip()
    if created_at:
        try:
            parsed = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.timestamp()
        except ValueError:
            pass
    try:
        stat = path.stat()
    except OSError:
        return 0.0
    return float(getattr(stat, "st_birthtime", stat.st_mtime))


def _unique_slug(presets_dir: Path, base_slug: str, *, discovery_dir: Path | None = None) -> str:
    slug = base_slug
    index = 2
    while (presets_dir / f"{slug}.json").exists() or (
        discovery_dir is not None and (discovery_dir / f"{slug}.sqlite").exists()
    ):
        slug = f"{base_slug}_{index}"
        index += 1
    return slug


def _metadata_checksum(metadata_ids: np.ndarray) -> str:
    import hashlib

    contiguous = np.ascontiguousarray(metadata_ids, dtype=np.int64)
    return hashlib.sha1(contiguous.tobytes()).hexdigest()


class PolygonAreaRepository:
    """Persistence boundary for discovery polygon areas."""

    def __init__(self, state_db_path: Path) -> None:
        self._state_db_path = Path(state_db_path)
        self._state_db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def list_areas(self, discovery_scope: str) -> list[PolygonArea]:
        scope = str(discovery_scope or "").strip()
        if not scope:
            return []
        with closing(sqlite3.connect(self._state_db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT id, label, color_hex, polygon_json, arrangement_ref, area_visible, points_visible
                FROM polygon_areas
                WHERE discovery_scope = ?
                ORDER BY lower(label), id
                """,
                (scope,),
            ).fetchall()
            output: list[PolygonArea] = []
            for row in rows:
                area_id = str(row["id"] or "")
                polygon = self._parse_polygon_json(str(row["polygon_json"] or "[]"))
                metadata_rows = connection.execute(
                    "SELECT metadata_id FROM polygon_area_members WHERE area_id = ? ORDER BY metadata_id",
                    (area_id,),
                ).fetchall()
                metadata_ids = tuple(int(item[0]) for item in metadata_rows)
                output.append(
                    PolygonArea(
                        area_id=area_id,
                        label=str(row["label"] or ""),
                        color_hex=str(row["color_hex"] or "#3b82f6"),
                        polygon=polygon,
                        metadata_ids=metadata_ids,
                        discovery_scope=scope,
                        arrangement_ref=str(row["arrangement_ref"] or "original"),
                        area_visible=bool(int(row["area_visible"])),
                        points_visible=bool(int(row["points_visible"])),
                    )
                )
            return output

    def upsert_area(
        self,
        *,
        discovery_scope: str,
        label: str,
        polygon_points: list[list[float]],
        metadata_ids: list[int],
        color_hex: str = "#3b82f6",
        area_visible: bool = True,
        points_visible: bool = True,
        arrangement_ref: str = "original",
    ) -> PolygonArea:
        scope = str(discovery_scope or "").strip()
        area_label = str(label or "").strip()
        if not scope:
            raise ValueError("Discovery scope is required.")
        if not area_label:
            raise ValueError("Area label is required.")
        normalized_polygon = self._normalize_polygon_points(polygon_points)
        if len(normalized_polygon) < 3:
            raise ValueError("Area polygon must contain at least 3 points.")
        members = sorted({int(value) for value in metadata_ids})
        with closing(sqlite3.connect(self._state_db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT id FROM polygon_areas WHERE discovery_scope = ? AND label = ?",
                (scope, area_label),
            ).fetchone()
            area_id = str(row["id"]) if row is not None else uuid.uuid4().hex
            payload = json.dumps(normalized_polygon)
            if row is None:
                connection.execute(
                    """
                    INSERT INTO polygon_areas (
                        id, discovery_scope, label, color_hex, polygon_json, arrangement_ref, area_visible, points_visible
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        area_id,
                        scope,
                        area_label,
                        str(color_hex or "#3b82f6"),
                        payload,
                        str(arrangement_ref or "original"),
                        1 if area_visible else 0,
                        1 if points_visible else 0,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE polygon_areas
                    SET color_hex = ?, polygon_json = ?, arrangement_ref = ?, area_visible = ?, points_visible = ?
                    WHERE id = ?
                    """,
                    (
                        str(color_hex or "#3b82f6"),
                        payload,
                        str(arrangement_ref or "original"),
                        1 if area_visible else 0,
                        1 if points_visible else 0,
                        area_id,
                    ),
                )
            connection.execute("DELETE FROM polygon_area_members WHERE area_id = ?", (area_id,))
            if members:
                connection.executemany(
                    "INSERT INTO polygon_area_members (area_id, metadata_id) VALUES (?, ?)",
                    [(area_id, int(metadata_id)) for metadata_id in members],
                )
            connection.commit()
        return PolygonArea(
            area_id=area_id,
            label=area_label,
            color_hex=str(color_hex or "#3b82f6"),
            polygon=tuple((float(item[0]), float(item[1])) for item in normalized_polygon),
            metadata_ids=tuple(members),
            discovery_scope=scope,
            arrangement_ref=str(arrangement_ref or "original"),
            area_visible=bool(area_visible),
            points_visible=bool(points_visible),
        )

    def update_polygon(self, *, area_id: str, polygon_points: list[list[float]], metadata_ids: list[int]) -> None:
        area_key = str(area_id or "").strip()
        if not area_key:
            return
        normalized_polygon = self._normalize_polygon_points(polygon_points)
        if len(normalized_polygon) < 3:
            return
        members = sorted({int(value) for value in metadata_ids})
        with closing(sqlite3.connect(self._state_db_path)) as connection, connection:
            connection.execute(
                "UPDATE polygon_areas SET polygon_json = ? WHERE id = ?",
                (json.dumps(normalized_polygon), area_key),
            )
            connection.execute("DELETE FROM polygon_area_members WHERE area_id = ?", (area_key,))
            if members:
                connection.executemany(
                    "INSERT INTO polygon_area_members (area_id, metadata_id) VALUES (?, ?)",
                    [(area_key, int(metadata_id)) for metadata_id in members],
                )
            connection.commit()

    def set_area_visibility(self, *, area_id: str, area_visible: bool | None = None, points_visible: bool | None = None) -> None:
        area_key = str(area_id or "").strip()
        if not area_key:
            return
        with closing(sqlite3.connect(self._state_db_path)) as connection, connection:
            if area_visible is not None:
                connection.execute(
                    "UPDATE polygon_areas SET area_visible = ? WHERE id = ?",
                    (1 if area_visible else 0, area_key),
                )
            if points_visible is not None:
                connection.execute(
                    "UPDATE polygon_areas SET points_visible = ? WHERE id = ?",
                    (1 if points_visible else 0, area_key),
                )
            connection.commit()

    def set_areas_visibility(
        self,
        *,
        area_ids: list[str],
        area_visible: bool | None = None,
        points_visible: bool | None = None,
    ) -> None:
        area_keys = sorted(
            {
                str(area_id or "").strip()
                for area_id in area_ids
                if str(area_id or "").strip()
            }
        )
        if not area_keys:
            return
        with closing(sqlite3.connect(self._state_db_path)) as connection, connection:
            if area_visible is not None:
                connection.executemany(
                    "UPDATE polygon_areas SET area_visible = ? WHERE id = ?",
                    [(1 if area_visible else 0, area_key) for area_key in area_keys],
                )
            if points_visible is not None:
                connection.executemany(
                    "UPDATE polygon_areas SET points_visible = ? WHERE id = ?",
                    [(1 if points_visible else 0, area_key) for area_key in area_keys],
                )
            connection.commit()

    @staticmethod
    def export_member_rows(
        *,
        destination: Path,
        export_format: str,
        rows: list[dict[str, object]],
        fieldnames: list[str],
    ) -> Path:
        normalized_format = str(export_format or "").strip().lower()
        if normalized_format not in {"csv", "tsv", "json"}:
            raise ValueError(f"Unsupported area export format: {export_format}")
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.stem}.",
            suffix=".tmp",
            dir=str(target.parent),
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            if normalized_format in {"csv", "tsv"}:
                delimiter = "," if normalized_format == "csv" else "\t"
                with tmp_path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter=delimiter)
                    writer.writeheader()
                    writer.writerows(rows)
            else:
                tmp_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
            tmp_path.replace(target)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
        return target

    def remove_area(self, *, area_id: str) -> None:
        area_key = str(area_id or "").strip()
        if not area_key:
            return
        with closing(sqlite3.connect(self._state_db_path)) as connection, connection:
            connection.execute("DELETE FROM polygon_areas WHERE id = ?", (area_key,))
            connection.commit()

    def area_label_exists(self, *, discovery_scope: str, label: str, exclude_area_id: str | None = None) -> bool:
        scope = str(discovery_scope or "").strip()
        area_label = str(label or "").strip()
        if not scope or not area_label:
            return False
        with closing(sqlite3.connect(self._state_db_path)) as connection, connection:
            row = connection.execute(
                "SELECT id FROM polygon_areas WHERE discovery_scope = ? AND label = ?",
                (scope, area_label),
            ).fetchone()
        if row is None:
            return False
        if exclude_area_id is None:
            return True
        return str(row[0]) != str(exclude_area_id)

    def rename_area(self, *, area_id: str, label: str) -> None:
        area_key = str(area_id or "").strip()
        area_label = str(label or "").strip()
        if not area_key or not area_label:
            return
        with closing(sqlite3.connect(self._state_db_path)) as connection, connection:
            connection.execute("UPDATE polygon_areas SET label = ? WHERE id = ?", (area_label, area_key))
            connection.commit()

    def set_area_color(self, *, area_id: str, color_hex: str) -> None:
        area_key = str(area_id or "").strip()
        if not area_key:
            return
        with closing(sqlite3.connect(self._state_db_path)) as connection, connection:
            connection.execute("UPDATE polygon_areas SET color_hex = ? WHERE id = ?", (str(color_hex or "#3b82f6"), area_key))
            connection.commit()

    def _ensure_schema(self) -> None:
        with closing(sqlite3.connect(self._state_db_path)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS polygon_areas (
                    id TEXT PRIMARY KEY,
                    discovery_scope TEXT NOT NULL,
                    label TEXT NOT NULL,
                    color_hex TEXT NOT NULL DEFAULT '#3b82f6',
                    polygon_json TEXT NOT NULL,
                    arrangement_ref TEXT NOT NULL DEFAULT 'original',
                    area_visible INTEGER NOT NULL DEFAULT 1,
                    points_visible INTEGER NOT NULL DEFAULT 1
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_polygon_areas_scope_label
                ON polygon_areas(discovery_scope, label);
                CREATE INDEX IF NOT EXISTS idx_polygon_areas_scope
                ON polygon_areas(discovery_scope);
                CREATE TABLE IF NOT EXISTS polygon_area_members (
                    area_id TEXT NOT NULL REFERENCES polygon_areas(id) ON DELETE CASCADE,
                    metadata_id INTEGER NOT NULL,
                    PRIMARY KEY (area_id, metadata_id)
                );
                """
            )
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(polygon_areas)").fetchall()
            }
            if "arrangement_ref" not in columns:
                connection.execute(
                    "ALTER TABLE polygon_areas ADD COLUMN arrangement_ref TEXT NOT NULL DEFAULT 'original'"
                )
            connection.commit()

    @staticmethod
    def _parse_polygon_json(raw: str) -> tuple[tuple[float, float], ...]:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return ()
        if not isinstance(parsed, list):
            return ()
        points: list[tuple[float, float]] = []
        for item in parsed:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                try:
                    points.append((float(item[0]), float(item[1])))
                except (TypeError, ValueError):
                    continue
        return tuple(points)

    @staticmethod
    def _normalize_polygon_points(points: list[list[float]]) -> list[list[float]]:
        normalized: list[list[float]] = []
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            try:
                normalized.append([float(point[0]), float(point[1])])
            except (TypeError, ValueError):
                continue
        return normalized


class DiscoveryPinFileRepository:
    """Store project pin membership separately from per-discovery positions."""

    SCHEMA_VERSION = 3
    POSITION_SCHEMA_VERSION = 1
    PINS_DIRECTORY = "pins"
    POSITIONS_DIRECTORY = "positions"

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._pins_root = self._root / self.PINS_DIRECTORY
        self._positions_root = self._root / self.POSITIONS_DIRECTORY

    def list_pin_payloads(self, discovery_scope: str) -> list[dict[str, object]]:
        scope = str(discovery_scope or "").strip()
        if not scope:
            return []
        positions = self._load_scope_positions(scope)
        output: list[dict[str, object]] = []
        for path in sorted(self._pins_root.glob("*.json")):
            try:
                payload = json_store.read_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            annotation_id = str(payload.get("annotation_id") or "").strip()
            scope_mode = self._scope_mode(payload.get("scope_mode"))
            projection_scope = str(payload.get("projection_scope") or "").strip()
            if scope_mode == "projection" and projection_scope != scope:
                continue
            merged = dict(payload)
            merged["discovery_scope"] = scope
            position = positions.get(annotation_id)
            if position is None:
                merged["point"] = [float("nan"), float("nan")]
                merged["screen_offset"] = [48.0, -36.0]
                merged.pop("box_anchor", None)
            else:
                merged["point"] = position.get("point", [float("nan"), float("nan")])
                merged["screen_offset"] = position.get("screen_offset", [48.0, -36.0])
                if "box_anchor" in position:
                    merged["box_anchor"] = position["box_anchor"]
                else:
                    merged.pop("box_anchor", None)
                merged["updated_at"] = max(
                    str(merged.get("updated_at") or ""),
                    str(position.get("updated_at") or ""),
                )
            normalized = self._normalize_pin_payload(merged, discovery_scope=scope)
            if normalized is not None:
                output.append(normalized)
        output.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("annotation_id") or "")))
        return output

    def list_pins(self, discovery_scope: str) -> list[PinnedAnnotation]:
        return [
            self._payload_to_pin(payload)
            for payload in self.list_pin_payloads(discovery_scope)
        ]

    def upsert_pin_payload(self, discovery_scope: str, payload: dict[str, object]) -> None:
        scope = str(discovery_scope or "").strip()
        if not scope:
            raise ValueError("Discovery scope is required.")
        normalized = self._normalize_pin_payload(payload, discovery_scope=scope)
        if normalized is None:
            raise ValueError("Pin payload is not valid.")
        normalized["updated_at"] = dt.datetime.utcnow().isoformat(timespec="seconds")
        if self._pin_exists_for_metadata(
            int(normalized["metadata_id"]),
            except_annotation_id=str(normalized["annotation_id"]),
        ):
            raise ValueError("A pin already exists for this metadata item.")
        if normalized["scope_mode"] == "project":
            self._remove_projection_pins_for_metadata(
                int(normalized["metadata_id"]),
                except_annotation_id=str(normalized["annotation_id"]),
            )
        elif self._project_pin_exists_for_metadata(
            int(normalized["metadata_id"]),
            except_annotation_id=str(normalized["annotation_id"]),
        ):
            raise ValueError("A project-wide pin already exists for this metadata item.")
        self._write_split_payload(scope, normalized)

    def upsert_pin(self, discovery_scope: str, pin: PinnedAnnotation) -> None:
        self.upsert_pin_payload(
            discovery_scope,
            {
                "annotation_id": pin.annotation_id,
                "discovery_scope": discovery_scope,
                "metadata_id": pin.metadata_id,
                "point": list(pin.point),
                "screen_offset": list(pin.screen_offset),
                "visible": bool(pin.visible),
                "created_at": pin.created_at,
                "lines": list(pin.lines),
                "display": DiscoveryPinDisplaySettings().to_payload(),
            },
        )

    def replace_scope_pins(self, discovery_scope: str, payloads: list[dict[str, object]]) -> None:
        scope = str(discovery_scope or "").strip()
        if not scope:
            return
        seen: set[str] = set()
        for payload in payloads:
            normalized = self._normalize_pin_payload(payload, discovery_scope=scope)
            if normalized is None:
                continue
            annotation_id = str(normalized["annotation_id"])
            seen.add(self._safe_file_stem(annotation_id))
            self.upsert_pin_payload(scope, normalized)
        for path in self._pins_root.glob("*.json"):
            if path.stem in seen:
                continue
            definition = self._read_definition_path(path)
            if definition is None:
                continue
            scope_mode = self._scope_mode(definition.get("scope_mode"))
            projection_scope = str(definition.get("projection_scope") or "").strip()
            if scope_mode == "projection" and projection_scope != scope:
                continue
            self._remove_pin_files(path.stem)

    def set_pin_visibility(self, discovery_scope: str, annotation_id: str, visible: bool) -> None:
        payload = self._load_definition(annotation_id)
        if payload is None:
            return
        if (
            self._scope_mode(payload.get("scope_mode")) == "projection"
            and str(payload.get("projection_scope") or "").strip()
            != str(discovery_scope or "").strip()
        ):
            return
        payload["visible"] = bool(visible)
        payload["updated_at"] = dt.datetime.utcnow().isoformat(timespec="seconds")
        self._pins_root.mkdir(parents=True, exist_ok=True)
        json_store.write_json(self._definition_path(annotation_id), payload)

    def remove_pin(self, discovery_scope: str, annotation_id: str) -> None:
        annotation_key = str(annotation_id or "").strip()
        if not annotation_key:
            return
        definition = self._load_definition(annotation_key)
        if definition is None:
            return
        if (
            self._scope_mode(definition.get("scope_mode")) == "projection"
            and str(definition.get("projection_scope") or "").strip()
            != str(discovery_scope or "").strip()
        ):
            return
        self._remove_pin_files(self._safe_file_stem(annotation_key))

    def project_pin_exists_for_metadata(self, metadata_id: int) -> bool:
        return self._project_pin_exists_for_metadata(int(metadata_id))

    def pin_exists_for_metadata(self, metadata_id: int) -> bool:
        return self._pin_exists_for_metadata(int(metadata_id))

    def _pin_exists_for_metadata(
        self,
        metadata_id: int,
        *,
        except_annotation_id: str = "",
    ) -> bool:
        target = int(metadata_id)
        excluded = str(except_annotation_id or "").strip()
        for path in self._pins_root.glob("*.json"):
            payload = self._read_definition_path(path)
            if payload is None:
                continue
            if str(payload.get("annotation_id") or "").strip() == excluded:
                continue
            try:
                if int(payload.get("metadata_id")) == target:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def has_scope(self, discovery_scope: str) -> bool:
        scope_dir = self._position_scope_dir(discovery_scope)
        return scope_dir.exists() and any(scope_dir.glob("*.json"))

    def most_recent_discovery_scope(self) -> str | None:
        latest: tuple[str, str] | None = None
        for path in self._positions_root.glob("*/*.json"):
            try:
                payload = json_store.read_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            scope = str(payload.get("discovery_scope") or "").strip()
            if not scope:
                continue
            updated = str(payload.get("updated_at") or payload.get("created_at") or "")
            candidate = (updated, scope)
            if latest is None or candidate > latest:
                latest = candidate
        return None if latest is None else latest[1]

    def _load_definition(self, annotation_id: str) -> dict[str, object] | None:
        path = self._definition_path(annotation_id)
        return self._read_definition_path(path)

    @staticmethod
    def _read_definition_path(path: Path) -> dict[str, object] | None:
        if not path.exists():
            return None
        try:
            payload = json_store.read_json(path)
        except (OSError, json.JSONDecodeError):
            return None
        return dict(payload) if isinstance(payload, dict) else None

    def _load_scope_positions(self, discovery_scope: str) -> dict[str, dict[str, object]]:
        output: dict[str, dict[str, object]] = {}
        for path in sorted(self._position_scope_dir(discovery_scope).glob("*.json")):
            try:
                payload = json_store.read_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            annotation_id = str(payload.get("annotation_id") or "").strip()
            if annotation_id:
                output[annotation_id] = dict(payload)
        return output

    def _definition_path(self, annotation_id: str) -> Path:
        return self._pins_root / f"{self._safe_file_stem(annotation_id)}.json"

    def _position_scope_dir(self, discovery_scope: str) -> Path:
        return self._positions_root / self._scope_slug(discovery_scope)

    def _position_path(self, discovery_scope: str, annotation_id: str) -> Path:
        return self._position_scope_dir(discovery_scope) / f"{self._safe_file_stem(annotation_id)}.json"

    def _write_split_payload(self, discovery_scope: str, payload: dict[str, object]) -> None:
        annotation_id = str(payload["annotation_id"])
        definition = {
            "schema_version": self.SCHEMA_VERSION,
            "annotation_id": annotation_id,
            "metadata_id": int(payload["metadata_id"]),
            "scope_mode": self._scope_mode(payload.get("scope_mode")),
            "visible": bool(payload.get("visible", True)),
            "created_at": str(payload.get("created_at") or ""),
            "updated_at": str(payload.get("updated_at") or ""),
            "display": dict(payload.get("display") or {}),
            "lines": list(payload.get("lines") or []),
        }
        if definition["scope_mode"] == "projection":
            definition["projection_scope"] = str(
                payload.get("projection_scope") or discovery_scope
            ).strip()
        position = {
            "schema_version": self.POSITION_SCHEMA_VERSION,
            "annotation_id": annotation_id,
            "discovery_scope": discovery_scope,
            "point": list(payload.get("point") or [float("nan"), float("nan")]),
            "screen_offset": list(payload.get("screen_offset") or [48.0, -36.0]),
            "updated_at": str(payload.get("updated_at") or ""),
        }
        box_anchor = payload.get("box_anchor")
        if isinstance(box_anchor, (list, tuple)) and len(box_anchor) >= 2:
            position["box_anchor"] = [float(box_anchor[0]), float(box_anchor[1])]
        self._pins_root.mkdir(parents=True, exist_ok=True)
        position_path = self._position_path(discovery_scope, annotation_id)
        position_path.parent.mkdir(parents=True, exist_ok=True)
        json_store.write_json(self._definition_path(annotation_id), definition)
        json_store.write_json(position_path, position)

    def _remove_pin_files(self, safe_annotation_id: str) -> None:
        paths = [self._pins_root / f"{safe_annotation_id}.json"]
        paths.extend(self._positions_root.glob(f"*/{safe_annotation_id}.json"))
        for path in paths:
            try:
                path.unlink()
            except FileNotFoundError:
                continue

    @staticmethod
    def _scope_slug(discovery_scope: str) -> str:
        import hashlib

        raw = str(discovery_scope or "").strip() or "default"
        label = re.sub(r"[^a-zA-Z0-9._-]+", "_", Path(raw).stem or raw).strip("._-")[:48]
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
        return f"{label or 'scope'}_{digest}"

    @staticmethod
    def _safe_file_stem(value: str) -> str:
        stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(value or "").strip()).strip("._-")
        return stem or uuid.uuid4().hex

    @classmethod
    def _normalize_pin_payload(cls, payload: object, *, discovery_scope: str) -> dict[str, object] | None:
        if not isinstance(payload, dict):
            return None
        annotation_id = str(payload.get("annotation_id") or "").strip()
        if not annotation_id:
            return None
        try:
            metadata_id = int(payload.get("metadata_id"))
        except (TypeError, ValueError):
            return None
        point = cls._point_pair(payload.get("point"), fallback=(float("nan"), float("nan")))
        offset = cls._point_pair(payload.get("screen_offset"), fallback=(48.0, -36.0))
        box_anchor_value = payload.get("box_anchor")
        box_anchor = (
            cls._point_pair(box_anchor_value, fallback=(float("nan"), float("nan")))
            if isinstance(box_anchor_value, (list, tuple)) and len(box_anchor_value) >= 2
            else None
        )
        display = payload.get("display")
        if not isinstance(display, dict):
            display = {}
        settings = DiscoveryPinDisplaySettings.from_payload(display).to_payload()
        lines = payload.get("lines")
        if not isinstance(lines, list):
            lines = []
        created_at = str(payload.get("created_at") or dt.datetime.utcnow().isoformat(timespec="seconds"))
        updated_at = str(payload.get("updated_at") or created_at)
        normalized: dict[str, object] = {
            "schema_version": cls.SCHEMA_VERSION,
            "annotation_id": annotation_id,
            "discovery_scope": str(payload.get("discovery_scope") or discovery_scope),
            "metadata_id": metadata_id,
            "scope_mode": cls._scope_mode(payload.get("scope_mode")),
            "point": [float(point[0]), float(point[1])],
            "screen_offset": [float(offset[0]), float(offset[1])],
            "visible": bool(payload.get("visible", True)),
            "created_at": created_at,
            "updated_at": updated_at,
            "display": settings,
            "lines": [str(line) for line in lines],
        }
        if normalized["scope_mode"] == "projection":
            normalized["projection_scope"] = str(
                payload.get("projection_scope") or discovery_scope
            ).strip()
        if box_anchor is not None and all(np.isfinite(value) for value in box_anchor):
            normalized["box_anchor"] = [float(box_anchor[0]), float(box_anchor[1])]
        return normalized

    @staticmethod
    def _scope_mode(value: object) -> str:
        return "projection" if str(value or "").strip().lower() == "projection" else "project"

    def _project_pin_exists_for_metadata(
        self,
        metadata_id: int,
        *,
        except_annotation_id: str = "",
    ) -> bool:
        target = int(metadata_id)
        excluded = str(except_annotation_id or "").strip()
        for path in self._pins_root.glob("*.json"):
            payload = self._read_definition_path(path)
            if payload is None or self._scope_mode(payload.get("scope_mode")) != "project":
                continue
            if str(payload.get("annotation_id") or "").strip() == excluded:
                continue
            try:
                if int(payload.get("metadata_id")) == target:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _remove_projection_pins_for_metadata(
        self,
        metadata_id: int,
        *,
        except_annotation_id: str = "",
    ) -> None:
        target = int(metadata_id)
        excluded = str(except_annotation_id or "").strip()
        for path in list(self._pins_root.glob("*.json")):
            payload = self._read_definition_path(path)
            if payload is None or self._scope_mode(payload.get("scope_mode")) != "projection":
                continue
            if str(payload.get("annotation_id") or "").strip() == excluded:
                continue
            try:
                matches = int(payload.get("metadata_id")) == target
            except (TypeError, ValueError):
                matches = False
            if matches:
                self._remove_pin_files(path.stem)

    @staticmethod
    def _point_pair(value: object, *, fallback: tuple[float, float]) -> tuple[float, float]:
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            try:
                return float(value[0]), float(value[1])
            except (TypeError, ValueError):
                return fallback
        return fallback

    @staticmethod
    def _payload_to_pin(payload: dict[str, object]) -> PinnedAnnotation:
        point = payload.get("point")
        offset = payload.get("screen_offset")
        lines = payload.get("lines")
        point_pair = DiscoveryPinFileRepository._point_pair(point, fallback=(float("nan"), float("nan")))
        offset_pair = DiscoveryPinFileRepository._point_pair(offset, fallback=(48.0, -36.0))
        return PinnedAnnotation(
            annotation_id=str(payload.get("annotation_id") or ""),
            metadata_id=int(payload.get("metadata_id") or 0),
            point=point_pair,
            screen_offset=offset_pair,
            visible=bool(payload.get("visible", True)),
            created_at=str(payload.get("created_at") or ""),
            lines=tuple(str(line) for line in lines) if isinstance(lines, list) else (),
        )
