"""Bounded reads of approved record links, shared by Items and Discovery."""
from collections import defaultdict
from contextlib import closing
import re
import sqlite3

from MetadataArea.repository import MetadataRepository
from MetadataArea.domain import ResolvedCell, ResolvedValue
from MetadataArea.value_storage import decode_value


def relationship_edges(connection):
    """Orient direct links away from Items; retain legacy parent directions.

    Never walk back through Items: a shared source record is not permission to
    inherit another item's other records. Legacy source-to-source links remain
    directed, so a parent cannot fan back out into unrelated sibling records.
    """
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata_relationships'").fetchone():
        return ()
    return tuple((identity, target, source, True) if target == 'metadata'
                 else (identity, source, target, False)
                 for identity, source, target in connection.execute(
                     'SELECT id,source_table,target_table FROM metadata_relationships'))


def reachable_tables(connection, source='metadata'):
    edges = relationship_edges(connection)
    reachable = {source}
    while True:
        expanded = reachable | {target for _, origin, target, _ in edges if origin in reachable}
        if expanded == reachable:
            return reachable
        reachable = expanded


def physical_fields(connection, fields):
    """Resolve stable field IDs once; validate identifiers before composing SQL."""
    names = dict(((table, identity), name) for table, identity, name in connection.execute(
        'SELECT table_id,field_id,name FROM metadata_field_ids')) if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='metadata_field_ids'").fetchone() else {}
    grouped = defaultdict(dict)
    for field in fields:
        qualified = field if field.startswith('table:') else f'table:metadata:{field}'
        _, table, column = qualified.split(':', 2)
        if not re.fullmatch(r'metadata|table_[0-9a-f]{32}', table):
            raise ValueError('Invalid metadata table identity.')
        column = names.get((table, column), column)
        grouped[table][field] = column
    for table, columns in grouped.items():
        available = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
        if not set(columns.values()) <= available:
            raise ValueError(f'Unknown metadata field in {table}.')
    return grouped


def iter_field_rows(connection, fields, ids=None, *, source_table='metadata'):
    """Yield (item, record, table, field/value pairs), one query per table/page.

    The common direct-link case uses indexed joins. Recursive traversal is only
    needed for legacy parent graphs. UNION deduplicates paths and stops cycles.
    All-column previews traverse a source once, not once per column.
    """
    if not re.fullmatch(r'metadata|table_[0-9a-f]{32}', source_table):
        raise ValueError('Invalid metadata table identity.')
    grouped = physical_fields(connection, fields)
    edges = relationship_edges(connection)
    if source_table != 'metadata':
        edges = tuple((identity, source, target, False) for identity, source, target
                      in connection.execute('SELECT id,source_table,target_table FROM metadata_relationships')) if edges else ()
    direct = all(source == source_table for _, source, _, _ in edges)
    identities = None if ids is None else list(dict.fromkeys(int(item) for item in ids))
    batches = [None] if identities is None else (identities[start:start + 400]
                                                for start in range(0, len(identities), 400))
    for batch in batches:
        if batch is None:
            seeds = 'SELECT id FROM item_membership' if source_table == 'metadata' else f'SELECT id FROM {source_table}'
            parameters = []
        else:
            seeds = 'VALUES ' + ','.join('(?)' for _ in batch)
            parameters = batch
        prefix = f'WITH RECURSIVE seeds(id) AS ({seeds})'
        if not direct:
            edge_query = ('''SELECT id,
                CASE WHEN target_table='metadata' THEN target_table ELSE source_table END,
                CASE WHEN target_table='metadata' THEN source_table ELSE target_table END,
                target_table='metadata' FROM metadata_relationships''' if source_table == 'metadata' else
                'SELECT id,source_table,target_table,0 FROM metadata_relationships')
            prefix += f''', edges(identity,source,target,reverse) AS ({edge_query}),
                reached(item,source,record) AS (
                    SELECT id,?,id FROM seeds
                    UNION
                    SELECT r.item,e.target,l.target_id FROM reached r
                    JOIN edges e ON e.source=r.source AND e.reverse=0
                    JOIN metadata_record_links l ON l.relationship_id=e.identity AND l.source_id=r.record
                    UNION
                    SELECT r.item,e.target,l.source_id FROM reached r
                    JOIN edges e ON e.source=r.source AND e.reverse=1
                    JOIN metadata_record_links l ON l.relationship_id=e.identity AND l.target_id=r.record
                )'''
            parameters = [*parameters, source_table]
        for table, columns in grouped.items():
            selected = ','.join(f't.{MetadataRepository._quote_identifier(column)}' for column in columns.values())
            if table == source_table:
                query = f'SELECT s.id,t.id,{selected} FROM seeds s JOIN {table} t ON t.id=s.id'
                arguments = parameters
            elif direct:
                paths = [(identity, reverse) for identity, source, target, reverse in edges
                         if source == source_table and target == table]
                if not paths:
                    continue
                queries = []
                for _, reverse in paths:
                    origin, target = ('target_id', 'source_id') if reverse else ('source_id', 'target_id')
                    # SQLite otherwise sometimes scans every link before the
                    # small VALUES page. Keep bounded reads driven by that page.
                    join = 'CROSS JOIN' if batch is not None else 'JOIN'
                    queries.append(f'''SELECT s.id,t.id,{selected} FROM seeds s
                        {join} metadata_record_links l ON l.relationship_id=? AND l.{origin}=s.id
                        JOIN {table} t ON t.id=l.{target}''')
                query = ' UNION '.join(queries)
                arguments = [*parameters, *(identity for identity, _ in paths)]
            else:
                query = f'''SELECT r.item,t.id,{selected} FROM reached r
                    JOIN {table} t ON t.id=r.record WHERE r.source=?'''
                arguments = [*parameters, table]
            for item, record, *values in connection.execute(prefix + ' ' + query + ' ORDER BY 1,2', arguments):
                yield int(item), int(record), table, tuple(
                    (field, column, decode_value(value))
                    for (field, column), value in zip(columns.items(), values, strict=True))


def resolve_fields_in_connection(connection, fields, ids=None, *, source_table='metadata'):
    records = {field: {} for field in fields}
    for item, index, table, values in iter_field_rows(connection, fields, ids, source_table=source_table):
        for field, column, value in values:
            records[field].setdefault(item, []).append(ResolvedValue(value, table, index, column))
    return {field: {item: ResolvedCell(tuple(values)) for item, values in cells.items()}
            for field, cells in records.items()}


def resolve_field(database, field, ids=None, *, source_table='metadata'):
    with closing(sqlite3.connect(database)) as connection:
        connection.execute('BEGIN')
        return resolve_fields_in_connection(connection, [field], ids, source_table=source_table)[field]


def overview_members(field, aliases):
    members = {alias for alias, canonical in aliases.items()
               if canonical == field and alias.startswith('table:')
               and len(alias.split(':')[2]) == 32}
    if field.startswith('table:') or not any(member.startswith('table:metadata:') for member in members):
        members.add(field)
    return sorted(members)


def resolve_overview_fields(database, fields, aliases, ids=None):
    members = {field: overview_members(field, aliases) for field in fields}
    with closing(sqlite3.connect(database)) as connection:
        connection.execute('BEGIN')
        physical = resolve_fields_in_connection(connection, set().union(*members.values()), ids)
    result = {}
    for field, names in members.items():
        records = {}
        for name in names:
            for item, cell in physical[name].items():
                records.setdefault(item, {}).update(
                    ((record.source_table, record.record_index, record.column), record) for record in cell.records)
        result[field] = {item: ResolvedCell(tuple(values.values())) for item, values in records.items()}
    return result


def resolve_overview_field(database, field, aliases, ids=None):
    return resolve_overview_fields(database, [field], aliases, ids)[field]


def scalar_column_values(database, field, aliases):
    """Read one plotting column without allocating per-point provenance objects.

    Preserve ragged entries only for lists or ambiguous cells. Display/provenance
    reads use the bounded row resolver independently.
    """
    from MetadataArea.values import scalar_values
    values, ragged = {}, {}
    with closing(sqlite3.connect(database)) as connection:
        connection.execute('BEGIN')
        for item, _, _, fields in iter_field_rows(connection, overview_members(field, aliases)):
            for _, _, value in fields:
                incoming = tuple(dict.fromkeys(scalar_values(value)))
                previous = values.get(item)
                combined = incoming if previous is None else tuple(dict.fromkeys((*previous, *incoming)))
                values[item] = combined
                if isinstance(value, list) or len(combined) > 1 or item in ragged:
                    ragged[item] = combined
    return [(item, entries[0] if len(entries) == 1 else '') for item, entries in sorted(values.items())], ragged
