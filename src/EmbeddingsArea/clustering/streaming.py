"""File-backed MiniBatch input; preserve the existing sklearn fitting semantics."""
from contextlib import closing, contextmanager
from pathlib import Path

import numpy as np

from EmbeddingsArea.clustering.base import normalize_parameters
from EmbeddingsArea.clustering.parameters import PREPROCESSING
from EmbeddingsArea.clustering.pipeline import _apply_pca, _filter_rows, _normalize_rows
from EmbeddingsArea.runtime.readers import iter_embedding_batches


@contextmanager
def prepared_minibatch_input(path, directory, mapping, preprocessing, *, cpu_budget=None):
    p = normalize_parameters(PREPROCESSING, preprocessing)
    reasons = dict(non_finite=0, unmatched=0, zero_vectors=0)
    source_ids, matched_ids = set(), set()
    parts = []
    item_count = eligible_count = matched_count = 0
    dimensions = None
    staged = Path(directory) / "prepared-vectors.dat"
    try:
        with staged.open("wb") as target, closing(iter_embedding_batches(path, directory)) as batches:
            for matrix, ids in batches:
                if dimensions is not None and matrix.shape[1] != dimensions:
                    raise ValueError("Embedding chunks have inconsistent dimensions.")
                dimensions = matrix.shape[1]
                batch_ids = set(ids.tolist())
                if len(batch_ids) != len(ids) or not source_ids.isdisjoint(batch_ids):
                    raise ValueError("Malformed or duplicate embedding object IDs.")
                source_ids.update(batch_ids)
                item_count += len(matrix)
                values, canonical, skipped = _filter_rows(matrix, ids, mapping)
                matched_count += len(canonical)
                # Check before normalization removes zero rows, as in dense prepare.
                batch_matched = set(canonical.tolist())
                if not matched_ids.isdisjoint(batch_matched):
                    raise ValueError("Duplicate canonical metadata IDs in embedding inputs.")
                matched_ids.update(batch_matched)
                values, canonical = _normalize_rows(values, canonical, skipped, p, "euclidean")
                for key in reasons:
                    reasons[key] += skipped[key]
                values.tofile(target)
                parts.append(canonical)
                eligible_count += len(values)
        if not matched_count:
            raise ValueError("No embeddings matched prepared metadata rows.")
        if eligible_count < 2:
            raise ValueError("At least two eligible embeddings are required for clustering.")
        canonical = np.concatenate(parts)
        # Release Python identity sets before fitting; only O(n) numeric IDs remain.
        del source_ids, matched_ids, parts, matrix, values, ids
        values = np.memmap(staged, mode="r", dtype=np.float32, shape=(eligible_count, dimensions))
        try:
            prepared, canonical = _apply_pca(values, canonical, reasons, p, "euclidean", cpu_budget)
            yield prepared, canonical, reasons, p, item_count
        finally:
            values._mmap.close()
    finally:
        staged.unlink(missing_ok=True)
