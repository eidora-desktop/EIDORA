"""Read self-contained BibTeX records without importing computation libraries.

Package citations use literal braced, quoted, or numeric fields, so their text
can be rendered offline without an external bibliography or string macros.
The original record is retained for lossless BibTeX export.
"""
from __future__ import annotations

import re


def parse_bibtex(value: object) -> tuple[dict[str, str], ...]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("bibtex must be a non-empty string containing BibTeX records.")
    text = value.strip()
    position = 0
    records = []
    keys = set()

    def skip_space() -> None:
        nonlocal position
        while position < len(text):
            if text[position].isspace():
                position += 1
            elif text[position] == "%":
                newline = text.find("\n", position)
                position = len(text) if newline < 0 else newline + 1
            else:
                break

    def literal() -> str:
        nonlocal position
        if position >= len(text):
            raise ValueError("bibtex contains an unfinished field.")
        opening = text[position]
        if opening not in '{"':
            number = re.match(r"\d+", text[position:])
            if not number:
                raise ValueError("bibtex fields must use braced, quoted, or numeric literals; expand string macros first.")
            position += len(number[0])
            return number[0]
        position += 1
        start = position
        depth = 1 if opening == "{" else 0
        while position < len(text):
            character = text[position]
            if character == "\\":
                position += 2
                continue
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth < 0:
                    break
                if opening == "{" and depth == 0:
                    result = text[start:position]
                    position += 1
                    return result
            elif character == '"' and opening == '"' and depth == 0:
                result = text[start:position]
                position += 1
                return result
            position += 1
        raise ValueError("bibtex contains an unclosed or unbalanced field.")

    while True:
        skip_space()
        if position == len(text):
            break
        start = position
        header = re.match(r"@([A-Za-z]+)\s*([({])\s*([^\s,{}()]+)\s*,", text[position:])
        if not header or header[1].lower() in {"string", "comment", "preamble"}:
            raise ValueError("bibtex must contain complete @type{key, ...} citation records.")
        key = header[3]
        if key.casefold() in keys:
            raise ValueError(f"bibtex contains a duplicate citation key: {key}.")
        keys.add(key.casefold())
        closing = "}" if header[2] == "{" else ")"
        position += len(header[0])
        fields = {}
        while True:
            skip_space()
            if position < len(text) and text[position] == closing:
                position += 1
                break
            field = re.match(r"([A-Za-z][A-Za-z0-9_-]*)\s*=\s*", text[position:])
            if not field:
                raise ValueError(f"bibtex record {key} contains an invalid or unfinished field.")
            name = field[1].lower()
            if name in fields:
                raise ValueError(f"bibtex record {key} repeats field {name}.")
            position += len(field[0])
            fields[name] = literal()
            skip_space()
            if position < len(text) and text[position] == ",":
                position += 1
            elif position >= len(text) or text[position] != closing:
                raise ValueError(f"bibtex record {key} requires a comma between fields.")
        # Strip case-protection braces and fold whitespace for display only.
        fields = {name: " ".join(re.sub(r"(?<!\\)[{}]", "", content).split())
                  for name, content in fields.items()}
        if not fields.get("title", ""):
            raise ValueError(f"bibtex record {key} requires a title for caption citations.")
        records.append({**fields, "id": key, "bibtex": text[start:position]})
    if not records:
        raise ValueError("bibtex must contain at least one citation record.")
    return tuple(records)
