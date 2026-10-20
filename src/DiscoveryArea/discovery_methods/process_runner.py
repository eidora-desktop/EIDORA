from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np

from DiscoveryArea.discovery_execution import run_discovery_request
from project.domain import Project


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        print("usage: process_runner <request.json> <metadata_ids.npy> <result.json>", file=sys.stderr)
        return 2
    request_path = Path(args[0])
    metadata_ids_path = Path(args[1])
    result_path = Path(args[2])
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        project = Project(
            name=str(request["project_name"]),
            root=Path(str(request["project_root"])),
            schema_version=int(request["project_schema_version"]),
        )
        payload = request.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("Discovery request payload must be an object.")
        metadata_ids = np.load(metadata_ids_path, allow_pickle=False).astype(np.int64)
        artifact_path_raw = request.get("artifact_path")
        artifact_path = Path(str(artifact_path_raw)) if artifact_path_raw else None
        option = run_discovery_request(project, payload, metadata_ids, artifact_path)
        result_path.write_text(json.dumps({"ok": True, "option_key": option.key}), encoding="utf-8")
        return 0
    except Exception as exc:
        try:
            result_path.write_text(json.dumps({"ok": False, "error": str(exc)}), encoding="utf-8")
        except Exception:
            pass
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
