from __future__ import annotations

import argparse
from pathlib import Path
import subprocess


def _output(*command: str) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def _framework_rpath(dependency: str) -> str | None:
    parts = Path(dependency).parts
    framework_index = next((index for index, part in enumerate(parts) if part.endswith(".framework")), None)
    if framework_index is None:
        return None
    return "@rpath/" + "/".join(parts[framework_index:])


def _rpaths(library: Path) -> list[str]:
    lines = _output("otool", "-l", str(library)).splitlines()
    output: list[str] = []
    for index, line in enumerate(lines):
        if line.strip() != "cmd LC_RPATH":
            continue
        for candidate in lines[index + 1 : index + 5]:
            stripped = candidate.strip()
            if stripped.startswith("path "):
                output.append(stripped[5:].split(" (offset", 1)[0])
                break
    return output


def patch_library(library: Path, qt_lib_dir: Path) -> None:
    if not library.is_file():
        raise FileNotFoundError(library)
    if not qt_lib_dir.is_dir():
        raise NotADirectoryError(qt_lib_dir)
    dependencies = []
    for line in _output("otool", "-L", str(library)).splitlines()[1:]:
        dependency = line.strip().split(" (", 1)[0]
        replacement = _framework_rpath(dependency)
        if replacement is None or dependency.startswith(("@rpath/", "/System/", "/usr/lib/")):
            continue
        dependencies.append((dependency, replacement))
    for dependency, replacement in dependencies:
        subprocess.run(
            ["install_name_tool", "-change", dependency, replacement, str(library)],
            check=True,
        )
    existing_rpaths = _rpaths(library)
    for rpath in existing_rpaths:
        if rpath == str(qt_lib_dir) or rpath.startswith(("@loader_path", "@executable_path")):
            continue
        is_foreign_qt_sdk = rpath.startswith("/") and (
            "/Qt/" in rpath
            or "/opt/homebrew/" in rpath
            or "/usr/local/" in rpath
        )
        if is_foreign_qt_sdk:
            subprocess.run(
                ["install_name_tool", "-delete_rpath", rpath, str(library)],
                check=True,
            )
    if str(qt_lib_dir) not in _rpaths(library):
        subprocess.run(
            ["install_name_tool", "-add_rpath", str(qt_lib_dir), str(library)],
            check=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Bind a Qt plugin only to the PySide6 framework runtime")
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--qt-lib-dir", type=Path, required=True)
    args = parser.parse_args()
    patch_library(args.library.resolve(), args.qt_lib_dir.resolve())


if __name__ == "__main__":
    main()
