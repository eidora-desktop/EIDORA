from __future__ import annotations

from datetime import date
import re

from application_info.domain import Release

REPOSITORY_URL = "https://github.com/eidora-desktop/EIDORA"
WEBSITE_URL = "https://eidora.org"
DOWNLOADS_URL = f"{WEBSITE_URL}/download/"
RELEASES_URL = f"{WEBSITE_URL}/releases/"
UPDATE_CATALOG_URL = f"{WEBSITE_URL}/updates.json"


def _version(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value):
        raise ValueError("The update catalog has an unsupported version number.")
    return tuple(int(part) for part in value.split("."))


def platform_key(system: str, machine: str) -> str:
    architecture = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine.lower(), machine.lower())
    key = {
        ("darwin", "arm64"): "macos-arm64",
        ("darwin", "x86_64"): "macos-x86_64",
        ("win32", "x86_64"): "windows-x86_64",
        ("linux", "x86_64"): "linux-x86_64",
    }.get((system, architecture))
    if key is None:
        raise ValueError("No update channel is available for this operating system and architecture.")
    return key


def parse_release(payload: dict, current_version: str, system: str, machine: str) -> Release | None:
    """Read the website's explicitly recommended, published platform release."""
    if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise ValueError("The update catalog format is unsupported.")
    latest, releases = payload.get("latest"), payload.get("releases")
    key = platform_key(system, machine)
    if not isinstance(latest, dict) or not isinstance(releases, dict) or key not in latest:
        raise ValueError("The update catalog is incomplete.")
    version = latest[key]
    if version is None:
        return None
    newer = _version(version) > _version(current_version)
    record = releases.get(version)
    if not isinstance(record, dict):
        raise ValueError("The recommended release is missing from the update catalog.")
    channel, published_at, notes = record.get("channel"), record.get("published_at"), record.get("notes")
    if channel not in ("prototype", "preview", "stable"):
        raise ValueError("The release channel is invalid.")
    if not isinstance(published_at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", published_at):
        raise ValueError("The release date is invalid.")
    try:
        date.fromisoformat(published_at)
    except ValueError as exc:
        raise ValueError("The release date is invalid.") from exc
    if not isinstance(notes, str) or not notes.strip():
        raise ValueError("The release notes are invalid.")
    platforms = record.get("platforms")
    if not isinstance(platforms, dict) or not isinstance(platforms.get(key), dict) or not platforms[key]:
        raise ValueError("The recommended release is not available for this platform.")
    return Release(
        version=version, channel=channel, published_at=published_at, notes=notes,
        newer=newer, download_url=f"{DOWNLOADS_URL}#{key}",
        notes_url=f"{RELEASES_URL}#v{version}",
    )
