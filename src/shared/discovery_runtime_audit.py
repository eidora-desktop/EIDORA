"""Exercise the packaged UMAP worker, including Numba's executable memory."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess


def audit_discovery_runtime(directory: Path) -> dict[str, object]:
    import numpy as np

    from DiscoveryArea.discovery_execution import _projection_process_environment
    from project.service import ProjectService
    from shared.internal_workers import internal_worker_command, read_worker_result

    directory.mkdir(parents=True, exist_ok=True)
    project = ProjectService(directory / "workspace").create_empty_project("UMAP audit")
    ids = np.arange(1, 17, dtype=np.int64)
    artifact = directory / "vectors.npz"
    np.savez(
        artifact,
        embeddings=np.random.default_rng(42).normal(size=(len(ids), 8)).astype(np.float32),
        object_ids=ids,
    )
    request = directory / "request.json"
    ids_path = directory / "ids.npy"
    result_path = directory / "result.json"
    np.save(ids_path, ids)
    request.write_text(json.dumps({
        "project_name": project.name,
        "project_root": str(project.root),
        "project_schema_version": project.schema_version,
        "artifact_path": str(artifact),
        "payload": {
            "method": "umap", "name": "UMAP audit",
            "parameters": {"n_neighbors": 3, "epochs": 20, "reproducible": True},
        },
    }), encoding="utf-8")
    result_path.unlink(missing_ok=True)
    process = subprocess.run(
        internal_worker_command("discovery", request, ids_path, result_path),
        env=_projection_process_environment(),
        capture_output=True, text=True, timeout=120, check=False,
    )
    if process.returncode != 0:
        detail = (process.stderr or process.stdout or "no diagnostic output").strip()
        raise RuntimeError(f"UMAP runtime audit failed (exit {process.returncode}): {detail}")
    result = read_worker_result(result_path)
    if result["ok"] is not True:
        raise RuntimeError(f"UMAP runtime audit failed: {result.get('error')}")
    from DiscoveryArea.service import DiscoveryService

    service = DiscoveryService()
    option = next(
        (option for option in service.load_project_discovery_options(project, ids)
         if option.key == result["option_key"]),
        None,
    )
    if option is None:
        raise RuntimeError("UMAP runtime audit did not save a projection.")
    dataset = service.load_discovery_dataset_for_option(project, option, ids)
    if dataset.coordinates.shape != (len(ids), 2) or not np.isfinite(dataset.coordinates).all():
        raise RuntimeError("UMAP runtime audit produced invalid coordinates.")
    return {"method": "umap", "item_count": len(ids), "finite_coordinates": True}
