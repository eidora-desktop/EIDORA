"""Shared overview labels and conservative consolidation of approved link keys."""
from contextlib import closing
from dataclasses import replace
import sqlite3
import re

from MetadataArea.repository import MetadataRepository
from MetadataArea.table_repository import MetadataTableRepository


def present_fields(database, columns, aliases, fields_by_table):
    from MetadataArea.resolution import resolve_field
    store = MetadataTableRepository(database)
    representatives = {column.name: column.name for column in columns}
    order = {column.name: index for index, column in enumerate(columns)}
    field_values = {}
    source_tabs = {column.name: ({column.source_tab} if column.source_tab else set()) for column in columns}

    def representative(name):
        while representatives[name] != name:
            name = representatives[name]
        return name

    def values(name):
        field = name if name.startswith('table:') else f'table:metadata:{name}'
        if name not in field_values:
            field_values[name] = {item: cell.value if cell.is_list else cell.values
                                  for item, cell in resolve_field(database, field).items()}
        return field_values[name]

    with closing(sqlite3.connect(database)) as connection:
        has_keys = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='metadata_relationship_keys'").fetchone()
        keys = list(connection.execute(
            "SELECT relationship_id,source_table,source_field,target_table,target_field "
            "FROM metadata_relationship_keys ORDER BY rowid")) if has_keys else []
        overrides = dict(connection.execute("SELECT field,label FROM metadata_field_labels")) if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='metadata_field_labels'").fetchone() else {}
        for relation, source, source_field, target, target_field in keys:
            if source_field == 'id' or target_field == 'id':
                continue
            left = fields_by_table.get(source, {}).get(source_field)
            right = fields_by_table.get(target, {}).get(target_field)
            if left not in representatives or right not in representatives:
                continue
            source_repo, target_repo = store.repository(source), store.repository(target)
            source_names = {value: key for key, value in store.field_ids(source).items()}
            target_names = {value: key for key, value in store.field_ids(target).items()}
            if source_field not in source_names or target_field not in target_names:
                continue
            a = source_repo._quote_identifier(source_names[source_field])
            b = target_repo._quote_identifier(target_names[target_field])
            total, matched = connection.execute(f"""
                SELECT COUNT(*), COALESCE(SUM(t.id IS NOT NULL AND s.{a} = t.{b}
                    AND s.{a} IS NOT NULL AND TRIM(s.{a}) != ''),0)
                FROM {source_repo.sql_table} s
                LEFT JOIN metadata_record_links l ON l.source_id=s.id AND l.relationship_id=?
                LEFT JOIN {target_repo.sql_table} t ON t.id=l.target_id
            """, (relation,)).fetchone()
            if not total or total != matched:
                continue
            left, right = representative(left), representative(right)
            if left == right:
                continue
            # Other approved paths must not introduce conflicting values.
            lv, rv = values(left), values(right)
            if any(lv[key] != rv[key] for key in lv.keys() & rv.keys()):
                continue
            keep, hide = sorted((left, right), key=order.get)
            representatives[hide] = keep
            field_values[keep] = {**lv, **rv}
            source_tabs[keep].update(source_tabs[hide])

    # Imports may already contain numbered copies without a relationship-key entry.
    # Compare only the same label family; unrelated fields with coincidentally equal
    # values (for example width and height) remain separate.
    families = {}
    for column in columns:
        if column.name == 'id':
            continue
        base = re.sub(r'(?:_\d+| \(\d+\))$', '', column.label).casefold()
        families.setdefault(base, []).append(column.name)
    item_ids = set()
    if any(len(members) > 1 for members in families.values()):
        repository = store.repository('metadata')
        if repository.is_initialized():
            item_ids.update(repository.fetch_ids())
    for members in families.values():
        candidates = []
        for name in members:
            name = representative(name)
            if name in candidates:
                continue
            incoming = values(name) if len(members) > 1 else {}
            if not incoming:
                continue
            for previous in candidates:
                existing = values(previous)
                common = existing.keys() & incoming.keys()
                if not common or any(existing[key] != incoming[key] for key in common):
                    continue
                # Exact copies, or identical populated item coverage through approved links.
                item_existing = {key: value for key, value in existing.items() if key in item_ids}
                item_incoming = {key: value for key, value in incoming.items() if key in item_ids}
                if existing != incoming and (not item_existing or item_existing != item_incoming):
                    continue
                keep, hide = sorted((previous, name), key=order.get)
                representatives[hide] = keep
                field_values[keep] = {**existing, **incoming}
                source_tabs[keep].update(source_tabs[hide])
                break
            else:
                candidates.append(name)

    overrides = {aliases.get(key, key): value for key, value in overrides.items()}
    for name in representatives:
        canonical = representative(name)
        if name != canonical:
            aliases[name] = canonical
    aliases = {key: representative(value) if value in representatives else value
               for key, value in aliases.items()}
    visible = [column for column in columns if representative(column.name) == column.name]
    visible = [replace(column, source_tab="\n".join(sorted(source_tabs[column.name])) or None)
               for column in visible]
    return unique_column_labels(visible, overrides), aliases


def unique_column_labels(columns, overrides=None):
    """Assign case-insensitively unique labels without changing field identity."""
    overrides = overrides or {}
    visible_names = {column.name for column in columns}
    reserved = {label.casefold() for name, label in overrides.items() if name in visible_names}
    used, result = set(), []
    for column in columns:
        base = overrides.get(column.name, 'ID' if column.name == 'id' else column.label)
        label, suffix = base, 2
        while label.casefold() in used or (column.name not in overrides and label.casefold() in reserved):
            label = f'{base} ({suffix})'
            suffix += 1
        used.add(label.casefold())
        result.append(replace(column, display_name=label))
    return result


def rename_field(database, name, label):
    from MetadataArea.related_fields import discovery_columns
    label = label.strip()
    if not label:
        raise ValueError('Column label cannot be empty.')
    columns, aliases = discovery_columns(database)
    name = aliases.get(name, name)
    if name == 'id':
        raise ValueError('The item ID label cannot be renamed.')
    if name not in {column.name for column in columns}:
        raise ValueError('This field no longer exists.')
    if any(column.name != name and column.label.casefold() == label.casefold() for column in columns):
        raise ValueError(f'Column label already exists: {label}')
    if not name.startswith('table:'):
        field_id = MetadataTableRepository(database).field_ids('metadata').get(name)
        if field_id:
            name = f'table:metadata:{field_id}'
    repository = MetadataRepository(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE IF NOT EXISTS metadata_field_labels (field TEXT PRIMARY KEY,label TEXT NOT NULL)")
        connection.execute("INSERT INTO metadata_field_labels VALUES (?,?) ON CONFLICT(field) DO UPDATE SET label=excluded.label",
                           (name, label))
        repository.bump_revision(connection)
