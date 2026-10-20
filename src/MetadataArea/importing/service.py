from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unicodedata
import uuid

from MetadataArea.importing.domain import (
    MetadataColumnMapping,
    MetadataColumnSchema,
    MetadataDependencySummary,
    MetadataImportImpact,
    MetadataImportInspection,
    MetadataImportInterpretation,
    MetadataImportPlan,
    MetadataImportPreset,
    MetadataImportProbe,
    MetadataImportResult,
)
from MetadataArea.importing.readers import iter_records, probe_source, source_fingerprint
from MetadataArea.repository import MetadataRepository


class MetadataImportCancelled(RuntimeError):
    pass


class MetadataImportService:
    """Inspect, plan, and atomically publish metadata imports."""

    def __init__(self, project, repository: MetadataRepository) -> None:
        self.project = project
        self.repository = repository

    def probe_import(self, path: Path) -> MetadataImportProbe:
        return probe_source(Path(path))

    def prepare_import(
        self,
        probe: MetadataImportProbe,
        interpretation: MetadataImportInterpretation,
        *,
        progress_callback: Callable[[int, str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> MetadataImportInspection:
        staging_root = self.project.paths.metadata
        staging_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".metadata-import-", dir=staging_root))
        database = staging / "import.sqlite"
        columns: list[str] = []
        seen_columns: set[str] = set()
        samples: list[dict[str, str]] = []
        row_count = 0
        connection = sqlite3.connect(database)
        try:
            connection.executescript(
                """
                CREATE TABLE source_rows (
                  row_index INTEGER PRIMARY KEY,
                  payload_json TEXT NOT NULL
                );
                CREATE TABLE source_cells (
                  row_index INTEGER NOT NULL,
                  column_name TEXT NOT NULL,
                  value TEXT NOT NULL,
                  PRIMARY KEY (row_index, column_name)
                );
                CREATE INDEX source_cells_column_value
                  ON source_cells(column_name, value);
                CREATE TABLE source_columns (
                  position INTEGER PRIMARY KEY,
                  name TEXT NOT NULL UNIQUE
                );
                CREATE TABLE matches (
                  source_index INTEGER PRIMARY KEY,
                  metadata_id INTEGER NOT NULL UNIQUE
                );
                CREATE TABLE preview_state (token TEXT NOT NULL);
                CREATE TABLE final_rows (
                  metadata_id INTEGER PRIMARY KEY,
                  payload_json TEXT NOT NULL
                );
                """
            )
            for raw_record in iter_records(probe, interpretation):
                self._raise_if_cancelled(should_cancel)
                record = self._normalize_record(raw_record)
                if not record:
                    continue
                row_index = row_count
                row_count += 1
                for name in record:
                    if name not in seen_columns:
                        seen_columns.add(name)
                        columns.append(name)
                        connection.execute(
                            "INSERT INTO source_columns (position, name) VALUES (?, ?)",
                            (len(columns) - 1, name),
                        )
                connection.execute(
                    "INSERT INTO source_rows (row_index, payload_json) VALUES (?, ?)",
                    (row_index, json.dumps(record, ensure_ascii=False)),
                )
                connection.executemany(
                    "INSERT INTO source_cells (row_index, column_name, value) VALUES (?, ?, ?)",
                    ((row_index, name, value) for name, value in record.items()),
                )
                if len(samples) < 25:
                    samples.append(record)
                if row_count % 1000 == 0:
                    connection.commit()
                    if progress_callback is not None:
                        hint = probe.row_count_hint
                        value = min(90, int(row_count / hint * 90)) if hint else 0
                        progress_callback(value, f"Inspected {row_count:,} metadata rows…")
            connection.commit()
            if row_count == 0:
                raise ValueError("The selected metadata source contains no records.")
            if not columns:
                raise ValueError("The selected metadata source contains no fields.")
            schemas = tuple(self._infer_schemas(connection, columns))
            current_fingerprint = source_fingerprint(probe.source_path)
            if current_fingerprint != probe.source_fingerprint:
                raise ValueError("The metadata source changed during inspection. Inspect it again.")
            if progress_callback is not None:
                progress_callback(100, "Metadata is ready to configure.")
            return MetadataImportInspection(
                probe=probe,
                interpretation=interpretation,
                staging_path=staging,
                row_count=row_count,
                columns=tuple(columns),
                schemas=schemas,
                samples=tuple(samples),
                source_fingerprint=current_fingerprint,
                warnings=probe.warnings,
            )
        except Exception:
            connection.close()
            self._remove_staging(staging)
            raise
        finally:
            try:
                connection.close()
            except Exception:
                pass

    def default_mappings(
        self,
        inspection: MetadataImportInspection,
        preset: MetadataImportPreset,
    ) -> tuple[MetadataColumnMapping, ...]:
        existing = {
            column.name.casefold(): column.name
            for column in self.repository.list_columns()
            if column.name != "id"
        }
        types = {schema.name: schema.value_type for schema in inspection.schemas}
        formats = {schema.name: schema.value_format for schema in inspection.schemas}
        examples = {
            name: next((row.get(name, "") for row in inspection.samples if row.get(name, "")), "")
            for name in inspection.columns
        }
        mappings: list[MetadataColumnMapping] = []
        for source in inspection.columns:
            target = "id_imported" if source.casefold() == "id" else source
            target = existing.get(target.casefold(), target)
            mappings.append(
                MetadataColumnMapping(
                    source_name=source,
                    target_name=target,
                    value_type=types.get(source, "text"),
                    value_format=formats.get(source, ""),
                    example=examples.get(source, ""),
                )
            )
        return tuple(mappings)

    def dependency_summary(self) -> MetadataDependencySummary:
        """Return current metadata references that replacement should try to preserve."""
        ids = self.repository.fetch_ids()
        columns = {
            column.name for column in self.repository.list_columns() if column.name != "id"
        }
        impact = self._dependency_impact(ids, columns)
        return MetadataDependencySummary(
            groups=impact["affected_groups"],
            media_links=impact["affected_media_links"],
            embedding_inputs=impact["affected_embedding_inputs"],
            discovery_datasets=impact["affected_discovery_items"],
            thumbnails=impact["affected_thumbnails"],
            pins=impact["affected_pins"],
            marked_areas=impact["affected_marked_areas"],
        )

    def replacement_requires_matching(self) -> bool:
        """Return whether replacing every current item would break saved identity references."""

        existing_ids = self.repository.fetch_ids()
        return bool(existing_ids and self._replacement_blockers(existing_ids, set()))

    def preview_import(
        self,
        inspection: MetadataImportInspection,
        preset: MetadataImportPreset,
        *,
        mappings: tuple[MetadataColumnMapping, ...] | None = None,
        source_identifier_column: str = "",
        project_identifier_column: str = "",
        should_cancel: Callable[[], bool] | None = None,
    ) -> MetadataImportPlan:
        self._raise_if_cancelled(should_cancel)
        preset = MetadataImportPreset(preset)
        mappings = self.default_mappings(inspection, preset) if mappings is None else mappings
        # A matching key is always text, even if its values look numeric or like dates.
        mappings = tuple(
            replace(mapping, value_type="text", value_format="")
            if mapping.source_name == source_identifier_column else mapping
            for mapping in mappings
        )
        existing_rows = self.repository.count_rows()
        existing_columns = [column.name for column in self.repository.list_columns() if column.name != "id"]
        messages = self._validate_mappings(inspection, mappings, set(existing_columns))
        if preset == MetadataImportPreset.INITIAL and existing_rows:
            messages.append("Initial import is available only when project metadata is empty.")
        elif preset != MetadataImportPreset.INITIAL and not existing_rows:
            messages.append("Import metadata before appending, updating, or replacing items.")
        fingerprint = self.repository.metadata_fingerprint()
        with closing(sqlite3.connect(inspection.staging_path / "import.sqlite")) as stage:
            messages.extend(self._validate_mapping_types(stage, mappings, should_cancel))
            token = uuid.uuid4().hex
            stage.execute("DELETE FROM preview_state")
            stage.execute("INSERT INTO preview_state VALUES (?)", (token,))
            stage.execute("DELETE FROM matches")
            stage.execute("DELETE FROM final_rows")
            matched = duplicate_incoming = duplicate_existing = empty_incoming = empty_existing = 0
            matching_requested = (
                preset != MetadataImportPreset.INITIAL
                and bool(source_identifier_column or project_identifier_column)
            )
            matching_required = preset in {
                MetadataImportPreset.APPEND,
                MetadataImportPreset.UPDATE,
            }
            if matching_required or matching_requested:
                if source_identifier_column not in inspection.columns:
                    messages.append("Choose an incoming identifier column.")
                elif project_identifier_column not in {column.name for column in self.repository.list_columns()}:
                    messages.append("Choose an existing identifier column.")
                else:
                    matched, duplicate_incoming, duplicate_existing, empty_incoming, empty_existing = self._build_identifier_matches(
                        stage, source_identifier_column, project_identifier_column, should_cancel,
                    )
                    for count, message in (
                        (duplicate_incoming, "The source identifier field contains duplicate values."),
                        (duplicate_existing, "The project identifier column contains duplicate values."),
                        (empty_incoming, "The source identifier field contains empty values."),
                        (empty_existing, "The project identifier column contains empty values."),
                    ):
                        if count:
                            messages.append(message)
            targets = [mapping.target_name for mapping in mappings if mapping.enabled]
            removed_columns = tuple(name for name in existing_columns if name not in targets) if preset == MetadataImportPreset.REPLACE_TABLE else ()
            removed_ids = self._removed_ids(stage, preset == MetadataImportPreset.REPLACE_TABLE)
            dependencies = self._dependency_impact(removed_ids, set(removed_columns))
            if preset == MetadataImportPreset.REPLACE_TABLE:
                messages.extend(self._replacement_blockers(removed_ids, set(removed_columns), dependencies))
            if not targets:
                messages.append("Select at least one source field to import.")
            impact = MetadataImportImpact(
                existing_rows=existing_rows, incoming_rows=inspection.row_count, matched_rows=matched,
                appended_rows=inspection.row_count - matched if preset != MetadataImportPreset.UPDATE else 0,
                removed_rows=len(removed_ids), unchanged_rows=existing_rows - matched,
                duplicate_incoming=duplicate_incoming, duplicate_existing=duplicate_existing,
                empty_incoming_identifiers=empty_incoming, empty_existing_identifiers=empty_existing,
                columns_added=tuple(dict.fromkeys(name for name in targets if name not in existing_columns)),
                columns_updated=tuple(name for name in targets if name in existing_columns),
                columns_removed=removed_columns, **dependencies,
            )
            plan = MetadataImportPlan(
                inspection=inspection, preset=preset, mappings=mappings,
                source_identifier_column=source_identifier_column,
                project_identifier_column=project_identifier_column,
                metadata_fingerprint=fingerprint, preview_token=token, impact=impact,
                can_import=not messages, messages=tuple(messages),
            )
            if plan.can_import:
                unchanged, changed = self._build_final_rows(stage, plan, should_cancel)
                plan = replace(plan, impact=replace(impact, unchanged_rows=unchanged,
                    columns_updated=tuple(name for name in impact.columns_updated if name in changed)))
                if not stage.execute("SELECT 1 FROM final_rows LIMIT 1").fetchone():
                    plan = replace(plan, can_import=False, messages=("The import would produce an empty metadata table.",))
            stage.commit()
            return plan

    def commit_import(
        self,
        plan: MetadataImportPlan,
        *,
        progress_callback: Callable[[int, str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> MetadataImportResult:
        if not plan.can_import:
            raise ValueError("\n".join(plan.messages) or "The metadata import plan is invalid.")
        self._raise_if_cancelled(should_cancel)
        removed_ids = self._plan_removed_ids(plan)
        removed_columns = set(plan.impact.columns_removed)

        def validate_publication(connection: sqlite3.Connection | None = None):
            self._raise_if_cancelled(should_cancel)
            with closing(sqlite3.connect(plan.inspection.staging_path / "import.sqlite")) as stage:
                current = stage.execute("SELECT token FROM preview_state").fetchone()
                if current is None or current[0] != plan.preview_token:
                    raise ValueError("Import settings changed after validation. Check the import again.")
            if source_fingerprint(plan.inspection.probe.source_path) != plan.inspection.source_fingerprint:
                raise ValueError("The metadata source changed after inspection. Inspect it again.")
            fingerprint = (
                self.repository.metadata_fingerprint()
                if connection is None
                else self.repository.metadata_fingerprint_in_connection(connection)
            )
            if fingerprint != plan.metadata_fingerprint:
                raise ValueError("Project metadata changed after validation. Check the import again.")
            if plan.preset == MetadataImportPreset.REPLACE_TABLE:
                blockers = self._replacement_blockers(removed_ids, removed_columns)
                if blockers:
                    raise ValueError("\n".join(blockers))

        validate_publication()
        if progress_callback is not None:
            progress_callback(15, "Publishing metadata…")
        schemas = self._final_schemas(plan)
        try:
            self.repository.publish_staged_import(
                plan.inspection.staging_path / "import.sqlite",
                [schema.name for schema in schemas],
                schemas,
                validate_publication=validate_publication,
                groups_database=self.project.paths.metadata_groups_database,
                media_database=self.project.paths.media / "media_index.db",
                should_cancel=should_cancel,
                progress_callback=(
                    None
                    if progress_callback is None
                    else lambda completed, total: progress_callback(
                        min(90, 15 + int(completed / max(1, total) * 70)),
                        f"Publishing {completed:,} metadata rows…",
                    )
                ),
            )
        except InterruptedError as exc:
            raise MetadataImportCancelled(str(exc)) from exc
        if progress_callback is not None:
            progress_callback(100, "Metadata imported.")
        result = MetadataImportResult(
            preset=plan.preset,
            row_count=self.repository.count_rows(),
            column_count=max(0, len(self.repository.list_columns()) - 1),
            impact=plan.impact,
            source_path=plan.inspection.probe.source_path,
            warnings=(*plan.inspection.warnings, *plan.warnings),
        )
        self.cleanup_import(plan.inspection)
        return result

    def cleanup_import(self, inspection: MetadataImportInspection | None) -> None:
        if inspection is not None:
            self._remove_staging(inspection.staging_path)

    def preview_rows(self, plan: MetadataImportPlan, limit: int = 25) -> list[tuple[int, dict[str, str]]]:
        connection = sqlite3.connect(plan.inspection.staging_path / "import.sqlite")
        try:
            rows = connection.execute(
                "SELECT metadata_id, payload_json FROM final_rows ORDER BY metadata_id LIMIT ?",
                (max(0, int(limit)),),
            ).fetchall()
            return [(int(row[0]), json.loads(str(row[1]))) for row in rows]
        finally:
            connection.close()

    def preview_comparison_rows(
        self,
        plan: MetadataImportPlan,
        limit: int = 25,
    ) -> list[tuple[int, dict[str, str], dict[str, str]]]:
        """Return staged source values beside resulting values for a bounded preview."""

        connection = sqlite3.connect(plan.inspection.staging_path / "import.sqlite")
        try:
            rows = connection.execute(
                """
                SELECT final.metadata_id, final.payload_json, source.payload_json
                FROM final_rows AS final
                LEFT JOIN matches ON matches.metadata_id = final.metadata_id
                LEFT JOIN source_rows AS source ON source.row_index = matches.source_index
                ORDER BY final.metadata_id
                LIMIT ?
                """,
                (max(0, int(limit)),),
            ).fetchall()
            return [
                (
                    int(metadata_id),
                    {} if raw_source is None else json.loads(str(raw_source)),
                    json.loads(str(raw_result)),
                )
                for metadata_id, raw_result, raw_source in rows
            ]
        finally:
            connection.close()

    def _build_identifier_matches(
        self,
        stage: sqlite3.Connection,
        source_column: str,
        project_column: str,
        should_cancel=None,
    ) -> tuple[int, int, int, int, int]:
        incoming: dict[str, int] = {}
        duplicates_incoming: set[str] = set()
        empty_incoming = 0
        for row_index, value in stage.execute(
            "SELECT row_index, value FROM source_cells WHERE column_name = ? ORDER BY row_index",
            (source_column,),
        ):
            self._raise_if_cancelled(should_cancel)
            key = self._match_key(value)
            if not key:
                empty_incoming += 1
            elif key in incoming:
                duplicates_incoming.add(key)
            else:
                incoming[key] = int(row_index)
        present_incoming = int(stage.execute(
            "SELECT COUNT(*) FROM source_cells WHERE column_name = ?",
            (source_column,),
        ).fetchone()[0])
        total_incoming = int(stage.execute("SELECT COUNT(*) FROM source_rows").fetchone()[0])
        empty_incoming += total_incoming - present_incoming
        existing: dict[str, int] = {}
        duplicates_existing: set[str] = set()
        empty_existing = 0
        for metadata_id, value in self.repository.column_items(project_column):
            self._raise_if_cancelled(should_cancel)
            key = self._match_key(value)
            if not key:
                empty_existing += 1
            elif key in existing:
                duplicates_existing.add(key)
            else:
                existing[key] = int(metadata_id)
        if duplicates_incoming or duplicates_existing or empty_incoming or empty_existing:
            return 0, len(duplicates_incoming), len(duplicates_existing), empty_incoming, empty_existing
        pairs = [(source_index, existing[key]) for key, source_index in incoming.items() if key in existing]
        stage.executemany("INSERT INTO matches (source_index, metadata_id) VALUES (?, ?)", pairs)
        return len(pairs), 0, 0, 0, 0

    def _build_final_rows(
        self,
        stage: sqlite3.Connection,
        plan: MetadataImportPlan,
        should_cancel=None,
    ) -> tuple[int, set[str]]:
        stage.execute("DELETE FROM final_rows")
        match_by_id = {
            int(row[1]): int(row[0])
            for row in stage.execute("SELECT source_index, metadata_id FROM matches")
        }
        matched_source = {value for value in match_by_id.values()}
        mappings = [mapping for mapping in plan.mappings if mapping.enabled]
        existing_columns = [column.name for column in self.repository.list_columns() if column.name != "id"]
        targets = list(dict.fromkeys(mapping.target_name for mapping in mappings))
        final_columns = (
            [column for column in existing_columns if column in targets]
            if plan.preset == MetadataImportPreset.REPLACE_TABLE
            else list(existing_columns)
        )
        final_columns.extend(name for name in targets if name not in final_columns)
        high_water = self.repository.id_high_water_mark()
        unchanged_rows = 0
        changed_columns: set[str] = set()

        for row in self.repository.iter_rows():
            self._raise_if_cancelled(should_cancel)
            source_index = match_by_id.get(row.id)
            if source_index is None:
                if plan.preset == MetadataImportPreset.REPLACE_TABLE:
                    continue
                output = {name: row.values.get(name, "") for name in final_columns}
            elif plan.preset == MetadataImportPreset.APPEND:
                output = {name: row.values.get(name, "") for name in final_columns}
            else:
                source = self._stage_record(stage, source_index)
                output = self._apply_mappings(row.values, source, mappings, final_columns,
                                              preserve_blanks=plan.preset == MetadataImportPreset.UPDATE)
            if set(output) == set(row.values) and all(
                output.get(name, "") == row.values.get(name, "") for name in output
            ):
                unchanged_rows += 1
            else:
                changed_columns.update(
                    name for name in final_columns
                    if name in row.values and output.get(name, "") != row.values.get(name, "")
                )
            stage.execute(
                "INSERT INTO final_rows (metadata_id, payload_json) VALUES (?, ?)",
                (row.id, json.dumps(output, ensure_ascii=False)),
            )

        if plan.preset != MetadataImportPreset.UPDATE:
            next_id = high_water + 1
            for source_index, raw in stage.execute(
                "SELECT row_index, payload_json FROM source_rows ORDER BY row_index"
            ):
                self._raise_if_cancelled(should_cancel)
                if int(source_index) in matched_source:
                    continue
                source = json.loads(str(raw))
                output = self._apply_mappings({}, source, mappings, final_columns)
                stage.execute(
                    "INSERT INTO final_rows (metadata_id, payload_json) VALUES (?, ?)",
                    (next_id, json.dumps(output, ensure_ascii=False)),
                )
                next_id += 1
        return unchanged_rows, changed_columns

    @staticmethod
    def _apply_mappings(
        existing: dict[str, str],
        source: dict[str, str],
        mappings: list[MetadataColumnMapping],
        final_columns: list[str],
        *,
        preserve_blanks: bool = False,
    ) -> dict[str, str]:
        output = {name: str(existing.get(name, "")) for name in final_columns}
        for mapping in mappings:
            incoming = str(source.get(mapping.source_name, ""))
            if incoming.strip() and mapping.value_type in {"datetime", "list"}:
                incoming = MetadataImportService._normalize_typed_value(
                    incoming,
                    mapping.value_type,
                    mapping.value_format,
                )
            if incoming.strip() or not preserve_blanks:
                output[mapping.target_name] = incoming
        return output

    def _final_schemas(self, plan: MetadataImportPlan) -> tuple[MetadataColumnSchema, ...]:
        source = {schema.name: schema for schema in plan.inspection.schemas}
        existing = {column.name: column.value_type for column in self.repository.list_columns() if column.name != "id"}
        mappings = {mapping.target_name: mapping for mapping in plan.mappings if mapping.enabled}
        names = []
        stage = sqlite3.connect(plan.inspection.staging_path / "import.sqlite")
        try:
            first = stage.execute("SELECT payload_json FROM final_rows ORDER BY metadata_id LIMIT 1").fetchone()
            if first:
                names = list(json.loads(str(first[0])).keys())
        finally:
            stage.close()
        schemas: list[MetadataColumnSchema] = []
        for name in names:
            mapping = mappings.get(name)
            schema = source.get(mapping.source_name) if mapping else None
            value_type = mapping.value_type if mapping else existing.get(name, "text")
            if plan.preset == MetadataImportPreset.APPEND and name in existing:
                value_type = existing[name]
            schemas.append(
                MetadataColumnSchema(
                    name=name,
                    value_type=value_type,
                    value_format=mapping.value_format if mapping else "",
                    nullable=True if schema is None else schema.nullable,
                )
            )
        return tuple(schemas)

    def _dependency_impact(self, removed_ids: list[int], removed_columns: set[str]) -> dict[str, int]:
        return {
            "affected_groups": self._count_database_rows(
                self.project.paths.metadata_groups_database, "group_memberships", "metadata_id", removed_ids
            ),
            "affected_media_links": self._count_media_links(removed_ids, removed_columns),
            "affected_embedding_inputs": self._count_database_rows(
                self.project.paths.embeddings / "embeddings.db", "embedding_run_inputs", "metadata_row_id", removed_ids
            ),
            "affected_discovery_items": self._count_discovery_datasets(removed_ids),
            "affected_thumbnails": self._count_database_rows(
                self.project.paths.root / "thumbnails" / "variants.db", "thumbnail_variant_items", "metadata_id", removed_ids
            ),
            "affected_pins": self._count_database_rows(
                self.project.paths.state / "discovery_pins.db", "discovery_pins", "metadata_id", removed_ids
            ),
            "affected_marked_areas": self._count_database_rows(
                self.project.paths.state / "discovery_areas.db", "polygon_area_members", "metadata_id", removed_ids
            ),
        }

    def _replacement_blockers(self, removed_ids, removed_columns, impact=None) -> list[str]:
        from MetadataArea.importing.dependencies import replacement_blockers

        impact = self._dependency_impact(removed_ids, removed_columns) if impact is None else impact
        labels = {
            "affected_groups": "group memberships",
            "affected_media_links": "media links",
            "affected_embedding_inputs": "embedding inputs",
            "affected_discovery_items": "Discovery projections",
            "affected_thumbnails": "thumbnails",
            "affected_pins": "pins",
            "affected_marked_areas": "marked-area memberships",
        }
        messages = [
            f"Cannot replace: removal affects {count:,} {labels[kind]}. Retain their items and columns."
            for kind, count in impact.items() if count
        ]
        messages.extend(replacement_blockers(self.project, removed_ids, removed_columns))
        return messages

    def _count_media_links(self, ids: list[int], columns: set[str]) -> int:
        path = self.project.paths.media / "media_index.db"
        if not path.exists():
            return 0
        with closing(sqlite3.connect(path)) as connection:
            if not self._table_exists(connection, "media_links"):
                return 0
            affected: set[int] = set()
            for start in range(0, len(ids), 800):
                batch = ids[start:start + 800]
                affected.update(int(row[0]) for row in connection.execute(
                    f"SELECT rowid FROM media_links WHERE metadata_row_id IN ({','.join('?' for _ in batch)})",
                    batch,
                ))
            values = sorted(columns)
            for start in range(0, len(values), 800):
                batch = values[start:start + 800]
                affected.update(int(row[0]) for row in connection.execute(
                    f"SELECT rowid FROM media_links WHERE metadata_column IN ({','.join('?' for _ in batch)})",
                    batch,
                ))
            return len(affected)

    def _count_discovery_datasets(self, ids: list[int]) -> int:
        if not ids or not self.project.paths.discovery.exists():
            return 0
        affected = 0
        from MetadataArea.importing.dependencies import automatic_random_preset

        automatic = automatic_random_preset(self.project)
        for path in self.project.paths.discovery.glob("*.sqlite"):
            if automatic is not None and path.name == "random_discovery.sqlite":
                continue
            with closing(sqlite3.connect(path)) as connection:
                if not self._table_exists(connection, "coordinates"):
                    continue
                fields = {row[1] for row in connection.execute("PRAGMA table_info(coordinates)")}
                if "metadata_id" not in fields:
                    affected += int(connection.execute("SELECT 1 FROM coordinates LIMIT 1").fetchone() is not None)
                    continue
                found = False
                for start in range(0, len(ids), 800):
                    batch = ids[start:start + 800]
                    if connection.execute(
                        f"SELECT 1 FROM coordinates WHERE metadata_id IN ({','.join('?' for _ in batch)}) LIMIT 1",
                        batch,
                    ).fetchone() is not None:
                        found = True
                        break
                affected += int(found)
        return affected

    @classmethod
    def _count_database_rows(cls, path: Path, table: str, column: str, ids: list[int]) -> int:
        if not path.exists() or not ids:
            return 0
        total = 0
        with closing(sqlite3.connect(path)) as connection:
            if not cls._table_exists(connection, table):
                return 0
            for start in range(0, len(ids), 800):
                batch = ids[start:start + 800]
                total += int(connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({','.join('?' for _ in batch)})",
                    batch,
                ).fetchone()[0])
        return total

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None

    def _removed_ids(self, stage: sqlite3.Connection, remove: bool) -> list[int]:
        if not remove:
            return []
        matched = {int(row[0]) for row in stage.execute("SELECT metadata_id FROM matches")}
        return [metadata_id for metadata_id in self.repository.fetch_ids() if metadata_id not in matched]

    def _plan_removed_ids(self, plan: MetadataImportPlan) -> list[int]:
        connection = sqlite3.connect(plan.inspection.staging_path / "import.sqlite")
        try:
            return self._removed_ids(connection, plan.preset == MetadataImportPreset.REPLACE_TABLE)
        finally:
            connection.close()

    @staticmethod
    def _stage_record(connection: sqlite3.Connection, row_index: int) -> dict[str, str]:
        row = connection.execute("SELECT payload_json FROM source_rows WHERE row_index = ?", (row_index,)).fetchone()
        return {} if row is None else json.loads(str(row[0]))

    @staticmethod
    def _validate_mappings(
        inspection: MetadataImportInspection,
        mappings: tuple[MetadataColumnMapping, ...],
        existing_columns: set[str],
    ) -> list[str]:
        messages: list[str] = []
        known = set(inspection.columns)
        targets: set[str] = set()
        existing_casefold = {name.casefold() for name in existing_columns}
        for mapping in mappings:
            if not mapping.enabled:
                continue
            if mapping.source_name not in known:
                messages.append(f"Unknown source field: {mapping.source_name}")
            target = mapping.target_name.strip()
            if not target:
                messages.append(f"Choose a project field for {mapping.source_name}.")
            elif target.casefold() == "id":
                messages.append("The internal ID field cannot be imported or replaced.")
            elif target.casefold() in targets:
                messages.append(f"More than one source field maps to {target!r}.")
            elif target.casefold() in existing_casefold and target not in existing_columns:
                actual = next(name for name in existing_columns if name.casefold() == target.casefold())
                messages.append(f"Use the existing project field spelling {actual!r}.")
            targets.add(target.casefold())
        return messages

    @staticmethod
    def _validate_mapping_types(
        stage: sqlite3.Connection,
        mappings: tuple[MetadataColumnMapping, ...],
        should_cancel=None,
    ) -> list[str]:
        messages: list[str] = []
        for mapping in mappings:
            if not mapping.enabled:
                continue
            value_type = mapping.value_type
            if value_type == "text":
                continue
            if value_type in {"datetime", "list"} and not mapping.value_format:
                messages.append(
                    f"Choose a format for field {mapping.source_name!r}."
                )
                continue
            for (raw_value,) in stage.execute(
                "SELECT value FROM source_cells WHERE column_name = ? AND value != ''",
                (mapping.source_name,),
            ):
                MetadataImportService._raise_if_cancelled(should_cancel)
                value = str(raw_value).strip()
                if value_type in {"datetime", "list"}:
                    try:
                        MetadataImportService._normalize_typed_value(
                            value,
                            value_type,
                            mapping.value_format,
                        )
                    except ValueError as exc:
                        messages.append(f"Field {mapping.source_name!r}: {exc}")
                        break
                    continue
                valid = {
                    "integer": MetadataImportService._is_int,
                    "float": MetadataImportService._is_float,
                    "boolean": lambda item: item.casefold() in {"true", "false"},
                    "date": MetadataImportService._is_iso_date,
                    "datetime": MetadataImportService._is_iso_date,
                }.get(value_type, lambda _item: False)(value)
                if not valid:
                    messages.append(
                        f"Field {mapping.source_name!r} contains a value that is not {value_type}."
                    )
                    break
        return messages

    @staticmethod
    def _match_key(value: object) -> str:
        return unicodedata.normalize("NFC", "" if value is None else str(value)).strip()

    @staticmethod
    def _normalize_record(record: dict[str, str]) -> dict[str, str]:
        output: dict[str, str] = {}
        used: set[str] = set()
        for index, (raw_name, raw_value) in enumerate(record.items(), 1):
            base = str(raw_name).strip() or f"column_{index}"
            if base.casefold() == "id":
                base = "id_imported"
            candidate = base
            suffix = 2
            while candidate.casefold() in used:
                candidate = f"{base}_{suffix}"
                suffix += 1
            used.add(candidate.casefold())
            output[candidate] = "" if raw_value is None else str(raw_value)
        return output

    @staticmethod
    def _infer_schemas(connection: sqlite3.Connection, columns: list[str]) -> Iterator[MetadataColumnSchema]:
        for name in columns:
            values = connection.execute(
                "SELECT value FROM source_cells WHERE column_name = ?", (name,)
            )
            nonempty = 0
            all_int = all_float = all_bool = all_date = all_list = True
            nullable = False
            for (raw,) in values:
                value = str(raw).strip()
                if not value:
                    nullable = True
                    continue
                nonempty += 1
                all_int = all_int and MetadataImportService._is_int(value)
                all_float = all_float and MetadataImportService._is_float(value)
                all_bool = all_bool and value.casefold() in {"true", "false"}
                all_date = all_date and MetadataImportService._is_iso_date(value)
                all_list = all_list and MetadataImportService._is_list(value)
            kind = "text"
            value_format = ""
            if nonempty and all_bool:
                kind = "boolean"
            elif nonempty and all_int:
                kind = "integer"
            elif nonempty and all_float:
                kind = "float"
            elif nonempty and all_date:
                kind = "datetime"
                value_format = "iso8601"
            elif nonempty and all_list:
                kind = "list"
                value_format = "auto"
            unique = int(connection.execute(
                "SELECT COUNT(DISTINCT value) FROM source_cells WHERE column_name = ? AND TRIM(value) != ''",
                (name,),
            ).fetchone()[0])
            present = int(connection.execute(
                "SELECT COUNT(*) FROM source_cells WHERE column_name = ?", (name,)
            ).fetchone()[0])
            nullable = nullable or present < int(connection.execute("SELECT COUNT(*) FROM source_rows").fetchone()[0])
            yield MetadataColumnSchema(
                name=name,
                value_type=kind,
                value_format=value_format,
                nullable=nullable,
                unique_values=unique,
            )

    @staticmethod
    def _is_int(value: str) -> bool:
        try:
            int(value)
            return True
        except ValueError:
            return False

    @staticmethod
    def _is_float(value: str) -> bool:
        try:
            float(value)
            return True
        except ValueError:
            return False

    @staticmethod
    def _is_list(value: str) -> bool:
        text = value.strip()
        bracketed = text.startswith("[") and text.endswith("]")
        if text.startswith("[") != text.endswith("]"):
            return False
        content = text[1:-1].strip() if bracketed else text
        if not content:
            return bracketed
        separator = "," if "," in content else ";" if ";" in content else ""
        if not separator:
            return bracketed
        items = [item.strip() for item in content.split(separator)]
        return len(items) >= 2 and all(items)

    @staticmethod
    def _normalize_typed_value(value: str, value_type: str, value_format: str) -> str:
        if value_type == "datetime":
            return MetadataImportService._normalize_datetime(value, value_format)
        if value_type == "list":
            return MetadataImportService._normalize_list(value, value_format)
        return value

    @staticmethod
    def _normalize_datetime(value: str, value_format: str) -> str:
        from datetime import date, datetime

        text = value.strip()
        if value_format == "iso8601":
            try:
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                    return date.fromisoformat(text).isoformat()
                return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
            except ValueError as exc:
                raise ValueError("value does not match ISO 8601.") from exc

        readable_format = value_format.removeprefix("custom:")
        if not readable_format:
            raise ValueError("choose a datetime format.")
        python_format = MetadataImportService._datetime_python_format(readable_format)
        try:
            parsed = datetime.strptime(text, python_format)
        except ValueError as exc:
            raise ValueError(f"value does not match {readable_format}.") from exc
        includes_time = any(token in readable_format for token in ("HH", "mm", "ss"))
        return parsed.isoformat() if includes_time else parsed.date().isoformat()

    @staticmethod
    def _datetime_python_format(readable_format: str) -> str:
        tokens = {
            "YYYY": "%Y",
            "YY": "%y",
            "MM": "%m",
            "DD": "%d",
            "HH": "%H",
            "mm": "%M",
            "ss": "%S",
        }
        output: list[str] = []
        index = 0
        ordered = sorted(tokens, key=len, reverse=True)
        while index < len(readable_format):
            token = next(
                (candidate for candidate in ordered if readable_format.startswith(candidate, index)),
                "",
            )
            if token:
                output.append(tokens[token])
                index += len(token)
            else:
                output.append(readable_format[index].replace("%", "%%"))
                index += 1
        return "".join(output)

    @staticmethod
    def _normalize_list(value: str, value_format: str) -> str:
        text = value.strip()
        bracketed = text.startswith("[") and text.endswith("]")
        if text.startswith("[") != text.endswith("]"):
            raise ValueError("list brackets are not balanced.")
        content = text[1:-1].strip() if bracketed else text
        if value_format == "auto":
            separator = "," if "," in content else ";" if ";" in content else ""
        elif value_format == "comma":
            separator = ","
        elif value_format == "semicolon":
            separator = ";"
        elif value_format == "pipe":
            separator = "|"
        elif value_format.startswith("custom:"):
            separator = value_format.removeprefix("custom:")
        else:
            separator = ""
        if not separator:
            if bracketed and content:
                return f"[{content}]"
            raise ValueError("value does not contain the selected list separator.")
        items = [item.strip() for item in content.split(separator)]
        if len(items) < 2 or not all(items):
            raise ValueError("value is not a valid separated list.")
        return f"[{', '.join(items)}]"

    @staticmethod
    def _is_iso_date(value: str) -> bool:
        from datetime import datetime
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return True
        except ValueError:
            return False

    @staticmethod
    def _raise_if_cancelled(callback: Callable[[], bool] | None) -> None:
        if callback is not None and callback():
            raise MetadataImportCancelled("Metadata import was aborted.")

    @staticmethod
    def _remove_staging(path: Path) -> None:
        import shutil
        shutil.rmtree(path, ignore_errors=True)


def probe_import(path: Path) -> MetadataImportProbe:
    """Probe a metadata source without requiring an open project."""

    return probe_source(Path(path))


def prepare_import(
    service: MetadataImportService,
    probe: MetadataImportProbe,
    interpretation: MetadataImportInterpretation,
    **kwargs,
) -> MetadataImportInspection:
    return service.prepare_import(probe, interpretation, **kwargs)


def preview_import(
    service: MetadataImportService,
    inspection: MetadataImportInspection,
    preset: MetadataImportPreset,
    **kwargs,
) -> MetadataImportPlan:
    return service.preview_import(inspection, preset, **kwargs)


def commit_import(
    service: MetadataImportService,
    plan: MetadataImportPlan,
    **kwargs,
) -> MetadataImportResult:
    return service.commit_import(plan, **kwargs)


def cleanup_import(
    service: MetadataImportService,
    inspection: MetadataImportInspection | None,
) -> None:
    service.cleanup_import(inspection)
