"""Presentation and scalar access for metadata cells, including structured lists."""
import json
from collections.abc import Iterator


def display_value(value: object) -> str:
    if isinstance(value, list):
        validate_list(value)
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def validate_list(value: list) -> None:
    for entry in value:
        if isinstance(entry, list):
            validate_list(entry)
        elif entry is not None and not isinstance(entry, (str, int, float, bool)):
            raise ValueError("List cells may contain values or nested lists, not objects.")


def scalar_values(value: object) -> Iterator[str]:
    """Yield list entries for item-level queries without losing stored positions."""
    if isinstance(value, list):
        for entry in value:
            yield from scalar_values(entry)
    elif value is not None:
        yield display_value(value)
