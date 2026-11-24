"""Selection settings and deterministic, literal text excerpts."""
import hashlib

from MediaArea.clip_options import POSITIONS


def text_parameters(parameters):
    position = str(parameters.get("position", "first"))
    limit = int(parameters.get("character_limit", 400))
    boundary = str(parameters.get("text_boundary", "word"))
    seed = int(parameters.get("random_seed", 0))
    if position not in POSITIONS:
        raise ValueError("Choose a supported text excerpt position.")
    if not 1 <= limit <= 10000:
        raise ValueError("Text excerpt length must be between 1 and 10,000 characters.")
    if boundary not in {"word", "character"}:
        raise ValueError("Choose word boundaries or an exact character cut-off.")
    if not 0 <= seed <= 2147483647:
        raise ValueError("Random seed must be between 0 and 2147483647.")
    return dict(preview_kind="text_excerpt", position=position, character_limit=limit,
                text_boundary=boundary, random_seed=seed)


def select_excerpt(text, parameters, identity):
    """Return literal source text and half-open character offsets, without ellipses."""
    limit = parameters["character_limit"]
    available = max(0, len(text) - limit)
    position = parameters["position"]
    if position == "random":
        digest = hashlib.sha256(f"{identity}:{parameters['random_seed']}".encode()).digest()
        start = int.from_bytes(digest[:8], "big") % (available + 1)
    else:
        start = {"first": 0, "middle": available // 2, "last": available}[position]
    end = min(len(text), start + limit)
    original_start, original_end = start, end
    if parameters["text_boundary"] == "word":
        # Prefer complete whitespace-delimited words without enlarging the budget.
        if start and not text[start - 1].isspace():
            while start < end and not text[start].isspace():
                start += 1
        if end < len(text) and not text[end].isspace():
            while end > start and not text[end - 1].isspace():
                end -= 1
        # Long unbroken text (including languages without spaces) still gets a preview.
        if not text[start:end].strip():
            start, end = original_start, original_end
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return text[start:end], start, end


def text_description(parameters):
    boundary = "Prefer complete words" if parameters.get("text_boundary", "word") == "word" else "Character cut-off"
    return f"Up to {parameters.get('character_limit', 400):,} characters · {boundary}"
