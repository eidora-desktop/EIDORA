"""Read-only checks for a self-contained starter collection."""
from contextlib import closing
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re

from shared.persistence.sqlite import connect_read_only

PERSONAL_PATH = re.compile(rb"/(?:Users|home|Volumes)/[^\s\x00\"']+|[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]", re.I)
PATH_FIELDS = frozenset({
    'path', 'absolute_path', 'artifact_path', 'source_artifact', 'source_path',
    'source_dir', 'discovery_key', 'discovery_ref', 'discovery_file',
    'last_discovery_key', 'coordinates_ref', 'thumbnail',
})


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def local_file(root, reference):
    text = str(reference)
    if Path(text).is_absolute() or PureWindowsPath(text).drive or '\\' in text:
        raise ValueError(f'Nonportable starter reference: {text}')
    path = (root / text).resolve()
    if not path.is_relative_to(root.resolve()) or not path.exists():
        raise ValueError(f'Missing or escaping starter reference: {text}')
    return path


def validate_collection(root, expected=None, *, cancelled=lambda: False):
    """Validate stored identities and every media reference without mutating data."""
    import numpy as np
    from project.storage import ensure_current_project_schema
    root = Path(root)
    ensure_current_project_schema(root)
    for path in root.rglob('*'):
        if cancelled():
            raise InterruptedError('Starter installation cancelled.')
        if path.is_symlink():
            raise ValueError(f'Symlink in starter: {path.name}')
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if PERSONAL_PATH.search(raw):
            raise ValueError(f'Personal path in {path.relative_to(root)}')
        if path.suffix in {'.db', '.sqlite'}:
            with closing(connect_read_only(path)) as db:
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError(f'Invalid starter database: {path.name}')
        elif path.suffix == '.npz':
            with np.load(path, allow_pickle=False) as arrays:
                for name in arrays.files:
                    value = arrays[name]
                    if value.dtype.kind in 'US' and PERSONAL_PATH.search(str(value.tolist()).encode()):
                        raise ValueError(f'Personal path in {path.name}:{name}')
    def connect(relative):
        return closing(connect_read_only(root / relative))
    with connect('metadata/metadata.db') as db:
        metadata_ids = {row[0] for row in db.execute('SELECT id FROM metadata')}
    with connect('media/media_index.db') as db:
        for path, in db.execute('SELECT path FROM media_sources'):
            local_file(root, path)
        assets = db.execute('SELECT id, absolute_path FROM media_assets').fetchall()
        for _, path in assets:
            local_file(root, path)
        links = db.execute('SELECT media_asset_id, metadata_row_id FROM media_links').fetchall()
        asset_ids = {row[0] for row in assets}
        if any(item not in metadata_ids or asset not in asset_ids for asset, item in links):
            raise ValueError('Invalid starter media identities.')
    with connect('embeddings/embeddings.db') as db:
        runs = db.execute('SELECT id,artifact_path FROM embedding_runs').fetchall()
        dimensions = []
        for run_id, reference in runs:
            path = local_file(root, reference)
            mapping = dict(db.execute('SELECT ordinal,metadata_row_id FROM embedding_run_inputs WHERE run_id=?', (run_id,)))
            with np.load(path, allow_pickle=False) as data:
                matrix, ids = data['embeddings'], data['object_ids']
                if matrix.shape != (len(metadata_ids), 128) or not np.isfinite(matrix).all():
                    raise ValueError('Invalid starter embedding shape or values.')
                if len(set(ids.tolist())) != len(ids) or {mapping.get(int(i)) for i in ids} != metadata_ids:
                    raise ValueError('Invalid starter embedding identities.')
                dimensions.append(int(matrix.shape[1]))
        for path, in db.execute('SELECT absolute_path FROM embedding_run_inputs'):
            local_file(root, path)
        for payload, in db.execute('SELECT payload FROM embedding_run_input_components'):
            for part in json.loads(payload):
                local_file(root, part['absolute_path'])
    with connect('thumbnails/variants.db') as db:
        thumbnails = db.execute('SELECT metadata_id,relative_path,source_path FROM thumbnail_variant_items').fetchall()
        for item, path, source in thumbnails:
            if item not in metadata_ids:
                raise ValueError('Unknown thumbnail item.')
            local_file(root, 'thumbnails/' + path)
            if source:
                local_file(root, source)
    with connect('metadata/groups.db') as db:
        groups = db.execute('SELECT count(*) FROM group_sets').fetchone()[0]
        if any(row[0] not in metadata_ids for row in db.execute('SELECT metadata_id FROM group_memberships')):
            raise ValueError('Invalid group membership.')
        if any(row[0] not in dict(runs) for row in db.execute('SELECT run_id FROM clustering_runs')):
            raise ValueError('Clustering refers to a removed embedding.')
    for path in (root / 'presets').rglob('*.json'):
        def visit(value, key=''):
            if isinstance(value, dict):
                for k, v in value.items():
                    visit(v, k)
            elif isinstance(value, list):
                for v in value:
                    visit(v)
            elif isinstance(value, str) and value and (key in PATH_FIELDS or key in {'appearance', 'camera', 'filters', 'slicer', 'transform'}):
                if value == 'random' or value.startswith('run:'):
                    return
                if '/' in value or value.endswith(('.json', '.npz', '.sqlite', '.png')):
                    local_file(root, value)
        visit(json.loads(path.read_text()))
    report = dict(items=len(metadata_ids), images=len(assets), media_links=len(links),
                  thumbnails=len(thumbnails), embedding_runs=len(runs), dimensions=dimensions,
                  group_sets=groups, snapshots=len(list((root/'presets/snapshots').glob('*.json'))),
                  projections=len(list((root/'presets/discovery').glob('*.json'))))
    if expected is not None and report != expected:
        raise ValueError(f'Starter content differs from its manifest: {report}')
    return report
