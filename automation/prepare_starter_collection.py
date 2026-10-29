"""Freeze a private working project into the portable MET starter release."""
from contextlib import closing
import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from project.starter_validation import PATH_FIELDS, sha256, validate_collection


def prepare(source, output, attribution, *, version, clustering_note=''):
    if type(version) is not int or version < 1:
        raise ValueError('Use a positive collection version.')
    if output.exists():
        raise FileExistsError('Use a new output directory; existing releases are immutable.')
    source = source.resolve()
    output.mkdir(parents=True)
    before = {str(p.relative_to(source)): sha256(p) for p in source.rglob('*') if p.is_file()}
    with closing(sqlite3.connect((source/'media/media_index.db').as_uri()+'?mode=ro', uri=True)) as db:
        sources = db.execute('SELECT id,path FROM media_sources').fetchall()
    if len(sources) != 1:
        raise ValueError('MET starter expects one image source.')
    images = Path(sources[0][1])
    images = (images if images.is_absolute() else source / images).resolve()
    if not images.is_dir():
        raise ValueError('Starter image source is unavailable.')
    with tempfile.TemporaryDirectory(prefix='starter-prepare-', dir=output) as temp:
        staged = Path(temp)/'MET_Example'
        excluded = shutil.ignore_patterns(
            '.DS_Store', '.runs', '.backups', 'backups', '__pycache__',
            '*.tmp', '*.temp', '*.bak', '*.db-wal', '*.db-shm', '*.db-journal',
            '.recent_projects.json',
        )
        def ignore(directory, names):
            # Preserve empty staging directories without shipping partial work.
            return names if Path(directory).name == '.staging' else excluded(directory, names)
        shutil.copytree(source, staged, ignore=ignore)
        # SQLite backup includes committed WAL data and never copies a live DB mid-write.
        for path in source.rglob('*'):
            if path.suffix in {'.db', '.sqlite'}:
                destination = staged/path.relative_to(source)
                if not destination.exists():
                    continue
                destination.unlink(missing_ok=True)
                with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as reader:
                    with closing(sqlite3.connect(destination)) as writer:
                        reader.backup(writer)
        shutil.copytree(images, staged/'media/media_sources/met_images', ignore=shutil.ignore_patterns('.DS_Store'))
        shutil.copy2(attribution/'README.txt', staged/'COLLECTION_README.txt')
        shutil.copy2(attribution/'field_dictionary.json', staged/'field_dictionary.json')
        if clustering_note:
            with (staged/'COLLECTION_README.txt').open('a', encoding='utf-8') as stream:
                stream.write('\nSaved clustering provenance\n' + clustering_note + '\n')
        def convert(value, key=''):
            if isinstance(value, dict):
                return {k: convert(v, k) for k,v in value.items()}
            if isinstance(value, list):
                return [convert(v, key) for v in value]
            if isinstance(value, str) and value and key in PATH_FIELDS:
                path = Path(value)
                if path.is_absolute():
                    if path.is_relative_to(source):
                        return path.relative_to(source).as_posix()
                    if path.is_relative_to(images):
                        return (Path('media/media_sources/met_images')/path.relative_to(images)).as_posix()
                    raise ValueError(f'External dependency in {key}: {value}')
            return value
        for path in staged.rglob('*.json'):
            data = json.loads(path.read_text())
            path.write_text(json.dumps(convert(data), indent=2, ensure_ascii=False)+'\n')
        # Saved controls and compositions are release contents, not disposable UI cache.
        for directory in ('presets', 'state'):
            for path in (source/directory).rglob('*'):
                if not path.is_file() or path.suffix in {'.db', '.sqlite'}:
                    continue
                if path.name in excluded(path.parent, [path.name]):
                    continue
                saved = staged/path.relative_to(source)
                if path.suffix == '.json':
                    if json.loads(saved.read_text()) != convert(json.loads(path.read_text())):
                        raise ValueError(f'Saved settings changed during preparation: {path.name}')
                elif sha256(path) != sha256(saved):
                    raise ValueError(f'Saved view payload changed during preparation: {path.name}')
        # Structured fields only; artwork text and URLs are not rewritten.
        path_columns = {
            'embedding_runs': ['artifact_path'], 'embedding_run_inputs': ['absolute_path'],
            'media_sources': ['path'], 'media_assets': ['absolute_path'],
            'thumbnail_variants': ['source_dir'], 'thumbnail_variant_items': ['source_path'],
        }
        json_columns = {'embedding_run_input_components': ['payload'], 'clustering_runs': ['payload']}
        with closing(sqlite3.connect(staged/'embeddings/embeddings.db')) as db:
            run_ids = {row[0] for row in db.execute('SELECT id FROM embedding_runs')}
        for path in staged.rglob('*'):
            if path.suffix not in {'.db','.sqlite'}:
                continue
            with closing(sqlite3.connect(path)) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'clustering_runs' in tables:
                    for key,run_id,payload in db.execute('SELECT id,run_id,payload FROM clustering_runs').fetchall():
                        if run_id not in run_ids:
                            record = json.loads(payload)
                            if db.execute('SELECT 1 FROM group_sets WHERE group_set_id=?', (record.get('group_set_id',''),)).fetchone():
                                raise ValueError('Removed embedding still owns a published group set.')
                            if any(key in p.read_text() for p in (staged/'presets').rglob('*.json')):
                                raise ValueError('Removed clustering is still referenced by a preset.')
                            db.execute('DELETE FROM clustering_runs WHERE id=?', (key,))
                        elif clustering_note:
                            record = json.loads(payload)
                            record['warnings'] = list(dict.fromkeys([*record.get('warnings', []), clustering_note]))
                            db.execute('UPDATE clustering_runs SET payload=? WHERE id=?', (json.dumps(record), key))
                for table in ['processes','media_scan_events']:
                    if table in tables:
                        db.execute(f'DELETE FROM {table}')
                for mapping, is_json in [(path_columns,False),(json_columns,True)]:
                    for table,columns in mapping.items():
                        if table not in tables:
                            continue
                        for column in columns:
                            for row_id,value in db.execute(f'SELECT rowid,{column} FROM {table}').fetchall():
                                replacement = json.dumps(convert(json.loads(value))) if is_json else convert(value,column)
                                db.execute(f'UPDATE {table} SET {column}=? WHERE rowid=?', (replacement,row_id))
                db.commit()
                db.execute('VACUUM')
        counts = validate_collection(staged)
        if counts['items'] != 5400 or counts['embedding_runs'] != 2:
            raise ValueError(f'Unexpected MET contents: {counts}')
        archive_name = f'MET_Example-v{version}.zip'
        archive_path = output/archive_name
        temporary_archive = output/f'.{archive_name}.tmp'
        total = 0
        with zipfile.ZipFile(temporary_archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in [staged, *sorted(staged.rglob('*'))]:
                relative = path.relative_to(staged.parent).as_posix() + ('/' if path.is_dir() else '')
                info = zipfile.ZipInfo(relative, date_time=(2020,1,1,0,0,0))
                info.create_system = 3
                info.external_attr = (0o40755 if path.is_dir() else 0o100644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                if path.is_dir():
                    archive.writestr(info,b'')
                else:
                    total += path.stat().st_size
                    with path.open('rb') as reader, archive.open(info,'w') as writer:
                        shutil.copyfileobj(reader,writer)
        after = {str(p.relative_to(source)): sha256(p) for p in source.rglob('*') if p.is_file()}
        if before != after:
            temporary_archive.unlink()
            raise RuntimeError('Working project changed during preparation; retry after saving and closing it.')
        temporary_archive.replace(archive_path)
        manifest = dict(schema_version=1, collection_id='MET_Example', version=version,
                        archive=archive_path.name, sha256=sha256(archive_path),
                        archive_bytes=archive_path.stat().st_size, extracted_bytes=total,
                        url=f'https://github.com/eidora-desktop/EIDORA/releases/download/starter-met-v{version}/{archive_name}',
                        counts=counts)
        (output/'starter-collection.json').write_text(json.dumps(manifest,indent=2)+'\n')
        print(json.dumps(manifest,indent=2))
        return archive_path


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--attribution',type=Path,required=True)
    parser.add_argument('--version',type=int,required=True)
    parser.add_argument('--clustering-note',default='',help='Provenance note for preserved clustering results in this release copy.')
    args=parser.parse_args()
    prepare(args.project,args.output,args.attribution,version=args.version,clustering_note=args.clustering_note)
