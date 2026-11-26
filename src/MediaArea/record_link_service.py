"""Explicitly assign one indexed media asset to many independent records."""
from datetime import datetime, timezone
from pathlib import Path
import uuid

from EmbeddingsArea.media_library import MediaLibraryService
from EmbeddingsArea.repository import MediaLinkRepository, MediaRepository
from MetadataArea.repository import MetadataRepository


def assign_source(project, record_ids, path):
    ids = tuple(dict.fromkeys(int(value) for value in record_ids))
    if not ids:
        raise ValueError('Select records first.')
    media = MediaRepository(project.paths.media / 'media_index.db')
    metadata = MetadataRepository(project.paths.metadata_database)
    library = MediaLibraryService(project, media, metadata, MediaLinkRepository(media.database_path))
    source = library.register_source(Path(path))
    library.scan_source(source.id)
    assets = media.fetch_assets(source.id, limit=2)
    if len(assets) != 1:
        raise ValueError('Choose an individual source file.')
    asset = assets[0]
    now = datetime.now(timezone.utc).isoformat()
    with metadata.media_publication(media.database_path) as connection:
        tables = ['metadata']
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='metadata_tables'").fetchone():
            tables = [row[0] for row in connection.execute('SELECT id FROM metadata_tables')]
        for identity in ids:
            if not any(connection.execute(
                    f'SELECT 1 FROM {MetadataRepository._quote_identifier(table)} WHERE id=?', (identity,)).fetchone()
                    for table in tables):
                raise ValueError('A selected record no longer exists.')
        connection.executemany(
            "DELETE FROM media_store.media_links WHERE metadata_row_id=? AND media_asset_id IN (SELECT id FROM media_store.media_assets WHERE content_type=?)",
            ((identity,asset.content_type.value) for identity in ids),
        )
        connection.executemany(
            "INSERT INTO media_store.media_links(id,media_asset_id,source_id,metadata_row_id,metadata_column,matched_value,strategy,status,confidence,is_manual,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ((uuid.uuid4().hex,asset.id,source.id,identity,'','', 'identifier_equals','linked',1.0,1,now,now) for identity in ids),
        )
        MediaLinkRepository._finish_publication(connection, source.id, now)
        metadata.bump_revision(connection)
    return len(ids)
