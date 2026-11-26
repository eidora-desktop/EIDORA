"""Cheap projection feasibility checks shared by the dialog and execution service."""
from __future__ import annotations

import math
from pathlib import Path

from DiscoveryArea.domain import ProjectionAssessment, ProjectionInputInfo
from DiscoveryArea.method_catalog import DiscoveryMethodCatalog


# Prefer method-specific costs and adjustable settings over general input size.
_RUNTIME_MESSAGES = {
    "all_pairs": "This method compares all pairs of vectors, so larger selections may take longer.",
    "iterations": "More optimization iterations are requested, which may increase processing time.",
    "initializations": "Multiple initializations repeat the projection, which may increase processing time.",
    "neighbors": "These settings consider more neighbors per vector, which may increase processing time.",
    "comparisons": "These settings compare more vectors per item, which may increase processing time.",
    "sampling": "The chosen settings require additional sampling, which may increase processing time.",
    "dimensions": "These vectors have many dimensions, which may increase processing time.",
    "selection": "This selection contains many vectors, so projection may take longer.",
}


def available_memory_bytes() -> int:
    import psutil
    return int(psutil.virtual_memory().available)


def memory_text(size: int) -> str:
    if size >= 1024**3:
        return f"{size / 1024**3:,.1f} GiB"
    return f"{size / 1024**2:,.0f} MiB"


def inspect_projection_selection(project, source_key: str, filter_key: str) -> ProjectionInputInfo:
    """Plain background work: capture only project paths and selection keys."""
    from DiscoveryArea.metadata_access import DiscoveryMetadataAccess
    from DiscoveryArea.service import DiscoveryService
    from EmbeddingsArea.domain import EmbeddingRunState
    from EmbeddingsArea.repository import EmbeddingRunRepository

    if not source_key.startswith("run:"):
        raise ValueError("Select completed vector data before creating a projection.")
    repository = EmbeddingRunRepository(project.paths.embeddings / "embeddings.db")
    run = repository.get_run(source_key.removeprefix("run:"))
    if run is None or run.state != EmbeddingRunState.COMPLETED or not run.artifact_path:
        raise ValueError("The selected completed vector data is no longer available.")
    service = DiscoveryService()
    ids = DiscoveryMetadataAccess(project).metadata_ids()
    if filter_key:
        ids = service.metadata_ids_for_filter_preset(project, ids, Path(filter_key))
    return service.inspect_projection_inputs(project, Path(run.artifact_path), ids, source_key=source_key)


def _finite_settings(value) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite_settings(item) for item in value.values())
    if isinstance(value, (tuple, list)):
        return all(_finite_settings(item) for item in value)
    return True


def normalize_projection_settings(method_id: str, parameters: dict[str, object], *, catalog=None) -> dict[str, object]:
    settings = (catalog or DiscoveryMethodCatalog()).normalize_parameters(method_id, parameters)
    if not _finite_settings(settings):
        raise ValueError("Parameter values must be finite numbers.")
    if "n_jobs" in settings and (settings["n_jobs"] == 0 or settings["n_jobs"] < -1):
        raise ValueError("Workers must be -1 or a positive integer.")
    return settings


def assess_projection(
    method_id: str,
    parameters: dict[str, object],
    inputs: ProjectionInputInfo,
    *,
    available_bytes: int | None = None,
) -> ProjectionAssessment:
    """Block invalid settings or a necessary allocation exceeding the memory budget.

    Working-memory estimates and uncalibrated runtime heuristics only warn. The
    budget leaves 20% of currently available physical memory for the UI/OS.
    """
    def blocked(message):
        return ProjectionAssessment("blocked", "Cannot run with these settings", (message,))

    try:
        catalog = DiscoveryMethodCatalog()
        p = normalize_projection_settings(method_id, parameters, catalog=catalog)
        defaults = catalog.default_parameters(method_id)
    except (ValueError, TypeError, OverflowError) as exc:
        return blocked(str(exc))
    n, d = inputs.selected_count, inputs.dimensions
    minimum_rows = 4 if method_id in {"pacmap", "trimap"} else 3 if method_id in {"mds", "tsne", "opentsne"} else 2
    if n < minimum_rows:
        return blocked(f"This method requires at least {minimum_rows} selected vectors; {n:,} match the current source and filter.")
    if d < 1 or (method_id in {"pca", "pacmap", "trimap"} and d < 2):
        return blocked("This method requires at least two input dimensions." if d == 1 else "The vectors have no input dimensions.")
    if method_id in {"umap", "umap_05"} and p["min_dist"] > p["spread"]:
        return blocked("Minimum distance must not exceed spread.")
    if method_id == "pca" and p["svd_solver"] == "arpack" and min(n, d) <= 2:
        return blocked("The ARPACK solver requires more than two vectors and input dimensions.")
    pca_dimensions = int(p.get("pca_dimensions") or 0)
    reduced_d = min(pca_dimensions, n - 1, d - 1) if 0 < pca_dimensions < d else d
    if method_id == "tsne" and p["init"] == "pca" and reduced_d < 2:
        return blocked("PCA initialization requires two dimensions after preprocessing. Increase PCA dimensions or choose random initialization.")

    available = available_memory_bytes() if available_bytes is None else max(0, available_bytes)
    budget = int(available * 0.8)
    input_bytes = n * d * 4
    minimum = max(inputs.loading_min_bytes, input_bytes)
    estimated = inputs.loading_estimate_bytes + input_bytes * 2
    runtime_reasons: set[str] = set()
    if method_id == "mds":
        # Float64 input and a full distance matrix are present simultaneously.
        minimum = max(minimum, n * d * 8 + n * n * 8)
        estimated += n * d * 8 + 8 * 8 * n * n
        starts = p["n_init"] if p["init"] == "random" else 1
        if n >= 2000:
            runtime_reasons.add("all_pairs")
        elif n * n * p["max_iter"] * starts >= 5_000_000_000:
            runtime_reasons.add("iterations" if p["max_iter"] > defaults["max_iter"] else "initializations")
    elif method_id in {"tsne", "opentsne"}:
        perplexity = min(float(p["perplexity"]), max(1.0, (n - 1) / 3) if method_id == "tsne" else (n - 1) / 3)
        neighbors = min(n - 1, max(1, int(3 * perplexity)))
        estimated += n * neighbors * 48
        if method_id == "tsne" and p["method"] == "exact":
            minimum = max(minimum, n * n * 4)
            estimated += n * n * 32
            if n >= 5000:
                runtime_reasons.add("all_pairs")
        if method_id == "opentsne":
            backend = p["negative_gradient_method"]
            if backend == "auto":
                backend = "bh" if n < 10000 else "fft"
            if backend == "bh" and n >= 50000:
                runtime_reasons.add("selection")
        if n * neighbors >= 10_000_000:
            runtime_reasons.add("neighbors" if perplexity > defaults["perplexity"] else "selection")
        iterations = p.get("iterations", p.get("n_iter", 0) + p.get("early_exaggeration_iter", 0))
        if iterations > 2000:
            runtime_reasons.add("iterations")
        if d > 100 and pca_dimensions == 0:
            runtime_reasons.add("dimensions")
    elif method_id in {"umap", "umap_05", "pacmap", "trimap"}:
        if method_id.startswith("umap"):
            neighbors = min(n - 1, p["n_neighbors"])
            multiplier = 1
            more_neighbors = neighbors > defaults["n_neighbors"]
            more_comparisons = False
            epochs = p.get("n_epochs")
            if isinstance(epochs, list):
                epochs = max(epochs)
            if epochs is not None and epochs > 1000:
                runtime_reasons.add("iterations")
        elif method_id == "pacmap":
            neighbors = min(n - 1, p["n_neighbors"] or max(10, int(10 + 15 * (math.log10(max(n, 10000)) - 4))))
            multiplier = 1 + p["mid_near_ratio"] + p["further_ratio"]
            more_neighbors = neighbors > defaults["n_neighbors"]
            more_comparisons = multiplier > 1 + defaults["mid_near_ratio"] + defaults["further_ratio"]
        else:
            neighbors = min(n - 1, p["n_inliers"])
            multiplier = 1 + p["n_outliers"] + p["n_random"]
            more_neighbors = neighbors > defaults["n_inliers"]
            more_comparisons = multiplier > 1 + defaults["n_outliers"] + defaults["n_random"]
        estimated += int(n * neighbors * multiplier * 48)
        if n * neighbors * multiplier >= 10_000_000:
            runtime_reasons.add("neighbors" if more_neighbors else "comparisons" if more_comparisons else "selection")
        if p.get("iterations", 0) > 2000:
            runtime_reasons.add("iterations")
    elif method_id == "pca":
        solver = p["svd_solver"]
        if solver == "auto":
            solver = "covariance_eigh" if d <= 1000 and n >= 10 * d else "full" if max(n, d) <= 500 or min(n, d) <= 2 else "randomized"
        if solver == "covariance_eigh":
            minimum = max(minimum, d * d * 4)
            estimated += d * d * 24
        elif solver == "full":
            estimated += input_bytes * 3 + min(n, d) ** 2 * 8
            if n * d >= 100_000_000:
                runtime_reasons.add("dimensions" if d > 100 else "selection")
        elif solver == "randomized":
            samples = 2 + p["n_oversamples"]
            minimum = max(minimum, min(n, d) * samples * 4)
            estimated += (n + d) * samples * 16
            if samples > max(50, min(n, d)):
                runtime_reasons.add("sampling")
    if method_id not in {"pca", "random", "custom_coordinates"} and n >= 100000:
        runtime_reasons.add("selection")
    if minimum > budget:
        message = (
            f"Source data needs at least {memory_text(inputs.loading_min_bytes)}. Use smaller vector data."
            if inputs.loading_min_bytes > budget
            else f"Requires at least {memory_text(minimum)}. Reduce the selection or change method."
        )
        return ProjectionAssessment(
            "blocked", "Cannot run within the memory budget", (message,),
            estimated, minimum, budget,
        )
    messages = []
    if estimated > budget:
        messages.append("May exceed the memory budget.")
    elif estimated > budget // 2:
        messages.append("Memory use may be high.")
    runtime_message = next((text for reason, text in _RUNTIME_MESSAGES.items() if reason in runtime_reasons), None)
    if runtime_message:
        messages.append(runtime_message)
    return ProjectionAssessment(
        "warning" if messages else "ready",
        "Review before running" if messages else "No known resource issue",
        tuple(messages), estimated, minimum, budget,
    )
