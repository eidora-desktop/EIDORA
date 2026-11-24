"""Bounded source reads, with reusable offsets for explicitly linked JSONL records."""
import json

from shared.text import IDENTIFIER_KEYS, TEXT_KEYS, open_text, record_value

MAX_SOURCE_CHARACTERS = 2_000_000


class TextSourceReader:
    def __init__(self, checkpoint):
        self._checkpoint = checkpoint
        self._signature = None
        self._offsets = {}

    def read(self, path, record_key=""):
        self._checkpoint()
        with open_text(path) as handle:
            if record_key:
                stat = path.stat()
                signature = (path, stat.st_size, stat.st_mtime_ns)
                if signature != self._signature:
                    offsets = {}
                    while True:
                        self._checkpoint()
                        offset = handle.tell()
                        line = handle.readline(MAX_SOURCE_CHARACTERS + 1)
                        if not line:
                            break
                        if len(line) > MAX_SOURCE_CHARACTERS:
                            raise ValueError("A text record exceeds the 2,000,000-character source limit.")
                        try:
                            payload = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(payload, dict):
                            key = record_value(payload, IDENTIFIER_KEYS).strip()
                            if key:
                                # Ambiguous record identities must never select arbitrary content.
                                offsets[key] = offset if key not in offsets else None
                    self._signature, self._offsets = signature, offsets
                if record_key not in self._offsets:
                    raise ValueError("The linked text record is no longer available.")
                offset = self._offsets[record_key]
                if offset is None:
                    raise ValueError("The linked text record identifier is duplicated.")
                handle.seek(offset)
                text = record_value(json.loads(handle.readline(MAX_SOURCE_CHARACTERS + 1)), TEXT_KEYS)
            else:
                chunks, count = [], 0
                while count <= MAX_SOURCE_CHARACTERS:
                    self._checkpoint()
                    chunk = handle.read(min(65536, MAX_SOURCE_CHARACTERS + 1 - count))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    count += len(chunk)
                text = "".join(chunks)
            self._checkpoint()
        if len(text) > MAX_SOURCE_CHARACTERS:
            raise ValueError("Text exceeds the 2,000,000-character source limit; split the document.")
        if "\x00" in text:
            raise ValueError("This file does not contain supported Unicode text.")
        if not text.strip():
            raise ValueError("The text source is empty or contains only whitespace.")
        return text
