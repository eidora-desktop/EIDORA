"""Create a new starter archive with reviewed Discovery settings only."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from project.starter_collection import read_manifest
from project.starter_validation import sha256


def update(manifest_path: Path, settings_path: Path, output: Path, version: int) -> dict:
    manifest = read_manifest(manifest_path)
    if type(version) is not int or version <= manifest["version"]:
        raise ValueError("Use a new collection version greater than the existing version.")
    archive_path = manifest_path.parent / manifest["archive"]
    if (archive_path.stat().st_size != manifest["archive_bytes"]
            or sha256(archive_path) != manifest["sha256"]):
        raise ValueError("The existing starter archive does not match its manifest.")
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    if not isinstance(settings, dict) or not isinstance(settings.get("tooltip"), dict):
        raise ValueError("Expected a Discovery settings object containing tooltip settings.")
    if output.exists():
        raise FileExistsError("Use a new output directory; existing releases are immutable.")
    payload = (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    settings_name = "MET_Example/state/discovery_settings.json"
    archive_name = f"MET_Example-v{version}.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="starter-settings-", dir=output.parent) as temporary:
        stage = Path(temporary) / "release"
        stage.mkdir()
        destination = stage / archive_name
        with zipfile.ZipFile(archive_path) as old, zipfile.ZipFile(destination, "w") as new:
            names = set()
            total = 0
            for info in old.infolist():
                path = PurePosixPath(info.filename)
                if (not path.parts or path.parts[0] != "MET_Example"
                        or ".." in path.parts or "\\" in info.filename
                        or any(":" in part for part in path.parts)
                        or path.is_absolute() or stat.S_ISLNK(info.external_attr >> 16)
                        or info.filename.casefold() in names):
                    raise ValueError("Unsafe or duplicate starter archive entry.")
                names.add(info.filename.casefold())
                if info.filename == settings_name:
                    continue
                total += info.file_size
                with old.open(info) as reader, new.open(copy.copy(info), "w") as writer:
                    shutil.copyfileobj(reader, writer)
            info = zipfile.ZipInfo(settings_name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            new.writestr(info, payload)
        result = {
            **manifest,
            "version": version,
            "archive": archive_name,
            "sha256": sha256(destination),
            "archive_bytes": destination.stat().st_size,
            "extracted_bytes": total + len(payload),
            "url": f"https://github.com/eidora-desktop/EIDORA/releases/download/starter-met-v{version}/{archive_name}",
        }
        (stage / "starter-collection.json").write_text(json.dumps(result, indent=2) + "\n")
        stage.rename(output)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(update(args.manifest, args.settings, args.output, args.version), indent=2))
