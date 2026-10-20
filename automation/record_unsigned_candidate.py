"""Record unsigned build outputs for later private maintainer verification."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def record(target: str, output: Path) -> dict:
    targets = json.loads((ROOT / "packaging/release-targets.json").read_text())
    configuration = next(item for item in targets if item["target"] == target)
    machine = {"AMD64": "x86_64"}.get(platform.machine(), platform.machine())
    expected_os = "darwin" if target.startswith("apple-") else "win32" if target == "windows" else "linux"
    if sys.platform != expected_os or machine != configuration["native_arch"]:
        raise ValueError("Build host does not match the requested target.")
    subprocess.run(["git", "diff", "--exit-code", "HEAD", "--"], cwd=ROOT, check=True)
    source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if os.environ.get("GITHUB_SHA") != source_sha:
        raise ValueError("The checkout differs from the workflow commit.")
    build = ROOT / "build-output"
    report = json.loads((build / "release-self-test.json").read_text())
    if report.get("ok") is not True:
        raise ValueError("A successful packaged self-test is required.")
    sys.path.insert(0, str(ROOT / "src"))
    from eidora_version import __version__

    output.mkdir(parents=True, exist_ok=False)
    suffixes = {".msix", ".exe"} if target == "windows" else {".dmg"} if target.startswith("apple-") else {".deb", ".AppImage"}
    artifacts = [path for folder in ("installers", "store") for path in (build / folder).glob("*")
                 if path.is_file() and path.suffix in suffixes]
    if not artifacts:
        raise ValueError("No installation artifacts were produced.")
    for path in artifacts:
        shutil.copy2(path, output / path.name)
    for path in (build / "release-self-test.json", ROOT / "build-dependencies.txt"):
        shutil.copy2(path, output / path.name)
    if target.startswith("apple-"):
        app = build / "dist/EIDORA.app"
        if not app.is_dir():
            raise ValueError("Missing macOS application bundle.")
        # A tar archive preserves bundle modes and symlinks through Actions ZIP storage.
        with tarfile.open(output / "EIDORA.tar", "w") as archive:
            archive.add(app, arcname="EIDORA.app")
    receipt = {
        "version": __version__, "source_sha": source_sha,
        "source_tree": subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True).strip(),
        "target": target, "status": "unsigned-public-candidate",
        "repository": os.environ["GITHUB_REPOSITORY"],
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "files": {path.name: sha256(path) for path in sorted(output.iterdir()) if path.is_file()},
        "limitations": ["Unsigned build; no Developer ID, notarization, or Store certification.",
                        "Private regression checks and native platform QA are separate gates."],
    }
    (output / "candidate.json").write_text(json.dumps(receipt, indent=2) + "\n")
    (output / "SHA256SUMS").write_text("".join(f"{digest}  {name}\n" for name, digest in receipt["files"].items()))
    print(json.dumps(receipt, indent=2))
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    record(args.target, args.output)
