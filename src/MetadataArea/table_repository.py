"""Named metadata tables and direct relationships in the existing SQLite store."""
from contextlib import closing
from pathlib import Path
import sqlite3
from MetadataArea.value_storage import encode_value
import uuid

from MetadataArea.repository import MetadataRepository
from MetadataArea.domain import MetadataTable


def list_tables(database: Path) -> tuple[MetadataTable, ...]:
    if not database.exists():
        return ()
    with closing(sqlite3.connect(database)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata_tables'").fetchone():
            return tuple(MetadataTable(*row) for row in connection.execute("SELECT id,name FROM metadata_tables ORDER BY position"))
        return (MetadataTable("metadata", "Metadata"),)


class MetadataTableRepository:
    def __init__(self, database: Path):
        self.database = Path(database)

    def linked_table_ids(self) -> set[str]:
        """Tables directly linked to Items, in either direction."""
        if not self.database.exists():
            return set()
        with closing(sqlite3.connect(self.database)) as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if not {"metadata_relationships", "metadata_record_links"} <= tables:
                return set()
            pairs = connection.execute("""SELECT source_table, target_table
                FROM metadata_relationships r
                WHERE (source_table='metadata' OR target_table='metadata')
                AND EXISTS (SELECT 1 FROM metadata_record_links l WHERE l.relationship_id=r.id)
            """)
            return {table_id for pair in pairs for table_id in pair}

    def unlink_table(self, table_id):
        """Remove a source's relationships atomically, retaining every record."""
        if table_id == 'metadata':
            raise ValueError('Select a metadata source tab to unlink.')
        repo = self.repository(table_id)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            self._ensure_catalog(connection)
            cursor = connection.execute('''DELETE FROM metadata_record_links
                WHERE relationship_id IN (SELECT id FROM metadata_relationships
                WHERE source_table=? OR target_table=?)''', (table_id, table_id))
            count = cursor.rowcount
            connection.execute('DELETE FROM metadata_relationship_keys WHERE source_table=? OR target_table=?',
                               (table_id, table_id))
            removed = connection.execute('DELETE FROM metadata_relationships WHERE source_table=? OR target_table=?',
                                         (table_id, table_id)).rowcount
            if removed:
                repo.bump_revision(connection)
        return count

    def repository(self, table_id="metadata"):
        if table_id not in {table.id for table in list_tables(self.database)}:
            raise ValueError("This metadata table no longer exists.")
        return MetadataRepository(self.database, table_id=table_id)

    @staticmethod
    def _ensure_catalog(connection):
        connection.execute("CREATE TABLE IF NOT EXISTS metadata_tables (id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,position INTEGER NOT NULL)")
        connection.execute("INSERT OR IGNORE INTO metadata_tables VALUES ('metadata','Metadata',0)")
        connection.execute("CREATE TABLE IF NOT EXISTS metadata_relationships (id TEXT PRIMARY KEY,name TEXT NOT NULL,source_table TEXT NOT NULL,target_table TEXT NOT NULL,UNIQUE(source_table,name))")
        connection.execute("CREATE TABLE IF NOT EXISTS metadata_record_links (relationship_id TEXT NOT NULL,source_id INTEGER NOT NULL,target_id INTEGER NOT NULL,PRIMARY KEY(relationship_id,source_id,target_id))")
        keys = [row[1] for row in connection.execute('PRAGMA table_info(metadata_record_links)') if row[5]]
        if len(keys) == 2:
            connection.execute('ALTER TABLE metadata_record_links RENAME TO metadata_record_links_previous')
            connection.execute('CREATE TABLE metadata_record_links (relationship_id TEXT NOT NULL, source_id INTEGER NOT NULL, target_id INTEGER NOT NULL, PRIMARY KEY(relationship_id,source_id,target_id))')
            connection.execute('INSERT INTO metadata_record_links SELECT * FROM metadata_record_links_previous')
            connection.execute('DROP TABLE metadata_record_links_previous')
        connection.execute("CREATE INDEX IF NOT EXISTS metadata_record_links_target ON metadata_record_links(target_id)")
        connection.execute("CREATE TABLE IF NOT EXISTS metadata_relationship_keys (relationship_id TEXT PRIMARY KEY,source_table TEXT NOT NULL,source_field TEXT NOT NULL,target_table TEXT NOT NULL,target_field TEXT NOT NULL,UNIQUE(source_table,source_field,target_table,target_field))")

    def create(self, name: str) -> MetadataTable:
        from MetadataArea.table_migration import prepare_table_storage
        prepare_table_storage(self.database)
        name = self._name(name)
        identity = f"table_{uuid.uuid4().hex}"
        repo = MetadataRepository(self.database, table_id=identity)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            self._ensure_catalog(connection)
            connection.execute("INSERT INTO metadata_tables SELECT ?,?,COALESCE(MAX(position),0)+1 FROM metadata_tables", (identity,name))
            connection.execute(f"CREATE TABLE {identity} (id INTEGER PRIMARY KEY AUTOINCREMENT)")
            repo._ensure_schema_tables(connection)
            repo.bump_revision(connection)
        repo.refresh_schema_mirror()
        return MetadataTable(identity, name)

    def field_ids(self, table_id):
        repo = self.repository(table_id)
        columns = repo.list_columns()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS metadata_field_ids (table_id TEXT NOT NULL,name TEXT NOT NULL,field_id TEXT NOT NULL UNIQUE,PRIMARY KEY(table_id,name))")
            connection.executemany("INSERT OR IGNORE INTO metadata_field_ids VALUES (?,?,?)", ((table_id,column.name,uuid.uuid4().hex) for column in columns if column.name != 'id'))
            return dict(connection.execute("SELECT name,field_id FROM metadata_field_ids WHERE table_id=?", (table_id,)))

    def rename(self, table_id, name):
        repo = self.repository(table_id)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            self._ensure_catalog(connection)
            connection.execute("UPDATE metadata_tables SET name=? WHERE id=?", (self._name(name),table_id))
            repo.bump_revision(connection)

    def delete(self, table_id):
        """Caller owns dependency review; record IDs remain permanently reserved."""
        repo = self.repository(table_id)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            self._ensure_catalog(connection)
            connection.execute("DELETE FROM metadata_record_links WHERE relationship_id IN (SELECT id FROM metadata_relationships WHERE source_table=? OR target_table=?)", (table_id,table_id))
            connection.execute("DELETE FROM metadata_relationship_keys WHERE source_table=? OR target_table=?", (table_id,table_id))
            connection.execute("DELETE FROM metadata_relationships WHERE source_table=? OR target_table=?", (table_id,table_id))
            if table_id == "metadata":
                # The default storage anchor remains available to legacy consumers.
                connection.execute("DELETE FROM metadata")
                for column in repo.list_columns():
                    if column.name != 'id':
                        connection.execute(f'ALTER TABLE metadata DROP COLUMN {repo._quote_identifier(column.name)}')
                connection.execute("DELETE FROM metadata_column_schema")
                connection.execute("UPDATE metadata_tables SET name='Metadata' WHERE id='metadata'")
            else:
                connection.execute(f"DROP TABLE {repo.sql_table}")
                connection.execute(f"DROP TABLE {repo.sql_schema}")
                connection.execute("DELETE FROM metadata_tables WHERE id=?", (table_id,))
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata_field_ids'").fetchone():
                connection.execute("DELETE FROM metadata_field_ids WHERE table_id=?", (table_id,))
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata_sources'").fetchone():
                connection.execute("DELETE FROM metadata_sources WHERE table_id=?", (table_id,))
            repo.bump_revision(connection)
        if table_id == "metadata":
            repo.refresh_schema_mirror()
        else:
            repo._columns_path.unlink(missing_ok=True)

    def add_row(self, table_id):
        repo = self.repository(table_id)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            repo._ensure_item_registry(connection)
            registry = "project_items" if table_id == "metadata" else "source_record_indexes"
            identity = connection.execute(f"INSERT INTO {registry} DEFAULT VALUES").lastrowid
            connection.execute(f"INSERT INTO {repo.sql_table}(id) VALUES (?)", (identity,))
            repo.bump_revision(connection)
        return int(identity)

    def add_field(self, table_id, name):
        repo = self.repository(table_id)
        name = self._name(name)
        if name.lower() == 'id':
            raise ValueError("ID is reserved for permanent record identity.")
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(f"ALTER TABLE {repo.sql_table} ADD COLUMN {repo._quote_identifier(name)} TEXT")
            connection.execute(f"INSERT INTO {repo.sql_schema}(name,position,value_type,nullable,unique_values) SELECT ?,COUNT(*),'text',1,0 FROM {repo.sql_schema}", (name,))
            repo.bump_revision(connection)
        repo.refresh_schema_mirror()

    def set_values(self, table_id, ids, column, value):
        repo = self.repository(table_id)
        repo._validate_column_name(column)
        if column == 'id':
            raise ValueError("Permanent IDs cannot be edited.")
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.executemany(f"UPDATE {repo.sql_table} SET {repo._quote_identifier(column)}=? WHERE id=?", ((encode_value(value),int(identity)) for identity in ids))
            connection.execute(f"UPDATE {repo.sql_schema} SET unique_values=(SELECT COUNT(DISTINCT {repo._quote_identifier(column)}) FROM {repo.sql_table}) WHERE name=?", (column,))
            repo.bump_revision(connection)
        repo.refresh_schema_mirror()

    def link_records(self, source_table, ids, target_table, target_id, name):
        source, target = self.repository(source_table), self.repository(target_table)
        targets = tuple(dict.fromkeys(target_id if isinstance(target_id, (list, tuple)) else [target_id]))
        name = self._name(name)
        ids = tuple(dict.fromkeys(int(identity) for identity in ids))
        with closing(sqlite3.connect(self.database)) as connection, connection:
            self._ensure_catalog(connection)
            for target_index in targets:
                if not connection.execute(f"SELECT 1 FROM {target.sql_table} WHERE id=?", (target_index,)).fetchone():
                    raise ValueError("A target record no longer exists.")
            relationship = connection.execute("SELECT id,target_table FROM metadata_relationships WHERE source_table=? AND name=?", (source_table,name)).fetchone()
            if relationship and relationship[1] != target_table:
                raise ValueError("This relationship already refers to a different table.")
            identity = relationship[0] if relationship else uuid.uuid4().hex
            connection.execute("INSERT OR IGNORE INTO metadata_relationships VALUES (?,?,?,?)", (identity,name,source_table,target_table))
            for record in ids:
                if not connection.execute(f"SELECT 1 FROM {source.sql_table} WHERE id=?", (record,)).fetchone():
                    raise ValueError("A selected record no longer exists.")
            connection.executemany("DELETE FROM metadata_record_links WHERE relationship_id=? AND source_id=?", ((identity, record) for record in ids))
            connection.executemany("INSERT OR IGNORE INTO metadata_record_links VALUES (?,?,?)", ((identity,record,target_index) for record in ids for target_index in targets))
            source.bump_revision(connection)
        return identity

    @staticmethod
    def _name(value):
        value = str(value).strip()
        if not value:
            raise ValueError("Enter a name.")
        return value
