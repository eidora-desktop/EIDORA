"""Small, disposable batch profiles and CoreML caches, separate from project data."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import platform
import shutil

from filelock import FileLock, Timeout

from EmbeddingsArea.runtime.checkpoints import _atomic_write_text


class BatchProfile:
    def __init__(self, identity: dict, *, root: Path | None = None) -> None:
        from shared.paths import application_cache_root
        from importlib.metadata import PackageNotFoundError, version
        from eidora_version import __version__
        import psutil

        try:
            runtime_version = version('onnxruntime')
        except PackageNotFoundError:
            runtime_version = 'bundled'  # Frozen builds may omit distribution metadata.
        identity = {**identity, 'app': __version__, 'runtime': runtime_version, 'system': platform.platform(),
                    'machine': platform.machine(), 'cpu': platform.processor(),
                    'ram': psutil.virtual_memory().total, 'cpus': psutil.cpu_count()}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        self.root = root or application_cache_root() / 'embedding-batches'
        self.directory = self.root / key
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(self.root / f'{key}.lock')
        self.lock.acquire(timeout=0)

    def read_size(self) -> int | None:
        try:
            data = json.loads((self.directory / 'batch.json').read_text())
            # Older profiles may contain a temporary safety reduction.
            if not isinstance(data, dict) or data.get('policy') != 4:
                return None
            value = data['size']
            return value if isinstance(value, int) and 1 <= value <= 512 else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save_size(self, size: int) -> None:
        _atomic_write_text(self.directory / 'batch.json', json.dumps({'size': size, 'policy': 4}))

    def coreml_directory(self, size: int, runtime_version: str = '') -> Path:
        # Shapes must have distinct cache keys even when the ONNX path is identical.
        directory = self.directory / f'{size}-{runtime_version}'
        directory.mkdir(exist_ok=True)
        directory.touch()
        return directory

    def close(self) -> None:
        try:
            self.directory.touch(exist_ok=True)
            self.lock.release()
            # Only prune between executions. Never remove another process's live cache.
            directories = sorted((p for p in self.root.iterdir() if p.is_dir()),
                                 key=lambda p: p.stat().st_mtime, reverse=True)
            total = 0
            for index, directory in enumerate(directories):
                try:
                    with FileLock(self.root / f'{directory.name}.lock', timeout=0):
                        size = sum(p.stat().st_size for p in directory.rglob('*') if p.is_file())
                        if index >= 32:
                            shutil.rmtree(directory)
                        else:
                            # Keep the small learned profile when compiled models
                            # exceed the budget; evict older shapes before the last used.
                            compiled = sorted((p for p in directory.iterdir() if p.is_dir()),
                                              key=lambda p: p.stat().st_mtime)
                            for cached in compiled:
                                if total + size <= 512 * 1024 ** 2:
                                    break
                                removed = sum(p.stat().st_size for p in cached.rglob('*') if p.is_file())
                                shutil.rmtree(cached)
                                size -= removed
                            total += size
                except (OSError, Timeout):
                    continue
        except OSError:
            pass  # Disposable cache maintenance must never change a run's outcome.
        finally:
            self.lock.release()
