"""Copy size-specific packaging artwork into importable runtime resources."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]


def sync(source: Path, destination: Path) -> None:
    """Preserve each exported logical size and display density without resizing."""
    macos = destination / "macos"
    linux = destination / "linux"
    macos.mkdir(parents=True, exist_ok=True)
    linux.mkdir(parents=True, exist_ok=True)
    with Image.open(source / "macos/eidora_icon.icns") as icon:
        for width, height, density in icon.info["sizes"]:
            suffix = "@2x" if density == 2 else ""
            icon.icns.getimage((width, height, density)).save(macos / f"{width}{suffix}.png")
    for path in (source / "linux/hicolor").glob("*/apps/eidora_icon.png"):
        size, _, density = path.parents[1].name.partition("@")
        width = size.split("x")[0]
        suffix = "@2x" if density == "2" else ""
        shutil.copy2(path, linux / f"{width}{suffix}.png")
    shutil.copy2(source / "windows/eidora_icon.ico", destination / "eidora_icon_windows.ico")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=ROOT / "packaging/icons")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "src/shared/branding")
    arguments = parser.parse_args()
    sync(arguments.source_dir, arguments.output_dir)
