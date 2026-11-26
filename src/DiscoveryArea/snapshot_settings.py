"""Resolve captured settings, consulting preset files only for legacy omissions."""
from collections.abc import Callable


def snapshot_component_states(payload: dict, load_ref: Callable) -> dict:
    raw = payload.get("states")
    states = dict(raw) if isinstance(raw, dict) else {}
    raw = payload.get("refs")
    refs = raw if isinstance(raw, dict) else {}
    for kind in ("appearance", "filters", "camera", "slicer", "transform"):
        if kind in states:
            continue  # An explicitly empty/disabled state is authoritative too.
        legacy = load_ref(refs.get(kind)) or {}
        states[kind] = legacy.get(kind, legacy) if kind in ("camera", "slicer", "transform") else legacy
    return states
