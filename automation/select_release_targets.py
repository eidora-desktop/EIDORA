"""Select release jobs, sharing one Windows build between installer formats."""

from __future__ import annotations

import json
import os
from pathlib import Path


def select_targets(targets: list[dict], selection: str) -> list[dict]:
    windows_formats = {"windows": "exe", "windows-msix": "msix", "windows-both": "both"}
    platform = "windows" if selection in windows_formats else selection
    selected = []
    for target in targets:
        if platform != "all" and target["target"] != platform:
            continue
        job = dict(target)
        job["windows_format"] = windows_formats.get(selection, "msix") if target["target"] == "windows" else "exe"
        selected.append(job)
    if not selected:
        raise ValueError(f"Unknown build target: {selection}")
    return selected


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    targets = json.loads((root / "packaging/release-targets.json").read_text())
    matrix = {"include": select_targets(targets, os.environ["BUILD_TARGET"])}
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write("matrix=" + json.dumps(matrix) + "\n")


if __name__ == "__main__":
    main()
