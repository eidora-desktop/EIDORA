from __future__ import annotations

from collections.abc import Callable, Iterator
import json
import os
import shutil
from pathlib import Path
import tempfile

import numpy as np


EMBEDDING_ARTIFACT_SCHEMA_VERSION = 1


class ArtifactStore:
    """Persistence boundary for embedding run artifacts."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.chunks_dir = run_dir / "chunks"

    def init_manifest(self, payload: dict) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(
            self.run_dir / "manifest.json",
            json.dumps({"chunk_schema": 2, **payload}, indent=2, sort_keys=True),
        )

    def read_manifest(self) -> dict | None:
        path = self.run_dir / "manifest.json"
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None

    def append_chunk(
        self,
        chunk_index: int,
        object_ids: np.ndarray,
        embeddings: np.ndarray,
        identifiers: np.ndarray,
    ) -> None:
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        if embeddings.ndim != 2 or len(embeddings) != len(object_ids) or len(identifiers) != len(object_ids):
            raise ValueError("Chunk vectors, IDs and identifiers must have matching row counts.")
        if not np.isfinite(embeddings).all():
            raise ValueError("Embedding vectors contain NaN or infinity.")
        _atomic_write_npy(self.chunks_dir / f"id_{chunk_index:06d}.npy", object_ids.astype(np.int64))
        _atomic_write_npy(self.chunks_dir / f"emb_{chunk_index:06d}.npy", embeddings.astype(np.dtype((self.read_manifest() or {}).get("dtype", "float32")), copy=False))
        _atomic_write_npy(self.chunks_dir / f"name_{chunk_index:06d}.npy", identifiers.astype(np.str_))
        _atomic_write_text(self.chunks_dir / f"commit_{chunk_index:06d}.json", json.dumps({"rows": len(object_ids), "dimensions": embeddings.shape[1]}))

    def iter_chunks_with_names(self) -> Iterator[tuple[np.ndarray, np.ndarray, np.ndarray]]:
        for chunk_index in range(self.chunk_count()):
            yield self._load_committed_chunk(chunk_index)

    def iter_chunks(self) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        for object_ids, embeddings, _identifiers in self.iter_chunks_with_names():
            yield object_ids, embeddings

    def chunk_count(self) -> int:
        count = 0
        committed = (self.read_manifest() or {}).get("chunk_schema", 1) >= 2
        for index in sorted(int(p.stem.split("_")[1]) for p in self.chunks_dir.glob("emb_*.npy")):
            if index != count:
                raise RuntimeError("Embedding chunks are not contiguous; cannot resume safely.")
            if committed and not (self.chunks_dir / f"commit_{index:06d}.json").exists():
                break
            if not all((self.chunks_dir / f"{prefix}_{index:06d}.npy").exists() for prefix in ("id", "name")):
                # A crash between atomic member writes leaves an uncommitted tail.
                break
            count += 1
        return count

    def completed_rows(self) -> int:
        return sum(len(object_ids) for object_ids, _embeddings in self.iter_chunks())

    def _load_committed_chunk(
        self,
        chunk_index: int,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        index = f"{chunk_index:06d}"
        try:
            object_ids = np.load(self.chunks_dir / f"id_{index}.npy", mmap_mode="r")
            embeddings = np.load(self.chunks_dir / f"emb_{index}.npy", mmap_mode="r")
            identifiers = np.load(
                self.chunks_dir / f"name_{index}.npy", mmap_mode="r"
            )
            marker = json.loads(
                (self.chunks_dir / f"commit_{index}.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Embedding chunk {chunk_index} is unreadable.") from exc
        if (
            object_ids.ndim != 1
            or object_ids.dtype.kind not in "iu"
            or embeddings.ndim != 2
            or identifiers.ndim != 1
            or len(embeddings) != len(object_ids)
            or len(identifiers) != len(object_ids)
        ):
            raise RuntimeError(f"Embedding chunk {chunk_index} has inconsistent members.")
        try:
            marker_matches = isinstance(marker, dict) and (
                int(marker.get("rows", -1)) == len(object_ids)
                and int(marker.get("dimensions", -1)) == embeddings.shape[1]
            )
        except (TypeError, ValueError):
            marker_matches = False
        if not marker_matches:
            raise RuntimeError(
                f"Embedding chunk {chunk_index} does not match its commit marker."
            )
        manifest_dtype = (self.read_manifest() or {}).get("dtype")
        if manifest_dtype and np.dtype(manifest_dtype) != embeddings.dtype:
            raise RuntimeError(
                f"Embedding chunk {chunk_index} does not match the run data type."
            )
        return object_ids, embeddings, identifiers

    def clear(self) -> None:
        if not self.run_dir.exists():
            return
        if self.chunks_dir.exists():
            for file_path in self.chunks_dir.glob("*"):
                file_path.unlink()
        cache_dir = self.run_dir / "cache"
        if cache_dir.exists():
            for file_path in cache_dir.glob("*"):
                if file_path.is_file():
                    file_path.unlink()
        manifest = self.run_dir / "manifest.json"
        if manifest.exists():
            manifest.unlink()

    def finalize_npz(
        self,
        output_path: Path,
        *,
        model_id: str,
        model_label: str,
        backend: str = "fake",
        content_types: tuple[str, ...] = (),
        input_count: int,
        check_cancelled: Callable[[], None] | None = None,
    ) -> Path:
        def check() -> None:
            if check_cancelled is not None:
                check_cancelled()

        check()
        first = next(self.iter_chunks_with_names(), None)
        if first is None:
            raise RuntimeError("Cannot finalize embedding artifact without chunks.")
        row_count = 0
        name_bytes = 4
        for ids, vectors, names in self.iter_chunks_with_names():
            check()
            row_count += len(ids)
            name_bytes = max(name_bytes, names.dtype.itemsize)
        if row_count != input_count:
            raise RuntimeError("Committed embedding rows do not match the prepared input count.")
        dimensions = first[1].shape[1]
        cache = self.run_dir / "cache"
        cache.mkdir(exist_ok=True)
        required = row_count * (dimensions * first[1].dtype.itemsize + 8 + name_bytes) * 2
        if shutil.disk_usage(cache).free < required:
            raise RuntimeError(f"Not enough free disk space to finalize embeddings (about {required / 1024**3:.2f} GiB required).")
        embeddings = np.lib.format.open_memmap(cache / "embeddings.npy", mode="w+", dtype=first[1].dtype, shape=(row_count, dimensions))
        object_ids = np.lib.format.open_memmap(cache / "object_ids.npy", mode="w+", dtype=np.int64, shape=(row_count,))
        name_dtype = np.dtype(f"U{name_bytes // 4}")
        identifiers = np.lib.format.open_memmap(cache / "identifiers.npy", mode="w+", dtype=name_dtype, shape=(row_count,))
        offset = 0
        for ids, vectors, names in self.iter_chunks_with_names():
            check()
            if vectors.shape != (len(ids), dimensions) or len(names) != len(ids):
                raise RuntimeError("Inconsistent embedding chunk shape.")
            end = offset + len(ids)
            object_ids[offset:end], embeddings[offset:end], identifiers[offset:end] = ids, vectors, names
            offset = end
        for array in (object_ids, embeddings, identifiers):
            array.flush()
        manifest = self.read_manifest() or {}
        normalized = True
        for start in range(0, row_count, 4096):
            check()
            if not np.allclose(
                np.linalg.norm(embeddings[start:start + 4096], axis=1),
                1.0,
                rtol=1e-5,
                atol=1e-6,
            ):
                normalized = False
                break
        self.init_manifest({**manifest, "raw_dimensions": dimensions, "dimensions": dimensions, "input_count": row_count, "l2_normalized": bool(normalized)})
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd: int | None = None
        tmp_path = ""
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=f".{output_path.stem}.",
                suffix=".npz",
                dir=str(output_path.parent),
            )
            with os.fdopen(tmp_fd, "wb") as handle:
                tmp_fd = None
                check()
                np.savez(
                    handle,
                    artifact_schema_version=EMBEDDING_ARTIFACT_SCHEMA_VERSION,
                    object_ids=object_ids,
                    embeddings=embeddings,
                    filenames=identifiers,
                    model_id=model_id,
                    model_name=model_label,
                    backend=backend,
                    source=(self.read_manifest() or {}).get("source", "runtime_chunks"),
                    input_modalities=json.dumps(list(content_types)),
                    preprocess=json.dumps((self.read_manifest() or {}).get("preprocess", {})),
                    postprocess=json.dumps((self.read_manifest() or {}).get("postprocess", [])),
                    provenance=json.dumps((self.read_manifest() or {}).get("provenance", {})),
                    raw_dimensions=int(embeddings.shape[1]),
                    l2_normalized=(self.read_manifest() or {}).get("l2_normalized", False),
                    input_count=int(input_count),
                    dimensions=int(embeddings.shape[1]),
                )
            check()
            os.replace(tmp_path, output_path)
            stat = output_path.stat()
            _atomic_write_text(cache / "complete.json", json.dumps({"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}))
        finally:
            if tmp_fd is not None:
                os.close(tmp_fd)
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
        return output_path


def _atomic_write_npy(path: Path, array: np.ndarray) -> None:
    tmp_fd: int | None = None
    tmp_path = ""
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".npy", dir=str(path.parent))
        with os.fdopen(tmp_fd, "wb") as handle:
            tmp_fd = None
            np.save(handle, array)
        os.replace(tmp_path, path)
    finally:
        if tmp_fd is not None:
            os.close(tmp_fd)
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _atomic_write_text(path: Path, content: str) -> None:
    tmp_fd: int | None = None
    tmp_path = ""
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            tmp_fd = None
            handle.write(content)
            handle.flush()
        os.replace(tmp_path, path)
    finally:
        if tmp_fd is not None:
            os.close(tmp_fd)
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
