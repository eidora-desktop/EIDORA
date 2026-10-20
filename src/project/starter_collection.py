"""Install the bundled starter once per workspace, independently of app upgrades."""
from contextlib import contextmanager
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import zipfile

from project.service import ProjectService
from project.starter_validation import sha256, validate_collection
from shared.persistence.json_store import write_json


def bundled_resources():
    root = Path(__file__).parent / 'resources'
    return root / 'starter-collection.json'


def read_manifest(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if (value.get('schema_version') != 1 or value.get('collection_id') != 'MET_Example'
            or not isinstance(value.get('version'), int) or value['version'] < 1
            or len(value.get('sha256', '')) != 64
            or value.get('archive_bytes', 0) <= 0 or value.get('extracted_bytes', 0) <= 0
            or Path(value.get('archive', '')).name != value.get('archive')
            or not value['archive'].endswith('.zip')):
        raise ValueError('Invalid starter collection manifest.')
    return value


@contextmanager
def workspace_lock(workspace):
    """OS-owned locks are released even after a crash; the lock file may remain."""
    with (workspace / '.starter-collection.lock').open('a+b') as stream:
        stream.seek(0)
        try:
            if sys.platform == 'win32':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Another EIDORA window is installing the starter collection.') from exc
        try:
            if stream.seek(0, 2) == 0:
                stream.write(b'0')
                stream.flush()
            yield
        finally:
            if sys.platform == 'win32':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def install_starter(service: ProjectService, manifest_path: Path, *, progress=lambda value, text: None,
                    cancelled=lambda: False, recreate=False):
    workspace = service.workspace_root
    receipt = workspace / '.starter-collection.json'
    journal = workspace / '.starter-collection-pending.json'
    stage = workspace / '.starter-collection-staging'
    def check():
        if cancelled():
            raise InterruptedError('Starter installation cancelled.')
    with workspace_lock(workspace):
        if receipt.exists() and not recreate:
            return None
        manifest = read_manifest(manifest_path)
        origin = {'collection_id': manifest['collection_id'], 'version': manifest['version'], 'sha256': manifest['sha256']}
        # Recover a completed rename if registration or the receipt write was interrupted.
        if journal.exists():
            pending = json.loads(journal.read_text())
            name = pending.get('destination', '')
            if not name or Path(name).name != name or not name.startswith('MET_Example'):
                raise ValueError('Invalid pending starter destination.')
            destination = workspace / name
            marker = destination / '.starter-origin.json'
            if marker.is_file() and json.loads(marker.read_text()) == pending.get('origin'):
                project = service.open_project(destination)
                write_json(receipt, pending)
                journal.unlink()
                return project
            journal.unlink()
        if stage.exists():
            if stage.is_symlink():
                raise ValueError('Unsafe starter staging directory.')
            shutil.rmtree(stage)
        archive_path = Path(manifest_path).parent / manifest['archive']
        progress(0, 'Checking starter collection…')
        check()
        if archive_path.stat().st_size != manifest['archive_bytes'] or sha256(archive_path) != manifest['sha256']:
            raise ValueError('The bundled starter collection is damaged.')
        if shutil.disk_usage(workspace).free < manifest['extracted_bytes'] + 8 * 1024**2:
            raise OSError('Not enough free disk space for the starter collection.')
        destination = service._next_available_project_path('MET_Example')
        published = False
        try:
            stage.mkdir()
            with zipfile.ZipFile(archive_path) as archive:
                entries = archive.infolist()
                names = set()
                total = sum(info.file_size for info in entries)
                if total != manifest['extracted_bytes']:
                    raise ValueError('Starter archive size differs from its manifest.')
                for info in entries:
                    parts = PurePosixPath(info.filename).parts
                    mode = info.external_attr >> 16
                    if (not parts or parts[0] != 'MET_Example' or '..' in parts
                            or '\\' in info.filename or any(':' in part for part in parts)
                            or info.filename.startswith('/') or stat.S_ISLNK(mode)
                            or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))):
                        raise ValueError('Unsafe starter archive entry.')
                    key = '/'.join(parts).casefold()
                    if key in names:
                        raise ValueError('Duplicate starter archive entry.')
                    names.add(key)
                completed = 0
                for info in entries:
                    check()
                    path = stage.joinpath(*PurePosixPath(info.filename).parts[1:])
                    if info.is_dir():
                        path.mkdir(parents=True, exist_ok=True)
                        continue
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as reader, path.open('wb') as writer:
                        while chunk := reader.read(1024 * 1024):
                            check()
                            writer.write(chunk)
                            completed += len(chunk)
                    progress(min(90, int(90 * completed / max(1,total))), 'Preparing MET_Example…')
            progress(92, 'Verifying images, embeddings, and saved settings…')
            validate_collection(stage, manifest['counts'], cancelled=cancelled)
            check()
            write_json(stage / '.starter-origin.json', origin)
            pending = {'destination': destination.name, 'origin': origin}
            write_json(journal, pending)
            progress(99, 'Adding MET_Example to your projects…')
            # Do not accept cancellation after publication begins.
            if destination.exists():
                raise FileExistsError(destination)
            stage.rename(destination)
            published = True
            project = service.open_project(destination)
            write_json(receipt, pending)
            journal.unlink()
            progress(100, 'MET_Example is ready')
            return project
        finally:
            if stage.exists():
                shutil.rmtree(stage)
            if not published:
                journal.unlink(missing_ok=True)
