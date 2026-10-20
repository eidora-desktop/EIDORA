"""Compile the reviewed Icon Composer document (requires full Xcode 26+).

Release builds stage the committed catalog; they do not regenerate artwork.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


ICON_DIR = Path(__file__).resolve().parents[1] / "packaging/icons/macos"


def icon_checksums(icon_dir: Path) -> dict[str, str]:
    paths = [icon_dir / "Assets.car", *sorted((icon_dir / "EIDORA.icon").rglob("*"))]
    return {
        path.relative_to(icon_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths if path.is_file()
    }


def validate_macos_icon(icon_dir: Path) -> None:
    receipt = json.loads((icon_dir / "catalog.json").read_text(encoding="utf-8"))
    if icon_checksums(icon_dir) != receipt["sha256"]:
        raise RuntimeError("macOS icon sources/catalog changed; run automation/prepare_macos_icon.py and review the icon.")


def prepare(icon_dir: Path = ICON_DIR) -> None:
    with tempfile.TemporaryDirectory(prefix="eidora-icon-") as directory:
        output = Path(directory)
        subprocess.run(
            [
                "xcrun", "actool", str(icon_dir / "EIDORA.icon"),
                "--compile", str(output), "--platform", "macosx",
                "--target-device", "mac", "--minimum-deployment-target", "12.0",
                "--app-icon", "EIDORA", "--include-all-app-icons",
                "--output-partial-info-plist", str(output / "icon-info.plist"),
                "--output-format", "human-readable-text",
            ],
            check=True,
        )
        catalog = output / "Assets.car"
        entries = json.loads(subprocess.check_output(["xcrun", "assetutil", "--info", str(catalog)]))
        if not any(entry.get("AssetType") == "IconImageStack" and entry.get("Name") == "EIDORA" for entry in entries):
            raise RuntimeError("Apple's compiler did not produce the EIDORA layered icon.")
        # Keep the approved size-specific .icns, not actool's generated fallback.
        shutil.copy2(catalog, icon_dir / "Assets.car")
    xcode = subprocess.check_output(["xcodebuild", "-version"], text=True).strip()
    receipt = {"compiler": xcode, "sha256": icon_checksums(icon_dir)}
    (icon_dir / "catalog.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    prepare()
