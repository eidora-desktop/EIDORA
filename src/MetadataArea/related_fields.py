"""Related metadata lookups without multiplying analysis membership."""
from contextlib import closing
from dataclasses import replace
import sqlite3
import re
from functools import lru_cache
from pathlib import Path

from MetadataArea.table_repository import MetadataTableRepository, list_tables


def relationships(database):
    with closing(sqlite3.connect(database)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata_relationships'").fetchone():
            return ()
        return tuple(connection.execute('SELECT id,name,source_table,target_table FROM metadata_relationships'))


def additional_columns(database, *, source_table=None):
    if source_table == 'metadata':
        columns, _ = discovery_columns(database)
        from MetadataArea.resolution import reachable_tables
        with closing(sqlite3.connect(database)) as connection:
            reachable = reachable_tables(connection)
        return [column for column in columns
                if column.name.startswith('table:') and column.name.split(':')[1] in reachable
                and not column.name.endswith(':id')]
    result=[]
    tables = {table.id:table for table in list_tables(database)}
    store=MetadataTableRepository(database)
    if source_table is None:
        for table in tables.values():
            if table.id == 'metadata':
                continue
            field_ids=store.field_ids(table.id)
            result.extend(replace(column, name=f'table:{table.id}:{field_ids[column.name]}',display_name=column.label, source_tab=table.name)
                          for column in store.repository(table.id).list_columns() if column.name != 'id')
    for identity,name,source,target in relationships(database):
        if source_table is not None and source != source_table:
            continue
        if target not in tables:
            continue
        field_ids=store.field_ids(target)
        result.extend(replace(column,name=f'relation:{identity}:{field_ids[column.name]}',display_name=column.label, source_tab=tables[target].name)
                      for column in store.repository(target).list_columns() if column.name != 'id')
    columns, aliases = discovery_columns(database)
    labels = {column.name: column.label for column in columns}
    own = set()
    if source_table:
        for field_id in store.field_ids(source_table).values():
            field = f'table:{source_table}:{field_id}'
            own.add(aliases.get(field, field))
    return [replace(column, display_name=labels.get(aliases.get(column.name, column.name), column.label))
            for column in result if aliases.get(column.name, column.name) not in own]


def discovery_columns(database):
    from MetadataArea.repository import MetadataRepository
    database = Path(database)
    with closing(sqlite3.connect(database)) as connection:
        revision = MetadataRepository.revision_in_connection(connection)
    stat = database.stat()
    columns, aliases = _discovery_columns_cached(database, revision, stat.st_ino, stat.st_mtime_ns)
    return list(columns), dict(aliases)


def read_field_catalog(connection):
    """Read definitions only; field IDs and summaries are maintained by writers."""
    from MetadataArea.domain import MetadataColumn
    tables = dict(connection.execute("SELECT id,name FROM metadata_tables ORDER BY position")) if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='metadata_tables'").fetchone() else {'metadata': 'Metadata'}
    field_ids = dict(((table, name), identity) for table, name, identity in connection.execute(
        'SELECT table_id,name,field_id FROM metadata_field_ids')) if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='metadata_field_ids'").fetchone() else {}
    result, aliases, fields_by_table = [], {}, {}
    for table, label in tables.items():
        if not re.fullmatch(r'metadata|table_[0-9a-f]{32}', table):
            raise ValueError('Invalid metadata table identity.')
        schema = 'metadata_column_schema' if table == 'metadata' else f'{table}_columns'
        summaries = {name: (kind, count) for name, kind, count in connection.execute(
            f'SELECT name,value_type,unique_values FROM {schema}')} if connection.execute(
                'SELECT 1 FROM sqlite_master WHERE name=?', (schema,)).fetchone() else {}
        names = {}
        for row in connection.execute(f'PRAGMA table_info({table})'):
            name = row[1]
            field_id = field_ids.get((table, name), name)
            qualified = f'table:{table}:{field_id}'
            if name == 'id':
                names[field_id] = names[name] = 'id'
                aliases[qualified] = 'id'
                if table == 'metadata':
                    result.append(MetadataColumn('id', 'integer'))
                continue
            canonical = name if table == 'metadata' else qualified
            names[field_id] = names[name] = canonical
            kind, count = summaries.get(name, ('text', None))
            result.append(MetadataColumn(canonical, kind, count, display_name=name,
                                         source_tab=None if table == 'metadata' else label))
            aliases[qualified] = canonical
            aliases[f'table:{table}:{name}'] = canonical
        fields_by_table[table] = names
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata_relationships'").fetchone():
        for identity, target in connection.execute('SELECT id,target_table FROM metadata_relationships'):
            for field, canonical in fields_by_table.get(target, {}).items():
                aliases[f'relation:{identity}:{field}'] = canonical
    return result, aliases, fields_by_table


@lru_cache(maxsize=16)
def _discovery_columns_cached(database, revision, inode, modified):
    from MetadataArea.field_presentation import present_fields
    with closing(sqlite3.connect(database)) as connection:
        connection.execute('BEGIN')
        columns, aliases, _ = read_field_catalog(connection)
        columns, aliases = present_fields(connection, columns, aliases)
    return tuple(columns), tuple(aliases.items())


def field_items(database, field, ids=None):
    """Compatibility adapter for scalar consumers of the shared resolver."""
    from MetadataArea.resolution import resolve_field
    if field.startswith('relation:'):
        _, identity, column = field.split(':', 2)
        relation = next((row for row in relationships(database) if row[0] == identity), None)
        if relation is None:
            return []
        field = f'table:{relation[3]}:{column}'
        ids = MetadataTableRepository(database).repository(relation[2]).fetch_ids() if ids is None else ids
        return sorted((item, cell.scalar) for item, cell in resolve_field(database, field, ids, source_table=relation[2]).items())
    return sorted((item, cell.scalar) for item, cell in resolve_field(database, field, ids).items())


def overview_field_items(database, field, aliases, ids=None):
    """Scalar compatibility for consumers that require an unambiguous value."""
    from MetadataArea.resolution import resolve_overview_field
    return sorted((item, cell.scalar) for item, cell in
                  resolve_overview_field(database, field, aliases, ids).items())
