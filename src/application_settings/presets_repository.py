"""Inspect and remove reusable presets without opening another workspace."""
from pathlib import Path

from DiscoveryArea.presets_repository import PresetRepository
from shared.persistence import json_store


# Discovery manifests own computed projections; snapshots own saved work.
# Neither belongs in the reusable-settings cleanup list.
PRESET_TYPES = (
    ("appearance", "Appearance"),
    ("camera", "Camera"),
    ("canvas", "Canvas"),
    ("figure_plot_settings", "Figure settings"),
    ("filters", "Filters"),
    ("slicer", "Slicer"),
    ("transform", "Transform"),
)


class ProjectPresetsRepository:
    def __init__(self, project):
        self.root = project.root.resolve()

    def _directory(self, kind):
        if kind not in dict(PRESET_TYPES):
            raise ValueError("This is not a reusable preset type.")
        path = self.root / "presets" / kind
        if (self.root / "presets").is_symlink() or path.is_symlink():
            raise ValueError("Preset management cannot follow linked folders.")
        return path

    def list_groups(self):
        groups = []
        for kind, title in PRESET_TYPES:
            directory = self._directory(kind)
            entries = PresetRepository(directory, create=False).list_entries()
            entries = [entry for entry in entries if entry.path.is_file() and not entry.path.is_symlink()]
            if entries:
                groups.append((title, sorted(entries, key=lambda entry: (entry.label.casefold(), entry.path.name))))
        return groups

    def validate_paths(self, paths):
        checked = []
        for value in dict.fromkeys(paths):
            path = Path(value).absolute()
            directory = self._directory(path.parent.name)
            if path.parent != directory or path.suffix != ".json" or path.is_symlink():
                raise ValueError("Only presets in the selected project can be deleted.")
            if not path.is_file():
                raise FileNotFoundError(f"Preset no longer exists: {path.name}")
            checked.append(path)
        return checked

    def required_references(self):
        """Find live legacy snapshot and animation dependencies, not copied settings."""
        references = {}

        def remember(value, owner):
            if isinstance(value, str) and value:
                path = Path(value.replace("\\", "/"))
                path = path if path.is_absolute() else self.root / path
                references.setdefault(path.resolve(), set()).add(owner)

        def inspect(value, owner):
            if isinstance(value, dict):
                refs = value.get("refs")
                states = value.get("states")
                if isinstance(refs, dict):
                    for kind, path in refs.items():
                        if not isinstance(states, dict) or kind not in states:
                            remember(path, owner)
                for key in ("slicer_preset_path", "slicer_path"):
                    remember(value.get(key), owner)
                for child in value.values():
                    if isinstance(child, (dict, list)):
                        inspect(child, owner)
            elif isinstance(value, list):
                for child in value:
                    inspect(child, owner)

        files = list((self.root / "presets" / "snapshots").glob("*.json"))
        # Figure-owned snapshot copies may still contain legacy component refs.
        files.extend((self.root / "state").rglob("*.json"))
        for path in files:
            if path.is_symlink() or not path.resolve().is_relative_to(self.root):
                raise ValueError("Cannot check dependencies in linked project files.")
            try:
                value = json_store.read_json(path)
            except (OSError, ValueError) as exc:
                raise ValueError(f"Cannot check preset dependencies in {path.relative_to(self.root)}: {exc}") from exc
            inspect(value, str(path.relative_to(self.root)))
        return references

    def delete_paths(self, paths):
        removed, failures = [], []
        for path in paths:
            try:
                self.validate_paths([path])
                path.unlink()
                removed.append(path)
            except (OSError, ValueError) as exc:
                failures.append(f"{path.name}: {exc}")
        return removed, failures
