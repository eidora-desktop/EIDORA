"""Stage the validated platform application icons for release packaging."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

if __package__:
    from .prepare_macos_icon import validate_macos_icon
else:
    from prepare_macos_icon import validate_macos_icon

DEFAULT_ICON_DIR = Path(__file__).resolve().parents[1] / "packaging" / "icons"
LINUX_ICON_PATHS = tuple(
    Path(directory) / "apps" / "eidora_icon.png"
    for directory in (
        "16x16",
        "16x16@2",
        "20x20",
        "20x20@2",
        "22x22",
        "22x22@2",
        "24x24",
        "24x24@2",
        "32x32",
        "32x32@2",
        "48x48",
        "48x48@2",
        "64x64",
        "64x64@2",
        "96x96",
        "128x128",
        "128x128@2",
        "256x256",
        "256x256@2",
        "512x512",
        "512x512@2",
        "1024x1024",
    )
) + (
    Path("scalable/apps/eidora_icon.svg"),
    Path("scalable/apps/eidora_icon-small.svg"),
    Path("symbolic/apps/eidora_icon-symbolic.svg"),
)


def generate(source_dir: Path, output_dir: Path) -> None:
    """Copy pre-rendered icons without resampling size-specific artwork."""
    validate_macos_icon(source_dir / "macos")
    macos_icon = source_dir / "macos" / "eidora_icon.icns"
    windows_icon = source_dir / "windows" / "eidora_icon.ico"
    linux_icons = source_dir / "linux" / "hicolor"
    linux_bundle_icon = linux_icons / "256x256" / "apps" / "eidora_icon.png"
    missing = [
        path
        for path in (
            macos_icon,
            windows_icon,
            *(linux_icons / relative for relative in LINUX_ICON_PATHS),
        )
        if not path.exists()
    ]
    if missing:
        raise RuntimeError(
            "Validated application icon assets are missing: "
            + ", ".join(str(path) for path in missing)
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(macos_icon, output_dir / "eidora.icns")
    shutil.copy2(source_dir / "macos/Assets.car", output_dir / "Assets.car")
    shutil.copy2(windows_icon, output_dir / "eidora.ico")
    shutil.copy2(linux_bundle_icon, output_dir / "eidora-256.png")
    shutil.copytree(linux_icons, output_dir / "hicolor", dirs_exist_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_ICON_DIR,
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        generate(arguments.source_dir, arguments.output_dir)
    except (OSError, RuntimeError) as error:
        print(f"Icon generation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
