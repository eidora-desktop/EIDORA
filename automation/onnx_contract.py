"""Publish/check the Desktop-owned ONNX contract snapshot in both repositories."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from EmbeddingsArea.preparation_contract import catalog_document

SOURCES = ("src/EmbeddingsArea/preparation_contract.py", "src/EmbeddingsArea/model_package.py",
           "src/EmbeddingsArea/runtime/preparers.py", "src/EmbeddingsArea/runtime/adapters/onnx.py",
           "src/EmbeddingsArea/runtime/adapters/base.py", "src/EmbeddingsArea/runtime/contracts.py",
           "src/EmbeddingsArea/runtime/package_validation.py", "src/shared/bibtex.py")
DOCS = ("ONNX_CONFIG_SCHEMA.md", "ONNX_MODEL_REQUIREMENTS.md", "ONNX_MODEL_REQUIREMENTS_AGENTS.md", "ONNX_PREPARATION_OPTIONS.md")


def snapshot_bytes():
    document = catalog_document()
    document["implementation_sha256"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline-root", type=Path)
    parser.add_argument("--write", action="store_true", help="Explicitly synchronize; otherwise check only.")
    args = parser.parse_args()
    files = {ROOT / "automation/onnx_contract/onnx-contract.json": snapshot_bytes()}
    if args.pipeline_root:
        files[args.pipeline_root / "eidora_onnx/contract/onnx-contract.json"] = snapshot_bytes()
        files.update({args.pipeline_root / "docs" / name: (ROOT / "automation/onnx_contract" / name).read_bytes() for name in DOCS})
    failed = []
    for path, data in files.items():
        if args.write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        elif not path.exists() or path.read_bytes() != data:
            failed.append(str(path))
    if failed:
        raise SystemExit("ONNX contract drift; explicitly synchronize these files:\n" + "\n".join(failed))
    print("ONNX contract snapshots agree.")


if __name__ == "__main__":
    main()
