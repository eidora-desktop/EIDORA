from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlparse

REPOSITORY_URL = "https://github.com/eidora-desktop/EIDORA"
RELEASES_URL = f"{REPOSITORY_URL}/releases"
LATEST_RELEASE_API = "https://api.github.com/repos/eidora-desktop/EIDORA/releases/latest"
WEBSITE_URL = "https://eidora.org"


@dataclass(frozen=True)
class Download:
    name: str
    url: str


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    downloads: tuple[Download, ...]
    newer: bool


def _version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:\+[\w.-]+)?", value)
    if match is None:
        raise ValueError("The release has an unsupported version number.")
    return tuple(int(part) for part in match.groups())


def parse_release(payload: dict, current_version: str, system: str, machine: str) -> Release:
    """Read stable releases and select only official installers for this runtime."""
    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        raise ValueError("No published stable release is available.")
    tag = payload.get("tag_name", "")
    if not isinstance(tag, str):
        raise ValueError("The release has no valid version number.")
    version = tag.removeprefix("v")
    newer = _version(version) > _version(current_version)
    architecture = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine.lower(), machine.lower())
    suffixes = {
        ("darwin", "arm64"): ("macos-arm64.dmg",),
        ("darwin", "x86_64"): ("macos-x86_64.dmg",),
        ("win32", "x86_64"): ("windows-x86_64-setup.exe",),
        ("linux", "x86_64"): ("linux-x86_64.deb", "linux-x86_64.AppImage"),
    }.get((system, architecture), ())
    assets = payload.get("assets", [])
    if not isinstance(assets, list):
        raise ValueError("The release download list is invalid.")
    downloads = []
    for suffix in suffixes:
        expected_name = f"EIDORA-{version}-{suffix}"
        for asset in assets:
            if not isinstance(asset, dict) or asset.get("name") != expected_name:
                continue
            url = asset.get("browser_download_url", "")
            if not isinstance(url, str):
                continue
            parsed = urlparse(url)
            if (parsed.scheme == "https" and parsed.netloc == "github.com"
                    and url.startswith(f"{RELEASES_URL}/download/") and not parsed.fragment):
                downloads.append(Download(expected_name, url))
                break
    notes = payload.get("body") or "No release notes were provided."
    if not isinstance(notes, str):
        raise ValueError("The release notes are invalid.")
    return Release(version, notes, tuple(downloads), newer)
