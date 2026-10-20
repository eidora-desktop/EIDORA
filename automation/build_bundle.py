"""Assemble an unsigned, one-folder EIDORA application bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from resource_inventory import inventory
from fetch_starter_collection import fetch


_REQUIRED_MACOS_QT_FRAMEWORKS = frozenset(
    {
        "QtConcurrent",
        "QtCore",
        "QtDBus",
        "QtGui",
        "QtNetwork",
        "QtOpenGL",
        "QtOpenGLWidgets",
        "QtQml",
        "QtQmlMeta",
        "QtQmlModels",
        "QtQmlNetwork",
        "QtQmlWorkerScript",
        "QtQuick",
        "QtQuickWidgets",
        "QtSvg",
        "QtUiTools",
        "QtWidgets",
    }
)


def _repair_macos_qt_framework_links(bundle: Path) -> None:
    """Restore top-level Qt links required by PySide extension rpaths."""
    if sys.platform != "darwin":
        return
    contents = bundle / "Contents"
    qt_frameworks = contents / "Frameworks" / "PySide6" / "Qt" / "lib"
    if not qt_frameworks.is_dir():
        raise RuntimeError(f"Bundled Qt framework directory is missing: {qt_frameworks}")
    bundled_frameworks = {
        framework.stem: framework
        for framework in sorted(qt_frameworks.glob("Qt*.framework"))
    }
    missing = sorted(_REQUIRED_MACOS_QT_FRAMEWORKS - bundled_frameworks.keys())
    if missing:
        raise RuntimeError("Required bundled Qt frameworks are missing: " + ", ".join(missing))
    for name, framework in bundled_frameworks.items():
        relative_target = f"PySide6/Qt/lib/{framework.name}/Versions/A/{name}"
        for container_name in ("Frameworks", "Resources"):
            link = contents / container_name / name
            if link.exists():
                continue
            if link.is_symlink():
                link.unlink()
            os.symlink(relative_target, link)
    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(bundle)],
        check=True,
    )


def _file_manifest(bundle: Path) -> dict[str, object]:
    files = []
    symlinks = []
    for path in sorted(bundle.rglob("*")):
        if path.is_symlink():
            symlinks.append(
                {
                    "path": path.relative_to(bundle).as_posix(),
                    "target": os.readlink(path),
                }
            )
            continue
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        files.append(
            {
                "path": path.relative_to(bundle).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": digest.hexdigest(),
            }
        )
    return {
        "bundle": bundle.name,
        "file_count": len(files),
        "symlink_count": len(symlinks),
        "total_bytes": sum(item["bytes"] for item in files),
        "files": files,
        "symlinks": symlinks,
    }


def build(
    *,
    native_stage: Path,
    output_dir: Path,
    work_dir: Path,
    app_icon: Path | None = None,
    max_mib: float | None = None,
) -> Path:
    root = Path(__file__).resolve().parents[1]
    starter_archive = fetch(root)
    inventory(root)
    work_dir = work_dir.resolve()
    output_dir = output_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment["EIDORA_STARTER_ARCHIVE"] = str(starter_archive)
    environment["EIDORA_NATIVE_STAGE"] = str(native_stage.resolve())
    if app_icon is not None:
        if not app_icon.is_file():
            raise RuntimeError(f"Application icon does not exist: {app_icon}")
        environment["EIDORA_APP_ICON"] = str(app_icon.resolve())
    environment["PYINSTALLER_CONFIG_DIR"] = str(work_dir / "pyinstaller-config")
    environment["MPLCONFIGDIR"] = str(work_dir / "matplotlib-config")
    environment["XDG_CACHE_HOME"] = str(work_dir / "cache")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(output_dir),
            "--workpath",
            str(work_dir),
            str(root / "packaging" / "eidora.spec"),
        ],
        cwd=root,
        env=environment,
        check=True,
    )
    bundle = output_dir / ("EIDORA.app" if sys.platform == "darwin" else "EIDORA")
    if not bundle.exists():
        raise RuntimeError(f"PyInstaller did not create the expected bundle: {bundle}")
    _repair_macos_qt_framework_links(bundle)
    report = _file_manifest(bundle)
    report_path = output_dir / "bundle-inventory.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    starter_entries = [entry for entry in report["files"] if entry["path"].endswith("/project/resources/" + starter_archive.name)]
    starter_manifest = json.loads((root / "src/project/resources/starter-collection.json").read_text())
    if (len(starter_entries) != 1
            or starter_entries[0]["bytes"] != starter_manifest["archive_bytes"]
            or starter_entries[0]["sha256"] != starter_manifest["sha256"]):
        raise RuntimeError("The bundle must contain exactly one verified starter archive.")
    measured_mib = (report["total_bytes"] - starter_entries[0]["bytes"]) / (1024 * 1024)
    print(f"Starter archive: {starter_entries[0]['bytes'] / 1024**2:.1f} MiB (separate from application budget)")
    print("Largest bundled files (uncompressed MiB):", flush=True)
    for entry in sorted(report["files"], key=lambda entry: entry["bytes"], reverse=True)[:30]:
        print(f"  {entry['bytes'] / (1024 * 1024):8.1f}  {entry['path']}", flush=True)
    if max_mib is not None and measured_mib > max_mib:
        raise RuntimeError(
            f"Application size {measured_mib:.1f} MiB (excluding the verified starter archive) "
            f"exceeds the {max_mib:.1f} MiB release budget"
        )
    print(
        f"Built {bundle} with {report['file_count']} files "
        f"({measured_mib:.1f} MiB application plus the pinned starter archive)."
    )
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-stage", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--app-icon", type=Path)
    parser.add_argument("--max-mib", type=float)
    arguments = parser.parse_args()
    try:
        build(
            native_stage=arguments.native_stage,
            output_dir=arguments.output_dir,
            work_dir=arguments.work_dir,
            app_icon=arguments.app_icon,
            max_mib=arguments.max_mib,
        )
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Bundle build failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
