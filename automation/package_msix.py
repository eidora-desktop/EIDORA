"""Build an unsigned Store MSIX from the existing Windows application bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote
import xml.etree.ElementTree as ET
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "packaging/windows/store.json"
NAMESPACES = {
    "": "http://schemas.microsoft.com/appx/manifest/foundation/windows10",
    "uap": "http://schemas.microsoft.com/appx/manifest/uap/windows10",
    "uap10": "http://schemas.microsoft.com/appx/manifest/uap/windows10/10",
    "rescap": "http://schemas.microsoft.com/appx/manifest/foundation/windows10/restrictedcapabilities",
}


def package_version(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+\.0", value):
        raise ValueError("Store package version must have four integers with a zero fourth component")
    parts = tuple(int(part) for part in value.split("."))
    if not parts[0] or any(part > 65535 for part in parts):
        raise ValueError("Store version components must be 0–65535, with a nonzero first component")
    if value != ".".join(str(part) for part in parts):
        raise ValueError("Store version components must not have leading zeroes")
    return parts


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Store configuration must be a JSON object")
    for key in ("identity_name", "publisher", "publisher_display_name", "display_name"):
        value = config.get(key)
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f"Set {key} in {path} to the exact Partner Center value")
        if any(marker in value.lower() for marker in ("placeholder", "replace_me", "yourpublisher", "yourcompany", "todo", "<", ">")):
            raise ValueError(f"Placeholder Store identity: {key}")
    if not re.fullmatch(r"[A-Za-z0-9.-]{3,50}", config["identity_name"]):
        raise ValueError("Invalid Store identity_name")
    if not config["publisher"].startswith("CN="):
        raise ValueError("Store publisher must be the Partner Center distinguished name (CN=...)")
    version = package_version(config.get("package_version"))
    previous = config.get("last_submitted_version")
    if previous is not None and version <= package_version(previous):
        raise ValueError("package_version must exceed last_submitted_version; update the submission ledger")
    return config


def sdk_tool(name: str, sdk_bin: Path | None = None) -> Path:
    """Use one explicit x64 SDK directory, or the newest installed complete SDK."""
    if sdk_bin is not None:
        candidate = sdk_bin / name
        if not candidate.is_file():
            raise RuntimeError(f"Windows SDK tool is missing: {candidate}")
        return candidate.resolve()
    base = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Windows Kits/10/bin"
    versions = sorted(
        (path for path in base.glob("*/x64") if re.fullmatch(r"\d+(\.\d+){3}", path.parent.name)),
        key=lambda path: tuple(int(value) for value in path.parent.name.split(".")),
        reverse=True,
    )
    for directory in versions:
        if all((directory / tool).is_file() for tool in ("makeappx.exe", "makepri.exe")):
            return sdk_tool(name, directory)
    raise RuntimeError("Install the Windows SDK with MakeAppx and MakePRI, or supply --sdk-bin")


def write_manifest(config: dict, destination: Path) -> None:
    for prefix, namespace in NAMESPACES.items():
        ET.register_namespace(prefix, namespace)
    tree = ET.parse(ROOT / "packaging/windows/AppxManifest.xml")
    root = tree.getroot()
    ns = {"m": NAMESPACES[""], "uap": NAMESPACES["uap"]}
    identity = root.find("m:Identity", ns)
    identity.set("Name", config["identity_name"])
    identity.set("Publisher", config["publisher"])
    identity.set("Version", config["package_version"])
    root.find("m:Properties/m:DisplayName", ns).text = config["display_name"]
    root.find("m:Properties/m:PublisherDisplayName", ns).text = config["publisher_display_name"]
    root.find("m:Applications/m:Application/uap:VisualElements", ns).set("DisplayName", config["display_name"])
    tree.write(destination, encoding="utf-8", xml_declaration=True)


def generate_assets(destination: Path, source: Path | None = None) -> None:
    """Render new Store sizes from approved vectors; do not resample EXE assets."""
    from PySide6 import QtCore, QtGui, QtSvg

    source = source or ROOT / "packaging/icons/linux/hicolor/scalable/apps"
    normal = source / "eidora_icon.svg"
    small = source / "eidora_icon-small.svg"
    for path in (normal, small):
        if not path.is_file():
            raise ValueError(f"Missing approved icon: {path}")
    destination.mkdir(parents=True, exist_ok=True)
    sizes = {}
    for name, size in (("StoreLogo", 50), ("Square44x44Logo", 44), ("Square150x150Logo", 150)):
        for scale in (100, 125, 150, 200, 400):
            sizes[f"{name}.scale-{scale}.png"] = (size * scale + 99) // 100
    for size in (16, 24, 32, 48, 256):
        sizes[f"Square44x44Logo.targetsize-{size}_altform-unplated.png"] = size
    for name, size in sizes.items():
        renderer = QtSvg.QSvgRenderer(str(small if size <= 24 else normal))
        if not renderer.isValid():
            raise ValueError("Invalid approved SVG icon")
        bitmap = QtGui.QImage(size, size, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        bitmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(bitmap)
        renderer.render(painter)
        painter.end()
        if not bitmap.save(str(destination / name), "PNG"):
            raise RuntimeError(f"Could not write Store artwork: {name}")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_payload(package: Path, staging: Path) -> int:
    """Ensure MakeAppx retained every payload byte and no package signature."""
    with zipfile.ZipFile(package) as archive:
        # MSIX stores percent-encoded ZIP names (for example, '+' becomes '%2B').
        # Decode once; unquote_plus would incorrectly turn literal '+' into spaces.
        entries = {}
        for entry in archive.infolist():
            name = unquote(entry.filename, encoding="utf-8", errors="strict")
            if name in entries:
                raise RuntimeError(f"Duplicate MSIX payload path: {name}")
            entries[name] = entry
        if "AppxSignature.p7x" in entries:
            raise RuntimeError("The Store submission package must remain unsigned")
        if archive.testzip() is not None:
            raise RuntimeError("MSIX archive integrity check failed")
        count = 0
        for path in staging.rglob("*"):
            if not path.is_file():
                continue
            name = path.relative_to(staging).as_posix()
            if name not in entries:
                raise RuntimeError(f"MSIX payload missing: {name}")
            with archive.open(entries[name]) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != sha256(path):
                    raise RuntimeError(f"MSIX payload differs: {name}")
            count += 1
    return count


def build(bundle: Path, output: Path, config: dict, app_version: str, commit: str, sdk_bin: Path | None = None) -> Path:
    source = bundle / "EIDORA" if (bundle / "EIDORA").is_dir() else bundle
    if not (source / "EIDORA.exe").is_file() or not (source / "_internal").is_dir():
        raise ValueError(f"Complete Windows application bundle is missing: {source}")
    makeappx = sdk_tool("makeappx.exe", sdk_bin)
    makepri = sdk_tool("makepri.exe", makeappx.parent)
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / f"EIDORA-{config['package_version']}-windows-x64-store.msix"
    with tempfile.TemporaryDirectory(prefix="eidora-msix-", dir=output) as temporary:
        work = Path(temporary)
        staging = work / "payload"
        shutil.copytree(source, staging)
        for reserved in ("AppxManifest.xml", "AppxSignature.p7x", "AppxBlockMap.xml", "[Content_Types].xml", "resources.pri", "Assets"):
            if (staging / reserved).exists():
                raise ValueError(f"Reserved MSIX path already exists in bundle: {reserved}")
        write_manifest(config, staging / "AppxManifest.xml")
        generate_assets(staging / "Assets")
        pri_config = work / "priconfig.xml"
        subprocess.run([str(makepri), "createconfig", "/cf", str(pri_config), "/dq", "en-US", "/o"], check=True)
        subprocess.run([str(makepri), "new", "/pr", str(staging), "/cf", str(pri_config), "/of", str(staging / "resources.pri"), "/o"], check=True)
        staged_package = work / artifact.name
        subprocess.run([str(makeappx), "pack", "/d", str(staging), "/p", str(staged_package), "/h", "SHA256", "/o"], check=True)
        files = verify_payload(staged_package, staging)
        digest = sha256(staged_package)
        report = {
            "application_version": app_version, "package_version": config["package_version"],
            "identity": config, "commit": commit, "artifact": artifact.name, "sha256": digest,
            "makeappx": str(makeappx), "makepri": str(makepri), "payload_files": files,
            "validation": {"makeappx": "passed", "payload": "passed", "unsigned": True,
                           "installed_windows_tests": "not_run", "store_certification": "not_run"},
        }
        staged_package.replace(artifact)
        (output / "SHA256SUMS").write_text(f"{digest}  {artifact.name}\n", encoding="utf-8")
        (output / "msix-build-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--validate-config-only", action="store_true")
    parser.add_argument("--bundle-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--commit")
    parser.add_argument("--sdk-bin", type=Path)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.validate_config_only:
            print("Store configuration validated; confirm identity and submission history in Partner Center.")
            return 0
        if args.bundle_dir is None or args.output_dir is None or not args.version:
            raise ValueError("--bundle-dir, --output-dir and --version are required for packaging")
        commit = args.commit or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        print(build(args.bundle_dir.resolve(), args.output_dir.resolve(), config, args.version, commit, args.sdk_bin))
    except (OSError, ValueError, RuntimeError, KeyError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        print(f"MSIX packaging failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
