from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
import re
from typing import TYPE_CHECKING, Callable, Iterable, Iterator

from MetadataArea.domain import MetadataColumn, MetadataRow, MetadataSearchMatch, MetadataValue
from MetadataArea.value_storage import decode_value, encode_value
from MetadataArea.values import display_value, scalar_values
from shared.persistence import json_store
from project.item_repository import ItemRepository, ensure_item_registry, item_high_water
from project.record_identity import ensure_record_registry, record_high_water

if TYPE_CHECKING:
    from MetadataArea.importing.domain import MetadataColumnSchema


class MetadataRepository:
    """Persistence boundary for project metadata."""

    def __init__(self, database_path: Path, columns_path: Path | None = None, *, table_id: str = "metadata") -> None:
        if not re.fullmatch(r"metadata|table_[0-9a-f]{32}", table_id):
            raise ValueError("Invalid metadata table identity.")
        self.table_id = table_id
        self.sql_table = table_id
        self.sql_schema = "metadata_column_schema" if table_id == "metadata" else f"{table_id}_columns"
        self._database_path = database_path
        self._columns_path = (columns_path or database_path.parent / "metadata_columns.json") if table_id == "metadata" else database_path.parent / f"{table_id}_columns.json"
        if self._database_path.exists() and self.is_initialized():
            with closing(self._connect()) as connection, connection:
                self._ensure_item_registry(connection)
                from MetadataArea.table_repository import ensure_link_indexes
                ensure_link_indexes(connection)
            self.ensure_column_summaries()
            with closing(self._connect()) as connection, connection:
                from MetadataArea.field_presentation import ensure_field_presentation
                ensure_field_presentation(connection)

    @property
    def database_path(self) -> Path:
        return self._database_path

    def revision(self) -> int:
        if not self._database_path.exists():
            return 0
        with closing(self._connect()) as connection:
            return self.revision_in_connection(connection)

    @staticmethod
    def revision_in_connection(connection: sqlite3.Connection) -> int:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata_state'").fetchone():
            return 0
        row = connection.execute("SELECT value FROM metadata_state WHERE key='revision'").fetchone()
        return int(row[0]) if row else 0

    def bump_revision(self, connection: sqlite3.Connection) -> None:
        self._ensure_schema_tables(connection)
        connection.execute(
            "INSERT INTO metadata_state(key,value) VALUES ('revision','1') "
            "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1"
        )
        from MetadataArea.field_presentation import refresh_field_presentation
        refresh_field_presentation(connection)

    @contextmanager
    def media_publication(self, media_database: Path, should_cancel=None):
        """Own metadata allocation and the attached media transaction together."""
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("ATTACH DATABASE ? AS media_store", (str(media_database),))
            if should_cancel:
                connection.set_progress_handler(lambda: int(should_cancel()), 1000)
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                if should_cancel and should_cancel():
                    raise RuntimeError("Media operation cancelled.")
                # COMMIT is the point of no cancellation. An interrupted rollback
                # must never leave publication half-finished.
                connection.set_progress_handler(None, 0)
                connection.commit()
            except BaseException:
                connection.set_progress_handler(None, 0)
                connection.rollback()
                raise

    def append_media_rows(self, connection, columns: list[str], rows: list[tuple[str, ...]]) -> list[int]:
        """Append a bounded batch using the authoritative, never-reused ID range."""
        self._ensure_schema_tables(connection)
        connection.execute(f"CREATE TABLE IF NOT EXISTS {self.sql_table} (id INTEGER PRIMARY KEY AUTOINCREMENT)")
        existing = self._column_names(connection)
        for column in columns:
            if column == "id":
                raise ValueError("The internal metadata ID cannot be a matching column.")
            if column not in existing:
                connection.execute(f"ALTER TABLE {self.sql_table} ADD COLUMN {self._quote_identifier(column)} TEXT")
        first = self._id_high_water_in_connection(connection) + 1
        ids = list(range(first, first + len(rows)))
        names = ', '.join(self._quote_identifier(column) for column in columns)
        placeholders = ','.join('?' for _ in range(len(columns) + 1))
        connection.executemany(
            f"INSERT INTO {self.sql_table} (id,{names}) VALUES ({placeholders})",
            ((row_id, *(encode_value(value) for value in row)) for row_id, row in zip(ids, rows, strict=True)),
        )
        if ids:
            connection.execute(
                "INSERT INTO metadata_state(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (self._allocation_key, str(ids[-1]))
            )
        return ids

    def finish_media_rows(self, connection, columns: list[str]) -> None:
        """Refresh only changed columns, in the publication transaction."""
        names = self._column_names(connection)
        for column in columns:
            stats = _ColumnStats()
            for (value,) in connection.execute(f"SELECT {self._quote_identifier(column)} FROM {self.sql_table}"):
                stats.observe(decode_value(value))
            connection.execute(
                f"INSERT INTO {self.sql_schema}(name,position,value_type,nullable,unique_values) "
                "VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "value_type=excluded.value_type, unique_values=excluded.unique_values",
                (column, names.index(column)-1, self._canonical_schema_type(stats.infer_type()), 1, stats.unique_count),
            )
        self.bump_revision(connection)

    def refresh_schema_mirror(self) -> None:
        try:
            self._write_schema_mirror_from_database()
        except OSError:
            pass  # SQLite is authoritative; opening the project can repair this mirror.

    def initialize_empty(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            self._ensure_schema_tables(connection)
            has_metadata = connection.execute(f"SELECT 1 FROM sqlite_master WHERE name='{self.sql_table}'").fetchone()
            high_water = self._id_high_water_in_connection(connection) if has_metadata else 0
            connection.execute(f"DROP TABLE IF EXISTS {self.sql_table}")
            connection.execute(f"CREATE TABLE {self.sql_table} (id INTEGER PRIMARY KEY AUTOINCREMENT)")
            self._ensure_schema_tables(connection)
            connection.execute(f"DELETE FROM {self.sql_schema}")
            connection.execute(
                "INSERT INTO metadata_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (self._allocation_key, str(high_water))
            )
            self.bump_revision(connection)
            connection.commit()
        json_store.write_json(self._columns_path, {})

    def initialize_from_rows(
        self,
        rows: list[dict[str, MetadataValue]],
        columns: list[str],
        row_ids: list[int] | None = None,
    ) -> None:
        if not rows:
            raise ValueError("Metadata import did not contain any rows.")
        if not columns:
            raise ValueError("Metadata import did not contain any columns.")
        if row_ids is not None and len(row_ids) != len(rows):
            raise ValueError("Metadata row ids must match the number of imported rows.")
        if row_ids is not None and len(set(row_ids)) != len(row_ids):
            raise ValueError("Metadata row ids must be unique.")
        if row_ids is not None and any(row_id < 1 for row_id in row_ids):
            raise ValueError("Metadata row ids must start at 1.")

        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        column_definitions = ", ".join(f"{self._quote_identifier(column)} TEXT" for column in columns)
        column_list = ", ".join(self._quote_identifier(column) for column in columns)
        placeholders = ", ".join("?" for _ in columns)
        insert_columns = column_list
        insert_placeholders = placeholders
        if row_ids is not None:
            insert_columns = f"id, {column_list}"
            insert_placeholders = f"?, {placeholders}"
        stats = {column: _ColumnStats() for column in columns}

        with closing(self._connect()) as connection, connection:
            self._ensure_item_registry(connection)
            connection.execute(f"DROP TABLE IF EXISTS {self.sql_table}")
            connection.execute(
                f"CREATE TABLE {self.sql_table} (id INTEGER PRIMARY KEY AUTOINCREMENT, {column_definitions})"
            )
            self._ensure_item_registry(connection)
            for row_index, row in enumerate(rows):
                values: list[str | bytes] = []
                for column in columns:
                    value = row.get(column, "")
                    if not isinstance(value, list):
                        value = display_value(value).strip()
                    values.append(encode_value(value))
                    stats[column].observe(value)
                if row_ids is not None:
                    values = [str(row_ids[row_index]), *values]
                connection.execute(f"INSERT INTO {self.sql_table} ({insert_columns}) VALUES ({insert_placeholders})", values)
            self._ensure_schema_tables(connection)
            connection.execute(f"DELETE FROM {self.sql_schema}")
            for position, column in enumerate(columns):
                connection.execute(
                    f"""
                    INSERT INTO {self.sql_schema}
                        (name, position, value_type, nullable, unique_values)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        column,
                        position,
                        self._canonical_schema_type(stats[column].infer_type()),
                        1,
                        stats[column].unique_count,
                    ),
                )
            high_water = self._registry_high_water(connection)
            connection.execute(
                "INSERT INTO metadata_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (self._allocation_key, str(high_water)),
            )
            self.bump_revision(connection)
            connection.commit()

        json_store.write_json(
            self._columns_path,
            {
                column: {
                    "type": stats[column].infer_type(),
                    "unique_values": stats[column].unique_count,
                }
                for column in columns
            },
        )

    def is_initialized(self) -> bool:
        if not self._database_path.exists():
            return False
        try:
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    f"SELECT name FROM sqlite_master WHERE type = 'table' AND name = '{self.sql_table}'"
                ).fetchone()
        except sqlite3.Error:
            return False
        return row is not None

    @staticmethod
    def contains_metadata(database_path: Path) -> bool:
        """Validate recent-project entries without migrations or schema writes."""
        from shared.persistence.sqlite import connect_read_only
        if not database_path.is_file():
            return False
        try:
            with closing(connect_read_only(database_path)) as connection:
                return connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").fetchone() is not None
        except sqlite3.Error:
            return False

    def list_columns(self) -> list[MetadataColumn]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(f"PRAGMA table_info({self.sql_table})").fetchall()
            self._ensure_schema_tables(connection)
            schema_rows = connection.execute(
                f"SELECT name, value_type, unique_values FROM {self.sql_schema}"
            ).fetchall()
        schema = {
            str(row[0]): {"type": str(row[1]), "unique_values": int(row[2])}
            for row in schema_rows
        }
        summaries = json_store.read_json(self._columns_path, default={}) or {}
        columns: list[MetadataColumn] = []
        for row in rows:
            name = str(row[1])
            source = schema.get(name, summaries.get(name, {}))
            value_type = "integer" if name == "id" else str(source.get("type", "text"))
            raw_unique = source.get("unique_values")
            try:
                unique_values = None if raw_unique is None else max(0, int(raw_unique))
            except (TypeError, ValueError):
                unique_values = None
            columns.append(
                MetadataColumn(
                    name=name,
                    value_type=value_type,
                    unique_values=unique_values,
                )
            )
        return columns

    @staticmethod
    def has_items(database_path: Path) -> bool:
        """Compatibility entry point for project item navigation eligibility."""
        return ItemRepository.has_items(database_path)

    def count_rows(self) -> int:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(f"SELECT COUNT(*) FROM {self.sql_table}").fetchone()
        return int(row[0]) if row is not None else 0

    def fetch_ids(self, *, order_by="id", descending=False) -> list[int]:
        """Return canonical metadata IDs without materializing metadata values."""

        columns = self.list_columns()
        column = order_by if order_by in {column.name for column in columns} else "id"
        order = self._order_expression(column, columns)
        direction = "DESC" if descending else "ASC"
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(f"SELECT id FROM {self.sql_table} ORDER BY {order} {direction}").fetchall()
        return [int(row[0]) for row in rows]

    def fetch_rows(
        self,
        order_by: str = "id",
        descending: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[MetadataRow]:
        columns = self.list_columns()
        if not columns:
            return []
        if limit is not None and limit < 0:
            raise ValueError("Metadata row limit must be zero or greater.")
        if offset < 0:
            raise ValueError("Metadata row offset must be zero or greater.")

        column_names = [column.name for column in columns]
        sort_column = order_by if order_by in column_names else "id"
        direction = "DESC" if descending else "ASC"
        select_sql = ", ".join(self._quote_identifier(column) for column in column_names)
        order_sql = self._order_expression(sort_column, columns)
        query = f"SELECT {select_sql} FROM {self.sql_table} ORDER BY {order_sql} {direction}"
        parameters: list[int] = []
        if limit is not None:
            query = f"{query} LIMIT ? OFFSET ?"
            parameters = [limit, offset]

        with closing(self._connect()) as connection, connection:
            rows = connection.execute(query, parameters).fetchall()

        metadata_rows: list[MetadataRow] = []
        for raw_row in rows:
            row_values = dict(zip(column_names, raw_row, strict=True))
            metadata_id = int(row_values.pop("id"))
            metadata_rows.append(
                MetadataRow(
                    id=metadata_id,
                    values={key: decode_value(value) for key, value in row_values.items()},
                )
            )
        return metadata_rows

    def fetch_row(self, metadata_id: int) -> MetadataRow | None:
        """Read one canonical row by primary key, without scanning preceding rows."""
        columns = [column.name for column in self.list_columns()]
        if not columns:
            return None
        select_sql = ", ".join(self._quote_identifier(column) for column in columns)
        with closing(self._connect()) as connection:
            raw_row = connection.execute(
                f"SELECT {select_sql} FROM {self.sql_table} WHERE id = ?",
                (int(metadata_id),),
            ).fetchone()
        if raw_row is None:
            return None
        values = dict(zip(columns, raw_row, strict=True))
        return MetadataRow(
            id=int(values.pop("id")),
            values={key: decode_value(value) for key, value in values.items()},
        )

    def iter_rows(self) -> Iterator[MetadataRow]:
        """Yield canonical metadata rows without materializing the table."""

        columns = self.list_columns()
        if not columns:
            return
        names = [column.name for column in columns]
        select_sql = ", ".join(self._quote_identifier(name) for name in names)
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(f"SELECT {select_sql} FROM {self.sql_table} ORDER BY id")
            for raw in cursor:
                values = dict(zip(names, raw, strict=True))
                metadata_id = int(values.pop("id"))
                yield MetadataRow(
                    id=metadata_id,
                    values={key: decode_value(value) for key, value in values.items()},
                )

    def row_offset_for_id(
        self,
        metadata_id: int,
        *,
        order_by: str = "id",
        descending: bool = False,
    ) -> int | None:
        """Return a metadata row's zero-based offset in the requested table order."""

        columns = self.list_columns()
        if not columns:
            return None
        column_names = [column.name for column in columns]
        sort_column = order_by if order_by in column_names else "id"
        direction = "DESC" if descending else "ASC"
        order_sql = self._order_expression(sort_column, columns)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                f"""
                SELECT row_offset
                FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        ORDER BY {order_sql} {direction}
                    ) - 1 AS row_offset
                    FROM {self.sql_table}
                )
                WHERE id = ?
                """,
                (int(metadata_id),),
            ).fetchone()
        return None if row is None else int(row[0])

    def update_cell(self, row_id: int, column_name: str, value: MetadataValue) -> None:
        if row_id < 1:
            raise ValueError("Metadata row id must be 1 or greater.")
        if column_name == "id":
            raise ValueError("The internal metadata ID column cannot be edited.")

        column_names = [column.name for column in self.list_columns()]
        if column_name not in column_names:
            raise ValueError(f"Unknown metadata column: {column_name}")

        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                f"UPDATE {self.sql_table} SET {self._quote_identifier(column_name)} = ? WHERE id = ?",
                (encode_value(value), row_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Metadata row {row_id} does not exist.")
            self.bump_revision(connection)
            connection.commit()
        self.recompute_column_summaries()

    def rename_column(self, old_name: str, new_name: str) -> None:
        old_name = old_name.strip()
        new_name = new_name.strip()
        if old_name == "id" or new_name == "id":
            raise ValueError("The internal metadata ID column cannot be renamed or replaced.")

        with closing(self._connect()) as connection, connection:
            column_names = self._column_names(connection)
            if old_name not in column_names:
                raise ValueError(f"Unknown metadata column: {old_name}")
            if new_name in column_names:
                raise ValueError(f"Metadata column already exists: {new_name}")
            connection.execute(
                f"ALTER TABLE {self.sql_table} RENAME COLUMN {self._quote_identifier(old_name)} "
                f"TO {self._quote_identifier(new_name)}"
            )
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata_field_ids'").fetchone():
                connection.execute("UPDATE metadata_field_ids SET name=? WHERE table_id=? AND name=?", (new_name,self.table_id,old_name))
            self.bump_revision(connection)
            connection.commit()

        self.recompute_column_summaries()

    def delete_column(self, column_name: str) -> None:
        column_name = column_name.strip()
        if column_name == "id":
            raise ValueError("The internal metadata ID column cannot be deleted.")

        with closing(self._connect()) as connection, connection:
            column_names = self._column_names(connection)
            if column_name not in column_names:
                raise ValueError(f"Unknown metadata column: {column_name}")
            self._rebuild_without_columns(connection, [column_name])
            self.bump_revision(connection)
            connection.commit()

        self.recompute_column_summaries()

    def column_values_by_id(self, column_name: str) -> dict[int, MetadataValue]:
        return dict(self.column_items(column_name))

    def column_has_lists(self, column_name: str) -> bool:
        self._validate_column_name(column_name)
        with closing(self._connect()) as connection:
            return connection.execute(
                f"SELECT 1 FROM {self.sql_table} WHERE typeof({self._quote_identifier(column_name)})='blob' LIMIT 1"
            ).fetchone() is not None

    def column_items(self, column_name: str, *, should_cancel=None) -> list[tuple[int, MetadataValue]]:
        """Return one metadata column ordered by canonical metadata ID."""

        self._validate_column_name(column_name)
        with closing(self._connect()) as connection, connection:
            if should_cancel:
                connection.set_progress_handler(lambda: int(should_cancel()), 1000)
            cursor = connection.execute(
                f"SELECT id, {self._quote_identifier(column_name)} FROM {self.sql_table} ORDER BY id"
            )
            values = []
            while rows := cursor.fetchmany(2000):
                if should_cancel and should_cancel():
                    raise RuntimeError("Media operation cancelled.")
                values.extend((int(row[0]), decode_value(row[1]) if isinstance(row[1], bytes)
                               else display_value(row[1]).strip()) for row in rows)
        return values

    def column_fingerprint(self, column_name: str) -> str:
        """Return a bounded-memory identity snapshot for one metadata column."""

        self._validate_column_name(column_name)
        quoted = self._quote_identifier(column_name)
        digest = hashlib.sha256()
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                f"SELECT id, {quoted} FROM {self.sql_table} ORDER BY id"
            )
            for row_id, raw_value in cursor:
                value = "" if raw_value is None else str(raw_value).strip()
                digest.update(str(int(row_id)).encode("ascii"))
                digest.update(b"\0")
                digest.update(value.encode("utf-8"))
                digest.update(b"\n")
        return digest.hexdigest()

    def metadata_fingerprint(self, column_name: str | None = None) -> str:
        """Return a bounded-memory snapshot of the complete table or one column."""

        if not self._database_path.exists():
            return hashlib.sha256().hexdigest()
        if column_name:
            return self.column_fingerprint(column_name)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN")
            return self.metadata_fingerprint_in_connection(connection)

    def metadata_fingerprint_in_connection(self, connection: sqlite3.Connection) -> str:
        """Read the live table through its transaction owner, including during publication.

        Large writes can spill SQLite's cache and block other connections. Never
        interpret a failed read through another connection as an empty table.
        """
        digest = hashlib.sha256()
        if not connection.execute(f"SELECT 1 FROM main.sqlite_master WHERE name='{self.sql_table}'").fetchone():
            return digest.hexdigest()
        # Allocation history and type changes invalidate staged imports even
        # when the visible cell values have returned to their previous state.
        for table, order in (("metadata_state", "key"), (self.sql_schema, "name"), ("sqlite_sequence", "name")):
            if connection.execute("SELECT 1 FROM main.sqlite_master WHERE name=?", (table,)).fetchone():
                where = f" WHERE name IN ('{self.table_id}','project_items','source_record_indexes')" if table == "sqlite_sequence" else ""
                rows = [tuple(row) for row in connection.execute(f"SELECT * FROM main.{table}{where} ORDER BY {order}")]
                digest.update(json.dumps(rows, ensure_ascii=False).encode("utf-8"))
        columns = self._column_names(connection)
        for column in columns:
            digest.update(column.encode("utf-8"))
            digest.update(b"\0")
        quoted = ", ".join(self._quote_identifier(column) for column in columns)
        for row in connection.execute(f"SELECT {quoted} FROM main.{self.sql_table} ORDER BY id"):
            for raw_value in row:
                value = "" if raw_value is None else str(raw_value)
                encoded = value.encode("utf-8")
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
        return digest.hexdigest()

    def id_high_water_mark(self) -> int:
        with closing(self._connect()) as connection, connection:
            self._ensure_schema_tables(connection)
            return self._id_high_water_in_connection(connection)

    def complete_unique_column_names(self, selected_columns=None) -> tuple[str, ...]:
        """Return nonempty columns whose normalized values identify every row."""

        choices: list[str] = []
        with closing(self._connect()) as connection, connection:
            for column in self.list_columns():
                if column.value_type == "list":
                    continue
                if selected_columns is not None and column.name not in selected_columns:
                    continue
                quoted = self._quote_identifier(column.name)
                row = connection.execute(
                    f"""
                    SELECT COUNT(*),
                           COUNT(DISTINCT TRIM(COALESCE(CAST({quoted} AS TEXT), ''))),
                           SUM(CASE WHEN TRIM(COALESCE(CAST({quoted} AS TEXT), '')) = '' THEN 1 ELSE 0 END)
                    FROM {self.sql_table}
                    """
                ).fetchone()
                total = int(row[0]) if row else 0
                distinct = int(row[1]) if row else 0
                empty = int(row[2] or 0) if row else 0
                if total > 0 and distinct == total and empty == 0:
                    choices.append(column.name)
        return tuple(choices)

    def distinct_column_values(
        self,
        column_name: str,
        *,
        include_empty: bool = False,
    ) -> list[str]:
        """Return distinct values for one column without loading complete rows."""

        self._validate_column_name(column_name)
        quoted = self._quote_identifier(column_name)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                f"SELECT DISTINCT {quoted} FROM {self.sql_table} ORDER BY {quoted}"
            ).fetchall()
        values = sorted({value.strip() for row in rows for value in scalar_values(decode_value(row[0]))})
        return values if include_empty else [value for value in values if value]

    def column_value_counts(self, column_name: str) -> list[tuple[str, int]]:
        """Return value frequencies for one column using SQLite aggregation."""

        self._validate_column_name(column_name)
        quoted = self._quote_identifier(column_name)
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                f"SELECT {quoted}, COUNT(*) AS value_count "
                f"FROM {self.sql_table} GROUP BY {quoted} "
                "ORDER BY value_count DESC, 1 ASC"
            ).fetchall()
        counts = {}
        for raw, count in rows:
            for value in set(scalar_values(decode_value(raw))):
                value = value.strip()
                counts[value] = counts.get(value, 0) + int(count)
        return sorted(counts.items(), key=lambda item: (-item[1], item[0]))

    def add_identifier_rows(self, column_name: str, values: list[str]) -> int:
        column_name = column_name.strip()
        if not column_name or column_name == "id":
            raise ValueError("Choose an editable metadata identifier column.")
        normalized_values = [str(value).strip() for value in values if str(value).strip()]
        if not normalized_values:
            return 0
        column_names = [column.name for column in self.list_columns()]
        if column_name not in column_names:
            raise ValueError(f"Unknown metadata column: {column_name}")

        with closing(self._connect()) as connection, connection:
            first = self._id_high_water_in_connection(connection) + 1
            connection.executemany(
                f"INSERT INTO {self.sql_table} (id,{self._quote_identifier(column_name)}) VALUES (?,?)",
                ((first + index,value) for index,value in enumerate(normalized_values)),
            )
            self.bump_revision(connection)
            connection.commit()

        self.recompute_column_summaries()
        return len(normalized_values)

    def merge_add_columns(
        self,
        updates_by_row_id: dict[int, dict[str, str]],
        columns_to_add: list[str],
        fill_value: str,
    ) -> None:
        if any(column == "id" for column in columns_to_add):
            raise ValueError("The internal metadata ID column cannot be added or edited.")
        for values in updates_by_row_id.values():
            if "id" in values:
                raise ValueError("The internal metadata ID column cannot be added or edited.")

        with closing(self._connect()) as connection, connection:
            existing_columns = self._column_names(connection)
            for column in columns_to_add:
                if column not in existing_columns:
                    connection.execute(f"ALTER TABLE {self.sql_table} ADD COLUMN {self._quote_identifier(column)} TEXT")
                    existing_columns.append(column)
                    connection.execute(
                        f"UPDATE {self.sql_table} SET {self._quote_identifier(column)} = ?",
                        (str(fill_value),),
                    )
            for row_id, values in updates_by_row_id.items():
                self._update_row_values(connection, row_id, values)
            self.bump_revision(connection)
            connection.commit()

        self.recompute_column_summaries()

    def merge_add_rows(
        self,
        updates_by_row_id: dict[int, dict[str, str]],
        rows_to_insert: list[dict[str, str]],
        columns_to_add: list[str],
        columns_to_remove: list[str] | None = None,
    ) -> None:
        protected = {"id"}
        if protected.intersection(columns_to_add) or protected.intersection(columns_to_remove or []):
            raise ValueError("The internal metadata ID column cannot be added, removed, or edited.")
        if any("id" in values for values in updates_by_row_id.values()):
            raise ValueError("The internal metadata ID column cannot be added, removed, or edited.")
        if any("id" in row for row in rows_to_insert):
            raise ValueError("The internal metadata ID column cannot be added, removed, or edited.")

        with closing(self._connect()) as connection, connection:
            existing_columns = self._column_names(connection)
            for column in columns_to_add:
                if column not in existing_columns:
                    connection.execute(f"ALTER TABLE {self.sql_table} ADD COLUMN {self._quote_identifier(column)} TEXT")
                    existing_columns.append(column)
            if columns_to_remove:
                existing_columns = self._rebuild_without_columns(connection, columns_to_remove)
            current_value_columns = [column for column in existing_columns if column != "id"]
            for row_id, values in updates_by_row_id.items():
                self._update_row_values(connection, row_id, values)
            if rows_to_insert:
                self._ensure_item_registry(connection)
                connection.execute('UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=?',
                                   (self._registry_high_water(connection), self.table_id))
                insert_columns = [
                    column
                    for column in current_value_columns
                    if any(column in row for row in rows_to_insert)
                ]
                if insert_columns:
                    column_sql = ", ".join(self._quote_identifier(column) for column in insert_columns)
                    placeholders = ", ".join("?" for _ in insert_columns)
                    query = f"INSERT INTO {self.sql_table} ({column_sql}) VALUES ({placeholders})"
                    for row in rows_to_insert:
                        values = [encode_value(row.get(column, "")) for column in insert_columns]
                        connection.execute(query, values)
            self.bump_revision(connection)
            connection.commit()

        self.recompute_column_summaries()

    def recompute_column_summaries(self) -> None:
        with closing(self._connect()) as connection, connection:
            columns = [column for column in self._column_names(connection) if column != "id"]
            stats = {column: _ColumnStats() for column in columns}
            if columns:
                select_sql = ", ".join(self._quote_identifier(column) for column in columns)
                for row in connection.execute(f"SELECT {select_sql} FROM {self.sql_table}"):
                    for index, column in enumerate(columns):
                        value = decode_value(row[index])
                        stats[column].observe(value)

            self._ensure_schema_tables(connection)
            connection.execute(f"DELETE FROM {self.sql_schema}")
            for position, column in enumerate(columns):
                connection.execute(
                    f"""
                    INSERT INTO {self.sql_schema}
                        (name, position, value_type, nullable, unique_values)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        column,
                        position,
                        self._canonical_schema_type(stats[column].infer_type()),
                        1,
                        stats[column].unique_count,
                    ),
                )
            connection.commit()

        self._write_column_summary_mirror(
            {
                column: {
                    "type": stats[column].infer_type(),
                    "unique_values": stats[column].unique_count,
                }
                for column in columns
            }
        )

    def ensure_column_summaries(self) -> None:
        """Backfill the authoritative schema and compatibility mirror when absent."""

        with closing(self._connect()) as connection, connection:
            self._ensure_schema_tables(connection)
            column_count = int(connection.execute(
                f"SELECT COUNT(*) FROM {self.sql_schema}"
            ).fetchone()[0])
        try:
            payload = json_store.read_json(self._columns_path, default={}) or {}
        except (OSError, ValueError):
            payload = {}
        if not column_count:
            self.recompute_column_summaries()
        elif not payload:
            try:
                self._write_schema_mirror_from_database()
            except OSError:
                pass

    def publish_staged_import(
        self,
        staging_database: Path,
        columns: list[str],
        schemas: Iterable["MetadataColumnSchema"],
        *,
        groups_database: Path,
        media_database: Path,
        should_cancel: Callable[[], bool] | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
        validate_publication: Callable[[sqlite3.Connection], None] | None = None,
        source_provenance: tuple[str, str] | None = None,
    ) -> None:
        """Publish a validated staged table without changing any item associations."""

        if not columns:
            raise ValueError("Metadata import did not contain any importable columns.")
        schema_by_name = {schema.name: schema for schema in schemas}
        connection = self._connect()
        attached: list[str] = []
        try:
            connection.execute("ATTACH DATABASE ? AS import_stage", (str(staging_database),))
            attached.append("import_stage")
            if Path(groups_database).exists():
                connection.execute("ATTACH DATABASE ? AS groups_store", (str(groups_database),))
                attached.append("groups_store")
            if Path(media_database).exists():
                connection.execute("ATTACH DATABASE ? AS media_store", (str(media_database),))
                attached.append("media_store")
            connection.execute("BEGIN IMMEDIATE")
            if validate_publication is not None:
                validate_publication(connection)
            self._ensure_schema_tables(connection)
            previous_high_water = self._id_high_water_in_connection(connection)
            definitions = ", ".join(
                f"{self._quote_identifier(column)} TEXT" for column in columns
            )
            connection.execute("DROP TABLE IF EXISTS metadata_import_next")
            connection.execute(
                f"CREATE TABLE metadata_import_next (id INTEGER PRIMARY KEY AUTOINCREMENT, {definitions})"
            )
            column_sql = ", ".join(self._quote_identifier(column) for column in columns)
            placeholders = ", ".join("?" for _ in range(len(columns) + 1))
            source = connection.execute(
                "SELECT metadata_id, payload_json FROM import_stage.final_rows ORDER BY metadata_id"
            )
            total_rows = int(connection.execute(
                "SELECT COUNT(*) FROM import_stage.final_rows"
            ).fetchone()[0])
            inserted = 0
            maximum_id = previous_high_water
            list_columns = set()
            for metadata_id, raw_payload in source:
                if should_cancel is not None and should_cancel():
                    raise InterruptedError("Metadata import was aborted before publication.")
                payload = json.loads(str(raw_payload))
                list_columns.update(name for name, value in payload.items() if isinstance(value, list))
                row_id = int(metadata_id)
                connection.execute(
                    f"INSERT INTO metadata_import_next (id, {column_sql}) VALUES ({placeholders})",
                    (row_id, *(encode_value(payload.get(column, "")) for column in columns)),
                )
                inserted += 1
                maximum_id = max(maximum_id, row_id)
                if progress_callback is not None and (inserted % 1000 == 0 or inserted == total_rows):
                    progress_callback(inserted, total_rows)
            if not inserted:
                raise ValueError("The import plan would produce an empty metadata table.")

            if should_cancel is not None and should_cancel():
                raise InterruptedError("Metadata import was aborted before publication.")
            if validate_publication is not None:
                validate_publication(connection)
            connection.execute(f"DROP TABLE {self.sql_table}")
            connection.execute(f"ALTER TABLE metadata_import_next RENAME TO {self.sql_table}")
            self._ensure_item_registry(connection)
            connection.execute(f"DELETE FROM sqlite_sequence WHERE name = '{self.sql_table}'")
            connection.execute(
                f"INSERT INTO sqlite_sequence (name, seq) VALUES ('{self.sql_table}', ?)",
                (maximum_id,),
            )
            connection.execute(f"DELETE FROM {self.sql_schema}")
            for position, column in enumerate(columns):
                schema = schema_by_name.get(column)
                unique = int(connection.execute(
                    f"SELECT COUNT(DISTINCT {self._quote_identifier(column)}) FROM {self.sql_table} "
                    f"WHERE TRIM(COALESCE({self._quote_identifier(column)}, '')) != ''"
                ).fetchone()[0])
                nullable = int(connection.execute(
                    f"SELECT COUNT(*) FROM {self.sql_table} WHERE TRIM(COALESCE({self._quote_identifier(column)}, '')) = ''"
                ).fetchone()[0]) > 0
                connection.execute(
                    f"""
                    INSERT INTO {self.sql_schema}
                        (name, position, value_type, nullable, unique_values)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        column,
                        position,
                        "list" if column in list_columns else str(getattr(schema, "value_type", "text")),
                        1 if nullable else 0,
                        unique,
                    ),
                )
            connection.execute(
                "INSERT INTO metadata_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (self._allocation_key, str(maximum_id)),
            )
            if source_provenance is not None:
                from MetadataArea.source_repository import MetadataSourceRepository
                MetadataSourceRepository.publish(connection, self.table_id, *source_provenance)
            self.bump_revision(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            for name in reversed(attached):
                try:
                    connection.execute(f"DETACH DATABASE {name}")
                except sqlite3.Error:
                    pass
            connection.close()
        try:
            self._write_schema_mirror_from_database()
        except OSError:
            # The SQLite schema is authoritative. A later repository open repairs
            # this derived compatibility file via ensure_column_summaries().
            pass

    def search_row_offsets(
        self,
        query: str,
        regex: bool = False,
        match_case: bool = False,
        match_word: bool = False,
        order_by: str = "id",
        descending: bool = False,
        search_column: str | None = None,
    ) -> list[int]:
        return [
            match.row_offset
            for match in self.search_matches(
                query,
                regex=regex,
                match_case=match_case,
                match_word=match_word,
                order_by=order_by,
                descending=descending,
                search_column=search_column,
            )
        ]

    def search_matches(
        self,
        query: str,
        regex: bool = False,
        match_case: bool = False,
        match_word: bool = False,
        order_by: str = "id",
        descending: bool = False,
        search_column: str | None = None,
    ) -> list[MetadataSearchMatch]:
        columns = self.list_columns()
        if not columns:
            return []

        column_names = [column.name for column in columns]
        if search_column is not None and search_column not in column_names:
            raise ValueError(f"Unknown metadata search column: {search_column}")
        sort_column = order_by if order_by in column_names else "id"
        direction = "DESC" if descending else "ASC"
        select_sql = ", ".join(self._quote_identifier(column) for column in column_names)
        order_sql = self._order_expression(sort_column, columns)
        search_indices = (
            [column_names.index(search_column)]
            if search_column is not None
            else list(range(len(column_names)))
        )

        pattern = self._compile_search_pattern(query, regex=regex, match_case=match_case, match_word=match_word)
        matches = lambda value: pattern.search(value) is not None

        search_matches: list[MetadataSearchMatch] = []
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(f"SELECT {select_sql} FROM {self.sql_table} ORDER BY {order_sql} {direction}")
            for row_offset, raw_row in enumerate(cursor):
                matching_columns = tuple(
                    index
                    for index in search_indices
                    if matches(display_value(decode_value(raw_row[index])))
                )
                if matching_columns:
                    search_matches.append(
                        MetadataSearchMatch(
                            row_offset=row_offset,
                            column_indexes=matching_columns,
                        )
                    )
        return search_matches

    @staticmethod
    def _compile_search_pattern(
        query: str,
        regex: bool,
        match_case: bool,
        match_word: bool,
    ) -> re.Pattern[str]:
        pattern_text = query if regex else re.escape(query)
        if match_word:
            pattern_text = rf"\b(?:{pattern_text})\b"
        flags = 0 if match_case else re.IGNORECASE
        return re.compile(pattern_text, flags)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._database_path)

    def _ensure_schema_tables(self, connection: sqlite3.Connection) -> None:
        self._ensure_item_registry(connection)
        # Avoid executescript here: it can commit an active transaction before
        # running the statements, which would break atomic import publication.
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.sql_schema} (
              name TEXT PRIMARY KEY,
              position INTEGER NOT NULL,
              value_type TEXT NOT NULL DEFAULT 'text',
              nullable INTEGER NOT NULL DEFAULT 1,
              unique_values INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata_state (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            )
            """
        )

    @property
    def _allocation_key(self):
        return 'id_high_water' if self.table_id == 'metadata' else 'source_index_high_water'

    def _registry_high_water(self, connection):
        return item_high_water(connection) if self.table_id == 'metadata' else record_high_water(connection)

    def _ensure_item_registry(self, connection):
        ensure_item_registry(connection)
        if self.table_id == 'metadata':
            return
        ensure_record_registry(connection)
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (self.table_id,)).fetchone():
            return
        trigger_name = f'{self.table_id}_identity'
        trigger = connection.execute('SELECT sql FROM sqlite_master WHERE name=?', (trigger_name,)).fetchone()
        if trigger and 'source_record_indexes' in trigger[0]:
            return
        connection.execute(f'DROP TRIGGER IF EXISTS {trigger_name}')
        connection.execute(f'INSERT OR IGNORE INTO source_record_indexes(id) SELECT id FROM {self.sql_table}')
        connection.execute(f'''CREATE TRIGGER {trigger_name} AFTER INSERT ON {self.sql_table} BEGIN
            INSERT OR IGNORE INTO source_record_indexes(id) VALUES (NEW.id); END''')
        connection.execute('DELETE FROM sqlite_sequence WHERE name=?', (self.table_id,))
        connection.execute('INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)', (self.table_id, record_high_water(connection)))

    def _id_high_water_in_connection(self, connection: sqlite3.Connection) -> int:
        row = connection.execute('SELECT value FROM metadata_state WHERE key=?', (self._allocation_key,)).fetchone()
        exists = connection.execute('SELECT 1 FROM sqlite_master WHERE name=?', (self.table_id,)).fetchone()
        maximum = connection.execute(f'SELECT COALESCE(MAX(id),0) FROM {self.sql_table}').fetchone()[0] if exists else 0
        return max(self._registry_high_water(connection), int(row[0]) if row else 0, int(maximum))

    def _write_schema_mirror_from_database(self) -> None:
        with closing(self._connect()) as connection, connection:
            self._ensure_schema_tables(connection)
            rows = connection.execute(
                f"SELECT name, value_type, unique_values FROM {self.sql_schema} ORDER BY position"
            ).fetchall()
        self._write_column_summary_mirror({
            str(name): {
                "type": self._compatibility_mirror_type(str(value_type)),
                "unique_values": int(unique),
            }
            for name, value_type, unique in rows
        })

    def _write_column_summary_mirror(self, payload: dict[str, dict[str, object]]) -> None:
        json_store.write_json(self._columns_path, payload)

    def _column_names(self, connection: sqlite3.Connection) -> list[str]:
        return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({self.sql_table})").fetchall()]

    def _validate_column_name(self, column_name: str) -> None:
        if column_name not in {column.name for column in self.list_columns()}:
            raise ValueError(f"Unknown metadata column: {column_name}")

    def _update_row_values(
        self,
        connection: sqlite3.Connection,
        row_id: int,
        values: dict[str, str],
    ) -> None:
        if not values:
            return
        assignments = ", ".join(f"{self._quote_identifier(column)} = ?" for column in values)
        parameters = [encode_value(value) for value in values.values()]
        cursor = connection.execute(
            f"UPDATE {self.sql_table} SET {assignments} WHERE id = ?",
            (*parameters, row_id),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Metadata row {row_id} does not exist.")

    def _rebuild_without_columns(
        self,
        connection: sqlite3.Connection,
        columns_to_remove: list[str],
    ) -> list[str]:
        remove_set = set(columns_to_remove)
        source_columns = self._column_names(connection)
        kept_columns = [column for column in source_columns if column not in remove_set]
        value_columns = [column for column in kept_columns if column != "id"]
        value_definitions = ", ".join(f"{self._quote_identifier(column)} TEXT" for column in value_columns)
        create_sql = "CREATE TABLE metadata_rebuild (id INTEGER PRIMARY KEY AUTOINCREMENT"
        if value_definitions:
            create_sql = f"{create_sql}, {value_definitions}"
        create_sql = f"{create_sql})"
        select_sql = ", ".join(self._quote_identifier(column) for column in kept_columns)
        connection.execute("DROP TABLE IF EXISTS metadata_rebuild")
        connection.execute(create_sql)
        connection.execute(f"INSERT INTO metadata_rebuild ({select_sql}) SELECT {select_sql} FROM {self.sql_table}")
        connection.execute(f"DROP TABLE {self.sql_table}")
        connection.execute(f"ALTER TABLE metadata_rebuild RENAME TO {self.sql_table}")
        self._ensure_item_registry(connection)
        return kept_columns

    @staticmethod
    def _quote_identifier(identifier: str) -> str:
        safe = identifier.replace('"', '""')
        return f'"{safe}"'

    @staticmethod
    def _canonical_schema_type(value_type: str) -> str:
        return {
            "string": "text",
            "int": "integer",
        }.get(str(value_type).lower(), str(value_type).lower())

    @staticmethod
    def _compatibility_mirror_type(value_type: str) -> str:
        return {
            "text": "string",
            "integer": "int",
            "boolean": "string",
            "date": "string",
            "datetime": "string",
        }.get(str(value_type).lower(), str(value_type).lower())

    def _order_expression(self, column_name: str, columns: list[MetadataColumn]) -> str:
        quoted = self._quote_identifier(column_name)
        value_type = next((column.value_type.lower() for column in columns if column.name == column_name), "text")
        if column_name == "id":
            return quoted  # INTEGER PRIMARY KEY already has the required order/index.
        if value_type in {"int", "integer"}:
            return f"CAST({quoted} AS INTEGER)"
        if value_type == "float":
            return f"CAST({quoted} AS REAL)"
        return quoted


@dataclass
class _ColumnStats:
    unique_values: set[str] = field(default_factory=set)
    non_empty: int = 0
    all_int: bool = True
    all_float: bool = True
    list_like_hits: int = 0
    has_lists: bool = False

    @property
    def unique_count(self) -> int:
        return len(self.unique_values)

    def observe(self, value: MetadataValue) -> None:
        if isinstance(value, list):
            self.has_lists = True
            self.unique_values.update(scalar_values(value))
            return
        normalized = value.strip()
        if not normalized:
            return
        self.non_empty += 1
        self.unique_values.add(normalized)
        if not self._is_int(normalized):
            self.all_int = False
        if not self._is_float(normalized):
            self.all_float = False
        if normalized.startswith("[") and normalized.endswith("]") or any(sep in normalized for sep in (";", "|")):
            self.list_like_hits += 1

    def infer_type(self) -> str:
        if self.has_lists:
            return "list"
        if self.non_empty == 0:
            return "string"
        if self.all_int:
            return "int"
        if self.all_float:
            return "float"
        if self.list_like_hits >= max(1, self.non_empty // 2):
            return "list"
        return "string"

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
