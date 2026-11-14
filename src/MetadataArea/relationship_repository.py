"""Exact-key relationship queries and atomic publication in metadata.db."""
from contextlib import closing
import sqlite3
import uuid

from MetadataArea.domain import TableLinkPreview, TableLinkRequest
from MetadataArea.repository import MetadataRepository
from MetadataArea.table_repository import MetadataTableRepository


class MetadataRelationshipRepository:
    def __init__(self, database):
        self.store = MetadataTableRepository(database)

    def _query(self, request):
        if request.source_table == request.target_table:
            raise ValueError('Choose two different tables.')
        if not request.name.strip():
            raise ValueError('Enter a relationship name.')
        source = self.store.repository(request.source_table)
        target = self.store.repository(request.target_table)
        source._validate_column_name(request.source_column)
        target._validate_column_name(request.target_column)
        for repository, name in ((source, request.source_column), (target, request.target_column)):
            if any(column.name == name and column.value_type == "list" for column in repository.list_columns()):
                raise ValueError("Linking requires a single identifier per record, not a list.")
        source_key = source._quote_identifier(request.source_column)
        target_key = target._quote_identifier(request.target_column)
        # Group targets first: duplicate keys never multiply source records.
        query = f"""WITH targets AS (
            SELECT {target_key} AS match_key, MIN(id) AS target_id, COUNT(*) AS matches
            FROM {target.sql_table}
            WHERE {target_key} IS NOT NULL AND TRIM({target_key}) != ''
            GROUP BY {target_key}
        ), matched AS (
            SELECT source.id AS source_id, source.{source_key} AS match_key,
                   targets.target_id, COALESCE(targets.matches, 0) AS matches
            FROM {source.sql_table} source
            LEFT JOIN targets ON source.{source_key} = targets.match_key
        ) """
        fields = (self.store.field_ids(request.source_table).get(request.source_column, 'id'),
                  self.store.field_ids(request.target_table).get(request.target_column, 'id'))
        return source, query, fields

    @staticmethod
    def _relationship(connection, request, query, fields):
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata_relationships'").fetchone():
            return None
        has_keys = connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata_relationship_keys'").fetchone()
        if has_keys:
            bound = connection.execute('''SELECT relationship_id FROM metadata_relationship_keys
                WHERE source_table=? AND source_field=? AND target_table=? AND target_field=?''',
                (request.source_table, fields[0], request.target_table, fields[1])).fetchone()
            if bound:
                return bound[0]
        row = connection.execute(
            'SELECT id,target_table FROM metadata_relationships WHERE source_table=? AND name=?',
            (request.source_table, request.name),
        ).fetchone()
        if row and row[1] != request.target_table:
            raise ValueError('This relationship name already refers to a different table.')
        if row:
            if has_keys:
                bound = connection.execute('SELECT source_field,target_field FROM metadata_relationship_keys WHERE relationship_id=?', (row[0],)).fetchone()
                if bound and tuple(bound) != fields:
                    raise ValueError('This relationship uses different matching columns. Choose a different name.')
            return row[0]
        # Older projects stored only names and record pairs. Adopt an equivalent
        # relationship without changing its ID or breaking saved field references.
        unbound = ('AND NOT EXISTS (SELECT 1 FROM metadata_relationship_keys k WHERE k.relationship_id=r.id)'
                   if has_keys else '')
        row = connection.execute(query + f'''SELECT r.id FROM metadata_relationships r
            WHERE r.source_table=? AND r.target_table=? {unbound}
            AND EXISTS (SELECT 1 FROM metadata_record_links l WHERE l.relationship_id=r.id)
            AND NOT EXISTS (
                SELECT 1 FROM metadata_record_links l LEFT JOIN matched m ON m.source_id=l.source_id
                WHERE l.relationship_id=r.id AND (m.matches IS NULL OR m.matches!=1 OR m.target_id!=l.target_id)
            ) ORDER BY r.rowid LIMIT 1''', (request.source_table, request.target_table)).fetchone()
        return row[0] if row else None

    def preview(self, request: TableLinkRequest, row_filter="all") -> TableLinkPreview:
        _source, query, fields = self._query(request)
        with closing(sqlite3.connect(self.store.database)) as connection:
            connection.execute('BEGIN')
            identity = self._relationship(connection, request, query, fields)
            existing = ('EXISTS (SELECT 1 FROM metadata_record_links link '
                        'WHERE link.relationship_id=? AND link.source_id=matched.source_id)'
                        if identity else '0')
            parameters = (identity,) if identity else ()
            counts = connection.execute(query + f""", classified AS (
                SELECT *, {existing} AS existing FROM matched
            ) SELECT COALESCE(SUM(matches=1),0), COALESCE(SUM(matches=0),0),
                     COALESCE(SUM(matches>1),0), COALESCE(SUM(existing),0),
                     COALESCE(SUM((matches=1 OR (matches>1 AND {int(request.accept_multiple)})) AND NOT existing),0) FROM classified""", parameters).fetchone()
            condition = {"all": "1", "unmatched": "m.matches=0", "multiple": "m.matches>1"}[row_filter]
            target = self.store.repository(request.target_table)
            key = target._quote_identifier(request.target_column)
            samples = list(connection.execute(query + f"""SELECT m.source_id,m.match_key,m.target_id,m.matches,t.{key}
                FROM matched m LEFT JOIN {target.sql_table} t ON t.id=m.target_id
                WHERE {condition} ORDER BY m.source_id LIMIT 25"""))
            rows = tuple((int(source), '' if value is None else str(value),
                          int(target_id) if matches == 1 else None,
                          'Matched' if matches == 1 else 'Missing' if matches == 0 else 'Ambiguous')
                         for source, value, target_id, matches, target_value in samples)
            return TableLinkPreview(request, MetadataRepository.revision_in_connection(connection),
                                    *counts, rows,
                                    tuple('' if row[4] is None else str(row[4]) for row in samples),
                                    tuple(row[3] for row in samples))

    def apply(self, preview: TableLinkPreview) -> int:
        request = preview.request
        source, query, fields = self._query(request)
        with closing(sqlite3.connect(self.store.database)) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if MetadataRepository.revision_in_connection(connection) != preview.revision:
                raise ValueError('Metadata changed. Preview the matches again before linking.')
            self.store._ensure_catalog(connection)
            identity = self._relationship(connection, request, query, fields)
            if not identity and not preview.linkable_count:
                return 0
            identity = identity or uuid.uuid4().hex
            connection.execute('INSERT OR IGNORE INTO metadata_relationships VALUES (?,?,?,?)',
                               (identity, request.name, request.source_table, request.target_table))
            connection.execute('INSERT OR IGNORE INTO metadata_relationship_keys VALUES (?,?,?,?,?)',
                               (identity, request.source_table, fields[0], request.target_table, fields[1]))
            before = connection.total_changes
            if request.accept_multiple:
                target = self.store.repository(request.target_table)
                source_key = source._quote_identifier(request.source_column)
                target_key = target._quote_identifier(request.target_column)
                connection.execute(f'''INSERT OR IGNORE INTO metadata_record_links
                    SELECT ?,s.id,t.id FROM {source.sql_table} s
                    JOIN {target.sql_table} t ON s.{source_key}=t.{target_key}
                    WHERE TRIM(COALESCE(s.{source_key},''))!=''
                    AND NOT EXISTS (SELECT 1 FROM metadata_record_links old
                                    WHERE old.relationship_id=? AND old.source_id=s.id)''', (identity, identity))
            else:
                connection.execute(query + '''INSERT OR IGNORE INTO metadata_record_links
                    SELECT ?,source_id,target_id FROM matched WHERE matches=1
                    AND NOT EXISTS (SELECT 1 FROM metadata_record_links old
                                    WHERE old.relationship_id=? AND old.source_id=matched.source_id)''', (identity, identity))
            count = connection.total_changes - before
            if count:
                source.bump_revision(connection)
            return count

    def best_match(self, table_id, cancelled):
        """Score exact matches in both directions without sampling or publishing links."""
        from MetadataArea.table_repository import list_tables
        tables = list_tables(self.store.database)
        columns = {table.id: [column.name for column in self.store.repository(table.id).list_columns()
                             if column.name != 'id' and column.value_type != 'list'] for table in tables}
        best, best_score = None, None
        with closing(sqlite3.connect(self.store.database)) as connection:
            connection.set_progress_handler(lambda: int(cancelled.is_set()), 1000)
            for source in tables:
                for target in tables:
                    if (source.id == target.id or table_id not in (source.id, target.id)
                            or 'metadata' not in (source.id, target.id)):
                        continue
                    for source_column in columns[source.id]:
                        for target_column in columns[target.id]:
                            if cancelled.is_set():
                                return None
                            left = MetadataRepository._quote_identifier(source_column)
                            right = MetadataRepository._quote_identifier(target_column)
                            matched, ambiguous, total = connection.execute(f'''
                                WITH targets AS (
                                    SELECT {right} AS key, COUNT(*) AS n FROM {target.id}
                                    WHERE {right} IS NOT NULL AND TRIM({right})!='' GROUP BY {right}
                                ) SELECT COALESCE(SUM(t.n=1),0), COALESCE(SUM(t.n>1),0), COUNT(*)
                                  FROM {source.id} s LEFT JOIN targets t ON s.{left}=t.key
                            ''').fetchone()
                            if not matched:
                                continue
                            score = (matched, matched / max(1, total), -ambiguous,
                                     source_column.casefold() == target_column.casefold(), source.id == table_id)
                            if best_score is None or score > best_score:
                                label = 'Items' if target.id == 'metadata' else target.name
                                best = TableLinkRequest(source.id, source_column, target.id, target_column,
                                                        f'{label} ({source_column} → {target_column})')
                                best_score = score
        return best
