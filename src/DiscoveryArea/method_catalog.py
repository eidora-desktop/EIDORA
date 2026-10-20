from __future__ import annotations

import copy
import importlib
import os
from pathlib import Path
from typing import Any

import yaml

from DiscoveryArea.discovery_methods.base import DiscoveryMethod, DiscoveryMethodDefinition
from shared.env import get_env


DISCOVERY_METHOD_PATHS_ENV_VAR = "EIDORA_DISCOVERY_METHOD_PATHS"


class DiscoveryMethodCatalog:
    """Discovers and instantiates configured discovery algorithms."""

    def __init__(
        self,
        methods_root: Path | None = None,
        extra_methods_roots: list[Path] | None = None,
    ) -> None:
        self._methods_root = methods_root or Path(__file__).resolve().parent / "discovery_methods" / "methods"
        self._extra_methods_roots = list(extra_methods_roots or [])
        self._extra_methods_roots.extend(
            Path(value)
            for value in get_env(DISCOVERY_METHOD_PATHS_ENV_VAR).split(os.pathsep)
            if value.strip()
        )
        self._definitions: dict[str, DiscoveryMethodDefinition] | None = None
        self._errors: list[str] = []

    @property
    def errors(self) -> list[str]:
        if self._definitions is None:
            self.list_methods()
        return list(self._errors)

    def list_methods(self) -> list[DiscoveryMethodDefinition]:
        if self._definitions is None:
            self._definitions = self._discover()
        return list(self._definitions.values())

    def get(self, method_id: str) -> DiscoveryMethodDefinition | None:
        key = str(method_id or "").strip().lower()
        if not key:
            return None
        if self._definitions is None:
            self.list_methods()
        return self._definitions.get(key) if self._definitions is not None else None

    def create(self, method_id: str) -> DiscoveryMethod:
        definition = self.get(method_id)
        if definition is None:
            raise ValueError(f"Unknown discovery method: {method_id}")
        module = importlib.import_module(definition.package)
        cls = getattr(module, definition.class_name)
        return cls()

    def default_parameters(self, method_id: str) -> dict[str, object]:
        definition = self.get(method_id)
        if definition is None:
            raise ValueError(f"Unknown discovery method: {method_id}")
        defaults: dict[str, object] = {}
        for name, spec in definition.parameters.items():
            defaults[name] = copy.deepcopy(spec.get("default"))
        return defaults

    def normalize_parameters(self, method_id: str, values: dict[str, object] | None) -> dict[str, object]:
        definition = self.get(method_id)
        if definition is None:
            raise ValueError(f"Unknown discovery method: {method_id}")
        incoming = values or {}
        normalized = self.default_parameters(method_id)
        for name, spec in definition.parameters.items():
            if name not in incoming:
                continue
            normalized[name] = self._coerce_parameter(name, spec, incoming[name])
        return normalized

    def _discover(self) -> dict[str, DiscoveryMethodDefinition]:
        self._errors = []
        definitions: dict[str, DiscoveryMethodDefinition] = {}
        for config_path in self._iter_config_paths():
            try:
                payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    continue
                method_id = str(payload.get("id") or config_path.parent.name).strip().lower()
                if not method_id:
                    continue
                if method_id in definitions:
                    self._errors.append(f"{config_path}: duplicate discovery method id '{method_id}'")
                    continue
                status = str(payload.get("status") or "available").strip().lower()
                if status not in {"available", "enabled"}:
                    continue
                parameters = payload.get("parameters")
                if not isinstance(parameters, dict):
                    parameters = {}
                package = str(payload.get("package") or f"DiscoveryArea.discovery_methods.methods.{method_id}.method")
                class_name = str(payload.get("class") or "DiscoveryMethodImpl")
                execution = str(payload.get("execution") or "in_process").strip().lower()
                if execution not in {"in_process", "subprocess"}:
                    self._errors.append(f"{config_path}: unsupported execution mode '{execution}'")
                    continue
                output_dimensions = payload.get("output_dimensions")
                if not isinstance(output_dimensions, list) or not output_dimensions:
                    output_dimensions = [2]
                definitions[method_id] = DiscoveryMethodDefinition(
                    method_id=method_id,
                    name=str(payload.get("name") or method_id),
                    package=package,
                    class_name=class_name,
                    config_path=config_path,
                    description=str(payload.get("description") or ""),
                    parameters={
                        str(key): dict(value)
                        for key, value in parameters.items()
                        if isinstance(value, dict)
                    },
                    status=status,
                    execution=execution,
                    supports_transform=bool(payload.get("supports_transform", False)),
                    supports_arrangement_transform=bool(
                        payload.get("supports_arrangement_transform", False)
                    ),
                    output_dimensions=tuple(int(value) for value in output_dimensions),
                    citations=tuple(
                        dict(value)
                        for value in payload.get("citations", [])
                        if isinstance(value, dict)
                    ),
                    implementation=(
                        dict(payload.get("implementation"))
                        if isinstance(payload.get("implementation"), dict)
                        else {}
                    ),
                )
            except Exception as exc:
                self._errors.append(f"{config_path}: {exc}")
        return definitions

    def _iter_config_paths(self) -> list[Path]:
        roots = [self._methods_root, *self._extra_methods_roots]
        config_paths: list[Path] = []
        for root in roots:
            if root.exists():
                config_paths.extend(sorted(root.glob("*/config.yaml")))
        return config_paths

    def _coerce_parameter(self, name: str, spec: dict[str, Any], value: object) -> object:
        kind = str(spec.get("type") or "str").strip().lower()
        if kind == "int":
            if isinstance(value, bool):
                raise ValueError(f"Parameter '{name}' must be an integer.")
            try:
                numeric = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Parameter '{name}' must be an integer.") from exc
            if not numeric.is_integer():
                raise ValueError(f"Parameter '{name}' must be an integer.")
            coerced: object = int(numeric)
        elif kind == "float":
            if isinstance(value, bool):
                raise ValueError(f"Parameter '{name}' must be a number.")
            coerced = float(value)
        elif kind == "bool":
            coerced = _coerce_bool(value)
        else:
            coerced = str(value)
        choices = spec.get("choices")
        if isinstance(choices, list) and choices and coerced not in choices:
            raise ValueError(f"Parameter '{name}' must be one of: {', '.join(str(item) for item in choices)}")
        if kind in {"int", "float"}:
            numeric = float(coerced)
            if spec.get("min") is not None and numeric < float(spec["min"]):
                raise ValueError(f"Parameter '{name}' must be at least {spec['min']}.")
            if spec.get("max") is not None and numeric > float(spec["max"]):
                raise ValueError(f"Parameter '{name}' must be at most {spec['max']}.")
        return coerced


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Cannot parse boolean value: {value}")


ProjectionMethodCatalog = DiscoveryMethodCatalog
