from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, TYPE_CHECKING
import math

if TYPE_CHECKING:
    import numpy as np


@dataclass(frozen=True)
class ClusteringParameter:
    id: str
    label: str
    kind: str
    default: object
    minimum: float | int | None = None
    maximum: float | int | None = None
    choices: tuple[object, ...] = ()
    description: str = ""
    advanced: bool = False
    nullable: str = ""
    enabled_when: tuple[tuple[str, tuple[object, ...]], ...] = ()

    def enabled(self, values: dict[str, object]) -> bool:
        return all(values.get(key) in choices for key, choices in self.enabled_when)


@dataclass(frozen=True)
class ClusteringMethodDescriptor:
    id: str
    name: str
    description: str = ""
    parameters: tuple[ClusteringParameter, ...] = ()


@dataclass(frozen=True)
class ClusterRunResult:
    id: str
    run_id: str
    label: str
    method_id: str
    method_name: str
    group_set_id: str
    cluster_count: int
    item_count: int
    matched_item_count: int
    skipped_item_count: int
    parameters: dict[str, object]
    created_at: str


class ClusteringMethod(Protocol):
    """Protocol implemented by clustering algorithms."""

    id: str
    name: str
    description: str
    parameters: tuple[ClusteringParameter, ...]

    def fit_predict(self, embeddings: np.ndarray, parameters: dict[str, object]) -> np.ndarray:
        """Return one cluster label per embedding row."""

    def fit(self, embeddings: np.ndarray, parameters: dict[str, object]) -> ClusteringOutput:
        """Return labels, optional strengths, and bounded diagnostics."""

    def validate(self, parameters: dict[str, object], row_count: int) -> dict[str, object]:
        """Validate scalar and input-dependent constraints."""

    def available_choices(self, values: dict[str, object]) -> dict[str, tuple[object, ...]]:
        """Return compatible choices for conditional editors."""

    def descriptor(self) -> ClusteringMethodDescriptor:
        """Return user-facing metadata for this method."""


def descriptor_for(method: ClusteringMethod) -> ClusteringMethodDescriptor:
    return ClusteringMethodDescriptor(
        id=method.id,
        name=method.name,
        description=method.description,
        parameters=method.parameters,
    )


def normalize_parameters(schema, raw_parameters):
    unknown = set(raw_parameters) - {p.id for p in schema}
    if unknown:
        raise ValueError(f"Unknown clustering parameter: {sorted(unknown)[0]}")
    normalized = {}
    for parameter in schema:
        raw = raw_parameters.get(parameter.id, parameter.default)
        if raw is None and parameter.nullable:
            normalized[parameter.id] = None
            continue
        try:
            value = _coerce_value(raw, parameter.kind)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError(f"Invalid value for {parameter.label}.") from exc
        if isinstance(value, (int, float)) and not math.isfinite(value):
            raise ValueError(f"{parameter.label} must be finite.")
        if parameter.minimum is not None and value < parameter.minimum:
            raise ValueError(f"{parameter.label} must be >= {parameter.minimum}.")
        if parameter.maximum is not None and value > parameter.maximum:
            raise ValueError(f"{parameter.label} must be <= {parameter.maximum}.")
        if parameter.choices and value not in parameter.choices:
            raise ValueError(f"Invalid choice for {parameter.label}: {value}")
        normalized[parameter.id] = value
    return normalized


def _coerce_value(value: Any, kind: str) -> object:
    if kind == "int":
        if isinstance(value, bool) or float(value) != int(value):
            raise ValueError("Expected an integer")
        return int(value)
    if kind == "float":
        return float(value)
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("Expected a boolean")
        return value
    return str(value)


@dataclass(frozen=True)
class ClusteringOutput:
    labels: np.ndarray
    strengths: np.ndarray | None = None
    diagnostics: dict[str, object] | None = None
