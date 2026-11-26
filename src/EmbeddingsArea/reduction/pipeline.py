"""PCA computation, isolated from widgets and application persistence."""
from pathlib import Path
import time

import numpy as np

from EmbeddingsArea.reduction.parameters import validate_request
from EmbeddingsArea.reduction.repository import inspect_source, source_arrays, write_result


def compute(path, directory, request, source_id, status=lambda value: None, *, source_label=""):
    from sklearn.decomposition import PCA, IncrementalPCA
    from threadpoolctl import threadpool_limits
    import sklearn

    started = time.monotonic()
    directory = Path(directory)
    summary, shape, input_dtype = inspect_source(path)
    validate_request(request, *shape)
    dtype = np.float64 if input_dtype.itemsize == 8 else np.float32
    status("Reading vector data…")
    with source_arrays(path, directory, lambda: None) as (values, ids, names), threadpool_limits(limits=1):
        rows, dimensions = values.shape
        # Keep input batches near 32 MiB, with room for at least k samples.
        batch_size = max(request.dimensions, min(4096, (32 * 1024**2) // (dimensions * np.dtype(dtype).itemsize)))
        for start in range(0, rows, batch_size):
            if not np.isfinite(values[start:start + batch_size]).all():
                raise ValueError("Vectors contain NaN or infinity. Repair them before reducing.")
        status("Fitting PCA…" if request.method == "pca" else "Fitting Incremental PCA…")
        if request.method == "pca":
            estimator = PCA(n_components=request.dimensions, whiten=False, svd_solver="auto", random_state=42)
            estimator.fit(np.asarray(values, dtype=dtype))
        else:
            estimator = IncrementalPCA(n_components=request.dimensions, whiten=False, batch_size=batch_size)
            start, last_report = 0, 0.0
            while start < rows:
                end = min(rows, start + batch_size)
                if rows - end < request.dimensions:
                    end = rows
                estimator.partial_fit(np.asarray(values[start:end], dtype=dtype))
                start = end
                if time.monotonic() - last_report >= .25:
                    status(f"Fitting Incremental PCA: {end:,} / {rows:,} vectors")
                    last_report = time.monotonic()
        ratio = estimator.explained_variance_ratio_
        variance = float(np.clip(np.sum(ratio), 0, 1)) if np.isfinite(ratio).all() else None
        output = np.lib.format.open_memmap(directory / "reduced.npy", mode="w+", dtype=dtype,
                                           shape=(rows, request.dimensions))
        try:
            status("Transforming vectors…")
            last_report = 0.0
            for start in range(0, rows, batch_size):
                end = min(rows, start + batch_size)
                transformed = estimator.transform(np.asarray(values[start:end], dtype=dtype))
                if not np.isfinite(transformed).all():
                    raise ValueError("PCA produced non-finite vectors.")
                output[start:end] = transformed
                if time.monotonic() - last_report >= .25:
                    status(f"Transforming vectors: {end:,} / {rows:,}")
                    last_report = time.monotonic()
            output.flush()
            recipe = dict(method=request.method, source_run_id=source_id, source_dimensions=dimensions,
                          source_label=source_label, output_label=request.label, item_count=rows,
                          output_mode="replace" if request.replace_original else "copy",
                          source_artifact=Path(path).name, dimensions=request.dimensions,
                          variance_retained=variance, whiten=False, normalized=False,
                          sklearn_version=sklearn.__version__,
                          solver=getattr(estimator, "_fit_svd_solver", "incremental"),
                          batch_size=batch_size if request.method == "incremental_pca" else None,
                          random_state=42 if request.method == "pca" else None)
            status("Saving reduced vectors…")
            write_result(directory / "result.npz", output, ids, names, summary, recipe, estimator, started=started)
        finally:
            output._mmap.close()
    return recipe
