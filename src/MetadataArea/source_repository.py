"""Imported-file provenance, independent of editable tab labels."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3


def content_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    paths = sorted(path.rglob('*')) if path.is_dir() else [path]
    for source in paths:
        if not source.is_file():
            continue
        if path.is_dir():
            digest.update(source.relative_to(path).as_posix().encode())
            digest.update(b'\0')
        with source.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
    return digest.hexdigest()


class MetadataSourceRepository:
    def __init__(self, database: Path):
        self.database = database

    def sources(self) -> dict[str, tuple[str, str]]:
        with closing(sqlite3.connect(self.database)) as connection:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata_sources'").fetchone():
                return {}
            return {row[0]: (row[1], row[2]) for row in connection.execute(
                'SELECT table_id,source_path,content_fingerprint FROM metadata_sources')}

    def find(self, path: Path) -> tuple[str, bool] | None:
        canonical = str(path.resolve())
        for table_id, (source_path, fingerprint) in self.sources().items():
            if source_path == canonical:
                return table_id, fingerprint == content_fingerprint(path)
        return None

    @staticmethod
    def publish(connection, table_id: str, path: str, fingerprint: str):
        connection.execute('CREATE TABLE IF NOT EXISTS metadata_sources ('
            'table_id TEXT PRIMARY KEY, source_path TEXT NOT NULL, content_fingerprint TEXT NOT NULL)')
        connection.execute('INSERT INTO metadata_sources VALUES (?,?,?) '
            'ON CONFLICT(table_id) DO UPDATE SET source_path=excluded.source_path, '
            'content_fingerprint=excluded.content_fingerprint', (table_id, path, fingerprint))
