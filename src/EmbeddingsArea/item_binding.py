"""Bind source observations to explicit items during vector publication."""
from dataclasses import replace
import uuid


def bind_source_items(connection, inputs, source_table):
    """Use the caller's attached metadata transaction; no partial items escape."""
    name = f'Embedding observations ({source_table})'
    relationship = connection.execute('''SELECT id FROM imported_metadata.metadata_relationships
        WHERE source_table='metadata' AND target_table=? AND name=? ''', (source_table, name)).fetchone()
    relationship_id = relationship[0] if relationship else uuid.uuid4().hex
    connection.execute('INSERT OR IGNORE INTO imported_metadata.metadata_relationships VALUES (?,?,?,?)',
                       (relationship_id, name, 'metadata', source_table))
    used = set()
    for observation in inputs:
        if observation.metadata_row_id is None:
            raise ValueError('An embedding observation has no source record.')
        candidates = [row[0] for row in connection.execute('''SELECT DISTINCT link.source_id
            FROM imported_metadata.metadata_record_links link
            JOIN imported_metadata.metadata_relationships relation ON relation.id=link.relationship_id
            JOIN imported_metadata.item_membership item ON item.id=link.source_id
            WHERE relation.source_table='metadata' AND relation.target_table=? AND link.target_id=?''',
            (source_table, observation.metadata_row_id))]
        if len(candidates) > 1:
            raise ValueError('A source record is linked to multiple items. Assign the vectors to items explicitly.')
        if candidates:
            item_id = candidates[0]
        else:
            item_id = connection.execute('INSERT INTO imported_metadata.metadata DEFAULT VALUES').lastrowid
        connection.execute('INSERT OR IGNORE INTO imported_metadata.metadata_record_links VALUES (?,?,?)',
                           (relationship_id, item_id, observation.metadata_row_id))
        if item_id in used:
            raise ValueError('Each vector must have a distinct item within its vector set.')
        used.add(item_id)
        yield replace(observation, metadata_row_id=item_id)


def bind_generated_items(connection, inputs):
    """Publish items only after model execution has produced vectors."""
    items = []
    by_source = {}
    used = set()
    tables = [row[0] for row in connection.execute(
        "SELECT id FROM imported_metadata.metadata_tables WHERE id!='metadata'")]
    for observation in inputs:
        index = observation.metadata_row_id
        if index is not None and connection.execute(
                'SELECT 1 FROM imported_metadata.item_membership WHERE id=?', (index,)).fetchone():
            if index in used:
                raise ValueError('Each vector must have a distinct item within its vector set.')
            used.add(index)
            items.append(observation)
            continue
        if index is not None:
            import re
            source = None
            for table in tables:
                if not re.fullmatch(r'table_[0-9a-f]{32}', table):
                    raise ValueError('Invalid source table identity.')
                if connection.execute(f'SELECT 1 FROM imported_metadata.{table} WHERE id=?', (index,)).fetchone():
                    source = table
                    break
            if source is None:
                raise ValueError('A prepared source record no longer exists.')
            by_source.setdefault(source, []).append(observation)
            continue
        existing = connection.execute('SELECT id FROM imported_metadata.project_items WHERE asset_id=?',
                                      (observation.media_item_id,)).fetchone()
        if existing and not connection.execute('SELECT 1 FROM imported_metadata.item_membership WHERE id=?', (existing[0],)).fetchone():
            connection.execute('UPDATE imported_metadata.project_items SET asset_id=NULL WHERE id=?', (existing[0],))
            existing = None
        if existing:
            item_id = existing[0]
        else:
            item_id = connection.execute('INSERT INTO imported_metadata.project_items(asset_id) VALUES (?)',
                                         (observation.media_item_id,)).lastrowid
        if item_id in used:
            raise ValueError('Each vector must have a distinct item within its vector set.')
        used.add(item_id)
        connection.execute('INSERT OR IGNORE INTO imported_metadata.metadata(id) VALUES (?)', (item_id,))
        items.append(replace(observation, metadata_row_id=item_id))
    for source, observations in by_source.items():
        items.extend(bind_source_items(connection, observations, source))
    if len({item.metadata_row_id for item in items}) != len(items):
        raise ValueError('Each vector must have a distinct item within its vector set.')
    return sorted(items, key=lambda item: item.ordinal)


def preview_source_items(database, source_table, record_indexes):
    from contextlib import closing
    import sqlite3
    with closing(sqlite3.connect(database)) as connection:
        candidates = {}
        for record, item in connection.execute('''SELECT DISTINCT link.target_id,link.source_id
            FROM metadata_record_links link JOIN metadata_relationships relation ON relation.id=link.relationship_id
            JOIN item_membership item ON item.id=link.source_id
            WHERE relation.source_table='metadata' AND relation.target_table=?''', (source_table,)):
            candidates.setdefault(record, set()).add(item)
    reused, additional, ambiguous = set(), 0, 0
    for index in record_indexes:
        choices = candidates.get(int(index), set()) - reused
        if len(choices) > 1:
            ambiguous += 1
        elif choices:
            reused.update(choices)
        else:
            additional += 1
    return len(reused), additional, ambiguous


def existing_item_bindings(database, inputs):
    """Resolve possible reuse once for a prepared batch, without allocating items."""
    from contextlib import closing
    import sqlite3
    from project.item_repository import ItemRepository

    active = set(ItemRepository(database).active_ids())
    record_ids = {item.metadata_row_id for item in inputs if item.metadata_row_id is not None} - active
    candidates = {}
    with closing(sqlite3.connect(database)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata_record_links'").fetchone():
            records = list(record_ids)
            for start in range(0, len(records), 800):
                batch = records[start:start + 800]
                query = f'''SELECT DISTINCT link.target_id, link.source_id FROM metadata_record_links link
                    JOIN metadata_relationships relation ON relation.id=link.relationship_id
                    JOIN item_membership item ON item.id=link.source_id
                    WHERE relation.source_table='metadata' AND link.target_id IN ({','.join('?' for _ in batch)})'''
                for record, identity in connection.execute(query, batch):
                    candidates.setdefault(record, set()).add(identity)
    assets = ItemRepository(database).item_ids_for_assets(
        [item.media_item_id for item in inputs if item.metadata_row_id is None])
    output = {}
    for item in inputs:
        record = item.metadata_row_id
        choices = {record} if record in active else candidates.get(record, set())
        if len(choices) > 1:
            raise ValueError('A source record matches multiple items. Resolve its links before running the model.')
        identity = next(iter(choices)) if choices else assets.get(item.media_item_id) if record is None else None
        output[item.ordinal] = identity if identity in active else None
    return output
