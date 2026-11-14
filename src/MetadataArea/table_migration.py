"""Guard projects using multiple metadata tables from older application versions."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from shared.persistence.json_store import write_json


def prepare_table_storage(database):
    database=Path(database)
    root=database.parent.parent
    manifest=root/'project.json'
    if not manifest.exists():
        return  # Standalone repository tests and recovery tools have no project manifest.
    payload=json.loads(manifest.read_text(encoding='utf-8'))
    if int(payload.get('schema_version',1))>=2:
        return
    backup=root/'.recovery'/'before-metadata-tables'
    backup.mkdir(parents=True,exist_ok=True)
    if not (backup/'metadata.db').exists():
        with closing(sqlite3.connect(database)) as reader, closing(sqlite3.connect(backup/'metadata.db')) as writer:
            reader.backup(writer)
        write_json(backup/'project.json',payload)
    write_json(manifest,{**payload,'schema_version':2})
