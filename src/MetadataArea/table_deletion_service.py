"""Preview table dependencies and remove them with recoverable local rollback."""
from contextlib import closing
from pathlib import Path
import json
import shutil
import sqlite3
import uuid

from MetadataArea.importing.dependencies import _projection_references, _references
from MetadataArea.table_repository import MetadataTableRepository
from MetadataArea.domain import TableDeletionPlan
from shared.persistence.json_store import write_json


def _copy_database(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as reader, closing(sqlite3.connect(destination)) as writer:
        reader.backup(writer)


def recover_table_removals(root):
    """Called at project open before any repositories start reading the project."""
    directory = Path(root)/'.recovery'
    for journal in directory.glob('table-*/journal.json'):
        payload=json.loads(journal.read_text(encoding='utf-8'))
        for relative in [*payload['databases'], *payload['files']]:
            if Path(relative).is_absolute() or not (Path(root)/relative).resolve().is_relative_to(Path(root).resolve()):
                raise ValueError('Invalid path in table recovery journal.')
        if payload['phase'] != 'complete':
            for relative in payload['databases']:
                _copy_database(journal.parent/'databases'/relative, Path(root)/relative)
            for relative in payload['files']:
                saved=journal.parent/'files'/relative
                if saved.exists():
                    destination=Path(root)/relative
                    destination.parent.mkdir(parents=True,exist_ok=True)
                    saved.replace(destination)
        shutil.rmtree(journal.parent)


class MetadataTableDeletionService:
    def __init__(self, project):
        self.project=project
        self.store=MetadataTableRepository(project.paths.metadata_database)

    def preview(self, table_id):
        repo=self.store.repository(table_id)
        ids=repo.fetch_ids()
        removed=set(ids)
        fields={column.name if table_id=='metadata' else f'table:{table_id}:{column.name}' for column in repo.list_columns()}
        from MetadataArea.related_fields import additional_columns, relationships
        relation_ids={row[0] for row in relationships(repo.database_path) if table_id in row[2:]}
        fields.update(column.name for column in additional_columns(repo.database_path)
                      if column.name.startswith(f'table:{table_id}:') or any(column.name.startswith(f'relation:{identity}:') for identity in relation_ids))
        statements=[]; files=set(); descriptions=[]; fingerprints={}
        runs=set()
        root=self.project.root.resolve()
        def remember(path):
            if path.exists():
                stat=path.stat();fingerprints[path]=(stat.st_size,stat.st_mtime_ns)
        def owned(value):
            path=Path(str(value))
            path=(root/path).resolve() if not path.is_absolute() else path.resolve()
            return path if path.is_relative_to(root) and path != root and path.exists() else None
        def select(connection, table, field, values, output):
            rows=[]
            for start in range(0,len(values),800):
                batch=values[start:start+800]
                rows.extend(connection.execute(f'SELECT {output} FROM "{table}" WHERE "{field}" IN ({",".join("?" for _ in batch)})',batch))
            return rows
        def remove(database,table,field,values):
            for start in range(0,len(values),800):
                batch=tuple(values[start:start+800])
                statements.append((database,f'DELETE FROM "{table}" WHERE "{field}" IN ({",".join("?" for _ in batch)})',batch))
        database=self.project.paths.embeddings/'embeddings.db'
        if database.exists():
            remember(database)
            with closing(sqlite3.connect(database)) as connection:
                runs={str(row[0]) for row in select(connection,'embedding_run_inputs','metadata_row_id',ids,'DISTINCT run_id')}
                for run_id,label,state,artifact in select(connection,'embedding_runs','id',sorted(runs),'id,label,state,artifact_path'):
                    if state in {'running','queued'}:
                        raise ValueError(f'Stop the running embedding job {label!r} before deleting this table.')
                    descriptions.append(f'Embedding set: {label}')
                    for value in (artifact,self.project.paths.embeddings/'.runs'/run_id):
                        path=owned(value)
                        if path:files.add(path)
                for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                    columns={row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
                    if 'run_id' in columns:remove(database,table,'run_id',sorted(runs))
                remove(database,'embedding_runs','id',sorted(runs))
        # Remove associations, preserving the source assets and records in other tables.
        specs=[(self.project.paths.media/'media_index.db','media_links','metadata_row_id','Media links'),
               (self.project.paths.metadata_groups_database,'group_memberships','metadata_id','Group memberships'),
               (self.project.root/'thumbnails/variants.db','thumbnail_variant_items','metadata_id','Thumbnail associations'),
               (self.project.paths.state/'discovery_pins.db','discovery_pins','metadata_id','Pins'),
               (self.project.paths.state/'discovery_areas.db','polygon_area_members','metadata_id','Marked-area memberships')]
        for database,table,field,label in specs:
            if not database.exists():continue
            remember(database)
            with closing(sqlite3.connect(database)) as connection:
                if not connection.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():continue
                count=sum(int(row[0]) for row in select(connection,table,field,ids,'COUNT(*)'))
                if count:
                    descriptions.append(f'{label}: {count:,}')
                    remove(database,table,field,ids)
                if table=='group_memberships' and runs and connection.execute("SELECT 1 FROM sqlite_master WHERE name='clustering_runs'").fetchone():
                    clusters=select(connection,'clustering_runs','run_id',sorted(runs),'id,state,payload')
                    for cluster_id,state,raw in clusters:
                        if state in {'running','saving'}:raise ValueError('Stop clustering before deleting this table.')
                        group_set=str(json.loads(raw).get('group_set_id') or '')
                        group_ids=[row[0] for row in connection.execute('SELECT group_id FROM groups WHERE group_set_id=?',(group_set,))]
                        remove(database,'group_memberships','group_id',group_ids)
                        remove(database,'groups','group_set_id',[group_set])
                        remove(database,'group_sets','group_set_id',[group_set])
                    if clusters:descriptions.append(f'Clustering results: {len(clusters):,}')
                    remove(database,'clustering_runs','run_id',sorted(runs))
        # Resolve saved projections and snapshots by their canonical item membership.
        documents=[]
        for folder in (self.project.paths.presets,self.project.paths.state,self.project.paths.annotations):
            for path in folder.rglob('*.json'):
                remember(path)
                payload=json.loads(path.read_text(encoding='utf-8'))
                columns,record_ids=_references(payload)
                affected=bool(fields & columns or removed & record_ids)
                coordinates=owned(payload.get('discovery_file','')) if isinstance(payload,dict) else None
                if coordinates:
                    remember(coordinates)
                    affected |= _projection_references(coordinates,ids)
                projection=payload.get('projection',{}) if isinstance(payload,dict) else {}
                snapshot=owned(projection.get('coordinates_ref','')) if isinstance(projection,dict) else None
                if snapshot and snapshot.suffix=='.npz':
                    import numpy as np
                    remember(snapshot)
                    with np.load(snapshot,allow_pickle=False) as archive:
                        affected |= not removed.isdisjoint(map(int,archive['metadata_ids'].reshape(-1)))
                if affected:
                    files.add(path)
                    if coordinates:files.add(coordinates)
                    if snapshot:files.add(snapshot)
                documents.append((path,payload))
        def strings(value):
            if isinstance(value,str):yield value
            elif isinstance(value,dict):
                for item in value.values():yield from strings(item)
            elif isinstance(value,list):
                for item in value:yield from strings(item)
        # Saved compositions may reference another saved result rather than item IDs.
        while True:
            references=runs | {str(path) for path in files} | {path.relative_to(root).as_posix() for path in files}
            added={path for path,payload in documents if path not in files and any(value in references for value in strings(payload))}
            if not added:break
            files.update(added)
        # apply() rescans all documents for new dependencies. Only documents
        # covered by the deletion need protection against intervening edits.
        for path, _ in documents:
            if path not in files and files.isdisjoint(path.parents):
                fingerprints.pop(path, None)
        files={path for path in files if not any(parent in files for parent in path.parents)}
        descriptions.extend(f'Saved data: {path.relative_to(root)}' for path in sorted(files) if path.suffix=='.json')
        remember(repo.database_path)
        mirror=repo._columns_path
        if mirror.exists():files.add(mirror);remember(mirror)
        return TableDeletionPlan(table_id,len(ids),tuple(descriptions),tuple(statements),tuple(sorted(files)),
                                 tuple((path,*value) for path,value in fingerprints.items()))

    def apply(self, plan):
        for path,size,mtime in plan.fingerprints:
            try:
                stat=path.stat()
            except FileNotFoundError as exc:
                raise ValueError(f'{path.name} disappeared after the deletion preview. Review it again.') from exc
            if (stat.st_size,stat.st_mtime_ns)!=(size,mtime):
                raise ValueError(f'{path.name} changed after the deletion preview. Review it again.')
        current = self.preview(plan.table_id)
        if (current.row_count, current.descriptions, current.statements, current.files) != (
                plan.row_count, plan.descriptions, plan.statements, plan.files):
            raise ValueError('Project dependencies changed after the deletion preview. Review it again.')
        root=self.project.root.resolve()
        directory=root/'.recovery'/f'table-{uuid.uuid4().hex}'
        databases={self.store.database} | {database for database,_,_ in plan.statements}
        for database in databases:_copy_database(database,directory/'databases'/database.relative_to(root))
        journal={'phase':'prepared','databases':[str(path.relative_to(root)) for path in databases],
                 'files':[str(path.relative_to(root)) for path in plan.files]}
        write_json(directory/'journal.json',journal)
        try:
            for path in plan.files:
                destination=directory/'files'/path.relative_to(root)
                destination.parent.mkdir(parents=True,exist_ok=True)
                path.replace(destination)
            for database in databases:
                with closing(sqlite3.connect(database)) as connection,connection:
                    for target,query,values in plan.statements:
                        if target==database:connection.execute(query,values)
            self.store.delete(plan.table_id)
            journal['phase']='complete';write_json(directory/'journal.json',journal)
        except BaseException:
            recover_table_removals(root)
            raise
        shutil.rmtree(directory)
