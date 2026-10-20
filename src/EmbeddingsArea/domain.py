from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from MetadataArea.domain import MetadataMergePreview


class MediaContentType(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    TEXT = "text"


class MediaSourceKind(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"
    METADATA_COLUMN = "metadata_column"


class MediaSourceStatus(str, Enum):
    READY = "ready"
    UNLINKED = "unlinked"
    UNMATCHED = "unlinked"
    STALE = "stale"
    MISSING = "missing"
    ERROR = "error"


class MediaAssetStatus(str, Enum):
    READY = "ready"
    MISSING = "missing"
    ERROR = "error"


class MediaLinkStatus(str, Enum):
    LINKED = "linked"
    CANDIDATE = "candidate"
    REJECTED = "rejected"


class MediaMatchStrategy(str, Enum):
    IDENTIFIER_EQUALS = "identifier_equals"
    FILENAME = "filename"
    FILENAME_STEM = "filename_stem"
    RELATIVE_PATH = "relative_path"
    METADATA_ROW_ID = "metadata_row_id"
    MANUAL = "manual"


class EmbeddingRunState(str, Enum):
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class ModelInstallState(str, Enum):
    INSTALLED = "installed"
    AVAILABLE = "available"
    BROKEN = "broken"


@dataclass(frozen=True)
class MediaSource:
    id: str
    label: str
    path: Path
    source_kind: MediaSourceKind
    content_type: MediaContentType
    recursive: bool = False
    status: MediaSourceStatus = MediaSourceStatus.UNLINKED
    created_at: str = ""
    updated_at: str = ""
    last_scanned_at: str | None = None
    scan_cursor: str = ""
    source_revision: int = 0
    error_message: str = ""


@dataclass(frozen=True)
class MediaAsset:
    id: str
    source_id: str
    source_key: str
    absolute_path: Path
    relative_path: str
    display_name: str
    content_type: MediaContentType
    fingerprint: str
    size_bytes: int | None
    mtime_ns: int | None
    status: MediaAssetStatus = MediaAssetStatus.READY
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class MediaLink:
    id: str
    media_asset_id: str
    source_id: str
    metadata_row_id: int
    metadata_column: str
    matched_value: str
    strategy: MediaMatchStrategy = MediaMatchStrategy.IDENTIFIER_EQUALS
    status: MediaLinkStatus = MediaLinkStatus.LINKED
    confidence: float = 1.0
    is_manual: bool = False
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class MediaSourceSummary:
    source: MediaSource
    asset_count: int
    linked_count: int
    unlinked_count: int
    missing_count: int
    error_count: int
    metadata_columns: tuple[str, ...] = ()

    @property
    def item_count(self) -> int:
        return self.asset_count

    @property
    def matched_count(self) -> int:
        return self.linked_count

    @property
    def metadata_column(self) -> str | None:
        return self.metadata_columns[0] if self.metadata_columns else None


@dataclass(frozen=True)
class MediaScanSummary:
    source_id: str
    discovered_count: int
    indexed_count: int
    missing_count: int
    error_message: str = ""


@dataclass(frozen=True)
class MediaLinkPreview:
    source_id: str
    metadata_column: str
    strategy: MediaMatchStrategy
    matched_count: int
    unmatched_asset_count: int
    unmatched_metadata_count: int
    missing_metadata_values: tuple[str, ...] = ()

    @property
    def unmatched_media_count(self) -> int:
        return self.unmatched_asset_count


@dataclass(frozen=True)
class MediaMetadataMergePreview:
    source_id: str
    columns: tuple[str, ...]
    row_count: int
    merge_preview: MetadataMergePreview | None = None

    @property
    def initializes_metadata(self) -> bool:
        return self.merge_preview is None


@dataclass(frozen=True)
class MediaMatchRule:
    source_id: str
    metadata_column: str
    strategy: MediaMatchStrategy = MediaMatchStrategy.IDENTIFIER_EQUALS


@dataclass(frozen=True)
class MediaLinkRequest:
    source_id: str
    source_field: str
    metadata_column: str
    comparison: str = "exact"
    ignore_case: bool = False
    replace_existing: bool = False
    create_missing: bool = False


@dataclass(frozen=True)
class MediaProgress:
    stage: str
    completed: int = 0
    total: int = 0


@dataclass(frozen=True)
class MediaLinkPlan:
    request: MediaLinkRequest
    source_revision: int
    metadata_revision: int
    matches: tuple[tuple[str, int, str], ...]
    new_rows: tuple[tuple[str, str], ...]
    preserved_count: int
    removed_count: int
    unmatched_count: int
    ambiguous_count: int
    blank_count: int
    examples: dict[str, tuple[tuple[str, int | None, str], ...]]
    unmatched_metadata_count: int = 0
    disappearing_count: int = 0


@dataclass(frozen=True)
class MediaOperationResult:
    summary: MediaSourceSummary
    catalog_changed: bool = False
    links_changed: bool = False
    metadata_changed: bool = False
    created_count: int = 0
    linked_count: int = 0
    cancelled: bool = False
    error_message: str = ""


@dataclass(frozen=True)
class SavedEmbeddingDataset:
    run_id: str
    name: str
    dimensions: int
    item_count: int
    artifact_path: str
    created_at: str = ""
    updated_at: str = ""
    execution_note: str = ""


@dataclass(frozen=True)
class ClusterResult:
    id: str
    dataset_run_id: str
    label: str
    method_name: str
    metadata_column: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class EmbeddingRun:
    id: str
    label: str
    state: EmbeddingRunState
    selected_model_id: str = ""
    selected_source_ids: tuple[str, ...] = ()
    progress: int = 0
    prepared_revision: int = 0
    prepared_input_count: int = 0
    processed_items: int = 0
    failed_items: int = 0
    last_chunk_at: str = ""
    created_at: str = ""
    updated_at: str = ""
    error_message: str = ""
    artifact_path: str = ""
    prepared_signature: str = ""


@dataclass(frozen=True)
class EmbeddingRunInputSnapshot:
    run_id: str
    ordinal: int
    media_item_id: str
    source_id: str
    source_key: str
    absolute_path: Path
    content_type: MediaContentType
    metadata_row_id: int | None
    size_bytes: int | None
    mtime_ns: int | None
    link_revision: int = 0
    components: tuple[EmbeddingRunInputSnapshot, ...] = ()

    @property
    def media_asset_id(self) -> str:
        return self.media_item_id


@dataclass(frozen=True)
class EmbeddingRunPreparation:
    run: EmbeddingRun
    input_count: int


@dataclass(frozen=True)
class EmbeddingRunInputBinding:
    input_id: str
    source_id: str


@dataclass(frozen=True)
class EmbeddingRunParameterValue:
    scope: str
    parameter_id: str
    value: str


@dataclass(frozen=True)
class EmbeddingRunConfiguration:
    run: EmbeddingRun
    input_bindings: tuple[EmbeddingRunInputBinding, ...] = ()
    parameter_values: tuple[EmbeddingRunParameterValue, ...] = ()


@dataclass(frozen=True)
class EmbeddingModelInputRequirement:
    id: str
    modality: MediaContentType
    label: str
    required: bool = True
    source_kind: str = "media_source"
    visible: bool = True
    preprocess: tuple[str, ...] = ()
    requirements: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class EmbeddingModelParameter:
    id: str
    scope: str
    label: str
    kind: str
    default: str = ""
    visible: bool = False
    choices: tuple[str, ...] = ()
    description: str = ""
    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True)
class EmbeddingModelDescriptor:
    id: str
    label: str
    compatible_content_types: tuple[MediaContentType, ...]
    version: str = ""
    model_family: str = ""
    description: str = ""
    runtime_adapter_id: str = ""
    runtime_input_names: tuple[str, ...] = ()
    runtime_output_name: str = ""
    backend: str = ""
    model_path: Path | None = None
    config_path: Path | None = None
    tokenizer_path: Path | None = None
    directory: Path | None = None
    group_id: str = ""
    group_label: str = ""
    group_order: int = 1000
    output_dimensions: int | None = None
    architecture: str = ""
    trained_on: str = ""
    feature_type: str = ""
    pooling: str = ""
    embedding_normalized: bool = False
    similarity_metric: str = ""
    input_requirements: tuple[EmbeddingModelInputRequirement, ...] = ()
    preprocess_parameters: tuple[EmbeddingModelParameter, ...] = ()
    postprocess_parameters: tuple[EmbeddingModelParameter, ...] = ()
    # Package-owned, versioned preprocessing contract.  This is deliberately
    # opaque to UI code; runtime adapters are its only executors.
    preprocessing_contract: dict[str, object] = field(default_factory=dict)
    package_schema_version: int = 0
    package_fingerprint: str = ""
    package_status: str = "ready"
    runtime_contract: dict[str, object] = field(default_factory=dict)
    shared_space_id: str = ""
    shared_space_revision: str = ""


@dataclass(frozen=True)
class ModelCatalogEntry:
    id: str
    label: str
    modality_labels: tuple[str, ...]
    backend: str
    description: str = ""
    dimensions: int | None = None
    size_bytes: int | None = None
    install_state: ModelInstallState = ModelInstallState.AVAILABLE
    is_installed: bool = False
    is_removable: bool = False


@dataclass(frozen=True)
class InstalledModel:
    descriptor: EmbeddingModelDescriptor
    size_bytes: int | None = None
    is_removable: bool = False
    health_label: str = "installed"


@dataclass(frozen=True)
class EmbeddingDataset:
    run_id: str = ""
    name: str = ""
    dimensions: int = 0
    item_count: int = 0
    artifact_path: str = ""
    model_id: str = ""
    source_labels: tuple[str, ...] = ()
    created_at: str = ""
    updated_at: str = ""
    clusters: tuple[ClusterResult, ...] = ()
    execution_note: str = ""


MediaItem = MediaAsset
MediaItemStatus = MediaAssetStatus
MediaMatchSummary = MediaLinkPreview
EmbeddingRunInput = EmbeddingRunInputSnapshot
