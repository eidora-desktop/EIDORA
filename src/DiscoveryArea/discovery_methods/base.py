from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np


ProgressCallback = Callable[[int], None]


@dataclass(frozen=True)
class DiscoveryMethodDefinition:
    """Configured discovery method discovered from a method package."""

    method_id: str
    name: str
    package: str
    class_name: str
    config_path: Path
    description: str = ""
    parameters: dict[str, dict[str, Any]] = field(default_factory=dict)
    status: str = "available"
    execution: str = "in_process"
    supports_transform: bool = False
    supports_arrangement_transform: bool = False
    output_dimensions: tuple[int, ...] = (2,)
    citations: tuple[dict[str, Any], ...] = ()
    implementation: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectionExecutionResult:
    """Coordinates plus the effective recipe used to produce them."""

    coordinates: np.ndarray
    requested_parameters: dict[str, object] = field(default_factory=dict)
    effective_parameters: dict[str, object] = field(default_factory=dict)
    derived_steps: tuple[str, ...] = ()
    implementation_version: str = ""
    repeatability: str = "unknown"
    complete: bool = True
    citations: tuple[dict[str, object], ...] = ()

    @property
    def shape(self):
        return self.coordinates.shape

    @property
    def dtype(self):
        return self.coordinates.dtype

    @property
    def ndim(self):
        return self.coordinates.ndim

    def __array__(self, dtype=None, copy=None):
        array = np.asarray(self.coordinates, dtype=dtype)
        return array.copy() if copy else array

    def __getitem__(self, item):
        return self.coordinates[item]


PCA_CITATION: dict[str, object] = {
    "id": "pearson1901pca",
    "role": "intermediate_method",
    "title": "On lines and planes of closest fit to systems of points in space",
    "authors": ["Pearson, Karl"],
    "year": 1901,
    "journal": "Philosophical Magazine",
    "volume": "2",
    "issue": "11",
    "pages": "559-572",
    "doi": "10.1080/14786440109462720",
}


class DiscoveryMethod(Protocol):
    """Protocol implemented by discovery algorithms."""

    method_id: str
    name: str

    def fit(
        self,
        vectors: np.ndarray,
        parameters: Mapping[str, object],
    ) -> "DiscoveryMethod":
        """Fit method state when the algorithm supports later transforms."""
        raise NotImplementedError

    def fit_transform(
        self,
        vectors: np.ndarray,
        parameters: Mapping[str, object],
        progress_callback: ProgressCallback | None = None,
    ) -> np.ndarray | ProjectionExecutionResult:
        """Project vectors into at least two coordinate dimensions."""

    def transform(self, vectors: np.ndarray) -> np.ndarray:
        """Project new vectors with fitted method state when supported."""
        raise NotImplementedError


class DiscoveryMethodBase:
    """Base class for methods that only implement fit_transform."""

    def fit(
        self,
        vectors: np.ndarray,
        parameters: Mapping[str, object],
    ) -> "DiscoveryMethodBase":
        _ = vectors, parameters
        raise NotImplementedError(f"{type(self).__name__} does not support fit().")

    def transform(self, vectors: np.ndarray) -> np.ndarray:
        _ = vectors
        raise NotImplementedError(f"{type(self).__name__} does not support transform().")


ProjectionMethodDefinition = DiscoveryMethodDefinition
ProjectionMethod = DiscoveryMethod
ProjectionMethodBase = DiscoveryMethodBase
