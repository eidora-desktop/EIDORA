"""Bounded structural detection and strict, non-executable vector parsing."""
from __future__ import annotations

import json
import re

import numpy as np


def parse_vector(value) -> list[float]:
    if isinstance(value, str):
        text = value.strip()
        if re.search(r"[,;]\s*[,;]", text):
            raise ValueError("A vector contains an empty component.")
        if text.startswith("["):
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                if not text.endswith("]"):
                    raise ValueError("A vector has an unclosed bracket.") from None
                value = re.split(r"[\s,;]+", text[1:-1].strip())
        else:
            value = re.split(r"[\s,;]+", text)
    if not isinstance(value, (list, tuple, np.ndarray)) or not len(value):
        raise ValueError("Expected a nonempty numeric vector.")
    if any(isinstance(item, (bool, list, tuple, dict)) for item in value):
        raise ValueError("Vectors must contain numbers, not nested values or booleans.")
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        raise ValueError("A vector contains a nonnumeric value.") from None


def detect_fields(columns, sample):
    """A sample proposes a layout; the streaming importer validates every row."""
    vectors = []
    for name in columns:
        value = sample.get(name)
        # A single numeric scalar is a field, not evidence of a vector.
        if not isinstance(value, (str, list, tuple, np.ndarray)):
            continue
        if isinstance(value, str) and not re.search(r"[\s,;\[\]]", value.strip()):
            continue
        try:
            parsed = parse_vector(value)
        except ValueError:
            continue
        if len(parsed) > 1 or isinstance(value, (list, tuple, np.ndarray)) or value.strip().startswith("["):
            vectors.append(name)
    numbered = []
    prefixes = set()
    for name in columns:
        match = re.fullmatch(r"(.+?)[_ ]?(\d+)", name)
        if match and match[1].rstrip("_ ").lower() in {
            "v", "feature", "dim", "dimension", "embedding", "vector",
        }:
            prefixes.add(match[1].rstrip("_ ").lower())
            numbered.append((int(match[2]), name))
    wide = tuple(name for _, name in sorted(numbered)) if len(prefixes) == 1 else ()
    used = set(vectors) | set(wide)
    return tuple(vectors), wide, tuple(name for name in columns if name not in used)
