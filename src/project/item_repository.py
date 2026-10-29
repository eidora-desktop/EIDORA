"""Permanent project item IDs, independent of optional metadata and media links.

The registry shares the metadata database so allocation and metadata publication
use one SQLite transaction. Existing metadata IDs are retained verbatim.
"""
from contextlib import closing
from pathlib import Path
import sqlite3

from shared.persistence.sqlite import connect_read_only


def ensure_item_registry(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS project_items ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id TEXT UNIQUE)"
    )
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata'").fetchone():
        return
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata_item_identity'").fetchone():
        return
    connection.execute("INSERT OR IGNORE INTO project_items(id) SELECT id FROM metadata")
    connection.execute(
        "CREATE TRIGGER metadata_item_identity AFTER INSERT ON metadata BEGIN "
        "INSERT OR IGNORE INTO project_items(id) VALUES (NEW.id); END"
    )
    high_water = item_high_water(connection)
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata_state'").fetchone():
        row = connection.execute("SELECT value FROM metadata_state WHERE key='id_high_water'").fetchone()
        high_water = max(high_water, int(row[0]) if row else 0)
    for name in ('metadata', 'project_items'):
        row = connection.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (name,)).fetchone()
        high_water = max(high_water, int(row[0]) if row else 0)
    for name in ('metadata', 'project_items'):
        connection.execute("DELETE FROM sqlite_sequence WHERE name=?", (name,))
        connection.execute("INSERT INTO sqlite_sequence(name,seq) VALUES (?,?)", (name, high_water))


def item_high_water(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT seq FROM sqlite_sequence WHERE name='project_items'").fetchone()
    return int(row[0]) if row else 0


class ItemRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            ensure_item_registry(connection)

    @staticmethod
    def has_items(database_path: Path) -> bool:
        """Read navigation eligibility without migrating or allocating storage."""
        if not database_path.is_file():
            return False
        with closing(connect_read_only(database_path)) as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='project_items'").fetchone():
                if connection.execute("SELECT 1 FROM project_items WHERE asset_id IS NOT NULL LIMIT 1").fetchone():
                    return True
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata'").fetchone():
                return False
            return connection.execute("SELECT 1 FROM metadata LIMIT 1").fetchone() is not None

    def active_ids(self) -> list[int]:
        """Current metadata plus asset-backed items explicitly prepared for analysis."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            has_metadata = connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata'").fetchone()
            query = "SELECT id FROM project_items WHERE asset_id IS NOT NULL"
            if has_metadata:
                query += " UNION SELECT id FROM metadata"
            return [int(row[0]) for row in connection.execute(query + " ORDER BY id")]

    def allocate_assets(self, asset_ids: list[str]) -> dict[str, int]:
        """Reuse asset analysis identities across runs without creating metadata links."""
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            ensure_item_registry(connection)
            result = {}
            for asset_id in dict.fromkeys(asset_ids):
                row = connection.execute("SELECT id FROM project_items WHERE asset_id=?", (asset_id,)).fetchone()
                if row is None:
                    cursor = connection.execute("INSERT INTO project_items(asset_id) VALUES (?)", (asset_id,))
                    result[asset_id] = int(cursor.lastrowid)
                else:
                    result[asset_id] = int(row[0])
            # Reserve the same ID range for legacy metadata INSERT callers too.
            high_water = item_high_water(connection)
            connection.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='metadata'", (high_water,))
            return result

    def asset_ids_for_items(self, item_ids) -> dict[int, str]:
        result = {}
        ids = list(dict.fromkeys(int(value) for value in item_ids))
        with closing(sqlite3.connect(self.database_path)) as connection:
            for start in range(0, len(ids), 800):
                batch = ids[start:start + 800]
                placeholders = ','.join('?' for _ in batch)
                result.update((int(row[0]), str(row[1])) for row in connection.execute(
                    f"SELECT id,asset_id FROM project_items WHERE id IN ({placeholders}) AND asset_id IS NOT NULL", batch
                ))
        return result

    def item_ids_for_assets(self, asset_ids) -> dict[str, int]:
        result = {}
        ids = list(dict.fromkeys(asset_ids))
        with closing(sqlite3.connect(self.database_path)) as connection:
            for start in range(0, len(ids), 800):
                batch = ids[start:start + 800]
                placeholders = ','.join('?' for _ in batch)
                result.update((str(row[0]), int(row[1])) for row in connection.execute(
                    f"SELECT asset_id,id FROM project_items WHERE asset_id IN ({placeholders})", batch
                ))
        return result
