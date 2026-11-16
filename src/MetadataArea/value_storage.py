"""SQLite cell codec. Lists are UTF-8 JSON blobs; existing text stays text.

The SQLite storage class distinguishes an array from literal text such as
"[1, 2]", without guessing from its spelling or changing existing projects.
"""
import json

from MetadataArea.values import display_value, validate_list


def encode_value(value: object) -> str | bytes:
    return display_value(value).encode("utf-8") if isinstance(value, list) else display_value(value)


def decode_value(value: object) -> str | list:
    if isinstance(value, bytes):
        decoded = json.loads(value)
        if not isinstance(decoded, list):
            raise ValueError("Stored metadata array must contain a JSON list.")
        validate_list(decoded)
        return decoded
    return display_value(value)
