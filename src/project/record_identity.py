"""Separate source-record indexes from user-facing item numbers.

Legacy media links use integer keys, so source indexes occupy a disjoint int64
range. Internal references are qualified as record:<table>:<index>.
Neither source allocation nor source deletion touches the item sequence.
"""
SOURCE_INDEX_BASE = 1 << 48


def ensure_record_registry(connection):
    connection.execute('CREATE TABLE IF NOT EXISTS source_record_indexes (id INTEGER PRIMARY KEY AUTOINCREMENT)')
    row = connection.execute("SELECT seq FROM sqlite_sequence WHERE name='source_record_indexes'").fetchone()
    if row is None:
        connection.execute("INSERT INTO sqlite_sequence(name,seq) VALUES ('source_record_indexes',?)", (SOURCE_INDEX_BASE,))


def record_high_water(connection):
    row = connection.execute("SELECT seq FROM sqlite_sequence WHERE name='source_record_indexes'").fetchone()
    return max(SOURCE_INDEX_BASE, int(row[0]) if row else 0)


def redirect_legacy_source_allocators(connection):
    """Redirect old source allocators without reusing any previously reserved key."""
    import re
    triggers = connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'table_%_identity'").fetchall()
    legacy = [(name, sql) for name, sql in triggers if 'project_items' in sql]
    if not legacy:
        return
    ensure_record_registry(connection)
    for trigger, _ in legacy:
        table = trigger.removesuffix('_identity')
        if not re.fullmatch(r'table_[0-9a-f]{32}', table):
            raise ValueError('Invalid source table identity.')
        connection.execute(f'DROP TRIGGER {trigger}')
        connection.execute(f'INSERT OR IGNORE INTO source_record_indexes SELECT id FROM {table}')
        connection.execute(f'''CREATE TRIGGER {trigger} AFTER INSERT ON {table} BEGIN
            INSERT OR IGNORE INTO source_record_indexes(id) VALUES (NEW.id); END''')
        connection.execute('UPDATE sqlite_sequence SET seq=? WHERE name=?', (record_high_water(connection), table))
