"""Validate and inventory resources included in an EIDORA release bundle."""

from __future__ import annotations

import argparse
import ast
from importlib.util import find_spec
import json
import re
import sys
import tomllib
from pathlib import Path
from xml.etree import ElementTree


ICON_CALL_PATTERN = re.compile(
    r"(?:icon|icon_path|tinted_icon_pixmap)\(\s*[\"']([a-z0-9_]+)[\"']"
)


class InventoryFailure(RuntimeError):
    pass


def _matches_any(path: Path, root: Path, patterns: list[str]) -> bool:
    relative = path.relative_to(root)
    return any(relative.match(pattern) for pattern in patterns)


def _load_resources(root: Path) -> tuple[dict[str, object], set[Path]]:
    manifest_path = root / "packaging" / "resource-manifest.toml"
    with manifest_path.open("rb") as stream:
        manifest = tomllib.load(stream)
    if manifest.get("schema_version") != 1:
        raise InventoryFailure("Unsupported resource manifest schema.")

    excluded = list(manifest.get("exclude", {}).get("patterns", []))
    resources: set[Path] = set()
    empty_patterns: list[str] = []
    for group in manifest.get("groups", []):
        for pattern in group.get("patterns", []):
            matches = {
                path.resolve()
                for path in root.glob(pattern)
                if path.is_file() and not _matches_any(path, root, excluded)
            }
            if not matches:
                empty_patterns.append(pattern)
            resources.update(matches)
    if empty_patterns:
        raise InventoryFailure(
            "Resource patterns matched no files: " + ", ".join(empty_patterns)
        )
    return manifest, resources


def _referenced_icons(root: Path, resources: set[Path], icon_names: set[str]) -> set[str]:
    names: set[str] = set()
    for path in resources:
        if path.suffix == ".ui":
            tree = ElementTree.parse(path)
            for property_node in tree.getroot().iter("property"):
                if property_node.attrib.get("name") != "iconName":
                    continue
                name = (property_node.findtext("string") or "").strip()
                if name:
                    names.add(name)
    for path in (root / "src").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        names.update(ICON_CALL_PATTERN.findall(source))
        # Controllers also assign iconName dynamically from local mappings.
        # Treat every exact icon-name string in Python as a reference. This is
        # conservative, but still lets the release omit assets never named by
        # either Python or a Designer form.
        tree = ast.parse(source, filename=str(path))
        names.update(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in icon_names
        )
    return names


def release_resources(root: Path) -> tuple[dict[str, object], set[Path], list[str]]:
    manifest, resources = _load_resources(root)
    icon_files = {
        path.stem: path
        for path in resources
        if path.parent.name == "material" and path.suffix == ".svg"
    }
    referenced_icons = _referenced_icons(root, resources, set(icon_files))
    missing_icons = sorted(referenced_icons - icon_files.keys())
    if missing_icons:
        raise InventoryFailure(
            "Referenced semantic icons are absent: " + ", ".join(missing_icons)
        )

    unused_icons = sorted(icon_files.keys() - referenced_icons)
    resources.difference_update(icon_files[name] for name in unused_icons)
    return manifest, resources, unused_icons


def inventory(root: Path) -> dict[str, object]:
    manifest, resources, unused_icons = release_resources(root)
    dynamic_modules = manifest.get("dynamic_imports", {}).get("modules", [])
    missing_modules: list[str] = []
    for module in dynamic_modules:
        if not _dynamic_module_exists(root, module):
            missing_modules.append(module)
    if missing_modules:
        raise InventoryFailure(
            "Dynamic import modules are absent: " + ", ".join(missing_modules)
        )

    entries = [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
        }
        for path in sorted(resources)
    ]
    starter_manifest = json.loads((root / "src/project/resources/starter-collection.json").read_text())
    archive = root / "build/starter" / starter_manifest["archive"]
    if archive.exists():
        import hashlib
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != starter_manifest["sha256"] or archive.stat().st_size != starter_manifest["archive_bytes"]:
            raise InventoryFailure("Starter collection checksum mismatch.")
    return {
        "starter_collection": {**starter_manifest, "archive_available": archive.is_file()},
        "schema_version": 1,
        "resource_count": len(entries),
        "total_bytes": sum(entry["bytes"] for entry in entries),
        "dynamic_imports": dynamic_modules,
        "excluded_unused_icons": unused_icons,
        "resources": entries,
    }


def _dynamic_module_exists(root: Path, module: str) -> bool:
    parts = module.split(".")
    source_root = root / "src"
    if (source_root / parts[0]).is_dir() or (source_root / f"{parts[0]}.py").is_file():
        module_path = source_root.joinpath(*parts)
        return module_path.with_suffix(".py").is_file() or (module_path / "__init__.py").is_file()
    # Export backends and format plugins live in installed dependencies, not src/.
    try:
        return find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        report = inventory(root)
    except (InventoryFailure, OSError, ElementTree.ParseError, tomllib.TOMLDecodeError) as error:
        print(f"Resource inventory failed: {error}", file=sys.stderr)
        return 1
    rendered = json.dumps(report, indent=2) + "\n"
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
