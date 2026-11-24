"""Read saved excerpts outside the GUI thread; return plain text only."""
import sqlite3

from DiscoveryArea.thumbnails.repository import ThumbnailVariantRepository


def load_pin_text_previews(root, variant, entries, cancelled):
    output, excerpts = {}, {}
    for annotation_id, metadata_id in entries:
        if cancelled.is_set():
            return None
        if metadata_id not in excerpts:
            try:
                excerpt = ThumbnailVariantRepository.read_preview_text(root, variant, metadata_id)
                if excerpt is None:
                    text = "Text preview unavailable"
                else:
                    text = ("…" if excerpt.start_character else "") + excerpt.text
                    if excerpt.end_character < excerpt.source_characters:
                        text += "…"
            except (OSError, ValueError, sqlite3.Error):
                text = "Text preview unavailable"
            excerpts[metadata_id] = text
        output[annotation_id] = excerpts[metadata_id]
    return None if cancelled.is_set() else output
