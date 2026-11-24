"""Bounded document reads and reusable, package-owned text tokenization."""
import json
from functools import cached_property
from pathlib import Path

import numpy as np

from shared.text import IDENTIFIER_KEYS, TEXT_KEYS, open_text, record_value


MAX_TEXT_CHARACTERS = 2_000_000


class TextPreparer:
    def __init__(self, prep: dict, package_directory: Path):
        self.prep = prep
        self.directory = package_directory / str(prep["tokenizer_path"])
        self._record_file = None
        self._record_offsets = {}

    @cached_property
    def tokenizer(self):
        from tokenizers import Tokenizer

        tokenizer = Tokenizer.from_file(str(self.directory / "tokenizer.json"))
        tokenizer.enable_truncation(max_length=int(self.prep["max_length"]), direction=self.prep["truncation_side"])
        token = "[PAD]"
        config = self.directory / "tokenizer_config.json"
        if config.is_file():
            try:
                raw = json.loads(config.read_text(encoding="utf-8")).get("pad_token")
                if isinstance(raw, dict):
                    raw = raw.get("content")
                if isinstance(raw, str) and raw:
                    token = raw
            except (OSError, ValueError):
                pass
        tokenizer.enable_padding(length=int(self.prep["max_length"]), direction=self.prep["padding_side"],
                                 pad_id=tokenizer.token_to_id(token) or 0, pad_token=token)
        return tokenizer

    def prepare(self, batch):
        result = {key: np.empty((len(batch), int(self.prep["max_length"])), dtype=np.int64)
                  for key in ("token_ids", "attention_mask", "token_type_ids")}
        for index, item in enumerate(batch):
            label = f"{item.path}" + (f" (record {item.text_record_key!r})" if item.text_record_key else "")
            try:
                text = self._read(item)
                if "\x00" in text:
                    raise ValueError("contains NUL characters; use a Unicode text file")
                if not text.strip():
                    raise ValueError("text is empty or contains only whitespace")
                # Check content before model prefixes can turn an empty document into a prompt.
                text = str(self.prep.get("prefix") or "") + text + str(self.prep.get("suffix") or "")
                encoded = self.tokenizer.encode(text, add_special_tokens=bool(self.prep["add_special_tokens"]))
                result["token_ids"][index] = encoded.ids
                result["attention_mask"][index] = encoded.attention_mask
                result["token_type_ids"][index] = encoded.type_ids
                # Do not retain full documents or tokenizer overflow across inputs.
                del encoded, text
            except (OSError, UnicodeError, ValueError) as exc:
                raise RuntimeError(f"Text input {label}: {exc}.") from exc
        return result

    def _read(self, item):
        with open_text(item.path) as handle:
            if not item.text_record_key:
                text = handle.read(MAX_TEXT_CHARACTERS + 1)
                if len(text) > MAX_TEXT_CHARACTERS:
                    raise ValueError(f"exceeds the {MAX_TEXT_CHARACTERS:,}-character text limit; split the document")
                return text
            stat = item.path.stat()
            signature = (item.path, stat.st_size, stat.st_mtime_ns)
            if signature != self._record_file:
                # Keep offsets for one source, never all record contents. TextIO's
                # seek cookies also work for BOM-marked UTF-16/32 sources.
                offsets = {}
                while True:
                    offset = handle.tell()
                    line = handle.readline(MAX_TEXT_CHARACTERS + 1)
                    if not line:
                        break
                    if len(line) > MAX_TEXT_CHARACTERS:
                        raise ValueError(f"a record exceeds the {MAX_TEXT_CHARACTERS:,}-character text limit")
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        key = record_value(record, IDENTIFIER_KEYS).strip()
                        if key:
                            if key in offsets:
                                raise ValueError(f"duplicate text record identifier {key!r}")
                            offsets[key] = offset
                self._record_offsets, self._record_file = offsets, signature
            offset = self._record_offsets.get(item.text_record_key)
            if offset is None:
                raise ValueError("text record was not found")
            handle.seek(offset)
            return record_value(json.loads(handle.readline(MAX_TEXT_CHARACTERS + 1)), TEXT_KEYS)
