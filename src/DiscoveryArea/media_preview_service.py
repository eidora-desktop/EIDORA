"""Common formats supported by the individual-item preview implementations.

Import recognition can be broader. Keep this preview contract small and share
it with the packaged release audit; extensions do not guarantee every codec.
"""
from pathlib import Path

SUPPORTED_PREVIEW_EXTENSIONS = {
    "image": frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}),
    "video": frozenset({".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".mpg", ".mpeg"}),
    "audio": frozenset({".wav", ".wave", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".aiff", ".aif"}),
    "text": frozenset({".txt", ".text", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".ndjson"}),
}


def supports_media_preview(content_type: str, path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_PREVIEW_EXTENSIONS.get(content_type, ())
