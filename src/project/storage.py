from __future__ import annotations

import json
from pathlib import Path

# Re-export value objects for callers using the original storage imports.
from project.domain import PROJECT_MANIFEST_FILENAME, ProjectManifest, ProjectStoragePaths
from shared.persistence import json_store


CURRENT_PROJECT_SCHEMA_VERSION = 1


def write_project_manifest(
    root: Path,
    name: str,
    schema_version: int = CURRENT_PROJECT_SCHEMA_VERSION,
) -> ProjectManifest:
    manifest = ProjectManifest(schema_version=schema_version, name=_validate_manifest_name(name))
    json_store.write_json(
        ProjectStoragePaths(root).manifest,
        {
            "name": manifest.name,
            "schema_version": manifest.schema_version,
        },
    )
    return manifest


def ensure_current_project_schema(root: Path) -> ProjectManifest:
    """Validate a project root and normalize valid pre-manifest projects to v1."""
    paths = ProjectStoragePaths(root)
    if paths.manifest.exists():
        manifest = read_project_manifest(paths.manifest)
        validate_project_layout(paths)
        return manifest

    validate_project_layout(paths)
    return write_project_manifest(root, root.name)


def read_project_manifest(path: Path) -> ProjectManifest:
    try:
        payload = json_store.read_json(path)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Project manifest is malformed JSON: {path}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"Project manifest must be a JSON object: {path}")

    schema_version = payload.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ValueError("Project manifest field 'schema_version' must be an integer.")
    if schema_version > CURRENT_PROJECT_SCHEMA_VERSION:
        raise ValueError(
            "Project schema version "
            f"{schema_version} is newer than this app supports "
            f"({CURRENT_PROJECT_SCHEMA_VERSION})."
        )
    if schema_version < 1:
        raise ValueError(f"Project schema version {schema_version} is not supported.")

    name = payload.get("name")
    return ProjectManifest(schema_version=schema_version, name=_validate_manifest_name(name))


def validate_project_layout(paths: ProjectStoragePaths) -> None:
    for directory in paths.required_directories():
        if not directory.is_dir():
            raise ValueError(
                "Selected directory is not a valid EIDORA project: "
                f"missing required directory {directory.relative_to(paths.root)}."
            )
    for file_path in (paths.metadata_database, paths.metadata_columns):
        if not file_path.is_file():
            raise ValueError(
                "Selected directory is not a valid EIDORA project: "
                f"missing required file {file_path.relative_to(paths.root)}."
            )


def _validate_manifest_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Project manifest field 'name' must be a non-empty string.")
    return value.strip()
