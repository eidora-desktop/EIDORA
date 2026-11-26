"""Bounded artifact access and publication for dimensionality reduction."""
from contextlib import contextmanager, ExitStack
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import shutil
import time
import zipfile

import numpy as np

from EmbeddingsArea.runtime.artifacts import ArtifactStore, EMBEDDING_ARTIFACT_SCHEMA_VERSION
from EmbeddingsArea.runtime.readers import load_embedding_summary, resolve_run_dir
from EmbeddingsArea.domain import EmbeddingRunState


def _array_header(handle):
    version = np.lib.format.read_magic(handle)
    if version == (1, 0):
        shape, _, dtype = np.lib.format.read_array_header_1_0(handle)
    elif version in {(2, 0), (3, 0)}:
        shape, _, dtype = np.lib.format.read_array_header_2_0(handle)
    else:
        raise ValueError("Unsupported vector array format.")
    return shape, dtype


def inspect_source(path):
    path = Path(path)
    summary = load_embedding_summary(path)
    if path.is_file():
        with zipfile.ZipFile(path) as archive, archive.open("embeddings.npy") as handle:
            shape, dtype = _array_header(handle)
    else:
        store = ArtifactStore(resolve_run_dir(path))
        with (store.chunks_dir / "emb_000000.npy").open("rb") as handle:
            _, dtype = _array_header(handle)
        shape = (summary["item_count"], summary["dimensions"])
    if len(shape) != 2 or dtype.kind not in "fiu":
        raise ValueError("Vector data must contain a numeric two-dimensional matrix.")
    return summary, shape, dtype


def fingerprint(path):
    path = Path(path)
    members = [path] if path.is_file() else sorted(resolve_run_dir(path).rglob("*"))
    return [(str(member), member.stat().st_size, member.stat().st_mtime_ns)
            for member in members if member.is_file()]


@contextmanager
def source_arrays(path, directory, check):
    """Extract compressed NPZ members to disk, never materialize the full input."""
    path, directory = Path(path), Path(directory)
    with ExitStack() as mappings:
        def mapped(name):
            value = np.load(directory / name, mmap_mode="r", allow_pickle=False)
            mappings.callback(value._mmap.close)
            return value

        if path.is_file():
            with zipfile.ZipFile(path) as archive:
                for key in ("embeddings", "object_ids", "filenames"):
                    name = key + ".npy"
                    if name not in archive.namelist():
                        continue
                    with archive.open(name) as source, (directory / name).open("wb") as target:
                        while True:
                            check()
                            block = source.read(1024 * 1024)
                            if not block:
                                break
                            target.write(block)
            values = mapped("embeddings.npy")
            ids = mapped("object_ids.npy") if (directory / "object_ids.npy").exists() else np.arange(len(values), dtype=np.int64)
            names = mapped("filenames.npy") if (directory / "filenames.npy").exists() else np.asarray([str(i) for i in range(len(values))])
        else:
            store = ArtifactStore(resolve_run_dir(path))
            summary, shape, dtype = inspect_source(path)
            width = 1
            for index in range(store.chunk_count()):
                with (store.chunks_dir / f"name_{index:06d}.npy").open("rb") as handle:
                    _, name_dtype = _array_header(handle)
                    width = max(width, name_dtype.itemsize // 4)
            arrays = []
            for name, array_shape, array_dtype in (("embeddings.npy", shape, dtype),
                    ("object_ids.npy", (shape[0],), np.int64), ("filenames.npy", (shape[0],), f"U{width}")):
                array = np.lib.format.open_memmap(directory / name, mode="w+", dtype=array_dtype, shape=array_shape)
                mappings.callback(array._mmap.close)
                arrays.append(array)
            values, ids, names = arrays
            offset = 0
            for index in range(store.chunk_count()):
                check()
                with ExitStack() as chunk_mappings:
                    chunk_ids, vectors, identifiers = store._load_committed_chunk(index, mappings=chunk_mappings)
                    end = offset + len(vectors)
                    values[offset:end], ids[offset:end], names[offset:end] = vectors, chunk_ids, identifiers
                    offset = end
            for array in arrays:
                array.flush()
        if (values.ndim != 2 or values.dtype.kind not in "fiu" or ids.ndim != 1
                or ids.dtype.kind not in "iu" or names.ndim != 1 or names.dtype.kind not in "US"
                or len(ids) != len(values) or len(names) != len(values)):
            raise ValueError("Vector data and item identities are inconsistent.")
        if len(np.unique(ids)) != len(ids) or (ids.dtype.kind == "u" and ids.size and ids.max() > np.iinfo(np.int64).max):
            raise ValueError("Vector data contains invalid or duplicate item identities.")
        yield values, ids, names


def write_result(path, values, ids, names, summary, recipe, estimator, *, started):
    previous = summary.get("postprocess") or []
    previous = previous if isinstance(previous, list) else [previous]
    provenance = dict(summary.get("provenance") or {})
    provenance["reduction"] = recipe
    np.savez(path, artifact_schema_version=EMBEDDING_ARTIFACT_SCHEMA_VERSION,
             embeddings=values, object_ids=ids, filenames=names,
             model_id=summary.get("model_id") or "", model_name=summary.get("model_name") or "",
             backend=summary.get("backend") or "", source="reduction",
             input_modalities=json.dumps(summary.get("input_modalities") or []),
             preprocess=json.dumps(summary.get("preprocess") or {}),
             raw_dimensions=summary.get("raw_dimensions") or recipe["source_dimensions"],
             l2_normalized=False, input_count=len(values), dimensions=values.shape[1],
             pca_mean=estimator.mean_, pca_components=estimator.components_,
             pca_explained_variance=estimator.explained_variance_)
    recipe["duration_seconds"] = time.monotonic() - started
    with zipfile.ZipFile(path, "a") as archive:
        for key, value in (("provenance", provenance), ("postprocess", [*previous, {"id": "pca", **recipe}])):
            with archive.open(key + ".npy", "w", force_zip64=True) as handle:
                np.lib.format.write_array(handle, np.asarray(json.dumps(value)), allow_pickle=False)



def publish(project, runs, source, output_id, staging, request, expected_fingerprint):
    """Publish a fresh artifact identity, then transactionally switch the run pointer."""
    from uuid import uuid4
    root = project.paths.embeddings
    # Keep the run-ID filename contract used by Discovery's identity resolver.
    destination = root / ".reductions" / uuid4().hex / f"{output_id}.npz"
    now = datetime.now(timezone.utc).isoformat()
    run = replace(source, id=output_id, label=request.label,
                  state=EmbeddingRunState.COMPLETED, progress=100, error_message="",
                  created_at=source.created_at if request.replace_original else now,
                  updated_at=now, artifact_path=str(destination))
    if fingerprint(source.artifact_path) != expected_fingerprint:
        raise ValueError("Source vector data changed during reduction. Try again.")
    destination.parent.mkdir(parents=True)
    try:
        os.replace(staging, destination)
        runs.publish_reduced_run(source, run)
    except Exception:
        destination.unlink(missing_ok=True)
        destination.parent.rmdir()
        raise
    warning = ""
    try:
        if request.replace_original:
            warning = _remove_original(root, runs, source)
    except Exception as exc:
        warning = f"Reduction saved, but original-file cleanup failed: {exc}"
    return run, warning


def _remove_original(root, runs, source):
    warning = ""
    # Only remove owned files; a committed result must survive cleanup failure.
    old = Path(source.artifact_path)
    shared = any(value.id != source.id and value.artifact_path == source.artifact_path for value in runs.list_runs())
    if not shared:
        paths = {old, resolve_run_dir(old), root / ".runs" / source.id}
        for path in paths:
            if not path.resolve().is_relative_to(root.resolve()) or path.resolve() == root.resolve():
                continue
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            except OSError as exc:
                warning = f"Reduction saved, but some original files could not be removed: {exc}"
    return warning
