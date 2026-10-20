"""Build and stage the native Discovery renderer from a clean Qt toolchain."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def _optional_tool(name: str) -> str | None:
    executable_name = f"{name}.exe" if os.name == "nt" else name
    environment_tool = Path(sys.executable).parent / executable_name
    if environment_tool.is_file():
        return str(environment_tool)
    return shutil.which(name)


def _tool(name: str) -> str:
    path = _optional_tool(name)
    if path is None:
        raise RuntimeError(f"Required build tool is unavailable: {name}")
    return path


def _default_architecture() -> str:
    architecture = platform.machine().lower()
    return "x86_64" if architecture in {"amd64", "x64"} else architecture


def build(
    *,
    qt_sdk: Path,
    build_dir: Path,
    stage_dir: Path,
    architecture: str,
) -> Path:
    from PySide6 import QtCore

    source_dir = Path(__file__).resolve().parents[1] / "src" / "DiscoveryArea" / "canvas" / "native"
    qt_sdk = qt_sdk.expanduser().resolve()
    build_dir = build_dir.expanduser().resolve()
    stage_dir = stage_dir.expanduser().resolve()
    if not (qt_sdk / "lib" / "cmake" / "Qt6").is_dir():
        raise RuntimeError(f"Qt SDK does not contain Qt6 CMake files: {qt_sdk}")
    if build_dir == source_dir or source_dir in build_dir.parents:
        raise RuntimeError("Use an out-of-tree native renderer build directory.")

    pyside_qt_root = Path(QtCore.__file__).resolve().parent / "Qt"
    configure = [
        _tool("cmake"),
        "--fresh",
        "-S",
        str(source_dir),
        "-B",
        str(build_dir),
        f"-DCMAKE_PREFIX_PATH={qt_sdk}",
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DEIDORA_PYSIDE6_QT_VERSION={QtCore.qVersion()}",
        f"-DEIDORA_PYSIDE6_QT_LIB_DIR={pyside_qt_root / 'lib'}",
    ]
    ninja = _optional_tool("ninja")
    if sys.platform == "win32":
        # The downloaded Qt SDK uses MSVC; Ninja otherwise discovers MinGW
        # from the runner PATH when no Visual Studio environment is active.
        configure.extend(["-G", "Visual Studio 17 2022", "-A", "x64"])
    elif ninja:
        configure.extend(["-G", "Ninja", f"-DCMAKE_MAKE_PROGRAM={ninja}"])
    if sys.platform == "darwin":
        configure.append(f"-DCMAKE_OSX_ARCHITECTURES={architecture}")

    build_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(configure, check=True)
    subprocess.run(
        [_tool("cmake"), "--build", str(build_dir), "--config", "Release"],
        check=True,
    )

    built_module = build_dir / "DiscoveryRenderer"
    if sys.platform == "win32":
        # Visual Studio is a multi-configuration generator.
        built_module = built_module / "Release"
    libraries = tuple(built_module.glob("*discovery_renderer.*"))
    if not (built_module / "qmldir").is_file() or not libraries:
        raise RuntimeError(f"Native renderer build output is incomplete: {built_module}")

    staged_module = stage_dir / "DiscoveryRenderer"
    if staged_module.exists():
        shutil.rmtree(staged_module)
    staged_module.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(built_module, staged_module)
    print(
        f"Staged Discovery renderer API for Qt {QtCore.qVersion()} "
        f"({architecture}) at {staged_module}"
    )
    return staged_module


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--qt-sdk",
        type=Path,
        default=os.environ.get("EIDORA_QT_SDK"),
        required="EIDORA_QT_SDK" not in os.environ,
    )
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--stage-dir", type=Path, required=True)
    parser.add_argument("--architecture", default=_default_architecture())
    arguments = parser.parse_args()
    try:
        build(
            qt_sdk=arguments.qt_sdk,
            build_dir=arguments.build_dir,
            stage_dir=arguments.stage_dir,
            architecture=arguments.architecture,
        )
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Native renderer build failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
