from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from MetadataArea.domain import MetadataGroup, MetadataGroupSet, MetadataGroupSetMode


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class MetadataGroupRepository:
    """SQLite persistence for normalized metadata group memberships."""

    _BATCH_SIZE = 800

    def __init__(self, database_path: Path) -> None:
        self._database_path = Path(database_path)

    @property
    def database_path(self) -> Path:
        return self._database_path

    @contextmanager
    def transaction(self):
        """One transaction shared with producer-owned records in this database."""
        with self._connect() as connection:
            self._initialize(connection)
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    def publish_exclusive_groups(self, connection, name, producer_key, assignments):
        """Publish a complete new producer set using the caller's transaction.

        assignments maps display labels to canonical metadata IDs. This method
        never commits, so producer records and memberships become visible together.
        """
        existing = connection.execute(
            "SELECT group_set_id FROM group_sets WHERE producer_key = ?", (producer_key,)
        ).fetchone()
        if existing:
            raise ValueError("This clustering result has already published groups.")
        group_set_id = uuid4().hex
        name = self._available_name(connection, "group_sets", self._required_name(name, "group set"))
        connection.execute(
            "INSERT INTO group_sets VALUES (?, ?, 'exclusive', ?)",
            (group_set_id, name, producer_key),
        )
        seen = set()
        for label, ids in assignments.items():
            ids = tuple(int(value) for value in ids)
            if seen.intersection(ids) or len(set(ids)) != len(ids):
                raise ValueError("Exclusive groups cannot contain duplicate metadata IDs.")
            seen.update(ids)
            group_id = uuid4().hex
            connection.execute("INSERT INTO groups VALUES (?, ?, ?, ?)",
                               (group_id, group_set_id, label, f"{producer_key}:{label}"))
            for offset in range(0, len(ids), self._BATCH_SIZE):
                connection.executemany(
                    "INSERT INTO group_memberships VALUES (?, ?)",
                    ((group_id, value) for value in ids[offset:offset+self._BATCH_SIZE]),
                )
        return group_set_id

    def list_group_sets(self) -> list[MetadataGroupSet]:
        if not self._database_path.exists():
            return []
        with self._connect() as connection:
            self._initialize(connection)
            rows = connection.execute(
                "SELECT group_set_id, name, mode FROM group_sets ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [
            MetadataGroupSet(str(row[0]), str(row[1]), MetadataGroupSetMode(str(row[2])))
            for row in rows
        ]

    def create_group_set(self, name: str, mode: MetadataGroupSetMode) -> MetadataGroupSet:
        normalized = self._required_name(name, "group set")
        group_set_id = uuid4().hex
        with self._connect() as connection:
            self._initialize(connection)
            try:
                connection.execute(
                    "INSERT INTO group_sets (group_set_id, name, mode) VALUES (?, ?, ?)",
                    (group_set_id, normalized, mode.value),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"A group set named '{normalized}' already exists.") from exc
        return MetadataGroupSet(group_set_id, normalized, mode)

    def ensure_producer_group_set(
        self,
        name: str,
        mode: MetadataGroupSetMode,
        producer_key: str,
    ) -> MetadataGroupSet:
        """Return the stable set owned by a feature, creating it when needed."""

        normalized = self._required_name(name, "group set")
        key = self._required_name(producer_key, "producer key")
        with self._connect() as connection:
            self._initialize(connection)
            row = connection.execute(
                "SELECT group_set_id, name, mode FROM group_sets WHERE producer_key = ?",
                (key,),
            ).fetchone()
            if row is not None:
                if str(row[2]) != mode.value:
                    raise ValueError("A producer-owned group set cannot change membership mode.")
                return MetadataGroupSet(str(row[0]), str(row[1]), mode)
            group_set_id = uuid4().hex
            available_name = self._available_name(connection, "group_sets", normalized)
            connection.execute(
                "INSERT INTO group_sets (group_set_id, name, mode, producer_key) VALUES (?, ?, ?, ?)",
                (group_set_id, available_name, mode.value, key),
            )
            connection.commit()
        return MetadataGroupSet(group_set_id, available_name, mode)

    def rename_group_set(self, group_set_id: str, name: str) -> None:
        normalized = self._required_name(name, "group set")
        with self._connect() as connection:
            self._initialize(connection)
            try:
                cursor = connection.execute(
                    "UPDATE group_sets SET name = ? WHERE group_set_id = ?",
                    (normalized, str(group_set_id)),
                )
                if cursor.rowcount == 0:
                    raise ValueError("The group set no longer exists.")
                connection.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"A group set named '{normalized}' already exists.") from exc

    def delete_group_set(self, group_set_id: str) -> None:
        self._delete("group_sets", "group_set_id", group_set_id)

    def list_groups(self, group_set_id: str | None = None) -> list[MetadataGroup]:
        if not self._database_path.exists():
            return []
        parameters: tuple[str, ...] = ()
        where = ""
        if group_set_id is not None:
            where = "WHERE g.group_set_id = ?"
            parameters = (str(group_set_id),)
        with self._connect() as connection:
            self._initialize(connection)
            rows = connection.execute(
                f"""
                SELECT g.group_id, g.group_set_id, g.name, COUNT(m.metadata_id)
                FROM groups AS g
                LEFT JOIN group_memberships AS m ON m.group_id = g.group_id
                {where}
                GROUP BY g.group_id, g.group_set_id, g.name
                ORDER BY g.name COLLATE NOCASE
                """,
                parameters,
            ).fetchall()
        return [MetadataGroup(str(row[0]), str(row[1]), str(row[2]), int(row[3])) for row in rows]

    def create_group(self, group_set_id: str, name: str) -> MetadataGroup:
        normalized = self._required_name(name, "group")
        group_id = uuid4().hex
        with self._connect() as connection:
            self._initialize(connection)
            try:
                connection.execute(
                    "INSERT INTO groups (group_id, group_set_id, name) VALUES (?, ?, ?)",
                    (group_id, str(group_set_id), normalized),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"A group named '{normalized}' already exists in this set.") from exc
        return MetadataGroup(group_id, str(group_set_id), normalized, 0)

    def ensure_producer_group(
        self,
        group_set_id: str,
        name: str,
        producer_key: str,
        *,
        rename_existing: bool = False,
    ) -> MetadataGroup:
        """Return a feature-owned group without relying on its editable name."""

        normalized = self._required_name(name, "group")
        key = self._required_name(producer_key, "producer key")
        with self._connect() as connection:
            self._initialize(connection)
            row = connection.execute(
                "SELECT group_id, group_set_id, name FROM groups WHERE producer_key = ?",
                (key,),
            ).fetchone()
            if row is not None:
                if str(row[1]) != str(group_set_id):
                    raise ValueError("The producer-owned group belongs to another set.")
                group_id = str(row[0])
                existing_name = str(row[2])
                if rename_existing and existing_name != normalized:
                    try:
                        connection.execute(
                            "UPDATE groups SET name = ? WHERE group_id = ?",
                            (normalized, group_id),
                        )
                        connection.commit()
                        existing_name = normalized
                    except sqlite3.IntegrityError as exc:
                        raise ValueError(
                            f"A group named '{normalized}' already exists in this set."
                        ) from exc
                count = connection.execute(
                    "SELECT COUNT(*) FROM group_memberships WHERE group_id = ?", (group_id,)
                ).fetchone()
                return MetadataGroup(group_id, str(group_set_id), existing_name, int(count[0]))
            group_id = uuid4().hex
            available_name = self._available_name(
                connection, "groups", normalized, group_set_id=str(group_set_id)
            )
            connection.execute(
                "INSERT INTO groups (group_id, group_set_id, name, producer_key) VALUES (?, ?, ?, ?)",
                (group_id, str(group_set_id), available_name, key),
            )
            connection.commit()
        return MetadataGroup(group_id, str(group_set_id), available_name, 0)

    def delete_producer_group(self, producer_key: str) -> None:
        if not self._database_path.exists():
            return
        with self._connect() as connection:
            self._initialize(connection)
            connection.execute("DELETE FROM groups WHERE producer_key = ?", (str(producer_key),))
            connection.commit()

    def rename_group(self, group_id: str, name: str) -> None:
        normalized = self._required_name(name, "group")
        with self._connect() as connection:
            self._initialize(connection)
            try:
                cursor = connection.execute(
                    "UPDATE groups SET name = ? WHERE group_id = ?",
                    (normalized, str(group_id)),
                )
                if cursor.rowcount == 0:
                    raise ValueError("The group no longer exists.")
                connection.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"A group named '{normalized}' already exists in this set.") from exc

    def delete_group(self, group_id: str) -> None:
        self._delete("groups", "group_id", group_id)

    def member_ids(self, group_id: str) -> tuple[int, ...]:
        if not self._database_path.exists():
            return ()
        with self._connect() as connection:
            self._initialize(connection)
            rows = connection.execute(
                "SELECT metadata_id FROM group_memberships WHERE group_id = ? ORDER BY metadata_id",
                (str(group_id),),
            ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def group_set_member_count(self, group_set_id: str) -> int:
        if not self._database_path.exists():
            return 0
        with self._connect() as connection:
            self._initialize(connection)
            row = connection.execute(
                """
                SELECT COUNT(DISTINCT m.metadata_id)
                FROM group_memberships AS m
                JOIN groups AS g ON g.group_id = m.group_id
                WHERE g.group_set_id = ?
                """,
                (str(group_set_id),),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def apply_memberships(self, group_id: str, metadata_ids: set[int], operation: str) -> int:
        normalized_operation = str(operation).strip().lower()
        if normalized_operation not in {"replace", "add", "remove"}:
            raise ValueError(f"Unsupported membership operation: {operation}")
        target_ids = sorted({int(value) for value in metadata_ids})
        with self._connect() as connection:
            self._initialize(connection)
            row = connection.execute(
                """
                SELECT g.group_set_id, s.mode
                FROM groups AS g JOIN group_sets AS s ON s.group_set_id = g.group_set_id
                WHERE g.group_id = ?
                """,
                (str(group_id),),
            ).fetchone()
            if row is None:
                raise ValueError("The group no longer exists.")
            group_set_id, mode = str(row[0]), str(row[1])
            if normalized_operation == "replace":
                connection.execute("DELETE FROM group_memberships WHERE group_id = ?", (str(group_id),))
            if normalized_operation == "remove":
                self._delete_members(connection, str(group_id), target_ids)
            else:
                if mode == MetadataGroupSetMode.EXCLUSIVE.value:
                    self._delete_set_members(connection, group_set_id, target_ids)
                connection.executemany(
                    "INSERT OR IGNORE INTO group_memberships (group_id, metadata_id) VALUES (?, ?)",
                    [(str(group_id), metadata_id) for metadata_id in target_ids],
                )
            connection.commit()
            count = connection.execute(
                "SELECT COUNT(*) FROM group_memberships WHERE group_id = ?", (str(group_id),)
            ).fetchone()
        return int(count[0]) if count is not None else 0

    def clear(self) -> None:
        if not self._database_path.exists():
            return
        with self._connect() as connection:
            self._initialize(connection)
            connection.execute("DELETE FROM group_sets")
            connection.commit()

    def membership_rows(self, group_set_id: str | None = None) -> list[tuple[int, str, str]]:
        if not self._database_path.exists():
            return []
        where = "WHERE s.group_set_id = ?" if group_set_id is not None else ""
        parameters = (str(group_set_id),) if group_set_id is not None else ()
        with self._connect() as connection:
            self._initialize(connection)
            rows = connection.execute(
                f"""
                SELECT m.metadata_id, s.name, g.name
                FROM group_memberships AS m
                JOIN groups AS g ON g.group_id = m.group_id
                JOIN group_sets AS s ON s.group_set_id = g.group_set_id
                {where}
                ORDER BY m.metadata_id, s.name COLLATE NOCASE, g.name COLLATE NOCASE
                """,
                parameters,
            ).fetchall()
        return [(int(row[0]), str(row[1]), str(row[2])) for row in rows]

    def membership_rows_for_groups(
        self,
        group_ids: list[str] | tuple[str, ...],
    ) -> list[tuple[int, str]]:
        """Return canonical item/group pairs for an explicit group selection."""

        selected = list(dict.fromkeys(str(group_id) for group_id in group_ids if str(group_id)))
        if not self._database_path.exists() or not selected:
            return []
        rows: list[tuple[int, str]] = []
        with self._connect() as connection:
            self._initialize(connection)
            for batch in self._batches(selected):
                placeholders = ",".join("?" for _ in batch)
                batch_rows = connection.execute(
                    f"""
                    SELECT metadata_id, group_id
                    FROM group_memberships
                    WHERE group_id IN ({placeholders})
                    ORDER BY metadata_id, group_id
                    """,
                    batch,
                ).fetchall()
                rows.extend((int(row[0]), str(row[1])) for row in batch_rows)
        selected_order = {group_id: index for index, group_id in enumerate(selected)}
        return sorted(rows, key=lambda row: (row[0], selected_order[row[1]]))

    def _delete(self, table: str, key: str, value: str) -> None:
        if not self._database_path.exists():
            return
        with self._connect() as connection:
            self._initialize(connection)
            connection.execute(f"DELETE FROM {table} WHERE {key} = ?", (str(value),))
            connection.commit()

    def _delete_members(self, connection: sqlite3.Connection, group_id: str, ids: list[int]) -> None:
        for batch in self._batches(ids):
            placeholders = ",".join("?" for _ in batch)
            connection.execute(
                f"DELETE FROM group_memberships WHERE group_id = ? AND metadata_id IN ({placeholders})",
                (group_id, *batch),
            )

    def _delete_set_members(self, connection: sqlite3.Connection, set_id: str, ids: list[int]) -> None:
        for batch in self._batches(ids):
            placeholders = ",".join("?" for _ in batch)
            connection.execute(
                f"""
                DELETE FROM group_memberships
                WHERE group_id IN (SELECT group_id FROM groups WHERE group_set_id = ?)
                  AND metadata_id IN ({placeholders})
                """,
                (set_id, *batch),
            )

    def _batches(self, values: list[int] | list[str]):
        for start in range(0, len(values), self._BATCH_SIZE):
            yield values[start : start + self._BATCH_SIZE]

    def _connect(self) -> sqlite3.Connection:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._database_path, factory=_ClosingConnection)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS group_sets (
                group_set_id TEXT PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                mode TEXT NOT NULL CHECK (mode IN ('exclusive', 'overlapping')),
                producer_key TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS groups (
                group_id TEXT PRIMARY KEY,
                group_set_id TEXT NOT NULL REFERENCES group_sets(group_set_id) ON DELETE CASCADE,
                name TEXT NOT NULL COLLATE NOCASE,
                producer_key TEXT UNIQUE,
                UNIQUE (group_set_id, name)
            );
            CREATE TABLE IF NOT EXISTS group_memberships (
                group_id TEXT NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
                metadata_id INTEGER NOT NULL,
                PRIMARY KEY (group_id, metadata_id)
            );
            CREATE INDEX IF NOT EXISTS index_group_memberships_metadata_id
                ON group_memberships(metadata_id);
            """
        )
        set_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(group_sets)").fetchall()
        }
        if "producer_key" not in set_columns:
            connection.execute("ALTER TABLE group_sets ADD COLUMN producer_key TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS index_group_sets_producer_key "
                "ON group_sets(producer_key) WHERE producer_key IS NOT NULL"
            )
        group_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(groups)").fetchall()
        }
        if "producer_key" not in group_columns:
            connection.execute("ALTER TABLE groups ADD COLUMN producer_key TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS index_groups_producer_key "
                "ON groups(producer_key) WHERE producer_key IS NOT NULL"
            )

    @staticmethod
    def _required_name(value: str, kind: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"Choose a {kind} name.")
        return normalized

    @staticmethod
    def _available_name(
        connection: sqlite3.Connection,
        table: str,
        requested: str,
        *,
        group_set_id: str | None = None,
    ) -> str:
        candidate = requested
        suffix = 2
        while True:
            if group_set_id is None:
                row = connection.execute(
                    f"SELECT 1 FROM {table} WHERE name = ? COLLATE NOCASE", (candidate,)
                ).fetchone()
            else:
                row = connection.execute(
                    f"SELECT 1 FROM {table} WHERE group_set_id = ? AND name = ? COLLATE NOCASE",
                    (group_set_id, candidate),
                ).fetchone()
            if row is None:
                return candidate
            candidate = f"{requested} ({suffix})"
            suffix += 1
