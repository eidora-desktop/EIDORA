"""Create the native installation artifact for the current operating system."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


def _write_checksums(output_dir: Path, artifacts: list[Path]) -> Path:
    records = []
    for artifact in artifacts:
        with artifact.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        records.append({"file": artifact.name, "bytes": artifact.stat().st_size, "sha256": digest})
    report = output_dir / "installer-checksums.json"
    report.write_text(json.dumps({"artifacts": records}, indent=2) + "\n", encoding="utf-8")
    (output_dir / "SHA256SUMS").write_text(
        "".join(f"{record['sha256']}  {record['file']}\n" for record in records),
        encoding="utf-8",
    )
    return report


def _macos(bundle_dir: Path, output_dir: Path, version: str) -> list[Path]:
    application = bundle_dir if bundle_dir.suffix == ".app" else bundle_dir / "EIDORA.app"
    if not application.is_dir():
        raise RuntimeError(f"macOS application bundle is missing: {application}")
    artifact = output_dir / f"EIDORA-{version}-macos-{os.uname().machine}.dmg"
    with tempfile.TemporaryDirectory(prefix="eidora-dmg-") as temporary:
        staging = Path(temporary) / "EIDORA"
        staging.mkdir()
        shutil.copytree(application, staging / application.name, symlinks=True)
        os.symlink("/Applications", staging / "Applications")
        subprocess.run(
            [
                "hdiutil",
                "create",
                "-volname",
                "EIDORA",
                "-srcfolder",
                str(staging),
                "-format",
                "UDZO",
                "-ov",
                str(artifact),
            ],
            check=True,
        )
    return [artifact]


def _windows(bundle_dir: Path, output_dir: Path, version: str, icon_dir: Path) -> list[Path]:
    source = bundle_dir / "EIDORA" if (bundle_dir / "EIDORA").is_dir() else bundle_dir
    if not (source / "EIDORA.exe").is_file():
        raise RuntimeError(f"Windows application bundle is missing: {source}")
    compiler = shutil.which("ISCC.exe") or shutil.which("iscc")
    if compiler is None:
        raise RuntimeError("Inno Setup compiler (ISCC.exe) is required")
    root = Path(__file__).resolve().parents[1]
    signing = []
    if os.environ.get("WINDOWS_CERTIFICATE_PFX_BASE64"):
        script = str(root / "automation" / "sign_windows.ps1").replace("$", "$$")
        signing = [
            "/DSignWindows=1",
            f'/SEIDORASign=pwsh -NoProfile -File $q{script}$q -Path $f',
        ]
    subprocess.run(
        [
            compiler,
            *signing,
            f"/DSourceDir={source.resolve()}",
            f"/DOutputDir={output_dir.resolve()}",
            f"/DAppVersion={version}",
            f"/DAppIcon={(icon_dir / 'eidora.ico').resolve()}",
            str(root / "packaging" / "windows" / "EIDORA.iss"),
        ],
        check=True,
    )
    return [output_dir / f"EIDORA-{version}-windows-x86_64-setup.exe"]


def _debian_installed_size(package_root: Path) -> int:
    """Estimate payload size in KiB without following symlinks (Debian Policy 5.6.20)."""
    size = 0
    for path in package_root.rglob("*"):
        if path.relative_to(package_root).parts[0] == "DEBIAN":
            continue  # Package control files are not installed payload.
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            size += (info.st_size + 1023) // 1024
        else:
            size += 1
    return size


def _linux(
    bundle_dir: Path,
    output_dir: Path,
    version: str,
    icon_dir: Path,
    appimagetool: Path,
) -> list[Path]:
    source = bundle_dir / "EIDORA" if (bundle_dir / "EIDORA").is_dir() else bundle_dir
    executable = source / "EIDORA"
    if not executable.is_file():
        raise RuntimeError(f"Linux application bundle is missing: {source}")

    artifacts: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="eidora-linux-") as temporary:
        temporary_path = Path(temporary)
        package_root = temporary_path / "deb"
        application_root = package_root / "usr" / "lib" / "eidora"
        shutil.copytree(source, application_root, symlinks=True)
        command_dir = package_root / "usr" / "bin"
        command_dir.mkdir(parents=True)
        os.symlink("../lib/eidora/EIDORA", command_dir / "eidora")
        applications = package_root / "usr" / "share" / "applications"
        applications.mkdir(parents=True)
        desktop_text = (
            "[Desktop Entry]\nType=Application\nName=EIDORA\n"
            "Exec=/usr/bin/eidora\nIcon=eidora_icon\nCategories=Graphics;Science;\nTerminal=false\n"
        )
        (applications / "eidora.desktop").write_text(desktop_text, encoding="utf-8")
        shutil.copytree(
            icon_dir / "hicolor",
            package_root / "usr" / "share" / "icons" / "hicolor",
            dirs_exist_ok=True,
        )
        debian = package_root / "DEBIAN"
        debian.mkdir()
        installed_size = _debian_installed_size(package_root)
        (debian / "control").write_text(
            "Package: eidora\n"
            f"Version: {version}\nArchitecture: amd64\n"
            "Maintainer: EIDORA\nSection: science\nPriority: optional\n"
            f"Installed-Size: {installed_size}\n"
            "Description: EIDORA desktop application\n",
            encoding="utf-8",
        )
        deb = output_dir / f"EIDORA-{version}-linux-x86_64.deb"
        subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(package_root), str(deb)], check=True)
        artifacts.append(deb)

        appdir = temporary_path / "EIDORA.AppDir"
        app_application = appdir / "usr" / "lib" / "eidora"
        shutil.copytree(source, app_application, symlinks=True)
        app_run = appdir / "AppRun"
        app_run.write_text(
            '#!/bin/sh\nHERE="$(dirname "$(readlink -f "$0")")"\nexec "$HERE/usr/lib/eidora/EIDORA" "$@"\n',
            encoding="utf-8",
        )
        app_run.chmod(0o755)
        (appdir / "eidora.desktop").write_text(desktop_text.replace("Exec=/usr/bin/eidora", "Exec=EIDORA"), encoding="utf-8")
        shutil.copytree(
            icon_dir / "hicolor",
            appdir / "usr" / "share" / "icons" / "hicolor",
            dirs_exist_ok=True,
        )
        shutil.copy2(icon_dir / "eidora-256.png", appdir / "eidora_icon.png")
        os.symlink("eidora_icon.png", appdir / ".DirIcon")
        appimage = output_dir / f"EIDORA-{version}-linux-x86_64.AppImage"
        environment = dict(os.environ)
        environment["ARCH"] = "x86_64"
        environment["APPIMAGE_EXTRACT_AND_RUN"] = "1"
        subprocess.run([str(appimagetool.resolve()), str(appdir), str(appimage)], env=environment, check=True)
        artifacts.append(appimage)
    return artifacts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--version")
    parser.add_argument("--icon-dir", type=Path)
    parser.add_argument("--checksums-only", action="store_true", help="Hash final installers after signing/notarization")
    parser.add_argument("--appimagetool", type=Path)
    arguments = parser.parse_args()
    arguments.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        if arguments.checksums_only:
            artifacts = sorted(path for path in arguments.output_dir.iterdir()
                               if path.is_file() and path.suffix in (".exe", ".dmg", ".deb", ".AppImage"))
            if not artifacts:
                raise RuntimeError("No final installers exist to checksum")
            print(_write_checksums(arguments.output_dir, artifacts))
            return 0
        if arguments.bundle_dir is None or not arguments.version or arguments.icon_dir is None:
            raise RuntimeError("--bundle-dir, --version and --icon-dir are required to create installers")
        if sys.platform == "darwin":
            artifacts = _macos(arguments.bundle_dir, arguments.output_dir, arguments.version)
        elif os.name == "nt":
            artifacts = _windows(arguments.bundle_dir, arguments.output_dir, arguments.version, arguments.icon_dir)
        else:
            if arguments.appimagetool is None:
                raise RuntimeError("--appimagetool is required on Linux")
            artifacts = _linux(
                arguments.bundle_dir,
                arguments.output_dir,
                arguments.version,
                arguments.icon_dir,
                arguments.appimagetool,
            )
        missing = [str(path) for path in artifacts if not path.is_file()]
        if missing:
            raise RuntimeError(f"Installer creation did not produce: {', '.join(missing)}")
        print(f"Created {len(artifacts)} installer(s); run --checksums-only after signing/notarization")
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Installer packaging failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
