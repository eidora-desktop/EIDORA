"""Reduction validation, storage estimates and cancellation signal."""


class ReductionCancelled(Exception):
    pass


def validate_request(request, rows, dimensions):
    if request.method not in {"pca", "incremental_pca"}:
        raise ValueError("Choose PCA or Incremental PCA.")
    maximum = min(dimensions - 1, rows - 1)
    if not 1 <= request.dimensions <= maximum:
        raise ValueError(f"Target dimensions must be between 1 and {maximum}; PCA needs more vectors than components.")


def storage_estimate(rows, dimensions, target, source_itemsize):
    """Vector payload only; 64-bit inputs use float64, other inputs use float32."""
    output_itemsize = 8 if source_itemsize == 8 else 4
    return rows * dimensions * source_itemsize, rows * target * output_itemsize
