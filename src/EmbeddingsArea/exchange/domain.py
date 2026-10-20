from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


ImportLinkStrategy = Literal["identifier", "row_order", "create_metadata"]


@dataclass(frozen=True)
class EmbeddingImportInterpretation:
    vector_key: str = ""
    identifier_key: str = ""
    vector_columns: tuple[str, ...] = ()
    identifier_path: Path | None = None
    identifier_file_key: str = ""
    sidecar_path: Path | None = None


@dataclass(frozen=True)
class EmbeddingImportCompanionProbe:
    path: Path
    format_label: str
    fingerprint: tuple[int, int]
    row_count_hint: int | None
    identifier_candidates: tuple[str, ...]
    default_identifier_key: str = ""


@dataclass(frozen=True)
class EmbeddingImportProbe:
    source_path: Path
    suffix: str
    format_label: str
    size_bytes: int
    source_fingerprint: tuple[int, int]
    row_count_hint: int | None
    vector_candidates: tuple[str, ...]
    identifier_candidates: tuple[str, ...]
    wide_vector_columns: tuple[str, ...] = ()
    default_vector_key: str = ""
    default_identifier_key: str = ""
    provenance: dict[str, object] = field(default_factory=dict)
    sidecar_path: Path | None = None
    sidecar_fingerprint: tuple[int, int] | None = None

    @property
    def default_interpretation(self) -> EmbeddingImportInterpretation:
        return EmbeddingImportInterpretation(
            vector_key=self.default_vector_key,
            identifier_key=self.default_identifier_key,
            vector_columns=(
                self.wide_vector_columns if not self.default_vector_key else ()
            ),
            sidecar_path=self.sidecar_path,
        )


@dataclass(frozen=True)
class EmbeddingImportSample:
    row_index: int
    identifier: str
    vector: tuple[float, ...]


@dataclass(frozen=True)
class EmbeddingImportInspection:
    probe: EmbeddingImportProbe
    interpretation: EmbeddingImportInterpretation
    staging_path: Path
    row_count: int
    dimensions: int
    dtype: str
    source_has_identifiers: bool
    samples: tuple[EmbeddingImportSample, ...]
    complete_metadata_columns: tuple[str, ...] = ()
    identifier_fingerprint: tuple[int, int] | None = None
    sidecar_fingerprint: tuple[int, int] | None = None


@dataclass(frozen=True)
class EmbeddingImportPlan:
    inspection: EmbeddingImportInspection
    strategy: ImportLinkStrategy
    metadata_column: str
    match_index_path: Path | None
    metadata_fingerprint: str
    matched_count: int
    unmatched_count: int
    unmatched_metadata_count: int
    selected_count: int
    allow_partial: bool
    can_import: bool
    message: str
