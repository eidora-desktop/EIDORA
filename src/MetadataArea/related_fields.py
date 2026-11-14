"""Related metadata lookups without multiplying analysis membership."""
from contextlib import closing
from dataclasses import replace
import sqlite3
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
        reachable = {'metadata'}
        relations = relationships(database)
        while True:
            expanded = reachable | {target for _, _, source, target in relations if source in reachable}
            if expanded == reachable:
                break
            reachable = expanded
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


@lru_cache(maxsize=16)
def _discovery_columns_cached(database, revision, inode, modified):
    """Expose each physical field once, with aliases for saved relationship fields."""
    store = MetadataTableRepository(database)
    result, aliases, fields_by_table = [], {}, {}
    relations = relationships(database)
    for table in list_tables(database):
        field_ids = store.field_ids(table.id)
        names = {}
        for column in store.repository(table.id).list_columns():
            field_id = field_ids.get(column.name, 'id')
            qualified = f'table:{table.id}:{field_id}'
            if column.name == 'id':
                # Source record identities are implementation details, not item fields.
                names[field_id] = names[column.name] = 'id'
                aliases[qualified] = 'id'
                if table.id == 'metadata':
                    result.append(column)
                continue
            canonical = column.name if table.id == 'metadata' else qualified
            names[field_id] = names[column.name] = canonical
            if table.id == 'metadata':
                result.append(column)
                aliases[qualified] = canonical
            else:
                result.append(replace(column, name=canonical,
                                      display_name=column.label, source_tab=table.name))
            # Older snapshots used column names before stable field IDs existed.
            aliases[f'table:{table.id}:{column.name}'] = canonical
        fields_by_table[table.id] = names
    for identity, _, _, target in relations:
        for field, canonical in fields_by_table.get(target, {}).items():
            aliases[f'relation:{identity}:{field}'] = canonical
    from MetadataArea.field_presentation import present_fields
    columns, aliases = present_fields(database, result, aliases, fields_by_table)
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
