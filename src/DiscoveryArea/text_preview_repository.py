"""Bounded, cancellable reads of text files and linked JSONL records."""
from dataclasses import dataclass
import json
from pathlib import Path
from threading import Event

from shared.text import IDENTIFIER_KEYS, TEXT_KEYS, open_text, record_value


MAX_PREVIEW_CHARACTERS = 200_000
MAX_RECORD_CHARACTERS = 2_000_000


@dataclass(frozen=True)
class TextPreview:
    text: str = ""
    status: str = ""


def read_text_preview(path: Path, record_key: str, cancelled: Event) -> TextPreview:
    if cancelled.is_set():
        return TextPreview()
    try:
        with open_text(path, errors="replace") as handle:
            if record_key:
                text = _read_record(handle, record_key, cancelled)
                if text is None:
                    return TextPreview(status="The linked text record is no longer available.")
            else:
                text = handle.read(MAX_PREVIEW_CHARACTERS + 1)
        if cancelled.is_set():
            return TextPreview()
        if "\x00" in text:
            return TextPreview(status="This file does not contain supported text.")
        if len(text) > MAX_PREVIEW_CHARACTERS:
            return TextPreview(text[:MAX_PREVIEW_CHARACTERS], "Showing the first 200,000 characters. Use Show File to open the full document.")
        return TextPreview(text, "Empty text document" if not text else "")
    except (OSError, UnicodeError, ValueError) as exc:
        return TextPreview(status=f"Text preview unavailable: {exc}")


def _read_record(handle, record_key: str, cancelled: Event) -> str | None:
    while not cancelled.is_set():
        line = handle.readline(MAX_RECORD_CHARACTERS + 1)
        if not line:
            break
        if len(line) > MAX_RECORD_CHARACTERS:
            raise ValueError("A text record is too large to preview.")
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        if record_value(payload, IDENTIFIER_KEYS).strip() == record_key:
            return record_value(payload, TEXT_KEYS)
    return None
