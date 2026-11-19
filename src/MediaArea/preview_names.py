"""Default names shared by preview dialogs and automatic preview creation."""
from __future__ import annotations

from collections.abc import Iterable


def default_preview_name(
    source_label: str,
    media_type: str,
    *,
    video_clips: bool = False,
    existing_names: Iterable[str] = (),
) -> str:
    suffix = {
        "image": "thumbs",
        "video": "video clips" if video_clips else "frames",
        "audio": "audio clips",
        "text": "labels",
    }[media_type]
    base = f"{source_label} ({suffix})"
    existing = {name.strip().casefold() for name in existing_names}
    candidate = base
    number = 2
    while candidate.casefold() in existing:
        candidate = f"{base} {number}"
        number += 1
    return candidate
