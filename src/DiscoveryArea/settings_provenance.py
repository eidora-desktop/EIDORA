"""Report saved Discovery settings without evaluating filters or coordinates."""
from __future__ import annotations

from shared.methods_report import CitationRecord, MethodsReport, eidora_citation_payload, _value, setting_rows
from eidora_version import __version__


def settings_report(label: str, *, filters: dict | None = None, slicer: dict | None = None,
                    transform: dict | None = None) -> MethodsReport:
    sentences, rows = [], []
    if transform is not None:
        snap = transform.get("snap_percent", 0)
        spacing = transform.get("minimum_spacing", 0)
        if snap:
            sentences.append(f"EIDORA Grid transform was applied at {snap}% with {transform.get('grid_aspect_preset', 'free')} aspect.")
        if spacing:
            sentences.append(f"EIDORA No Overlap was applied with minimum spacing {spacing} in normalized projection units.")
        if not snap and not spacing:
            sentences.append("No EIDORA coordinate transforms were applied." if transform else "No transform settings were recorded.")
        rows.extend((f"Transform / {key.replace('_', ' ')}", _value(value)) for key, value in transform.items())
    for name, state in (("Filters", filters), ("Slicer", slicer)):
        if state is None:
            continue
        enabled = state.get("enabled", bool(state))
        sentences.append(f"{name} {'enabled' if enabled else 'disabled'}; saved settings are reported below.")
        rows.extend(setting_rows(name, state) if state else [])
        if not state:
            rows.append((name, "No settings recorded"))
    return MethodsReport(f"{label} — Methods and Citations", " ".join(sentences), tuple(rows),
                         (CitationRecord.from_payload(eidora_citation_payload()),),
                         (("Report generated with EIDORA", __version__),))
