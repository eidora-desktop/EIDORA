from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, replace
import hashlib
from itertools import zip_longest
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from contextlib import closing

from EmbeddingsArea.domain import (
    EmbeddingRun,
    EmbeddingRunInput,
    EmbeddingRunInputBinding,
    EmbeddingRunParameterValue,
    EmbeddingRunState,
    MediaAsset,
    MediaAssetStatus,
    MediaContentType,
    MediaLink,
    MediaLinkStatus,
    MediaMatchRule,
    MediaMatchStrategy,
    MediaSource,
    MediaSourceKind,
    MediaSourceStatus,
    MediaSourceSummary,
)


from project.portable_paths import repository_paths

MEDIA_SCHEMA_VERSION = 5
EMBEDDING_RUNS_SCHEMA_VERSION = 2
SQLITE_IN_QUERY_BATCH_SIZE = 900


class EmbeddingRunRepository:
    """SQLite persistence boundary for embedding process state."""

    def __init__(self, database_path: Path, *, project_root: Path | None = None) -> None:
        self._database_path = database_path
        self._paths = repository_paths(database_path, project_root, "embeddings")
        self._ensure_schema()

    @property
    def database_path(self) -> Path:
        return self._database_path

    def upsert_run(self, run: EmbeddingRun) -> None:
        with closing(self._connect()) as connection, connection:
            self._upsert_run(connection, run)
            connection.commit()

    def list_runs(self) -> list[EmbeddingRun]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT id, label, state, selected_model_id, selected_source_ids,
                       progress, prepared_revision, prepared_input_count,
                       processed_items, failed_items, last_chunk_at,
                       created_at, updated_at, error_message, artifact_path, prepared_signature
                FROM embedding_runs
                ORDER BY created_at, label COLLATE NOCASE
                """
            ).fetchall()
        return [self._run_from_row(row) for row in rows]

    def get_run(self, run_id: str) -> EmbeddingRun | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT id, label, state, selected_model_id, selected_source_ids,
                       progress, prepared_revision, prepared_input_count,
                       processed_items, failed_items, last_chunk_at,
                       created_at, updated_at, error_message, artifact_path, prepared_signature
                FROM embedding_runs
                WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
        return None if row is None else self._run_from_row(row)

    def count_runs(self) -> int:
        with closing(self._connect()) as connection, connection:
            row = connection.execute("SELECT COUNT(*) FROM embedding_runs").fetchone()
        return int(row[0]) if row else 0

    def delete_run(self, run_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM embedding_run_input_components WHERE run_id = ?", (run_id,))
            connection.execute("DELETE FROM embedding_run_parameters WHERE run_id = ?", (run_id,))
            connection.execute("DELETE FROM embedding_run_input_bindings WHERE run_id = ?", (run_id,))
            connection.execute("DELETE FROM embedding_run_inputs WHERE run_id = ?", (run_id,))
            connection.execute("DELETE FROM embedding_runs WHERE id = ?", (run_id,))
            connection.commit()

    def replace_run_inputs(self, run_id: str, inputs: list[EmbeddingRunInput]) -> None:
        with closing(self._connect()) as connection, connection:
            self._replace_run_inputs(connection, run_id, inputs)
            connection.commit()

    def save_prepared_run(self, run: EmbeddingRun, inputs: list[EmbeddingRunInput]) -> None:
        with closing(self._connect()) as connection, connection:
            self._upsert_run(connection, run)
            self._replace_run_inputs(connection, run.id, inputs)
            connection.commit()

    def save_imported_run(
        self,
        run,
        inputs,
        *,
        metadata_database: Path,
        create_identifiers=None,
        metadata_column="",
        expected_metadata_fingerprint: str = "",
        expected_row_order_fingerprint: str = "",
    ) -> None:
        """Publish links and optional metadata rows in one SQLite transaction."""
        metadata_database.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("ATTACH DATABASE ? AS imported_metadata", (str(metadata_database),))
            connection.execute("BEGIN IMMEDIATE")
            if create_identifiers is not None:
                connection.execute("CREATE TABLE IF NOT EXISTS imported_metadata.metadata (id INTEGER PRIMARY KEY AUTOINCREMENT)")
                if connection.execute("SELECT COUNT(*) FROM imported_metadata.metadata").fetchone()[0]:
                    raise ValueError("Metadata changed after inspection. Inspect the import again.")
                columns = {row[1] for row in connection.execute("PRAGMA imported_metadata.table_info(metadata)")}
                if "embedding_identifier" not in columns:
                    connection.execute("ALTER TABLE imported_metadata.metadata ADD COLUMN embedding_identifier TEXT")
                sentinel = object()
                source_inputs = inputs
                source_identifiers = create_identifiers

                def linked_inputs():
                    for item, identifier in zip_longest(
                        source_inputs, source_identifiers, fillvalue=sentinel
                    ):
                        if item is sentinel or identifier is sentinel:
                            raise ValueError(
                                "Identifier count must equal the imported vector count."
                            )
                        cursor = connection.execute(
                            "INSERT INTO imported_metadata.metadata (embedding_identifier) VALUES (?)",
                            (str(identifier),),
                        )
                        yield replace(item, metadata_row_id=int(cursor.lastrowid))

                inputs = linked_inputs()
            elif metadata_column:
                if not expected_metadata_fingerprint:
                    raise ValueError("The import plan has no metadata identity snapshot.")
                quoted = '"' + metadata_column.replace('"', '""') + '"'
                digest = hashlib.sha256()
                rows = connection.execute(
                    f"SELECT id, {quoted} FROM imported_metadata.metadata ORDER BY id"
                )
                for row in rows:
                    value = "" if row[1] is None else str(row[1]).strip()
                    digest.update(str(int(row[0])).encode("ascii"))
                    digest.update(b"\0")
                    digest.update(value.encode("utf-8"))
                    digest.update(b"\n")
                if digest.hexdigest() != expected_metadata_fingerprint:
                    raise ValueError("Metadata changed after matching. Match the identifiers again.")
            elif expected_row_order_fingerprint:
                digest = hashlib.sha256()
                for row in connection.execute(
                    "SELECT id FROM imported_metadata.metadata ORDER BY id"
                ):
                    digest.update(str(int(row[0])).encode("ascii"))
                    digest.update(b"\n")
                if digest.hexdigest() != expected_row_order_fingerprint:
                    raise ValueError("Metadata changed after matching. Match the rows again.")
            self._upsert_run(connection, run)
            self._replace_run_inputs(connection, run.id, inputs)
            connection.commit()

    def count_run_inputs(self, run_id: str) -> int:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM embedding_run_inputs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def input_metadata_ids_by_ordinal(
        self,
        run_id: str,
    ) -> tuple[set[int], dict[int, int]]:
        """Return the compact identity map used by embedding artifacts."""

        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT ordinal, metadata_row_id
                FROM embedding_run_inputs
                WHERE run_id = ?
                ORDER BY ordinal
                """,
                (run_id,),
            ).fetchall()
        known_ordinals = {int(row["ordinal"]) for row in rows}
        metadata_id_by_ordinal = {
            int(row["ordinal"]): int(row["metadata_row_id"])
            for row in rows
            if row["metadata_row_id"] is not None
        }
        return known_ordinals, metadata_id_by_ordinal

    def fetch_run_inputs(self, run_id: str, limit: int = 200, offset: int = 0) -> list[EmbeddingRunInput]:
        if limit < 0:
            raise ValueError("Run input limit must be zero or greater.")
        if offset < 0:
            raise ValueError("Run input offset must be zero or greater.")
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT i.*, c.payload AS components_json
                FROM embedding_run_inputs AS i
                LEFT JOIN embedding_run_input_components AS c
                  ON c.run_id = i.run_id AND c.ordinal = i.ordinal
                WHERE i.run_id = ?
                ORDER BY i.ordinal
                LIMIT ? OFFSET ?
                """,
                (run_id, limit, offset),
            ).fetchall()
        return [self._run_input_from_row(row) for row in rows]

    def replace_run_input_bindings(self, run_id: str, bindings: list[EmbeddingRunInputBinding]) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM embedding_run_input_bindings WHERE run_id = ?", (run_id,))
            connection.executemany(
                """
                INSERT INTO embedding_run_input_bindings (run_id, input_id, source_id)
                VALUES (?, ?, ?)
                """,
                [(run_id, binding.input_id, binding.source_id) for binding in bindings],
            )
            connection.commit()

    def list_run_input_bindings(self, run_id: str) -> list[EmbeddingRunInputBinding]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT input_id, source_id
                FROM embedding_run_input_bindings
                WHERE run_id = ?
                ORDER BY input_id
                """,
                (run_id,),
            ).fetchall()
        return [
            EmbeddingRunInputBinding(
                input_id=str(row["input_id"]),
                source_id=str(row["source_id"]),
            )
            for row in rows
        ]

    def replace_run_parameter_values(self, run_id: str, values: list[EmbeddingRunParameterValue]) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM embedding_run_parameters WHERE run_id = ?", (run_id,))
            connection.executemany(
                """
                INSERT INTO embedding_run_parameters (run_id, scope, parameter_id, value)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (run_id, value.scope, value.parameter_id, value.value)
                    for value in values
                ],
            )
            connection.commit()

    def list_run_parameter_values(self, run_id: str) -> list[EmbeddingRunParameterValue]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT scope, parameter_id, value
                FROM embedding_run_parameters
                WHERE run_id = ?
                ORDER BY scope, parameter_id
                """,
                (run_id,),
            ).fetchall()
        return [
            EmbeddingRunParameterValue(
                scope=str(row["scope"]),
                parameter_id=str(row["parameter_id"]),
                value=str(row["value"]),
            )
            for row in rows
        ]

    def _ensure_schema(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            if _table_exists(connection, "embedding_runs"):
                row = (connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
                       if _table_exists(connection, "schema_meta") else None)
                if row is None or int(row[0]) != EMBEDDING_RUNS_SCHEMA_VERSION:
                    raise ValueError("Unsupported embedding database schema; automatic migration is not supported.")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS embedding_runs (
                  id TEXT PRIMARY KEY,
                  label TEXT NOT NULL,
                  state TEXT NOT NULL,
                  selected_model_id TEXT NOT NULL DEFAULT '',
                  selected_source_ids TEXT NOT NULL DEFAULT '[]',
                  progress INTEGER NOT NULL DEFAULT 0,
                  prepared_revision INTEGER NOT NULL DEFAULT 0,
                  prepared_input_count INTEGER NOT NULL DEFAULT 0,
                  processed_items INTEGER NOT NULL DEFAULT 0,
                  failed_items INTEGER NOT NULL DEFAULT 0,
                  last_chunk_at TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  error_message TEXT NOT NULL DEFAULT '',
                  artifact_path TEXT NOT NULL DEFAULT '',
                  prepared_signature TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS embedding_run_inputs (
                  run_id TEXT NOT NULL,
                  ordinal INTEGER NOT NULL,
                  media_asset_id TEXT NOT NULL,
                  source_id TEXT NOT NULL,
                  source_key TEXT NOT NULL,
                  absolute_path TEXT NOT NULL,
                  content_type TEXT NOT NULL,
                  metadata_row_id INTEGER,
                  size_bytes INTEGER,
                  mtime_ns INTEGER,
                  link_revision INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY (run_id, ordinal)
                );

                CREATE TABLE IF NOT EXISTS embedding_run_input_components (
                  run_id TEXT NOT NULL,
                  ordinal INTEGER NOT NULL,
                  payload TEXT NOT NULL,
                  PRIMARY KEY (run_id, ordinal)
                );

                CREATE TABLE IF NOT EXISTS embedding_run_input_bindings (
                  run_id TEXT NOT NULL,
                  input_id TEXT NOT NULL,
                  source_id TEXT NOT NULL,
                  PRIMARY KEY (run_id, input_id)
                );

                CREATE TABLE IF NOT EXISTS embedding_run_parameters (
                  run_id TEXT NOT NULL,
                  scope TEXT NOT NULL,
                  parameter_id TEXT NOT NULL,
                  value TEXT NOT NULL,
                  PRIMARY KEY (run_id, scope, parameter_id)
                );
                """
            )
            connection.execute(
                """
                INSERT INTO schema_meta (key, value)
                VALUES ('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(EMBEDDING_RUNS_SCHEMA_VERSION),),
            )
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_embedding_runs_state ON embedding_runs(state);
                CREATE INDEX IF NOT EXISTS idx_embedding_run_inputs_run ON embedding_run_inputs(run_id);
                CREATE INDEX IF NOT EXISTS idx_embedding_run_inputs_asset ON embedding_run_inputs(media_asset_id);
                CREATE INDEX IF NOT EXISTS idx_embedding_run_bindings_run ON embedding_run_input_bindings(run_id);
                CREATE INDEX IF NOT EXISTS idx_embedding_run_parameters_run ON embedding_run_parameters(run_id);
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _upsert_run(self, connection: sqlite3.Connection, run: EmbeddingRun) -> None:
        connection.execute(
            """
                INSERT INTO embedding_runs (
                    id, label, state, selected_model_id, selected_source_ids,
                    progress, prepared_revision, prepared_input_count,
                    processed_items, failed_items, last_chunk_at,
                    created_at, updated_at, error_message, artifact_path, prepared_signature
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    label = excluded.label,
                    state = excluded.state,
                    selected_model_id = excluded.selected_model_id,
                    selected_source_ids = excluded.selected_source_ids,
                    progress = excluded.progress,
                    prepared_revision = excluded.prepared_revision,
                    prepared_input_count = excluded.prepared_input_count,
                    processed_items = excluded.processed_items,
                    failed_items = excluded.failed_items,
                    last_chunk_at = excluded.last_chunk_at,
                    updated_at = excluded.updated_at,
                    error_message = excluded.error_message,
                    artifact_path = excluded.artifact_path,
                    prepared_signature = excluded.prepared_signature
            """,
            (
                run.id,
                run.label,
                run.state.value,
                run.selected_model_id,
                json.dumps(list(run.selected_source_ids)),
                int(run.progress),
                int(run.prepared_revision),
                int(run.prepared_input_count),
                int(run.processed_items),
                int(run.failed_items),
                run.last_chunk_at,
                run.created_at,
                run.updated_at,
                run.error_message,
                self._paths.encode(run.artifact_path),
                run.prepared_signature,
            ),
        )

    def _replace_run_inputs(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        inputs,
    ) -> None:
        connection.execute("DELETE FROM embedding_run_inputs WHERE run_id = ?", (run_id,))
        connection.execute("DELETE FROM embedding_run_input_components WHERE run_id = ?", (run_id,))

        def with_components():
            for item in inputs:
                if item.components:
                    connection.execute(
                        "INSERT INTO embedding_run_input_components VALUES (?, ?, ?)",
                        (run_id, item.ordinal, json.dumps([{**asdict(part), "absolute_path": self._paths.encode(part.absolute_path)} for part in item.components], default=str)),
                    )
                yield item

        connection.executemany(
            """
            INSERT INTO embedding_run_inputs (
                run_id, ordinal, media_asset_id, source_id, source_key, absolute_path,
                content_type, metadata_row_id, size_bytes, mtime_ns, link_revision
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    item.run_id,
                    int(item.ordinal),
                    item.media_item_id,
                    item.source_id,
                    item.source_key,
                    self._paths.encode(item.absolute_path),
                    item.content_type.value,
                    item.metadata_row_id,
                    item.size_bytes,
                    item.mtime_ns,
                    int(item.link_revision),
                )
                for item in with_components()
            ),
        )

    def _run_from_row(self, row: sqlite3.Row) -> EmbeddingRun:
        source_ids = json.loads(str(row["selected_source_ids"] or "[]"))
        if not isinstance(source_ids, list):
            source_ids = []
        return EmbeddingRun(
            id=str(row["id"]),
            label=str(row["label"]),
            state=EmbeddingRunState(str(row["state"])),
            selected_model_id=str(row["selected_model_id"] or ""),
            selected_source_ids=tuple(str(source_id) for source_id in source_ids),
            progress=int(row["progress"] or 0),
            prepared_revision=int(row["prepared_revision"] or 0),
            prepared_input_count=int(row["prepared_input_count"] or 0),
            processed_items=int(row["processed_items"] or 0),
            failed_items=int(row["failed_items"] or 0),
            last_chunk_at=str(row["last_chunk_at"] or ""),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            error_message=str(row["error_message"] or ""),
            artifact_path=self._paths.decode(row["artifact_path"]),
            prepared_signature=str(row["prepared_signature"] or ""),
        )

    def _run_input_from_row(self, row: sqlite3.Row) -> EmbeddingRunInput:
        metadata_row_id = row["metadata_row_id"]
        components = tuple(
            EmbeddingRunInput(**{
                **part, "absolute_path": Path(self._paths.decode(part["absolute_path"])),
                "content_type": MediaContentType(part["content_type"]), "components": (),
            }) for part in json.loads(row["components_json"] or "[]")
        )
        return EmbeddingRunInput(
            run_id=str(row["run_id"]),
            ordinal=int(row["ordinal"]),
            media_item_id=str(row["media_asset_id"] or ""),
            source_id=str(row["source_id"]),
            source_key=str(row["source_key"]),
            absolute_path=Path(self._paths.decode(row["absolute_path"])),
            content_type=MediaContentType(str(row["content_type"])),
            metadata_row_id=int(metadata_row_id) if metadata_row_id is not None else None,
            size_bytes=int(row["size_bytes"]) if row["size_bytes"] is not None else None,
            mtime_ns=int(row["mtime_ns"]) if row["mtime_ns"] is not None else None,
            link_revision=int(row["link_revision"] or 0),
            components=components,
        )


class MediaRepository:
    """SQLite persistence boundary for media sources and indexed assets."""

    def __init__(self, database_path: Path, *, project_root: Path | None = None) -> None:
        self._database_path = database_path
        self._paths = repository_paths(database_path, project_root, "media")
        self._ensure_schema()

    @property
    def database_path(self) -> Path:
        return self._database_path

    def upsert_source(self, source: MediaSource) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO media_sources (
                    id, label, path, source_kind, content_type, recursive, status,
                    created_at, updated_at, last_scanned_at, scan_cursor, source_revision, error_message
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    label = excluded.label,
                    path = excluded.path,
                    source_kind = excluded.source_kind,
                    content_type = excluded.content_type,
                    recursive = excluded.recursive,
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    last_scanned_at = excluded.last_scanned_at,
                    scan_cursor = excluded.scan_cursor,
                    source_revision = excluded.source_revision,
                    error_message = excluded.error_message
                """,
                (
                    source.id,
                    source.label,
                    self._paths.encode(source.path),
                    source.source_kind.value,
                    source.content_type.value,
                    1 if source.recursive else 0,
                    source.status.value,
                    source.created_at,
                    source.updated_at,
                    source.last_scanned_at,
                    source.scan_cursor,
                    int(source.source_revision),
                    source.error_message,
                ),
            )
            connection.commit()

    def get_source(self, source_id: str) -> MediaSource | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT id, label, path, source_kind, content_type, recursive, status,
                       created_at, updated_at, last_scanned_at, scan_cursor, source_revision, error_message
                FROM media_sources
                WHERE id = ?
                """,
                (source_id,),
            ).fetchone()
        return None if row is None else self._source_from_row(row)

    def get_source_for_path(
        self,
        path: Path,
        content_type: MediaContentType | str,
    ) -> MediaSource | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT id, label, path, source_kind, content_type, recursive, status,
                       created_at, updated_at, last_scanned_at, scan_cursor, source_revision, error_message
                FROM media_sources
                WHERE path IN (?, ?) AND content_type = ?
                ORDER BY created_at, id
                LIMIT 1
                """,
                (str(path.expanduser().resolve()), self._paths.encode(path.expanduser().resolve()), MediaContentType(content_type).value),
            ).fetchone()
        return None if row is None else self._source_from_row(row)

    def list_sources(self) -> list[MediaSource]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT id, label, path, source_kind, content_type, recursive, status,
                       created_at, updated_at, last_scanned_at, scan_cursor, source_revision, error_message
                FROM media_sources
                ORDER BY label COLLATE NOCASE, path COLLATE NOCASE
                """
            ).fetchall()
        return [self._source_from_row(row) for row in rows]

    def delete_source(self, source_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM media_scan_events WHERE source_id = ?", (source_id,))
            connection.execute("DELETE FROM media_links WHERE source_id = ?", (source_id,))
            connection.execute("DELETE FROM media_link_settings WHERE source_id = ?", (source_id,))
            connection.execute("DELETE FROM media_match_rules WHERE source_id = ?", (source_id,))
            connection.execute("DELETE FROM media_assets WHERE source_id = ?", (source_id,))
            connection.execute("DELETE FROM media_sources WHERE id = ?", (source_id,))
            connection.commit()

    def mark_source_assets_missing(self, source_id: str, updated_at: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE media_assets
                SET status = ?, updated_at = ?
                WHERE source_id = ?
                """,
                (MediaAssetStatus.MISSING.value, updated_at, source_id),
            )
            connection.execute("UPDATE media_sources SET source_revision=source_revision+1 WHERE id=?", (source_id,))
            connection.commit()

    def upsert_assets(self, assets: list[MediaAsset]) -> None:
        if not assets:
            return
        with closing(self._connect()) as connection, connection:
            self._upsert_assets(connection, assets)
            connection.executemany(
                "UPDATE media_sources SET source_revision=source_revision+1 WHERE id=?",
                ((source_id,) for source_id in {asset.source_id for asset in assets}),
            )
            connection.commit()

    def synchronize_source_assets(
        self, source_id, assets, *, updated_at, batch_size=2000,
        progress_callback=None, should_cancel=None, stage_callback=None,
    ):
        """Discover once off-store, then publish one complete catalog generation."""
        from EmbeddingsArea.domain import MediaProgress
        count, scan_cursor = 0, ""
        with TemporaryDirectory(prefix="eidora-media-scan-") as directory:
            stage_path = Path(directory) / "scan.db"
            with closing(self._connect()) as connection:
                schema = connection.execute("SELECT sql FROM sqlite_master WHERE name='media_assets'").fetchone()[0]
            with closing(sqlite3.connect(stage_path)) as stage, stage:
                stage.execute(schema)
                batch = []
                for asset in assets:
                    if should_cancel and should_cancel():
                        raise RuntimeError("Media indexing cancelled.")
                    batch.append(asset)
                    scan_cursor = asset.relative_path
                    if len(batch) == batch_size:
                        self._upsert_assets(stage, batch, strict=True)
                        count += len(batch)
                        batch.clear()
                        if progress_callback:
                            progress_callback(count)
                        if stage_callback:
                            stage_callback(MediaProgress("Discovering files", count))
                if batch:
                    self._upsert_assets(stage, batch, strict=True)
                    count += len(batch)
                    if progress_callback:
                        progress_callback(count)
                stage.commit()
            if should_cancel and should_cancel():
                raise RuntimeError("Media indexing cancelled.")
            with closing(self._connect()) as connection:
                connection.execute("ATTACH DATABASE ? AS scan_stage", (str(stage_path),))
                if should_cancel:
                    connection.set_progress_handler(lambda: int(should_cancel()), 1000)
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("UPDATE media_assets SET status=?,updated_at=? WHERE source_id=?",
                                       (MediaAssetStatus.MISSING.value, updated_at, source_id))
                    cursor = connection.execute("SELECT * FROM scan_stage.media_assets ORDER BY rowid")
                    completed = 0
                    while rows := cursor.fetchmany(batch_size):
                        if should_cancel and should_cancel():
                            raise RuntimeError("Media indexing cancelled.")
                        self._upsert_assets(connection, [self._asset_from_row(row) for row in rows])
                        completed += len(rows)
                        if stage_callback:
                            stage_callback(MediaProgress("Indexing", completed, count))
                    connection.execute("UPDATE media_sources SET source_revision=source_revision+1 WHERE id=?", (source_id,))
                    if stage_callback:
                        stage_callback(MediaProgress("Saving", count, count))
                    if should_cancel and should_cancel():
                        raise RuntimeError("Media indexing cancelled.")
                    connection.set_progress_handler(None, 0)
                    connection.commit()
                except BaseException:
                    connection.set_progress_handler(None, 0)
                    connection.rollback()
                    raise
        return count, scan_cursor

    def _upsert_assets(self, connection: sqlite3.Connection, assets: list[MediaAsset], *, strict=False) -> None:
        query = """
            INSERT INTO media_assets (
                id, source_id, source_key, absolute_path, relative_path, display_name,
                content_type, fingerprint, size_bytes, mtime_ns, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, source_key) DO UPDATE SET
                absolute_path = excluded.absolute_path,
                relative_path = excluded.relative_path,
                display_name = excluded.display_name,
                content_type = excluded.content_type,
                fingerprint = excluded.fingerprint,
                size_bytes = excluded.size_bytes,
                mtime_ns = excluded.mtime_ns,
                status = excluded.status,
                updated_at = excluded.updated_at
            """
        if strict:
            query = query.split("ON CONFLICT", 1)[0]
        connection.executemany(query,
            [
                (
                    asset.id,
                    asset.source_id,
                    asset.source_key,
                    self._paths.encode(asset.absolute_path),
                    asset.relative_path,
                    asset.display_name,
                    asset.content_type.value,
                    asset.fingerprint,
                    asset.size_bytes,
                    asset.mtime_ns,
                    asset.status.value,
                    asset.created_at,
                    asset.updated_at,
                )
                for asset in assets
            ],
        )

    def count_assets(self, source_id: str) -> int:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM media_assets WHERE source_id = ?",
                (source_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def fetch_assets(self, source_id: str, limit: int = 200, offset: int = 0) -> list[MediaAsset]:
        if limit < 0:
            raise ValueError("Media asset limit must be zero or greater.")
        if offset < 0:
            raise ValueError("Media asset offset must be zero or greater.")
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT id, source_id, source_key, absolute_path, relative_path, display_name,
                       content_type, fingerprint, size_bytes, mtime_ns, status, created_at, updated_at
                FROM media_assets
                WHERE source_id = ?
                ORDER BY display_name COLLATE NOCASE, source_key COLLATE NOCASE
                LIMIT ? OFFSET ?
                """,
                (source_id, limit, offset),
            ).fetchall()
        return [self._asset_from_row(row) for row in rows]

    def ready_assets_for_sources(self, source_ids: tuple[str, ...]) -> list[MediaAsset]:
        return list(self.iter_ready_assets_for_sources(source_ids))

    def iter_ready_assets_for_sources(
        self,
        source_ids: tuple[str, ...],
        *,
        batch_size: int = 2_000,
    ) -> Iterable[MediaAsset]:
        if not source_ids:
            return
        placeholders = ",".join("?" for _ in source_ids)
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                f"""
                SELECT id, source_id, source_key, absolute_path, relative_path, display_name,
                       content_type, fingerprint, size_bytes, mtime_ns, status, created_at, updated_at
                FROM media_assets
                WHERE source_id IN ({placeholders}) AND status = ? AND excluded = 0
                ORDER BY source_id, display_name COLLATE NOCASE, source_key COLLATE NOCASE
                """,
                (*source_ids, MediaAssetStatus.READY.value),
            )
            while rows := cursor.fetchmany(batch_size):
                for row in rows:
                    yield self._asset_from_row(row)

    def linked_assets_for_metadata_ids(
        self,
        metadata_ids: list[int] | tuple[int, ...],
    ) -> dict[int, MediaAsset]:
        """Resolve only requested linked media rows instead of scanning the catalog."""

        ordered_ids = list(dict.fromkeys(int(value) for value in metadata_ids))
        if not ordered_ids:
            return {}
        output: dict[int, MediaAsset] = {}
        with closing(self._connect()) as connection, connection:
            for start in range(0, len(ordered_ids), SQLITE_IN_QUERY_BATCH_SIZE):
                chunk = ordered_ids[start : start + SQLITE_IN_QUERY_BATCH_SIZE]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT
                        links.metadata_row_id AS metadata_row_id,
                        assets.id, assets.source_id, assets.source_key,
                        assets.absolute_path, assets.relative_path, assets.display_name,
                        assets.content_type, assets.fingerprint, assets.size_bytes,
                        assets.mtime_ns, assets.status, assets.created_at, assets.updated_at
                    FROM media_links AS links
                    INNER JOIN media_assets AS assets
                      ON assets.id = links.media_asset_id
                    WHERE links.metadata_row_id IN ({placeholders})
                      AND links.status = ?
                      AND assets.status = ?
                    ORDER BY links.metadata_row_id, links.updated_at DESC, assets.id
                    """,
                    (
                        *chunk,
                        MediaLinkStatus.LINKED.value,
                        MediaAssetStatus.READY.value,
                    ),
                ).fetchall()
                for row in rows:
                    metadata_id = int(row["metadata_row_id"])
                    if metadata_id not in output:
                        output[metadata_id] = self._asset_from_row(row)
        from project.item_repository import ItemRepository
        from project.domain import ProjectStoragePaths

        metadata_path = ProjectStoragePaths(self._database_path.parent.parent).metadata_database
        if metadata_path.exists():
            identities = ItemRepository(metadata_path).asset_ids_for_items(
                [value for value in ordered_ids if value not in output]
            )
            by_asset = {}
            values = list(set(identities.values()))
            with closing(self._connect()) as connection:
                for start in range(0, len(values), SQLITE_IN_QUERY_BATCH_SIZE):
                    batch = values[start:start + SQLITE_IN_QUERY_BATCH_SIZE]
                    placeholders = ','.join('?' for _ in batch)
                    for row in connection.execute(
                        f"SELECT * FROM media_assets WHERE id IN ({placeholders}) AND status=? AND excluded=0",
                        (*batch, MediaAssetStatus.READY.value),
                    ):
                        by_asset[str(row["id"])] = self._asset_from_row(row)
            output.update((item_id, by_asset[asset_id]) for item_id, asset_id in identities.items()
                          if asset_id in by_asset)
        return output

    def analysis_item_counts(self) -> dict[str, int]:
        """Count linked or independently prepared assets for thumbnail choices."""
        from project.domain import ProjectStoragePaths

        metadata_path = ProjectStoragePaths(self._database_path.parent.parent).metadata_database
        with closing(self._connect()) as connection:
            identity_join = ""
            identity_condition = ""
            if metadata_path.exists():
                connection.execute("ATTACH DATABASE ? AS item_store", (str(metadata_path),))
                if connection.execute("SELECT 1 FROM item_store.sqlite_master WHERE name='project_items'").fetchone():
                    identity_join = "LEFT JOIN item_store.project_items AS items ON items.asset_id=assets.id"
                    identity_condition = " OR items.id IS NOT NULL"
            return {str(row[0]): int(row[1]) for row in connection.execute(
                "SELECT assets.source_id,COUNT(DISTINCT assets.id) FROM media_assets AS assets "
                "LEFT JOIN media_links AS links ON links.media_asset_id=assets.id AND links.status='linked' "
                f"{identity_join} WHERE assets.status='ready' AND assets.excluded=0 "
                f"AND (links.metadata_row_id IS NOT NULL{identity_condition}) GROUP BY assets.source_id"
            )}

    def source_identifier_values(self, source_id: str, strategy: MediaMatchStrategy = MediaMatchStrategy.IDENTIFIER_EQUALS) -> set[str]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT source_key, relative_path, display_name
                FROM media_assets
                WHERE source_id = ? AND status = ?
                """,
                (source_id, MediaAssetStatus.READY.value),
            ).fetchall()
        values: set[str] = set()
        for row in rows:
            source_key = str(row["source_key"] or "").strip()
            relative_path = str(row["relative_path"] or "").strip()
            display_name = str(row["display_name"] or "").strip()
            if strategy == MediaMatchStrategy.RELATIVE_PATH and relative_path:
                values.add(relative_path)
            elif strategy == MediaMatchStrategy.FILENAME_STEM and display_name:
                values.add(Path(display_name).stem)
            elif strategy == MediaMatchStrategy.METADATA_ROW_ID and source_key:
                values.add(source_key)
            elif source_key:
                values.add(source_key)
        return values

    def remove_assets_by_ids(self, asset_ids: list[str]) -> int:
        ids = list(dict.fromkeys(asset_id.strip() for asset_id in asset_ids if asset_id.strip()))
        if not ids:
            return 0
        removed = 0
        with closing(self._connect()) as connection, connection:
            sources = set()
            for start in range(0, len(ids), SQLITE_IN_QUERY_BATCH_SIZE):
                chunk = ids[start : start + SQLITE_IN_QUERY_BATCH_SIZE]
                placeholders = ",".join("?" for _ in chunk)
                sources.update(row[0] for row in connection.execute(
                    f"SELECT DISTINCT source_id FROM media_assets WHERE id IN ({placeholders})", chunk))
                connection.execute(f"DELETE FROM media_links WHERE media_asset_id IN ({placeholders})", chunk)
                cursor = connection.execute(f"DELETE FROM media_assets WHERE id IN ({placeholders})", chunk)
                removed += int(cursor.rowcount or 0)
            connection.executemany("UPDATE media_sources SET source_revision=source_revision+1 WHERE id=?",
                                   ((source,) for source in sources))
            connection.commit()
        return removed

    def list_source_summaries(self) -> list[MediaSourceSummary]:
        sources = self.list_sources()
        if not sources:
            return []
        with closing(self._connect()) as connection, connection:
            count_rows = connection.execute(
                """
                SELECT
                    assets.source_id AS source_id,
                    COUNT(*) AS asset_count,
                    SUM(CASE WHEN links.media_asset_id IS NOT NULL THEN 1 ELSE 0 END) AS linked_count,
                    SUM(CASE WHEN assets.status = ? THEN 1 ELSE 0 END) AS missing_count,
                    SUM(CASE WHEN assets.status = ? THEN 1 ELSE 0 END) AS error_count
                FROM media_assets AS assets
                LEFT JOIN (SELECT DISTINCT media_asset_id FROM media_links WHERE status = ?) AS links
                  ON links.media_asset_id = assets.id
                GROUP BY assets.source_id
                """,
                (
                    MediaAssetStatus.MISSING.value,
                    MediaAssetStatus.ERROR.value,
                    MediaLinkStatus.LINKED.value,
                ),
            ).fetchall()
            rule_rows = connection.execute(
                "SELECT source_id, metadata_column FROM media_link_settings UNION "
                "SELECT source_id, metadata_column FROM media_match_rules"
            ).fetchall()
        counts = {
            str(row["source_id"]): {
                "asset_count": int(row["asset_count"] or 0),
                "linked_count": int(row["linked_count"] or 0),
                "missing_count": int(row["missing_count"] or 0),
                "error_count": int(row["error_count"] or 0),
            }
            for row in count_rows
        }
        columns_by_source: dict[str, list[str]] = {}
        for row in rule_rows:
            source_id = str(row["source_id"])
            metadata_column = str(row["metadata_column"] or "").strip()
            if metadata_column and metadata_column not in columns_by_source.get(source_id, []):
                columns_by_source.setdefault(source_id, []).append(metadata_column)
        summaries: list[MediaSourceSummary] = []
        for source in sources:
            source_counts = counts.get(source.id, {})
            asset_count = int(source_counts.get("asset_count", 0))
            linked_count = int(source_counts.get("linked_count", 0))
            summaries.append(
                MediaSourceSummary(
                    source=source,
                    asset_count=asset_count,
                    linked_count=linked_count,
                    unlinked_count=max(0, asset_count - linked_count),
                    missing_count=int(source_counts.get("missing_count", 0)),
                    error_count=int(source_counts.get("error_count", 0)),
                    metadata_columns=tuple(columns_by_source.get(source.id, [])),
                )
            )
        return summaries

    def record_scan_event(
        self,
        source_id: str,
        started_at: str,
        finished_at: str,
        discovered_count: int,
        indexed_count: int,
        missing_count: int,
        error_message: str = "",
    ) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO media_scan_events (
                    source_id, started_at, finished_at, discovered_count,
                    indexed_count, missing_count, error_message
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    started_at,
                    finished_at,
                    int(discovered_count),
                    int(indexed_count),
                    int(missing_count),
                    str(error_message),
                ),
            )
            connection.commit()

    def _ensure_schema(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            version = 0
            if _table_exists(connection, "schema_meta"):
                row = connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
                version = int(row[0]) if row else 0
            if version == MEDIA_SCHEMA_VERSION:
                return
            if version or _table_exists(connection, "media_sources"):
                raise ValueError("Unsupported media database schema; automatic migration is not supported.")
            connection.execute("BEGIN IMMEDIATE")
            _execute_statements(connection,
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS media_sources (
                  id TEXT PRIMARY KEY,
                  label TEXT NOT NULL,
                  path TEXT NOT NULL,
                  source_kind TEXT NOT NULL,
                  content_type TEXT NOT NULL,
                  recursive INTEGER NOT NULL DEFAULT 0,
                  status TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  last_scanned_at TEXT,
                  scan_cursor TEXT NOT NULL DEFAULT '',
                  source_revision INTEGER NOT NULL DEFAULT 0,
                  error_message TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS media_assets (
                  id TEXT PRIMARY KEY,
                  source_id TEXT NOT NULL,
                  source_key TEXT NOT NULL,
                  absolute_path TEXT NOT NULL,
                  relative_path TEXT NOT NULL,
                  display_name TEXT NOT NULL,
                  content_type TEXT NOT NULL,
                  fingerprint TEXT NOT NULL DEFAULT '',
                  excluded INTEGER NOT NULL DEFAULT 0,
                  size_bytes INTEGER,
                  mtime_ns INTEGER,
                  status TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(source_id, source_key)
                );

                CREATE TABLE IF NOT EXISTS media_links (
                  id TEXT PRIMARY KEY,
                  media_asset_id TEXT NOT NULL,
                  source_id TEXT NOT NULL,
                  metadata_row_id INTEGER NOT NULL,
                  metadata_column TEXT NOT NULL,
                  matched_value TEXT NOT NULL DEFAULT '',
                  strategy TEXT NOT NULL,
                  status TEXT NOT NULL,
                  confidence REAL NOT NULL DEFAULT 1.0,
                  is_manual INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(media_asset_id, metadata_row_id, metadata_column, strategy)
                );

                CREATE TABLE IF NOT EXISTS media_match_rules (
                  source_field TEXT NOT NULL DEFAULT '',
                  comparison TEXT NOT NULL DEFAULT 'exact',
                  case_sensitive INTEGER NOT NULL DEFAULT 1,
                  identifier_regex TEXT NOT NULL DEFAULT '',
                  source_id TEXT NOT NULL,
                  metadata_column TEXT NOT NULL,
                  strategy TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY (source_id, metadata_column, strategy)
                );

                CREATE TABLE IF NOT EXISTS media_scan_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  source_id TEXT NOT NULL,
                  started_at TEXT NOT NULL,
                  finished_at TEXT,
                  discovered_count INTEGER NOT NULL DEFAULT 0,
                  indexed_count INTEGER NOT NULL DEFAULT 0,
                  missing_count INTEGER NOT NULL DEFAULT 0,
                  error_message TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS media_link_settings (
                  source_id TEXT PRIMARY KEY,
                  source_field TEXT NOT NULL,
                  metadata_column TEXT NOT NULL,
                  comparison TEXT NOT NULL,
                  ignore_case INTEGER NOT NULL DEFAULT 0,
                  replace_existing INTEGER NOT NULL DEFAULT 0,
                  create_missing INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            _execute_statements(connection,
                """
                CREATE INDEX IF NOT EXISTS idx_media_assets_source ON media_assets(source_id);
                CREATE INDEX IF NOT EXISTS idx_media_assets_status ON media_assets(status);
                CREATE INDEX IF NOT EXISTS idx_media_links_source ON media_links(source_id);
                CREATE INDEX IF NOT EXISTS idx_media_links_asset ON media_links(media_asset_id);
                CREATE INDEX IF NOT EXISTS idx_media_links_metadata_row ON media_links(metadata_row_id);
                """
            )
            connection.execute(
                """
                INSERT INTO schema_meta (key, value)
                VALUES ('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(MEDIA_SCHEMA_VERSION),),
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _source_from_row(self, row: sqlite3.Row) -> MediaSource:
        status_value = str(row["status"] or "")
        if status_value == "unmatched":
            status_value = MediaSourceStatus.UNLINKED.value
        return MediaSource(
            id=str(row["id"]),
            label=str(row["label"]),
            path=Path(self._paths.decode(row["path"])),
            source_kind=MediaSourceKind(str(row["source_kind"])),
            content_type=MediaContentType(str(row["content_type"])),
            recursive=bool(row["recursive"]),
            status=MediaSourceStatus(status_value),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            last_scanned_at=str(row["last_scanned_at"]) if row["last_scanned_at"] is not None else None,
            scan_cursor=str(row["scan_cursor"] or ""),
            source_revision=int(row["source_revision"] or 0),
            error_message=str(row["error_message"] or ""),
        )

    def _asset_from_row(self, row: sqlite3.Row) -> MediaAsset:
        return MediaAsset(
            id=str(row["id"]),
            source_id=str(row["source_id"]),
            source_key=str(row["source_key"]),
            absolute_path=Path(self._paths.decode(row["absolute_path"])),
            relative_path=str(row["relative_path"]),
            display_name=str(row["display_name"]),
            content_type=MediaContentType(str(row["content_type"])),
            fingerprint=str(row["fingerprint"] or ""),
            size_bytes=int(row["size_bytes"]) if row["size_bytes"] is not None else None,
            mtime_ns=int(row["mtime_ns"]) if row["mtime_ns"] is not None else None,
            status=MediaAssetStatus(str(row["status"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


class MediaLinkRepository:
    """Persistence boundary for explicit media-to-metadata links."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        MediaRepository(database_path)

    def linked_metadata_ids(self) -> list[int]:
        """Item IDs with explicit links to available, included media assets."""
        with closing(self._connect()) as connection:
            return [int(row[0]) for row in connection.execute(
                """
                SELECT DISTINCT links.metadata_row_id
                FROM media_links AS links
                INNER JOIN media_assets AS assets ON assets.id = links.media_asset_id
                WHERE links.status = ? AND assets.status = ? AND assets.excluded = 0
                ORDER BY links.metadata_row_id
                """,
                (MediaLinkStatus.LINKED.value, MediaAssetStatus.READY.value),
            )]

    def matching_records(self, source_id: str, field: str, should_cancel=None):
        column = {"filename": "display_name", "relative_path": "relative_path", "identifier": "source_key"}.get(field)
        if column is None:
            raise ValueError("Choose a valid source field.")
        with closing(self._connect()) as connection:
            if should_cancel:
                connection.set_progress_handler(lambda: int(should_cancel()), 1000)
            cursor = connection.execute(
                f"SELECT id, {column} FROM media_assets WHERE source_id=? AND status=? AND excluded=0",
                (source_id, MediaAssetStatus.READY.value),
            )
            while rows := cursor.fetchmany(2000):
                if should_cancel and should_cancel():
                    raise RuntimeError("Media operation cancelled.")
                yield from ((str(row[0]), str(row[1])) for row in rows)

    def linked_pairs(self, source_id: str) -> list[tuple[str, int]]:
        with closing(self._connect()) as connection:
            return [(str(row[0]), int(row[1])) for row in connection.execute(
                "SELECT media_asset_id,metadata_row_id FROM media_links WHERE source_id=? AND status='linked'",
                (source_id,),
            )]

    def saved_request(self, source_id):
        from EmbeddingsArea.domain import MediaLinkRequest
        with closing(self._connect()) as connection:
            saved = connection.execute("SELECT * FROM media_link_settings WHERE source_id=?", (source_id,)).fetchone()
            if saved:
                return MediaLinkRequest(source_id, saved['source_field'], saved['metadata_column'], saved['comparison'],
                                        bool(saved['ignore_case']), bool(saved['replace_existing']), bool(saved['create_missing']))
            row = connection.execute(
                "SELECT * FROM media_match_rules WHERE source_id=? ORDER BY updated_at DESC LIMIT 1", (source_id,)
            ).fetchone()
        if row is None or row["identifier_regex"] or row["strategy"] not in {
            "identifier_equals", "filename", "filename_stem", "relative_path"
        }:
            return None
        field = row["source_field"]
        comparison = row["comparison"]
        if not field:
            field = {"relative_path": "relative_path", "identifier_equals": "identifier"}.get(row["strategy"], "filename")
            comparison = {"filename": "filename", "filename_stem": "stem"}.get(row["strategy"], "exact")
        return MediaLinkRequest(source_id, field, row["metadata_column"], comparison, not bool(row["case_sensitive"]))

    def publish_plan(self, plan, metadata, *, updated_at, progress=None, should_cancel=None):
        from EmbeddingsArea.domain import MediaProgress
        request = plan.request
        strategy = {"exact": "identifier_equals", "filename": "filename", "stem": "filename_stem"}[request.comparison]
        total = len(plan.matches) + len(plan.new_rows)
        with metadata.media_publication(self._database_path, should_cancel) as connection:
            source = connection.execute("SELECT source_revision FROM media_store.media_sources WHERE id=?", (request.source_id,)).fetchone()
            if source is None or int(source[0]) != plan.source_revision:
                raise ValueError("Media source changed after preview. Refresh the preview.")
            if metadata.revision_in_connection(connection) != plan.metadata_revision:
                raise ValueError("Metadata changed after preview. Refresh the preview.")
            if request.replace_existing:
                connection.execute("DELETE FROM media_store.media_links WHERE source_id=?", (request.source_id,))
            completed = 0
            for start in range(0, len(plan.matches), 2000):
                if should_cancel and should_cancel():
                    raise RuntimeError("Media operation cancelled.")
                batch = plan.matches[start:start+2000]
                self._publish_link_batch(connection, request.source_id, request.metadata_column, strategy, batch, updated_at)
                completed += len(batch)
                if progress:
                    progress(MediaProgress("Linking", completed, total))
            for start in range(0, len(plan.new_rows), 2000):
                if should_cancel and should_cancel():
                    raise RuntimeError("Media operation cancelled.")
                batch = plan.new_rows[start:start+2000]
                ids = metadata.append_media_rows(connection, [request.metadata_column], [(value,) for _, value in batch])
                self._publish_link_batch(connection, request.source_id, request.metadata_column, strategy,
                    [(asset_id, row_id, value) for (asset_id, value), row_id in zip(batch, ids, strict=True)], updated_at)
                completed += len(batch)
                if progress:
                    progress(MediaProgress("Creating metadata and linking", completed, total))
            if plan.new_rows:
                metadata.finish_media_rows(connection, [request.metadata_column])
            # Legacy rules remain untouched and are never executed automatically.
            connection.execute(
                "INSERT OR REPLACE INTO media_store.media_link_settings "
                "(source_id,source_field,metadata_column,comparison,ignore_case,replace_existing,create_missing) "
                "VALUES (?,?,?,?,?,?,?)",
                (request.source_id, request.source_field, request.metadata_column, request.comparison,
                 int(request.ignore_case), int(request.replace_existing), int(request.create_missing)),
            )
            self._finish_publication(connection, request.source_id, updated_at)
            if progress:
                progress(MediaProgress("Saving", total, total))
        if plan.new_rows:
            metadata.refresh_schema_mirror()

    def create_initial_metadata(self, source_id, metadata, *, updated_at, progress=None, should_cancel=None):
        from EmbeddingsArea.domain import MediaProgress
        count = 0
        with metadata.media_publication(self._database_path, should_cancel) as connection:
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata'").fetchone()
            if exists and connection.execute("SELECT 1 FROM metadata LIMIT 1").fetchone():
                return 0
            total = int(connection.execute(
                "SELECT COUNT(*) FROM media_store.media_assets WHERE source_id=? AND status='ready' AND excluded=0", (source_id,)
            ).fetchone()[0])
            # The same connection reads assets and writes links: no second reader lock.
            cursor = connection.execute(
                "SELECT id,display_name,relative_path FROM media_store.media_assets "
                "WHERE source_id=? AND status='ready' AND excluded=0 ORDER BY rowid", (source_id,)
            )
            while batch := cursor.fetchmany(2000):
                if should_cancel and should_cancel():
                    raise RuntimeError("Media operation cancelled.")
                ids = metadata.append_media_rows(connection, ["filename", "relative_path"], [(row[1], row[2]) for row in batch])
                self._publish_link_batch(connection, source_id, "filename", "manual",
                    [(row[0], row_id, row[1]) for row, row_id in zip(batch, ids, strict=True)], updated_at)
                count += len(batch)
                if progress:
                    progress(MediaProgress("Creating metadata and linking", count, total))
            if count:
                metadata.finish_media_rows(connection, ["filename", "relative_path"])
                self._finish_publication(connection, source_id, updated_at)
                if progress:
                    progress(MediaProgress("Saving", count, count))
        if count:
            metadata.refresh_schema_mirror()
        return count

    @staticmethod
    def _publish_link_batch(connection, source_id, column, strategy, batch, updated_at):
        connection.executemany(
            "INSERT INTO media_store.media_links "
            "(id,media_asset_id,source_id,metadata_row_id,metadata_column,matched_value,strategy,status,confidence,is_manual,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,'linked',1,?,?,?)",
            ((f"link_{source_id}_{asset_id}_{row_id}", asset_id, source_id, row_id, column, value,
              strategy, int(strategy == "manual"), updated_at, updated_at) for asset_id, row_id, value in batch),
        )

    @staticmethod
    def _finish_publication(connection, source_id, updated_at):
        connection.execute(
            "UPDATE media_store.media_sources SET source_revision=source_revision+1, updated_at=?, "
            "status=CASE WHEN EXISTS(SELECT 1 FROM media_store.media_links WHERE source_id=? AND status='linked') "
            "THEN 'ready' ELSE 'unlinked' END WHERE id=?", (updated_at, source_id, source_id)
        )

    def list_links_for_source(self, source_id: str) -> list[MediaLink]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT id, media_asset_id, source_id, metadata_row_id, metadata_column, matched_value,
                       strategy, status, confidence, is_manual, created_at, updated_at
                FROM media_links
                WHERE source_id = ? AND status = ?
                ORDER BY metadata_row_id, media_asset_id
                """,
                (source_id, MediaLinkStatus.LINKED.value),
            ).fetchall()
        return [self._link_from_row(row) for row in rows]

    def replace_links_for_source(
        self,
        source_id: str,
        metadata_column: str,
        strategy: MediaMatchStrategy,
        links: Iterable[MediaLink],
        updated_at: str,
    ) -> int:
        links = list(links)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                DELETE FROM media_links
                WHERE source_id = ? AND metadata_column = ? AND strategy = ?
                """,
                (source_id, metadata_column, strategy.value),
            )
            connection.execute(
                """
                DELETE FROM media_match_rules
                WHERE source_id = ? AND metadata_column = ? AND strategy = ?
                """,
                (source_id, metadata_column, strategy.value),
            )
            inserted_count = 0
            batch: list[MediaLink] = []
            for link in links:
                batch.append(link)
                if len(batch) >= 2_000:
                    self._insert_links(connection, batch)
                    inserted_count += len(batch)
                    batch.clear()
            if batch:
                self._insert_links(connection, batch)
                inserted_count += len(batch)
            connection.execute(
                """
                INSERT INTO media_match_rules (source_id, metadata_column, strategy, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (source_id, metadata_column, strategy.value, updated_at),
            )
            source = connection.execute(
                "SELECT source_revision FROM media_sources WHERE id = ?",
                (source_id,),
            ).fetchone()
            if source is not None:
                next_revision = int(source["source_revision"] or 0) + 1
                connection.execute(
                    """
                    UPDATE media_sources
                    SET source_revision = ?, updated_at = ?, status = ?
                    WHERE id = ?
                    """,
                    (
                        next_revision,
                        updated_at,
                        MediaSourceStatus.READY.value if inserted_count else MediaSourceStatus.UNLINKED.value,
                        source_id,
                    ),
                )
            connection.commit()
        return inserted_count

    @staticmethod
    def _insert_links(connection: sqlite3.Connection, links: list[MediaLink]) -> None:
        connection.executemany(
            """
            INSERT INTO media_links (
                id, media_asset_id, source_id, metadata_row_id, metadata_column, matched_value,
                strategy, status, confidence, is_manual, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    link.id,
                    link.media_asset_id,
                    link.source_id,
                    int(link.metadata_row_id),
                    link.metadata_column,
                    link.matched_value,
                    link.strategy.value,
                    link.status.value,
                    float(link.confidence),
                    1 if link.is_manual else 0,
                    link.created_at,
                    link.updated_at,
                )
                for link in links
            ],
        )

    def list_match_rules(self, source_id: str) -> list[MediaMatchRule]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """
                SELECT source_id, metadata_column, strategy
                FROM media_match_rules
                WHERE source_id = ?
                ORDER BY updated_at DESC
                """,
                (source_id,),
            ).fetchall()
        return [
            MediaMatchRule(
                source_id=str(row["source_id"]),
                metadata_column=str(row["metadata_column"]),
                strategy=MediaMatchStrategy(str(row["strategy"])),
            )
            for row in rows
        ]

    def source_linked_asset_ids(self, source_ids: tuple[str, ...]) -> set[str]:
        if not source_ids:
            return set()
        placeholders = ",".join("?" for _ in source_ids)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                f"""
                SELECT DISTINCT media_asset_id
                FROM media_links
                WHERE source_id IN ({placeholders}) AND status = ?
                """,
                (*source_ids, MediaLinkStatus.LINKED.value),
            ).fetchall()
        return {str(row[0]) for row in rows if str(row[0]).strip()}

    def links_for_assets(self, asset_ids: list[str]) -> dict[str, MediaLink]:
        ordered_ids = list(dict.fromkeys(str(asset_id) for asset_id in asset_ids if str(asset_id).strip()))
        if not ordered_ids:
            return {}
        links: dict[str, MediaLink] = {}
        with closing(self._connect()) as connection, connection:
            for start in range(0, len(ordered_ids), SQLITE_IN_QUERY_BATCH_SIZE):
                chunk = ordered_ids[start : start + SQLITE_IN_QUERY_BATCH_SIZE]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT id, media_asset_id, source_id, metadata_row_id, metadata_column, matched_value,
                           strategy, status, confidence, is_manual, created_at, updated_at
                    FROM media_links
                    WHERE media_asset_id IN ({placeholders}) AND status = ?
                    ORDER BY updated_at DESC
                    """,
                    (*chunk, MediaLinkStatus.LINKED.value),
                ).fetchall()
                for row in rows:
                    asset_id = str(row["media_asset_id"])
                    if asset_id not in links:
                        links[asset_id] = self._link_from_row(row)
        return links

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _link_from_row(row: sqlite3.Row) -> MediaLink:
        return MediaLink(
            id=str(row["id"]),
            media_asset_id=str(row["media_asset_id"]),
            source_id=str(row["source_id"]),
            metadata_row_id=int(row["metadata_row_id"]),
            metadata_column=str(row["metadata_column"]),
            matched_value=str(row["matched_value"] or ""),
            strategy=MediaMatchStrategy(str(row["strategy"])),
            status=MediaLinkStatus(str(row["status"])),
            confidence=float(row["confidence"] or 0.0),
            is_manual=bool(row["is_manual"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


class EmbeddingArtifactRepository:
    """Read-only helpers for completed embedding artifacts."""

    def list_saved_runs(self, run_repository: EmbeddingRunRepository) -> list[EmbeddingRun]:
        return [run for run in run_repository.list_runs() if run.state == EmbeddingRunState.COMPLETED]


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _execute_statements(connection, script):
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)
