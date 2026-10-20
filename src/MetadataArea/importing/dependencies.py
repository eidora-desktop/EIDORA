"""Read-only checks for saved references outside the metadata table."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3


def replacement_blockers(project, removed_ids: list[int], removed_columns: set[str]) -> list[str]:
    if not removed_ids and not removed_columns:
        return []
    blocked: list[str] = []
    removed = set(removed_ids)
    blocked.extend(_database_blockers(project, removed_ids, removed_columns))
    database = project.paths.media / "media_index.db"
    if removed_columns and database.exists():
        with closing(sqlite3.connect(database)) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("media_link_settings", "media_match_rules"):
                if table not in tables:
                    continue
                for source, column in connection.execute(f"SELECT source_id, metadata_column FROM {table}"):
                    if column in removed_columns:
                        blocked.append(f"Media source {source!r} uses column {column!r}.")
            if "media_sources" in tables:
                fields = {row[1] for row in connection.execute("PRAGMA table_info(media_sources)")}
                identity = "id" if "id" in fields else "''"
                for source_id, label, path in connection.execute(
                    f"SELECT {identity}, label, path FROM media_sources WHERE source_kind='metadata_column'"
                ):
                    column = (str(source_id).removeprefix("media_text_")
                              if str(source_id).startswith("media_text_") else Path(path).stem)
                    if column in removed_columns:
                        blocked.append(f"Text source {label!r} uses column {column!r}.")

    pin_database = project.paths.state / "discovery_pins.db"
    if removed_columns and pin_database.exists():
        with closing(sqlite3.connect(pin_database)) as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='discovery_pin_settings'").fetchone():
                for scope, raw in connection.execute("SELECT discovery_scope, settings_json FROM discovery_pin_settings"):
                    try:
                        columns, _ = _references(json.loads(raw))
                        if columns & removed_columns:
                            blocked.append(f"Pin settings {scope!r} use columns: {', '.join(sorted(columns & removed_columns))}.")
                    except (ValueError, TypeError) as exc:
                        blocked.append(f"Cannot verify pin settings {scope!r}: {exc}")

    # Read payloads directly: constructing repositories here can migrate/write files.
    roots = (project.paths.presets, project.paths.state, project.paths.annotations)
    for root in roots:
        for path in sorted(root.rglob("*.json")):
            label = str(path.relative_to(project.root))
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                columns, ids = _references(payload)
                fields = columns & removed_columns
                if fields:
                    blocked.append(f"{label} uses columns: {', '.join(sorted(fields))}.")
                if ids & removed:
                    blocked.append(f"{label} references items being removed.")
                if (removed and isinstance(payload, dict) and payload.get("discovery_file")
                        and path.resolve() != automatic_random_preset(project)):
                    coordinates = Path(str(payload["discovery_file"]))
                    if not coordinates.is_absolute():
                        coordinates = project.root / coordinates
                    if _projection_references(coordinates, removed_ids):
                        blocked.append(f"Discovery projection {payload.get('name') or label!r} references items being removed.")
                if removed and isinstance(payload, dict) and isinstance(payload.get("projection"), dict):
                    reference = payload["projection"].get("coordinates_ref")
                    if reference:
                        coordinates = Path(str(reference))
                        if not coordinates.is_absolute():
                            coordinates = project.root / coordinates
                        coordinates = coordinates.resolve()
                        coordinates.relative_to(project.paths.snapshot_presets.resolve())
                        import numpy as np

                        with np.load(coordinates, allow_pickle=False) as archive:
                            if not removed.isdisjoint(int(value) for value in archive["metadata_ids"].reshape(-1)):
                                blocked.append(f"Snapshot {label} references items being removed.")
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
                blocked.append(f"Cannot verify saved references in {label}: {exc}")
    return list(dict.fromkeys(blocked))


def _projection_references(path: Path, removed_ids: list[int]) -> bool:
    if not path.is_file():
        raise ValueError(f"Cannot verify Discovery projection {path.name}: file is missing.")
    with closing(sqlite3.connect(path)) as connection:
        fields = {row[1] for row in connection.execute("PRAGMA table_info(coordinates)")}
        if "metadata_id" not in fields:
            # Legacy projections depend on table order and cannot safely lose items.
            return connection.execute("SELECT 1 FROM coordinates LIMIT 1").fetchone() is not None
        for offset in range(0, len(removed_ids), 800):
            batch = removed_ids[offset:offset + 800]
            if connection.execute(f"SELECT 1 FROM coordinates WHERE metadata_id IN ({','.join('?' for _ in batch)}) LIMIT 1", batch).fetchone():
                return True
    return False


def _database_blockers(project, ids: list[int], columns: set[str]) -> list[str]:
    """Name a bounded sample of the objects behind the dependency counts."""
    specifications = (
        (project.paths.metadata_groups_database, "group_memberships", "metadata_id", "group_id", "groups", "group_id", "name", "Group"),
        (project.paths.media / "media_index.db", "media_links", "metadata_row_id", "source_id", "media_sources", "id", "label", "Media source"),
        (project.paths.embeddings / "embeddings.db", "embedding_run_inputs", "metadata_row_id", "run_id", "embedding_runs", "id", "label", "Embedding run"),
        (project.root / "thumbnails/variants.db", "thumbnail_variant_items", "metadata_id", "variant_id", "thumbnail_variants", "variant_id", "label", "Thumbnail set"),
        (project.paths.state / "discovery_pins.db", "discovery_pins", "metadata_id", "annotation_id", "", "", "", "Pin"),
        (project.paths.state / "discovery_areas.db", "polygon_area_members", "metadata_id", "area_id", "polygon_areas", "id", "label", "Marked area"),
    )
    blocked: list[str] = []
    for path, table, item_key, owner_key, owners, owner_id, label_key, title in specifications:
        if not path.exists() or (not ids and not (table == "media_links" and columns)):
            continue
        with closing(sqlite3.connect(path)) as connection:
            fields = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if not fields:
                continue
            selected = owner_key if owner_key in fields else item_key
            objects: set[str] = set()
            searches = [(item_key, ids)]
            if table == "media_links":
                searches.append(("metadata_column", sorted(columns)))
            for key, values in searches:
                for offset in range(0, len(values), 800):
                    batch = values[offset:offset + 800]
                    objects.update(str(row[0]) for row in connection.execute(
                        f"SELECT DISTINCT {selected} FROM {table} WHERE {key} IN ({','.join('?' for _ in batch)}) LIMIT 5", batch,
                    ))
                    if len(objects) >= 5:
                        break
            has_labels = owners and connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (owners,)).fetchone()
            for identity in sorted(objects)[:5]:
                name = None
                if has_labels and selected == owner_key:
                    name = connection.execute(f"SELECT {label_key} FROM {owners} WHERE {owner_id}=?", (identity,)).fetchone()
                description = str(name[0]) if name else identity
                blocked.append(f"{title} {description!r} ({path.relative_to(project.root)}) references items or columns being removed.")
    return blocked


def _references(value: object) -> tuple[set[str], set[int]]:
    columns: set[str] = set()
    ids: set[int] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"column", "field", "fields", "metadata_fields"} or key.endswith(("_column", "_field", "_fields")):
                if isinstance(child, str):
                    columns.add(child)
                elif isinstance(child, list):
                    columns.update(item for item in child if isinstance(item, str))
            if key == "advanced_query" and isinstance(child, str) and child.strip():
                from DiscoveryArea.advanced_query import inspect_advanced_query

                columns.update(inspect_advanced_query(child).columns)
            if key in {"metadata_id", "metadata_ids"}:
                for item in child if isinstance(child, list) else [child]:
                    if isinstance(item, (str, int)) and not isinstance(item, bool):
                        ids.add(int(item))
            nested_columns, nested_ids = _references(child)
            columns.update(nested_columns)
            ids.update(nested_ids)
    elif isinstance(value, list):
        for child in value:
            nested_columns, nested_ids = _references(child)
            columns.update(nested_columns)
            ids.update(nested_ids)
    return columns, ids


def automatic_random_preset(project) -> Path | None:
    """Recognize only the internal preset; explicit random analyses stay protected."""
    path = project.paths.discovery_presets / "random_discovery.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not payload.get("parameters", {}).get("_random_discovery"):
            return None
        coordinates = Path(str(payload.get("discovery_file", "")))
        if not coordinates.is_absolute():
            coordinates = project.root / coordinates
        expected = project.paths.discovery / "random_discovery.sqlite"
        return path.resolve() if coordinates.resolve() == expected.resolve() else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None
