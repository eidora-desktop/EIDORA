from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Release:
    version: str
    channel: str
    published_at: str
    notes: str
    newer: bool
    download_url: str
    notes_url: str
