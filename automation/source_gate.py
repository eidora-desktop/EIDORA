"""Validate that a clean, public checkout contains a deployable source tree.

This gate intentionally uses only the Python standard library. It runs before
release dependencies are installed. Architecture documentation is public; maintainer
tests, agent instructions, release credentials, and workspace data are not.
"""

from __future__ import annotations

import argparse
import compileall
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree


LOCAL_ONLY_ROOTS = frozenset({"scripts", ".private-release", "workspace_projects", "output", "tmp", "tests"})
REQUIRED_FILES = frozenset(
    {
        "LICENSE",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "docs/architecture.md",
        ".github/CODEOWNERS",
        "README.md",
        "pyproject.toml",
        "packaging/resource-manifest.toml",
        "packaging/runtime-requirements.txt",
        "packaging/release-tools.txt",
        "packaging/release-targets.json",
        "packaging/eidora.spec",
        "src/project/resources/starter-collection.json",
        "automation/fetch_starter_collection.py",
        "automation/prepare_starter_collection.py",
        "packaging/icons/macos/eidora_icon.icns",
        "packaging/icons/macos/Assets.car",
        "packaging/icons/macos/catalog.json",
        "packaging/icons/macos/EIDORA.icon/icon.json",
        "packaging/icons/macos/EIDORA.icon/Assets/eidora-mark.svg",
        "packaging/icons/macos/EIDORA.icon/Assets/eidora-background.svg",
        "automation/prepare_macos_icon.py",
        "packaging/icons/windows/eidora_icon.ico",
        "packaging/icons/linux/hicolor/16x16/apps/eidora_icon.png",
        "packaging/icons/linux/hicolor/256x256/apps/eidora_icon.png",
        "packaging/icons/linux/hicolor/scalable/apps/eidora_icon.svg",
        "automation/build_bundle.py",
        "automation/build_native_renderer.py",
        "automation/package_installers.py",
        "automation/package_msix.py",
        "automation/select_release_targets.py",
        "automation/sign_windows.ps1",
        "automation/prepare_msix_test.ps1",
        "automation/validate_msix.ps1",
        "packaging/windows/store.json",
        "packaging/windows/aqt.ini",
        "packaging/windows/AppxManifest.xml",
        ".github/workflows/bundle-matrix.yml",
        "src/app.py",
        "src/eidora_version.py",
        "src/MainWindow/ui/main_window.ui",
        "src/DiscoveryArea/canvas/quick/discovery_canvas.qml",
        "src/DiscoveryArea/canvas/native/CMakeLists.txt",
        "src/shared/icons/manifest.yaml",
    }
)
RESOURCE_SUFFIXES = frozenset(
    {".dll", ".dylib", ".onnx", ".png", ".qml", ".qsb", ".so", ".svg", ".ui", ".yaml"}
)


class GateFailure(RuntimeError):
    """Raised when the public source tree cannot be packaged reliably."""


def _public_files(root: Path) -> set[str]:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return {
        entry.decode("utf-8")
        for entry in completed.stdout.split(b"\0")
        if entry
    }


def _validate_public_boundaries(public_files: set[str]) -> None:
    published_local_files = sorted(
        path
        for path in public_files
        if (
            PurePosixPath(path).parts
            and PurePosixPath(path).parts[0] in LOCAL_ONLY_ROOTS
        )
        or PurePosixPath(path).name == "AGENTS.md"
        or path.startswith("automation/tests/")
    )
    if published_local_files:
        raise GateFailure(
            "Local-only files are tracked: " + ", ".join(published_local_files[:10])
        )

    missing = sorted(REQUIRED_FILES - public_files)
    if missing:
        raise GateFailure("Required public files are not tracked: " + ", ".join(missing))


def _validate_package_data(root: Path) -> None:
    with (root / "pyproject.toml").open("rb") as stream:
        configuration = tomllib.load(stream)
    patterns = configuration["tool"]["setuptools"]["package-data"]["*"]
    configured_suffixes = {
        Path(pattern.replace("*", "resource")).suffix for pattern in patterns
    }
    missing = sorted(RESOURCE_SUFFIXES - configured_suffixes)
    if missing:
        raise GateFailure(
            "Package-data patterns omit runtime resource types: " + ", ".join(missing)
        )

    project = configuration["project"]
    if "version" in project or project.get("dynamic") != ["version"]:
        raise GateFailure("Application version must be dynamic and have one source.")
    version_source = configuration["tool"]["setuptools"]["dynamic"]["version"]
    if version_source != {"attr": "eidora_version.__version__"}:
        raise GateFailure("Unexpected application version source.")


def _validate_ui_files(root: Path, public_files: set[str]) -> int:
    ui_paths = sorted(path for path in public_files if path.endswith(".ui"))
    if not ui_paths:
        raise GateFailure("No tracked Qt Designer forms were found.")

    failures: list[str] = []
    for relative_path in ui_paths:
        ui_path = root / relative_path
        try:
            tree = ElementTree.parse(ui_path)
        except ElementTree.ParseError as error:
            failures.append(f"{relative_path}: invalid XML ({error})")
            continue

        for property_node in tree.getroot().iter("property"):
            if property_node.attrib.get("name") not in {"icon", "pixmap"}:
                continue
            for node in property_node.iter():
                value = (node.text or "").strip()
                if not value or value.startswith(":/") or node.tag == "property":
                    continue
                if not value.lower().endswith((".svg", ".png", ".jpg", ".jpeg", ".ico")):
                    continue
                target = (ui_path.parent / value).resolve()
                try:
                    target_relative = target.relative_to(root).as_posix()
                except ValueError:
                    failures.append(f"{relative_path}: resource escapes repository ({value})")
                    continue
                if target_relative not in public_files:
                    failures.append(
                        f"{relative_path}: resource is missing from public checkout ({value})"
                    )

    if failures:
        raise GateFailure("Qt Designer resource errors:\n" + "\n".join(failures[:30]))
    return len(ui_paths)


def _validate_dynamic_methods(root: Path, public_files: set[str]) -> int:
    configs = sorted(
        path
        for path in public_files
        if path.startswith("src/DiscoveryArea/discovery_methods/methods/")
        and path.endswith("/config.yaml")
    )
    if not configs:
        raise GateFailure("No built-in Discovery method configurations were found.")

    failures: list[str] = []
    for config in configs:
        method_root = PurePosixPath(config).parent
        for filename in ("__init__.py", "method.py"):
            required = (method_root / filename).as_posix()
            if required not in public_files:
                failures.append(f"{config}: missing dynamic module {required}")
        if not (root / config).read_text(encoding="utf-8").lstrip().startswith("id:"):
            failures.append(f"{config}: expected an id as the first configuration field")

    if failures:
        raise GateFailure("Dynamic Discovery method errors:\n" + "\n".join(failures))
    return len(configs)


def _compile_source(root: Path) -> None:
    if not compileall.compile_dir(root / "src", quiet=1, force=True):
        raise GateFailure("Python source compilation failed.")


def run(root: Path, *, compile_source: bool) -> None:
    public_files = _public_files(root)
    _validate_public_boundaries(public_files)
    _validate_package_data(root)
    ui_count = _validate_ui_files(root, public_files)
    method_count = _validate_dynamic_methods(root, public_files)
    if compile_source:
        _compile_source(root)
    resource_count = sum(
        1 for path in public_files if Path(path).suffix.lower() in RESOURCE_SUFFIXES
    )
    print(
        "Source gate passed: "
        f"{len(public_files)} public files, {resource_count} runtime resources, "
        f"{ui_count} Designer forms, {method_count} dynamic Discovery methods."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-compile",
        action="store_true",
        help="Skip Python bytecode compilation.",
    )
    arguments = parser.parse_args()
    try:
        run(Path(__file__).resolve().parents[1], compile_source=not arguments.no_compile)
    except (GateFailure, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f"Source gate failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
