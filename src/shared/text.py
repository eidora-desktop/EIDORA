"""Small Unicode and record-field helpers for text inputs and previews."""
from pathlib import Path


TEXT_KEYS = ("text", "content", "body", "prompt")
IDENTIFIER_KEYS = ("id", "identifier", "filename", "file", "name")


def open_text(path: Path, *, errors: str = "strict"):
    with path.open("rb") as handle:
        prefix = handle.read(4)
    encoding = (
        "utf-32" if prefix.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"))
        else "utf-16" if prefix.startswith((b"\xff\xfe", b"\xfe\xff"))
        else "utf-8-sig"
    )
    return path.open("r", encoding=encoding, errors=errors)


def record_value(payload: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return ""
