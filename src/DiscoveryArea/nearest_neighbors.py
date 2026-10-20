from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

SEARCH_CHUNK_ROWS = 16_384


class NeighborSearchCancelled(RuntimeError):
    """Raised when a nearest-neighbor request is cancelled cooperatively."""


@dataclass(frozen=True)
class EmbeddingNeighborIndex:
    vectors: np.ndarray
    metadata_ids: np.ndarray


@dataclass(frozen=True)
class NeighborSearchRequest:
    query_metadata_id: int
    candidate_metadata_ids: tuple[int, ...]
    count: int = 10
    metric: str = "cosine"
    embedding_run_id: str = ""
    include_all_candidates: bool = False


@dataclass(frozen=True)
class NeighborEmbeddingSource:
    run_id: str
    label: str
    item_count: int


@dataclass(frozen=True)
class NeighborMatch:
    metadata_id: int
    score: float


@dataclass(frozen=True)
class NeighborSearchResult:
    query_metadata_id: int
    metric: str
    matches: tuple[NeighborMatch, ...]


def build_neighbor_index(
    vectors: np.ndarray,
    metadata_ids: np.ndarray,
) -> EmbeddingNeighborIndex:
    matrix = np.asarray(vectors, dtype=np.float32)
    ids = np.asarray(metadata_ids, dtype=np.int64).reshape(-1)
    if matrix.ndim != 2:
        raise ValueError("Embedding vectors must be a two-dimensional array.")
    if matrix.shape[0] != ids.size:
        raise ValueError("Embedding vectors and metadata IDs are not aligned.")
    if ids.size and np.unique(ids).size != ids.size:
        raise ValueError("Embedding metadata IDs must be unique.")
    return EmbeddingNeighborIndex(
        vectors=np.ascontiguousarray(matrix, dtype=np.float32),
        metadata_ids=np.ascontiguousarray(ids, dtype=np.int64),
    )


def search_neighbors(
    index: EmbeddingNeighborIndex,
    request: NeighborSearchRequest,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> NeighborSearchResult:
    is_cancelled = cancelled or (lambda: False)
    metric = str(request.metric or "cosine").strip().lower()
    if metric not in {"cosine", "euclidean"}:
        raise ValueError(f"Unsupported nearest-neighbor metric: {request.metric}")
    if is_cancelled():
        raise NeighborSearchCancelled()

    matrix = np.asarray(index.vectors, dtype=np.float32)
    ids = np.asarray(index.metadata_ids, dtype=np.int64).reshape(-1)
    query_positions = np.flatnonzero(ids == int(request.query_metadata_id))
    if query_positions.size == 0:
        raise ValueError("The selected item has no corresponding embedding vector.")
    query_index = int(query_positions[0])
    query = matrix[query_index]
    if not bool(np.isfinite(query).all()):
        raise ValueError("The selected item has an invalid embedding vector.")

    if request.include_all_candidates:
        eligible = np.ones(ids.size, dtype=bool)
    else:
        wanted = np.asarray(request.candidate_metadata_ids, dtype=np.int64).reshape(-1)
        eligible = np.isin(ids, wanted, assume_unique=False)
    eligible[query_index] = False
    candidate_rows = np.flatnonzero(eligible)
    if candidate_rows.size == 0:
        return NeighborSearchResult(int(request.query_metadata_id), metric, ())
    candidate_ids = ids[candidate_rows]
    if is_cancelled():
        raise NeighborSearchCancelled()

    scores = np.empty(candidate_rows.size, dtype=np.float64)
    if metric == "cosine":
        query_norm = float(np.linalg.norm(query))
        if not np.isfinite(query_norm) or query_norm <= 0.0:
            raise ValueError("Cosine similarity is unavailable for a zero-length query vector.")
        for start in range(0, candidate_rows.size, SEARCH_CHUNK_ROWS):
            if is_cancelled():
                raise NeighborSearchCancelled()
            stop = min(start + SEARCH_CHUNK_ROWS, candidate_rows.size)
            block = matrix[candidate_rows[start:stop]]
            finite = np.isfinite(block).all(axis=1)
            norms = np.linalg.norm(block, axis=1)
            valid = finite & np.isfinite(norms) & (norms > 0.0)
            chunk_scores = np.full(stop - start, -np.inf, dtype=np.float64)
            if np.any(valid):
                chunk_scores[valid] = np.clip(
                    np.asarray(block[valid] @ query, dtype=np.float64)
                    / (np.asarray(norms[valid], dtype=np.float64) * query_norm),
                    -1.0,
                    1.0,
                )
            scores[start:stop] = chunk_scores
        valid_scores = np.isfinite(scores)
        scores = scores[valid_scores]
        candidate_ids = candidate_ids[valid_scores]
        order = np.lexsort((candidate_ids, -scores))
    else:
        query64 = np.asarray(query, dtype=np.float64)
        query_squared = float(np.dot(query64, query64))
        for start in range(0, candidate_rows.size, SEARCH_CHUNK_ROWS):
            if is_cancelled():
                raise NeighborSearchCancelled()
            stop = min(start + SEARCH_CHUNK_ROWS, candidate_rows.size)
            block = np.asarray(matrix[candidate_rows[start:stop]], dtype=np.float64)
            finite = np.isfinite(block).all(axis=1)
            chunk_scores = np.full(stop - start, np.inf, dtype=np.float64)
            if np.any(finite):
                valid_block = block[finite]
                distances_squared = (
                    np.einsum("ij,ij->i", valid_block, valid_block)
                    + query_squared
                    - 2.0 * (valid_block @ query64)
                )
                chunk_scores[finite] = np.sqrt(np.maximum(distances_squared, 0.0))
            scores[start:stop] = chunk_scores
        valid_scores = np.isfinite(scores)
        scores = scores[valid_scores]
        candidate_ids = candidate_ids[valid_scores]
        order = np.lexsort((candidate_ids, scores))

    if is_cancelled():
        raise NeighborSearchCancelled()
    limit = min(max(1, int(request.count)), int(order.size))
    matches = tuple(
        NeighborMatch(int(candidate_ids[position]), float(scores[position]))
        for position in order[:limit]
    )
    return NeighborSearchResult(int(request.query_metadata_id), metric, matches)
