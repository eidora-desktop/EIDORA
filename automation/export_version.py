"""Export the authoritative application version for release automation."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github-output", action="store_true")
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    namespace: dict[str, object] = {}
    exec((root / "src" / "eidora_version.py").read_text(encoding="utf-8"), namespace)
    version = str(namespace["__version__"])
    if arguments.github_output:
        output = os.environ.get("GITHUB_OUTPUT")
        if not output:
            raise RuntimeError("GITHUB_OUTPUT is not set")
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write(f"version={version}\n")
    else:
        print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
