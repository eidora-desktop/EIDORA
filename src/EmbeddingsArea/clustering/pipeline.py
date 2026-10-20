"""Array-oriented preparation and computation, executed outside the GUI process."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import time
import warnings
import numpy as np
from EmbeddingsArea.clustering.base import normalize_parameters
from EmbeddingsArea.clustering.parameters import PREPROCESSING
from EmbeddingsArea.clustering.registry import ClusteringRegistry


def source_snapshot(project, run_id):
    from EmbeddingsArea.repository import EmbeddingRunRepository
    from MetadataArea.repository import MetadataRepository
    from EmbeddingsArea.runtime.readers import resolve_run_dir
    runs = EmbeddingRunRepository(project.paths.embeddings / "embeddings.db")
    run = runs.get_run(run_id)
    if run is None or not run.artifact_path:
        raise ValueError("The source embedding no longer exists or is incomplete.")
    path = Path(run.artifact_path)
    if not path.is_file():
        path = resolve_run_dir(path)
    if not path.exists():
        raise FileNotFoundError(path)
    files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
    artifacts = [(str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in files]
    metadata = MetadataRepository(project.paths.metadata_database, project.paths.metadata_columns)
    from project.item_repository import ItemRepository

    valid_ids = set(ItemRepository(metadata.database_path).active_ids())
    inputs = runs.fetch_run_inputs(run_id, limit=runs.count_run_inputs(run_id), offset=0)
    mapping = {int(item.ordinal): int(item.metadata_row_id) for item in inputs
               if item.metadata_row_id is not None and item.metadata_row_id in valid_ids}
    signature = hashlib.sha256(json.dumps([artifacts, sorted(mapping.items())]).encode()).hexdigest()
    return path, mapping, signature


def prepare(matrix, object_ids, mapping, preprocessing, metric):
    values = np.asarray(matrix, dtype=np.float32)
    ids = np.asarray(object_ids)
    if values.ndim != 2 or not values.shape[1]:
        raise ValueError("Embeddings must be a two-dimensional matrix.")
    if ids.ndim != 1 or len(ids) != len(values) or ids.dtype.kind not in "iu" or len(np.unique(ids)) != len(ids):
        raise ValueError("Malformed or duplicate embedding object IDs.")
    canonical = np.asarray([mapping.get(int(i), -1) for i in ids], dtype=np.int64)
    finite = np.isfinite(values).all(axis=1)
    matched = canonical >= 0
    reasons = {"non_finite": int((~finite).sum()), "unmatched": int((finite & ~matched).sum()), "zero_vectors": 0}
    keep = finite & matched
    values, canonical = values[keep], canonical[keep]
    if len(np.unique(canonical)) != len(canonical):
        raise ValueError("Duplicate canonical metadata IDs in embedding inputs.")
    if not len(values):
        raise ValueError("No embeddings matched prepared metadata rows.")
    p = normalize_parameters(PREPROCESSING, preprocessing)
    if p["normalize"] or metric == "cosine":
        norms = np.linalg.norm(values.astype(np.float64), axis=1)
        keep = norms > 0
        reasons["zero_vectors"] += int((~keep).sum())
        values, canonical, norms = values[keep], canonical[keep], norms[keep]
        if p["normalize"]:
            values = (values / norms[:, None]).astype(np.float32)
    if len(values) < 2:
        raise ValueError("At least two eligible embeddings are required for clustering.")
    if p["pca"]:
        from sklearn.decomposition import PCA
        if p["components"] > min(values.shape):
            raise ValueError(f"PCA components cannot exceed {min(values.shape)} for these eligible embeddings.")
        estimator = PCA(n_components=p["components"], whiten=p["whiten"], random_state=p["seed"], svd_solver="auto")
        values = estimator.fit_transform(values)
        p["resolved_solver"] = estimator._fit_svd_solver
        if not np.isfinite(values).all():
            raise ValueError("PCA produced non-finite values; disable whitening or reduce components.")
        if metric == "cosine":
            keep = np.linalg.norm(values, axis=1) > 0
            reasons["zero_vectors"] += int((~keep).sum())
            values, canonical = values[keep], canonical[keep]
    return values, canonical, reasons, p


def validate_output(labels, ids, strengths=None):
    labels, ids = np.asarray(labels), np.asarray(ids)
    if labels.ndim != 1 or ids.ndim != 1 or len(labels) != len(ids):
        raise ValueError("Clustering results do not align with metadata rows.")
    if labels.dtype.kind not in "iu" or (labels < -1).any():
        raise ValueError("Clustering labels must be integers with only -1 reserved for noise.")
    if ids.dtype.kind not in "iu" or len(np.unique(ids)) != len(ids):
        raise ValueError("Clustering metadata IDs must be unique integers.")
    if strengths is not None and (np.shape(strengths) != labels.shape or not np.isfinite(strengths).all()):
        raise ValueError("Invalid clustering membership strengths.")


def compute(project, record, directory, status=lambda value: None):
    from EmbeddingsArea.runtime.readers import load_embedding_matrix_with_ids
    from threadpoolctl import threadpool_limits
    import sklearn
    started = time.monotonic()
    status("Loading embeddings")
    path, mapping, signature = source_snapshot(project, record["run_id"])
    matrix, ids, _ = load_embedding_matrix_with_ids(path)
    method = ClusteringRegistry().require_method(record["method_id"])
    parameters = record["method_settings"].get(method.id, {})
    status("Preparing embeddings")
    with warnings.catch_warnings(record=True) as captured, threadpool_limits(limits=1):
        warnings.simplefilter("always")
        values, metadata_ids, reasons, preprocessing = prepare(matrix, ids, mapping, record["preprocessing"], parameters.get("metric", "euclidean"))
        status("Fitting " + method.name)
        output = method.fit(values, parameters)
    status("Finalizing results")
    validate_output(output.labels, metadata_ids, output.strengths)
    if source_snapshot(project, record["run_id"])[2] != signature:
        raise ValueError("Source embeddings or metadata links changed. Retry clustering.")
    directory = Path(directory)
    np.save(directory / "labels.npy", output.labels)
    np.save(directory / "metadata_ids.npy", metadata_ids)
    if output.strengths is not None:
        np.save(directory / "strengths.npy", output.strengths)
    return dict(method_name=method.name, parameters=method.validate(parameters, len(values)),
                preprocessing=preprocessing, source_signature=signature, source_artifact=str(path),
                backend="scikit-learn", backend_version=sklearn.__version__, algorithm_version=1,
                item_count=len(matrix), matched_item_count=len(metadata_ids),
                assigned_item_count=int((output.labels >= 0).sum()), noise_count=int((output.labels == -1).sum()),
                cluster_count=len(np.unique(output.labels[output.labels >= 0])),
                skipped_item_count=sum(reasons.values()), skipped_reasons=reasons,
                diagnostics=output.diagnostics, warnings=list(dict.fromkeys(str(w.message) for w in captured)),
                duration_seconds=time.monotonic()-started)
