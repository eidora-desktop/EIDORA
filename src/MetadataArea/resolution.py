"""Read linked values with provenance; never copy them into an overview table."""
from contextlib import closing
import sqlite3

from MetadataArea.repository import MetadataRepository
from MetadataArea.domain import ResolvedCell, ResolvedValue
from MetadataArea.value_storage import decode_value


def resolve_field(database, field, ids=None, *, source_table='metadata'):
    """Traverse table-qualified record pairs, coalescing duplicate paths and cycles."""
    if not field.startswith('table:'):
        field = f'table:metadata:{field}'
    _, target_table, column = field.split(':', 2)
    repo = MetadataRepository(database, table_id=target_table)
    with closing(sqlite3.connect(database)) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata_field_ids'").fetchone():
            row = connection.execute('SELECT name FROM metadata_field_ids WHERE table_id=? AND field_id=?',
                                     (target_table, column)).fetchone()
            if row:
                column = row[0]
        repo._validate_column_name(column)
        quoted = repo._quote_identifier(column)
        # Resolve only requested items for paged table reads. The UNION is also
        # the visited set: the same physical record is returned once per item.
        if ids is None:
            ids = [row[0] for row in connection.execute('SELECT id FROM item_membership')]
        ids = list(dict.fromkeys(ids))
        linked = connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata_record_links'").fetchone()
        result = {}
        for start in range(0, len(ids), 200):
            batch = ids[start:start + 200]
            seed = ','.join('(?,?,?)' for _ in batch)
            parameters = [value for identity in batch for value in (identity, source_table, identity)]
            traversal = '''UNION
                SELECT reached.item, relation.target_table, link.target_id
                FROM reached JOIN metadata_relationships relation ON relation.source_table=reached.source
                JOIN metadata_record_links link ON link.relationship_id=relation.id AND link.source_id=reached.record''' if linked else ''
            query = f'''WITH RECURSIVE reached(item,source,record) AS (
                VALUES {seed} {traversal}
            ) SELECT reached.item,target.id,target.{quoted}
                FROM reached JOIN {repo.sql_table} target ON target.id=reached.record
                WHERE reached.source=? ORDER BY reached.item,target.id'''
            for item, index, value in connection.execute(query, (*parameters, target_table)):
                result.setdefault(int(item), []).append(ResolvedValue(
                    decode_value(value), target_table, int(index), column))
        return {item: ResolvedCell(tuple(records)) for item, records in result.items()}


def resolve_overview_field(database, field, aliases, ids=None):
    members = {alias for alias, canonical in aliases.items()
               if canonical == field and alias.startswith('table:')
               and len(alias.split(':')[2]) == 32}
    if field.startswith('table:') or not any(member.startswith('table:metadata:') for member in members):
        members.add(field)
    records = {}
    for member in sorted(members):
        for item, cell in resolve_field(database, member, ids).items():
            records.setdefault(item, {}).update(
                ((record.source_table, record.record_index, record.column), record) for record in cell.records)
    return {item: ResolvedCell(tuple(values.values())) for item, values in records.items()}
