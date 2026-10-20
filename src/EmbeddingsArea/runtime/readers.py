from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import zipfile

import numpy as np

from EmbeddingsArea.runtime.artifacts import ArtifactStore
from EmbeddingsArea.runtime.checkpoints import CheckpointStore


def resolve_run_dir(path: Path) -> Path:
    if path.is_dir() and (path / "manifest.json").exists() and (path / "chunks").exists():
        return path
    return path.parent / ".runs" / path.stem


def load_embeddings_payload(path: Path) -> tuple[np.ndarray, list[str], dict[str, Any]]:
    if path.exists() and path.is_file():
        return _load_npz_payload(path)
    run_dir = resolve_run_dir(path)
    if run_dir.exists():
        return _load_run_payload(run_dir)
    raise FileNotFoundError(path)


def load_embeddings_payload_with_ids(path: Path) -> tuple[np.ndarray, list[str], np.ndarray, dict[str, Any]]:
    if path.exists() and path.is_file():
        matrix, names, object_ids, meta = _load_npz_payload_with_ids(path)
        return matrix, names, object_ids, meta
    run_dir = resolve_run_dir(path)
    if run_dir.exists():
        matrix, names, object_ids, meta = _load_run_payload_with_ids(run_dir)
        return matrix, names, object_ids, meta
    raise FileNotFoundError(path)


def load_embedding_matrix_with_ids(path: Path) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Load projection inputs without materializing unused identifier strings."""
    if path.exists() and path.is_file():
        cache = _valid_cache(path)
        if cache is not None:
            return np.load(cache / "embeddings.npy", mmap_mode="r"), np.load(cache / "object_ids.npy", mmap_mode="r"), load_embedding_summary(path)
        with np.load(path, allow_pickle=False) as data:
            embeddings = data.get("embeddings")
            object_ids = data.get("object_ids")
            if embeddings is None:
                raise RuntimeError("Embeddings file is missing the 'embeddings' array.")
            matrix = np.asarray(embeddings, dtype=np.float32)
            ids = (
                _validated_ids(object_ids)
                if object_ids is not None
                else np.arange(len(matrix), dtype=np.int64)
            )
        if matrix.ndim != 2:
            raise RuntimeError("Embeddings array must be 2-dimensional.")
        if ids.ndim != 1 or len(ids) != len(matrix):
            raise ValueError("Embedding object IDs must be a one-dimensional array matching the vector count.")
        return matrix, ids, load_embedding_summary(path)

    run_dir = resolve_run_dir(path)
    if not run_dir.exists():
        raise FileNotFoundError(path)
    chunks = list(ArtifactStore(run_dir).iter_chunks())
    if not chunks:
        raise RuntimeError(f"No chunk embeddings found in {run_dir}")
    matrix = np.concatenate([chunk[1] for chunk in chunks]).astype(np.float32)
    object_ids = np.concatenate([chunk[0] for chunk in chunks]).astype(np.int64)
    return matrix, object_ids, load_embedding_summary(run_dir)


def load_embedding_summary(path: Path) -> dict[str, Any]:
    if path.exists() and path.is_file():
        with np.load(path, allow_pickle=False, mmap_mode="r") as data:
            shape = _npz_array_shape(path, "embeddings")
            dimensions = data.get("dimensions")
            return {
                "model_id": _to_scalar_str(data.get("model_id")),
                "model_name": _to_scalar_str(data.get("model_name")),
                "backend": _to_scalar_str(data.get("backend")),
                "source": _to_scalar_str(data.get("source")),
                "input_modalities": _to_list(data.get("input_modalities")),
                "preprocess": _maybe_json(data.get("preprocess")),
                "postprocess": _maybe_json(data.get("postprocess")),
                "raw_dimensions": _to_scalar_int(data.get("raw_dimensions")),
                "l2_normalized": _to_scalar_bool(data.get("l2_normalized")),
                "pca_reduced": _has_pca(_maybe_json(data.get("postprocess"))),
                "item_count": int(shape[0]) if shape else _to_scalar_int(data.get("input_count")),
                "dimensions": _to_scalar_int(dimensions)
                if dimensions is not None
                else int(shape[1])
                if len(shape) >= 2
                else None,
                "format": "npz",
                "provenance": _maybe_json(data.get("provenance")),
            }

    run_dir = resolve_run_dir(path)
    if not run_dir.exists():
        raise FileNotFoundError(path)
    store = ArtifactStore(run_dir)
    manifest = store.read_manifest() or {}
    item_count = 0
    dimensions = None
    for _object_ids, embeddings in store.iter_chunks():
        item_count += int(embeddings.shape[0])
        if dimensions is None and len(embeddings.shape) >= 2:
            dimensions = int(embeddings.shape[1])
    return {
        "model_id": str(manifest.get("model_id")) if manifest.get("model_id") is not None else None,
        "model_name": str(manifest.get("model_name")) if manifest.get("model_name") is not None else None,
        "backend": str(manifest.get("backend")) if manifest.get("backend") is not None else None,
        "source": str(manifest.get("source") or "runtime_chunks"),
        "input_modalities": _to_list(manifest.get("input_modalities")),
        "preprocess": _maybe_json(manifest.get("preprocess")),
        "postprocess": _maybe_json(manifest.get("postprocess")),
        "raw_dimensions": _to_scalar_int(manifest.get("raw_dimensions")),
        "l2_normalized": _to_scalar_bool(manifest.get("l2_normalized")),
        "pca_reduced": _has_pca(_maybe_json(manifest.get("postprocess"))),
        "item_count": item_count,
        "dimensions": dimensions,
        "format": "runtime_chunks",
        "provenance": _maybe_json(manifest.get("provenance")),
    }


def inspect_resumable_runtime_run(output_file: Path) -> dict[str, Any]:
    run_dir = resolve_run_dir(output_file)
    checkpoint = CheckpointStore(run_dir).load()
    if checkpoint is None:
        return {"resumable": False, "reason": "Missing checkpoint."}
    if not (run_dir / "manifest.json").exists():
        return {"resumable": False, "reason": "Missing run manifest."}
    if not (run_dir / "chunks").is_dir():
        return {"resumable": False, "reason": "Missing chunk directory."}
    if checkpoint.total_items <= 0:
        return {"resumable": False, "reason": "Invalid checkpoint total_items."}
    if not (0 <= checkpoint.next_index < checkpoint.total_items):
        return {"resumable": False, "reason": "Checkpoint not resumable."}
    return {
        "resumable": True,
        "next_index": checkpoint.next_index,
        "total_items": checkpoint.total_items,
        "completed_chunks": checkpoint.completed_chunks,
        "failed_items": checkpoint.failed_items,
    }


def clear_runtime_artifacts(output_file: Path) -> None:
    run_dir = resolve_run_dir(output_file)
    ArtifactStore(run_dir).clear()
    CheckpointStore(run_dir).clear()


def _load_npz_payload(path: Path) -> tuple[np.ndarray, list[str], dict[str, Any]]:
    matrix, names, _object_ids, meta = _load_npz_payload_with_ids(path)
    return matrix, names, meta


def _load_npz_payload_with_ids(path: Path) -> tuple[np.ndarray, list[str], np.ndarray, dict[str, Any]]:
    cache = _valid_cache(path)
    if cache is not None:
        return (np.load(cache / "embeddings.npy", mmap_mode="r"),
                np.load(cache / "identifiers.npy", mmap_mode="r").tolist(),
                np.load(cache / "object_ids.npy", mmap_mode="r"), load_embedding_summary(path))
    with np.load(path, allow_pickle=False) as data:
        embeddings = data.get("embeddings")
        filenames = data.get("filenames")
        object_ids = data.get("object_ids")
        meta = load_embedding_summary(path)
    if embeddings is None:
        raise RuntimeError("Embeddings file is missing the 'embeddings' array.")
    matrix = np.asarray(embeddings)
    if matrix.ndim != 2:
        raise RuntimeError("Embeddings array must be 2-dimensional.")
    names = [str(name) for name in filenames.tolist()] if filenames is not None else []
    if len(names) != len(matrix):
        names = [str(index) for index in range(len(matrix))]
    ids = _validated_ids(object_ids) if object_ids is not None else np.arange(len(matrix), dtype=np.int64)
    if ids.ndim != 1 or len(ids) != len(matrix):
        raise ValueError("Embedding object IDs must be a one-dimensional array matching the vector count.")
    return matrix, names, ids, meta


def _load_run_payload(run_dir: Path) -> tuple[np.ndarray, list[str], dict[str, Any]]:
    matrix, names, _object_ids, meta = _load_run_payload_with_ids(run_dir)
    return matrix, names, meta


def _load_run_payload_with_ids(run_dir: Path) -> tuple[np.ndarray, list[str], np.ndarray, dict[str, Any]]:
    chunks = list(ArtifactStore(run_dir).iter_chunks_with_names())
    if not chunks:
        raise RuntimeError(f"No chunk embeddings found in {run_dir}")
    matrix = np.concatenate([chunk[1] for chunk in chunks]).astype(np.float32)
    names = [str(name) for chunk in chunks for name in chunk[2].tolist()]
    object_ids = np.concatenate([chunk[0] for chunk in chunks]).astype(np.int64)
    meta = load_embedding_summary(run_dir)
    return matrix, names, object_ids, meta


def _has_pca(value: Any) -> bool:
    if isinstance(value, list):
        return any(isinstance(step, dict) and str(step.get("id") or "").strip().lower() == "pca" for step in value)
    if isinstance(value, dict):
        return any(value.get(key) not in (None, "", 0, "0", False) for key in ("pca", "pca_components"))
    return False


def _to_scalar(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "shape") and getattr(value, "shape", ()) == ():
        try:
            return value.item()
        except Exception:
            return value
    return value


def _to_scalar_str(value: Any) -> str | None:
    scalar = _to_scalar(value)
    return None if scalar is None else str(scalar)


def _to_scalar_int(value: Any) -> int | None:
    scalar = _to_scalar(value)
    if scalar is None:
        return None
    try:
        return int(scalar)
    except Exception:
        return None


def _to_scalar_bool(value: Any) -> bool | None:
    scalar = _to_scalar(value)
    if scalar is None:
        return None
    if isinstance(scalar, (bool, np.bool_)):
        return bool(scalar)
    raw = str(scalar).strip().lower()
    if raw in {"true", "1", "yes"}:
        return True
    if raw in {"false", "0", "no"}:
        return False
    return None


def _to_list(value: Any) -> list[str]:
    scalar = _to_scalar(value)
    if scalar is None:
        return []
    if isinstance(scalar, str):
        parsed = _maybe_json(scalar)
        if isinstance(parsed, list):
            return [str(item) for item in parsed if item is not None]
        stripped = scalar.strip()
        return [stripped] if stripped else []
    if isinstance(scalar, (list, tuple)):
        return [str(item) for item in scalar if item is not None]
    return []


def _maybe_json(value: Any) -> Any:
    scalar = _to_scalar(value)
    if scalar is None:
        return None
    if isinstance(scalar, (dict, list)):
        return scalar
    raw = str(scalar).strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _npz_array_shape(path: Path, key: str) -> tuple[int, ...]:
    """Read only the NPY header inside a ZIP member, never its array payload."""
    with zipfile.ZipFile(path) as archive:
        if f"{key}.npy" not in archive.namelist():
            return ()
        with archive.open(f"{key}.npy") as handle:
            version = np.lib.format.read_magic(handle)
            shape, _, _ = (np.lib.format.read_array_header_1_0(handle) if version == (1, 0) else np.lib.format.read_array_header_2_0(handle))
            return shape


def _valid_cache(path: Path) -> Path | None:
    cache = resolve_run_dir(path) / "cache"
    marker = cache / "complete.json"
    if not marker.exists():
        return None
    try:
        stamp = json.loads(marker.read_text(encoding="utf-8"))
        stat = path.stat()
        if stamp != {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}:
            return None
        if all((cache / name).exists() for name in ("embeddings.npy", "object_ids.npy", "identifiers.npy")):
            return cache
    except (OSError, ValueError):
        pass
    return None


def _validated_ids(values) -> np.ndarray:
    ids = np.asarray(values)
    if ids.dtype.kind not in "iu" or ids.ndim != 1:
        raise ValueError("Embedding object IDs must be integer ordinals.")
    if ids.dtype.kind == "u" and ids.size and ids.max() > np.iinfo(np.int64).max:
        raise ValueError("Embedding object IDs exceed the supported integer range.")
    if len(np.unique(ids)) != len(ids):
        raise ValueError("Embedding object IDs must be unique.")
    return ids.astype(np.int64, copy=False)
