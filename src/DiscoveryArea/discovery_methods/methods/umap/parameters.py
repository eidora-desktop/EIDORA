"""Translate EIDORA's earlier UMAP controls to library parameter names."""

from collections.abc import Mapping


def migrate_legacy_parameters(parameters: Mapping[str, object]) -> dict[str, object]:
    values = dict(parameters)
    if "epochs" in values:
        epochs = int(values.pop("epochs") or 0)
        values.setdefault("n_epochs", epochs if epochs > 0 else None)
    if "reproducible" in values:
        repeatable = str(values.pop("reproducible")).strip().lower() in {"true", "1", "yes", "on"}
        values["random_state"] = values.get("random_state", 42) if repeatable else None
    return values


def resolve_legacy_method(method_id: str, parameters: Mapping[str, object]) -> str:
    compatibility = str(parameters.get("compatibility_layout", False)).strip().lower()
    return "umap_05" if method_id == "umap" and compatibility in {"true", "1", "yes", "on"} else method_id
