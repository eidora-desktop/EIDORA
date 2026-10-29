from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import urlsplit


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def connect_read_only(path: Path) -> sqlite3.Connection:
    """Open an existing database read-only, including Windows UNC shares."""
    uri = path.resolve().as_uri()
    parts = urlsplit(uri)
    if parts.netloc:
        # Keep the UNC server in the filename, not SQLite's restricted authority.
        uri = "file:////" + parts.netloc + parts.path
    return sqlite3.connect(uri + "?mode=ro", uri=True)
