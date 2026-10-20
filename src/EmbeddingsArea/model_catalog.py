from __future__ import annotations

import json
import re
import shutil
import ssl
import tempfile
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import urlopen
from typing import Any

from eidora_version import __version__ as EIDORA_VERSION

from EmbeddingsArea.domain import (
    EmbeddingModelDescriptor,
    EmbeddingModelInputRequirement,
    EmbeddingModelParameter,
    InstalledModel,
    MediaContentType,
    ModelCatalogEntry,
    ModelInstallState,
)
from EmbeddingsArea.model_package import (
    ModelPackageError,
    parse_model_package,
    resolve_package_path,
    validate_onnx_graph,
    validate_package_files,
)


HUGGINGFACE_ACCOUNT = "EIDORA"
MODEL_MANIFEST = "config.yaml"
# Temporary schema-repair escape hatch; strict installs are the default.
# Execution still uses the normal package contract and validation.
TEMPORARILY_ALLOW_UNVALIDATED_MODEL_DOWNLOADS = False
MODEL_DOWNLOAD_RECEIPT = ".eidora-download.json"
HUGGINGFACE_COLLECTION_TIMEOUT_SECONDS = 10.0
HUGGINGFACE_COLLECTION_URLS: tuple[str, ...] = (
    "https://huggingface.co/collections/EIDORA/image-models-6a50d43e8a3fe1f677f736b5",
    "https://huggingface.co/collections/EIDORA/text-models-6a50f7a1b04b25a4ef6e7e8f",
    "https://huggingface.co/collections/EIDORA/video-models-6a50f7af58a0caf8ad2496b5",
    "https://huggingface.co/collections/EIDORA/audio-models-6a50f7b86c93ce05a7cfc36e",
    "https://huggingface.co/collections/EIDORA/multimodal-models-6a50f7c8ed1d5015cd62be32",
)


@lru_cache(maxsize=1)
def _huggingface_ssl_context() -> ssl.SSLContext:
    """Use certifi because framework Python installs may lack macOS CA roots."""
    import certifi

    return ssl.create_default_context(cafile=certifi.where())


def _open_huggingface_url(url: str, *, timeout: float):
    try:
        return urlopen(url, timeout=timeout, context=_huggingface_ssl_context())  # noqa: S310
    except TypeError as exc:
        # Keep simple urlopen test doubles and older compatible callables usable.
        if "context" not in str(exc):
            raise
        return urlopen(url, timeout=timeout)  # noqa: S310


DEFAULT_LOCAL_MODELS: tuple[EmbeddingModelDescriptor, ...] = (
    EmbeddingModelDescriptor(
        id="local-image-placeholder",
        label="Image Embeddings",
        compatible_content_types=(MediaContentType.IMAGE,),
        description="Deterministic test adapter for image workflow development.",
        runtime_adapter_id="fake",
        backend="fake",
        group_id="image",
        group_label="Images",
        input_requirements=(
            EmbeddingModelInputRequirement(id="image", modality=MediaContentType.IMAGE, label="Images"),
        ),
    ),
    EmbeddingModelDescriptor(
        id="local-text-placeholder",
        label="Text Embeddings",
        compatible_content_types=(MediaContentType.TEXT,),
        description="Deterministic test adapter for text workflow development.",
        runtime_adapter_id="fake",
        backend="fake",
        group_id="text",
        group_label="Text",
        input_requirements=(
            EmbeddingModelInputRequirement(id="text", modality=MediaContentType.TEXT, label="Text"),
        ),
    ),
)

# Development-only adapters remain available to tests and internal workflows,
# but must never be presented as user-selectable installed models.
DEVELOPMENT_PLACEHOLDER_MODEL_IDS = frozenset(
    {"local-image-placeholder", "local-text-placeholder"}
)


DEFAULT_DISCOVERABLE_MODELS: tuple[ModelCatalogEntry, ...] = ()


def models_share_embedding_space(left: EmbeddingModelDescriptor, right: EmbeddingModelDescriptor) -> bool:
    """Return true only for explicitly compatible independent modality encoders."""
    return bool(left.shared_space_id) and (
        left.shared_space_id == right.shared_space_id
        and left.shared_space_revision == right.shared_space_revision
        and left.output_dimensions == right.output_dimensions
        and left.embedding_normalized == right.embedding_normalized
        and left.similarity_metric == right.similarity_metric
    )


@dataclass(frozen=True)
class DiscoverableModelGroup:
    id: str
    label: str
    source_url: str
    models: tuple[ModelCatalogEntry, ...]


class ModelInstallCancelled(RuntimeError):
    """Raised when a user cancels a Model Zoo install."""


class ModelPackageValidationError(RuntimeError):
    """A manifest is incompatible before any model artifacts are downloaded."""


class ModelCatalog:
    """Display catalog for installed and discoverable embedding models."""

    def __init__(
        self,
        roots: tuple[Path, ...] | None = None,
        fallback_models: tuple[EmbeddingModelDescriptor, ...] = DEFAULT_LOCAL_MODELS,
        discoverable_models: tuple[ModelCatalogEntry, ...] = DEFAULT_DISCOVERABLE_MODELS,
    ) -> None:
        self._roots = roots
        self._fallback_models = fallback_models
        self._discoverable_models = discoverable_models

    def list_local_models(self) -> list[EmbeddingModelDescriptor]:
        discovered = self._discover_local_models()
        models = [*discovered, *self._fallback_models]
        return sorted(
            _unique_by_id(models),
            key=lambda model: (model.group_order, model.group_label.casefold(), model.label.casefold()),
        )

    def list_installed_models(self) -> list[InstalledModel]:
        return [
            InstalledModel(
                descriptor=model,
                size_bytes=_model_size_bytes(model),
                is_removable=_is_removable_model(model),
                health_label=(
                    "requires repackaging"
                    if model.package_status == "requires_repackaging"
                    else "installed" if model.model_path or model.runtime_adapter_id == "fake" else "broken"
                ),
            )
            for model in self.list_local_models()
        ]

    def list_discoverable_models(self) -> list[ModelCatalogEntry]:
        installed_ids = {
            _model_id_key(model.id) for model in self.list_local_models()
            if model.package_status == "ready"
        }
        return [
            ModelCatalogEntry(
                id=entry.id,
                label=entry.label,
                modality_labels=entry.modality_labels,
                backend=entry.backend,
                description=entry.description,
                dimensions=entry.dimensions,
                size_bytes=entry.size_bytes,
                install_state=ModelInstallState.INSTALLED if _model_id_key(entry.id) in installed_ids else entry.install_state,
                is_installed=_model_id_key(entry.id) in installed_ids,
            )
            for entry in sorted(self._discoverable_models, key=lambda item: item.label.casefold())
        ]

    def list_discoverable_model_groups(self, *, force_refresh: bool = False) -> list[DiscoverableModelGroup]:
        if force_refresh:
            _fetch_huggingface_account_groups.cache_clear()
        installed_ids = {
            _model_id_key(model.id) for model in self.list_local_models()
            if model.package_status == "ready"
        }
        groups = _fetch_huggingface_account_groups(
            HUGGINGFACE_ACCOUNT,
            HUGGINGFACE_COLLECTION_URLS,
            frozenset(installed_ids),
        )
        if groups:
            return groups
        # Keep collection headings authoritative. A failed request is retried
        # on the next explicit browse or refresh instead of synthesizing a
        # category from model metadata.
        _fetch_huggingface_account_groups.cache_clear()
        return []

    def list_compatible_models(
        self,
        content_types: set[MediaContentType],
    ) -> list[EmbeddingModelDescriptor]:
        if not content_types:
            return self.list_local_models()
        return [
            model
            for model in self.list_local_models()
            if content_types.issubset(set(model.compatible_content_types))
        ]

    def get_model(self, model_id: str) -> EmbeddingModelDescriptor | None:
        for model in self.list_local_models():
            if model.id == model_id:
                return model
        return None

    def read_local_model_readme(self, model_id: str) -> str:
        model = self.get_model(model_id)
        if model is None:
            raise ValueError(f"Unknown installed model: {model_id}")
        if model.directory is None:
            raise ValueError(f"{model.label} does not have a local model package directory.")
        return _read_package_readme(model.directory, model.label)

    def fetch_discoverable_model_readme(self, entry: ModelCatalogEntry) -> str:
        return _read_huggingface_text_file(entry.id, "README.md")

    def fetch_discoverable_model_size(self, entry: ModelCatalogEntry) -> int | None:
        return _fetch_model_package_size_bytes(entry.id)

    def install_discoverable_model(
        self,
        entry: ModelCatalogEntry,
        *,
        cancel_event: threading.Event | None = None,
        progress_callback: Any = None,
    ) -> EmbeddingModelDescriptor:
        repo_id = entry.id.strip()
        _validate_eidora_repo_id(repo_id)
        _raise_if_cancelled(cancel_event)
        target_root = _app_local_models_root()
        target_root.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_model_dir_name(repo_id)
        target = target_root / safe_name
        staging_parent = target_root / ".downloads"
        staging_parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f"{safe_name}.", dir=staging_parent))
        backup = target_root / f".{safe_name}.previous"
        try:
            _report_progress(progress_callback, 5)
            _download_huggingface_model(repo_id, staging, cancel_event=cancel_event, progress_callback=progress_callback)
            _raise_if_cancelled(cancel_event)
            manifest = staging / MODEL_MANIFEST
            if TEMPORARILY_ALLOW_UNVALIDATED_MODEL_DOWNLOADS:
                (staging / MODEL_DOWNLOAD_RECEIPT).write_text(
                    json.dumps({"id": repo_id, "label": entry.label, "modalities": entry.modality_labels}),
                    encoding="utf-8",
                )
            else:
                _validate_installed_package(staging, manifest)
            _report_progress(progress_callback, 90)
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            if target.exists():
                target.rename(backup)
            staging.rename(target)
            if TEMPORARILY_ALLOW_UNVALIDATED_MODEL_DOWNLOADS:
                model = _descriptor_from_download(target)
            else:
                model = _descriptor_from_config(target / MODEL_MANIFEST)
            if model is None:
                raise RuntimeError("Installed model package could not be read by EIDORA.")
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            _fetch_huggingface_account_groups.cache_clear()
            _report_progress(progress_callback, 100)
            return model
        except Exception:
            if backup.exists():
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                backup.rename(target)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def _discover_local_models(self) -> list[EmbeddingModelDescriptor]:
        models: list[EmbeddingModelDescriptor] = []
        for root in self._model_roots():
            if not root.exists():
                continue
            download_directories = {
                path.parent for path in root.rglob(MODEL_DOWNLOAD_RECEIPT)
                if ".downloads" not in path.relative_to(root).parts
            }
            for directory in sorted(download_directories):
                model = _descriptor_from_download(directory)
                if model is not None:
                    models.append(model)
            manifest_paths = [
                *sorted(root.rglob(MODEL_MANIFEST)),
            ]
            for config_path in manifest_paths:
                if config_path.parent in download_directories or ".downloads" in config_path.relative_to(root).parts:
                    continue
                model = _descriptor_from_config(config_path)
                if model is not None:
                    models.append(model)
        return models

    def _model_roots(self) -> tuple[Path, ...]:
        if self._roots is not None:
            return tuple(root.expanduser() for root in self._roots)
        return (_app_local_models_root(),)


def _descriptor_from_download(directory: Path) -> EmbeddingModelDescriptor | None:
    """Keep raw downloads visible; use the normal descriptor once repaired."""
    try:
        model = _descriptor_from_config(directory / MODEL_MANIFEST)
    except (ValueError, TypeError, KeyError, AttributeError):
        model = None
    if model is not None and model.package_status == "ready":
        return model
    try:
        receipt = json.loads((directory / MODEL_DOWNLOAD_RECEIPT).read_text(encoding="utf-8"))
        model_id, label = receipt["id"], receipt["label"]
        modalities = tuple(MediaContentType(value) for value in receipt["modalities"]
                           if value in {item.value for item in MediaContentType})
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return EmbeddingModelDescriptor(
        id=model_id,
        label=label,
        compatible_content_types=modalities,
        backend="onnx",
        group_id=modalities[0].value if modalities else "",
        group_label=_group_label(modalities[0].value) if modalities else "Models",
        directory=directory,
        config_path=directory / MODEL_MANIFEST,
        package_status="requires_repackaging",
    )


def _descriptor_from_config(config_path: Path) -> EmbeddingModelDescriptor | None:
    payload = _load_yaml(config_path)
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") == 3:
        try:
            spec = parse_model_package(payload)
        except ModelPackageError:
            return None
        directory = config_path.parent
        metadata = spec.metadata or {}
        group = metadata.get("group") if isinstance(metadata.get("group"), dict) else {}
        from EmbeddingsArea.preparation_contract import model_parameters
        modalities = tuple(MediaContentType(item.modality) for item in spec.components or (spec,))
        tokenizer_path = None
        if spec.modality == "text":
            tokenizer_path = resolve_package_path(directory, str(spec.preparation["tokenizer_path"]))
        return EmbeddingModelDescriptor(
            id=spec.id,
            label=spec.name,
            compatible_content_types=modalities,
            version=spec.version,
            description=str(metadata.get("summary") or ""),
            runtime_adapter_id="onnx",
            runtime_input_names=tuple(item.name for item in spec.inputs),
            runtime_output_name=spec.output.name,
            tokenizer_path=tokenizer_path,
            backend="onnx",
            model_path=resolve_package_path(directory, spec.graph_path),
            config_path=config_path,
            directory=directory,
            group_id=str(group.get("id") or spec.modality),
            group_label=str(group.get("label") or _group_label(spec.modality)),
            group_order=_coerce_int(group.get("order"), 1000),
            output_dimensions=spec.dimensions,
            architecture=str(metadata.get("architecture") or ""),
            trained_on=str(metadata.get("trained_on") or ""),
            feature_type=str(metadata.get("feature_type") or "embedding"),
            pooling=str(metadata.get("pooling") or "inside_onnx"),
            embedding_normalized=spec.normalized,
            similarity_metric=spec.similarity,
            input_requirements=tuple(
                EmbeddingModelInputRequirement(
                    id=modality.value, modality=modality,
                    label=_group_label(modality.value), required=not bool(spec.components),
                ) for modality in modalities
            ),
            preprocess_parameters=model_parameters(spec.contract()),
            package_schema_version=3,
            package_fingerprint=spec.fingerprint(),
            package_status="ready",
            runtime_contract=spec.contract(),
            shared_space_id=spec.shared_space_id,
            shared_space_revision=spec.shared_space_revision,
        )
    directory = config_path.parent
    model_path = _model_path_from_config(directory, payload)
    if model_path is None or not model_path.exists():
        return None

    model_id = str(payload.get("id") or directory.name).strip()
    label = str(payload.get("name") or model_id).strip()
    backend = str(payload.get("backend") or "").strip() or model_path.suffix.lstrip(".") or "model"
    content_types = _content_types_from_config(payload)
    runtime_adapter = _runtime_adapter_from_config(payload, content_types, backend)
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    display = payload.get("display") if isinstance(payload.get("display"), dict) else {}
    group = display.get("group") if isinstance(display.get("group"), dict) else {}
    primary = content_types[0].value if content_types else "image"
    embedding = payload.get("embedding") if isinstance(payload.get("embedding"), dict) else {}
    input_requirements = _input_requirements_from_config(payload)
    parameters = payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {}
    preprocess_parameters = (
        _parameters_from_preprocess_payload(payload.get("preprocess"))
        + _parameters_from_block(parameters.get("preprocess"), scope="preprocess")
        + _input_preprocess_parameters(payload)
    )
    postprocess_parameters = (
        _parameters_from_block(payload.get("postprocess"), scope="postprocess")
        + _parameters_from_block(parameters.get("postprocess"), scope="postprocess")
    )
    return EmbeddingModelDescriptor(
        id=model_id,
        label=label,
        compatible_content_types=content_types,
        version=str(payload.get("version") or "").strip(),
        model_family=str(payload.get("model_family") or "").strip(),
        description=str(display.get("summary") or "").strip(),
        runtime_adapter_id=runtime_adapter,
        runtime_input_names=tuple(str(name).strip() for name in _string_tuple(runtime.get("input_names"))),
        runtime_output_name=str(runtime.get("output_name") or "").strip(),
        tokenizer_path=_tokenizer_dir_from_config(directory, payload),
        backend=backend,
        model_path=model_path,
        config_path=config_path,
        directory=directory,
        group_id=str(group.get("id") or primary).strip(),
        group_label=str(group.get("label") or _group_label(primary)).strip(),
        group_order=_coerce_int(group.get("order"), 1000),
        output_dimensions=_coerce_optional_int(embedding.get("dim") or embedding.get("dimensions")),
        architecture=str(payload.get("architecture") or embedding.get("architecture") or "").strip(),
        trained_on=str(payload.get("trained_on") or payload.get("training_data") or "").strip(),
        feature_type=str(payload.get("feature_type") or embedding.get("feature_type") or "").strip(),
        pooling=str(embedding.get("pooling") or payload.get("pooling") or "").strip(),
        embedding_normalized=_coerce_bool(embedding.get("normalized"), False),
        similarity_metric=str(embedding.get("similarity") or "").strip(),
        input_requirements=input_requirements,
        preprocess_parameters=preprocess_parameters,
        postprocess_parameters=postprocess_parameters,
        preprocessing_contract=_preprocessing_contract_from_config(payload),
        package_status="requires_repackaging",
    )


def _load_yaml(path: Path) -> Any:
    try:
        import yaml
    except Exception:
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _preprocessing_contract_from_config(payload: dict[str, Any]) -> dict[str, object]:
    """Return the normalized package contract, retaining legacy packages."""
    raw = _raw_preprocessing_contract(payload)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return {}
    return dict(raw)


def _raw_preprocessing_contract(payload: dict[str, Any]) -> Any:
    """`preprocessing` is canonical; accept versioned `preprocess` during migration."""
    raw = payload.get("preprocessing")
    if raw is not None:
        return raw
    legacy_name = payload.get("preprocess")
    if isinstance(legacy_name, dict) and "version" in legacy_name:
        return legacy_name
    return None


def _model_path_from_config(directory: Path, payload: dict[str, Any]) -> Path | None:
    runtime = payload.get("runtime")
    if isinstance(runtime, dict) and runtime.get("graph"):
        return _resolve_package_path(directory, runtime["graph"])
    if isinstance(runtime, dict) and isinstance(runtime.get("components"), dict):
        for component in runtime["components"].values():
            if isinstance(component, dict) and isinstance(component.get("runtime"), dict) and component["runtime"].get("graph"):
                return _resolve_package_path(directory, component["runtime"]["graph"])
    if isinstance(runtime, dict) and runtime.get("model_path"):
        return _resolve_package_path(directory, runtime["model_path"])
    artifact = payload.get("artifact")
    if isinstance(artifact, dict):
        raw = artifact.get("path") or artifact.get("model_path")
        if raw:
            return _resolve_package_path(directory, raw)
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, dict):
        primary = artifacts.get("primary")
        if isinstance(primary, dict):
            raw = primary.get("path") or primary.get("model_path") or primary.get("primary_path")
            if raw:
                return _resolve_package_path(directory, raw)
        raw = artifacts.get("path") or artifacts.get("model_path") or artifacts.get("primary_path")
        if raw:
            return _resolve_package_path(directory, raw)
    if payload.get("model_path"):
        return _resolve_package_path(directory, payload["model_path"])
    candidates = sorted(
        path
        for path in directory.iterdir()
        if path.suffix.lower() in {".onnx", ".pt", ".pth", ".bin", ".safetensors"}
    )
    return candidates[0] if candidates else None


def _resolve_package_path(directory: Path, raw: Any) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    candidate = directory / text
    try:
        resolved = candidate.resolve()
        root = directory.resolve()
    except OSError:
        return None
    if resolved == root or root in resolved.parents:
        return resolved
    return None


def _content_types_from_config(payload: dict[str, Any]) -> tuple[MediaContentType, ...]:
    modalities: list[str] = []
    if payload.get("modality") == "multimodal":
        modalities.extend((payload.get("runtime") or {}).get("components", {}))
    elif payload.get("modality"):
        modalities.append(str(payload["modality"]))
    parameter_inputs = _parameter_inputs_from_config(payload)
    for item in parameter_inputs:
        modalities.append(str(_default_from_value(item.get("input_type") or item.get("modality") or item.get("type")) or ""))
    inputs = payload.get("inputs")
    if isinstance(inputs, list):
        for item in inputs:
            if isinstance(item, dict):
                modalities.append(str(item.get("modality") or item.get("content_type") or ""))
    parameters = payload.get("parameters")
    if isinstance(parameters, dict):
        modalities.append(str(_default_from_value(parameters.get("input_type")) or ""))
    runtime = payload.get("runtime")
    if isinstance(runtime, dict):
        modalities.append(str(runtime.get("primary_modality") or runtime.get("input_type") or ""))

    content_types: list[MediaContentType] = []
    for raw in modalities or ["image"]:
        normalized = raw.strip().lower()
        if normalized in {"image", "images"} and MediaContentType.IMAGE not in content_types:
            content_types.append(MediaContentType.IMAGE)
        if normalized in {"text", "texts"} and MediaContentType.TEXT not in content_types:
            content_types.append(MediaContentType.TEXT)
        if normalized in {"audio", "audios"} and MediaContentType.AUDIO not in content_types:
            content_types.append(MediaContentType.AUDIO)
        if normalized in {"video", "videos"} and MediaContentType.VIDEO not in content_types:
            content_types.append(MediaContentType.VIDEO)
    return tuple(content_types or (MediaContentType.IMAGE,))


def _input_requirements_from_config(payload: dict[str, Any]) -> tuple[EmbeddingModelInputRequirement, ...]:
    requirements: list[EmbeddingModelInputRequirement] = []
    for item in _parameter_inputs_from_config(payload):
        modality = _content_type(_default_from_value(item.get("input_type") or item.get("modality") or item.get("type")))
        if modality is None:
            continue
        requirement_id = _sanitize_identifier(item.get("id") or item.get("key") or f"input_{modality.value}", "input")
        requirements.append(
            EmbeddingModelInputRequirement(
                id=requirement_id,
                modality=modality,
                label=str(item.get("label") or item.get("name") or _group_label(modality.value)).strip(),
                required=_coerce_bool(item.get("required"), True),
                source_kind=str(item.get("source_kind") or item.get("source") or "media_source").strip()
                or "media_source",
                visible=_coerce_bool(item.get("visible"), True),
                preprocess=tuple(
                    _sanitize_identifier(value, "preprocess")
                    for value in _string_tuple(item.get("preprocess"))
                ),
                requirements=_requirements_from_input(item),
            )
        )

    if requirements:
        return tuple(_dedupe_requirements(requirements))

    inputs = payload.get("inputs")
    if isinstance(inputs, list):
        for item in inputs:
            if not isinstance(item, dict):
                continue
            modality = _content_type(item.get("modality") or item.get("type") or item.get("kind"))
            if modality is None:
                continue
            requirement_id = _sanitize_identifier(item.get("id") or item.get("key") or modality.value, "input")
            preprocess = tuple(
                _sanitize_identifier(key, "preprocess")
                for key, _value in _iter_parameter_payloads(item.get("preprocess"))
            )
            requirements.append(
                EmbeddingModelInputRequirement(
                    id=requirement_id,
                    modality=modality,
                    label=str(item.get("label") or item.get("name") or _group_label(modality.value)).strip(),
                    required=_coerce_bool(item.get("required"), True),
                    source_kind=str(item.get("source_kind") or item.get("source") or "media_source").strip()
                    or "media_source",
                    visible=_coerce_bool(item.get("visible"), True),
                    preprocess=preprocess,
                    requirements=_requirements_from_input(item),
                )
            )

    parameters = payload.get("parameters")
    if not requirements and isinstance(parameters, dict):
        input_type = _content_type(_default_from_value(parameters.get("input_type")))
        if input_type is not None:
            requirements.append(
                EmbeddingModelInputRequirement(
                    id=input_type.value,
                    modality=input_type,
                    label=_group_label(input_type.value),
                )
            )

    if requirements:
        return tuple(_dedupe_requirements(requirements))
    content_types = _content_types_from_config(payload)
    return tuple(
        EmbeddingModelInputRequirement(id=content_type.value, modality=content_type, label=_group_label(content_type.value))
        for content_type in content_types
    )


def _parameters_from_block(block: Any, scope: str) -> tuple[EmbeddingModelParameter, ...]:
    parameters: list[EmbeddingModelParameter] = []
    for key, value in _iter_parameter_payloads(block):
        parameter_id = _sanitize_identifier(key, f"{scope}_parameter")
        label = str(key).replace("_", " ")
        kind = "choice"
        default = _default_from_value(value)
        visible = False
        choices = _choices_from_value(value)
        description = ""
        minimum = None
        maximum = None
        if isinstance(value, dict):
            label = str(value.get("label") or value.get("name") or label).strip() or label
            kind = str(value.get("kind") or value.get("type") or kind).strip().lower() or kind
            visible = _coerce_bool(value.get("visible"), False)
            description = str(value.get("description") or value.get("info") or "").strip()
            minimum = _coerce_optional_float(value.get("min"))
            maximum = _coerce_optional_float(value.get("max"))
        if parameter_id in {"resize_mode", "image_resize", "resize"} and not choices:
            choices = ("distort", "crop_center", "add_padding")
            visible = True
            label = "Image preprocessing" if label == parameter_id.replace("_", " ") else label
        parameters.append(
            EmbeddingModelParameter(
                id=parameter_id,
                scope=scope,
                label=label,
                kind=kind,
                default="" if default is None else str(default),
                visible=visible,
                choices=choices,
                description=description,
                minimum=minimum,
                maximum=maximum,
            )
        )
    return tuple(parameters)


def _parameters_from_preprocess_payload(block: Any) -> tuple[EmbeddingModelParameter, ...]:
    if not isinstance(block, dict):
        return _parameters_from_block(block, scope="preprocess")
    modality_keys = {"image", "text", "video", "audio"}
    nested: list[EmbeddingModelParameter] = []
    for key, value in block.items():
        if str(key).strip().lower() not in modality_keys or not isinstance(value, dict):
            continue
        nested.extend(_parameters_from_block(value, scope="preprocess"))
    if nested:
        return tuple(_dedupe_parameters(nested))
    return _parameters_from_block(block, scope="preprocess")


def _input_preprocess_parameters(payload: dict[str, Any]) -> tuple[EmbeddingModelParameter, ...]:
    parameters: list[EmbeddingModelParameter] = []
    inputs = payload.get("inputs")
    if not isinstance(inputs, list):
        return ()
    for item in inputs:
        if not isinstance(item, dict):
            continue
        parameters.extend(_parameters_from_block(item.get("preprocess"), scope="preprocess"))
    return tuple(_dedupe_parameters(parameters))


def _parameter_inputs_from_config(payload: dict[str, Any]) -> list[dict[str, Any]]:
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        return []
    inputs = parameters.get("inputs")
    if isinstance(inputs, list):
        return [item for item in inputs if isinstance(item, dict)]
    return []


def _requirements_from_input(item: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    requirements = item.get("requirements")
    if not isinstance(requirements, dict):
        requirements = {
            key: value
            for key, value in item.items()
            if key
            not in {
                "id",
                "key",
                "label",
                "name",
                "input_type",
                "modality",
                "type",
                "kind",
                "required",
                "source_kind",
                "source",
                "visible",
                "preprocess",
                "info",
                "description",
            }
        }
    rows: list[tuple[str, str]] = []
    for key, value in requirements.items():
        label = str(key).replace("_", " ").strip()
        text = _requirement_value_text(value)
        if label and text:
            rows.append((label, text))
    return tuple(rows)


def _requirement_value_text(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return " - ".join(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, dict):
        return ", ".join(
            f"{str(key).replace('_', ' ')}: {_requirement_value_text(item)}"
            for key, item in value.items()
            if _requirement_value_text(item)
        )
    return str(value).strip()


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if value:
        return (str(value).strip(),)
    return ()


def _runtime_adapter_from_config(
    payload: dict[str, Any],
    content_types: tuple[MediaContentType, ...],
    backend: str,
) -> str:
    runtime = payload.get("runtime")
    if isinstance(runtime, dict):
        adapter = runtime.get("adapter") or runtime.get("method")
        if adapter:
            return str(adapter).strip()
    if backend == "fake":
        return "fake"
    if MediaContentType.TEXT in content_types and MediaContentType.IMAGE not in content_types:
        return "onnx_text"
    return "onnx_image"


def _unique_by_id(models: list[EmbeddingModelDescriptor]) -> list[EmbeddingModelDescriptor]:
    unique: dict[str, EmbeddingModelDescriptor] = {}
    for model in models:
        unique[model.id] = model
    return list(unique.values())


def _group_label(value: str) -> str:
    return {"image": "Images", "text": "Text"}.get(value, value.replace("_", " ").title())


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _coerce_optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _coerce_optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    raw = str(value).strip().lower()
    if raw in {"true", "1", "yes", "required", "show"}:
        return True
    if raw in {"false", "0", "no", "optional", "hide"}:
        return False
    return default


def _content_type(value: Any) -> MediaContentType | None:
    raw = str(value or "").strip().lower()
    if raw in {"image", "images"}:
        return MediaContentType.IMAGE
    if raw in {"text", "texts"}:
        return MediaContentType.TEXT
    if raw in {"audio", "audios"}:
        return MediaContentType.AUDIO
    if raw in {"video", "videos"}:
        return MediaContentType.VIDEO
    return None


def _default_from_value(value: Any) -> Any:
    if isinstance(value, dict):
        for key in ("default", "value", "selected", "enabled", "modality", "type", "kind"):
            if key in value:
                return value.get(key)
        return None
    return value


def _choices_from_value(value: Any) -> tuple[str, ...]:
    if isinstance(value, dict):
        value = value.get("choices") or value.get("options") or ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _iter_parameter_payloads(block: Any):
    if isinstance(block, dict):
        for key, value in block.items():
            yield str(key), value
        return
    if isinstance(block, list):
        for index, value in enumerate(block, start=1):
            if isinstance(value, dict):
                key = str(value.get("id") or value.get("key") or value.get("name") or "").strip() or f"param_{index}"
                yield key, value


def _sanitize_identifier(value: Any, fallback: str) -> str:
    raw = "".join(ch if ch.isalnum() else "_" for ch in str(value or "").strip().lower()).strip("_")
    while "__" in raw:
        raw = raw.replace("__", "_")
    return raw or fallback


def _dedupe_requirements(
    requirements: list[EmbeddingModelInputRequirement],
) -> list[EmbeddingModelInputRequirement]:
    deduped: list[EmbeddingModelInputRequirement] = []
    seen: set[str] = set()
    for requirement in requirements:
        key = f"{requirement.id}:{requirement.modality.value}:{requirement.required}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(requirement)
    return deduped


def _dedupe_parameters(parameters: list[EmbeddingModelParameter]) -> list[EmbeddingModelParameter]:
    deduped: list[EmbeddingModelParameter] = []
    seen: set[str] = set()
    for parameter in parameters:
        key = f"{parameter.scope}:{parameter.id}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(parameter)
    return deduped


def _model_size_bytes(model: EmbeddingModelDescriptor) -> int | None:
    if model.directory is not None and model.directory.exists():
        total = 0
        for path in model.directory.rglob("*"):
            if not path.is_file():
                continue
            try:
                total += int(path.stat().st_size)
            except OSError:
                continue
        return total or None
    if model.model_path is not None and model.model_path.exists():
        try:
            return int(model.model_path.stat().st_size)
        except OSError:
            return None
    return None


def _is_removable_model(model: EmbeddingModelDescriptor) -> bool:
    if model.directory is None:
        return False
    try:
        resolved = model.directory.resolve()
        roots = (_app_local_models_root().resolve(),)
    except OSError:
        return False
    return any(resolved == root or root in resolved.parents for root in roots)


def _app_local_models_root() -> Path:
    try:
        from PySide6 import QtCore

        app_data = QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.StandardLocation.AppDataLocation
        )
    except Exception:
        app_data = ""
    root = Path(app_data) if app_data else Path.home() / ".eidora"
    return root / "models"


def _validate_eidora_repo_id(repo_id: str) -> None:
    owner, separator, name = repo_id.partition("/")
    if separator != "/" or owner.casefold() != HUGGINGFACE_ACCOUNT.casefold() or not name.strip():
        raise ValueError("Only public EIDORA Hugging Face model packages can be installed from the Model Zoo.")


def _safe_model_dir_name(repo_id: str) -> str:
    name = repo_id.rsplit("/", 1)[-1].strip()
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip(".-")
    if not safe:
        raise ValueError(f"Model repository id cannot be used as a local folder name: {repo_id}")
    return safe


def _model_id_key(model_id: str) -> str:
    """Hugging Face repository ids are case-insensitive for install state."""
    return str(model_id or "").strip().casefold()


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ModelInstallCancelled("Model install cancelled.")


def _report_progress(progress_callback: Any, value: int) -> None:
    if callable(progress_callback):
        progress_callback(max(0, min(100, int(value))))


def _download_huggingface_model(
    repo_id: str,
    target: Path,
    *,
    cancel_event: threading.Event | None = None,
    progress_callback: Any = None,
) -> None:
    _raise_if_cancelled(cancel_event)
    if TEMPORARILY_ALLOW_UNVALIDATED_MODEL_DOWNLOADS:
        _download_unvalidated_model(repo_id, target, cancel_event=cancel_event, progress_callback=progress_callback)
        return
    try:
        _download_huggingface_model_via_http(
            repo_id,
            target,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )
        return
    except (ModelInstallCancelled, ModelPackageValidationError):
        raise
    except Exception:
        pass
    try:
        from huggingface_hub import snapshot_download
    except Exception:
        raise
    try:
        # Fetch only the manifest first. A malformed or unsupported package
        # must never trigger a multi-hundred-megabyte artifact download.
        snapshot_download(
            repo_id=repo_id,
            repo_type="model",
            local_dir=str(target),
            allow_patterns=(MODEL_MANIFEST,),
        )
        _validate_download_manifest_preflight(target / MODEL_MANIFEST)
        manifest_payload = _load_yaml(target / MODEL_MANIFEST)
        declared_paths = _declared_package_asset_paths(manifest_payload if isinstance(manifest_payload, dict) else {})
        snapshot_download(
            repo_id=repo_id,
            repo_type="model",
            local_dir=str(target),
            allow_patterns=(MODEL_MANIFEST, "README.md", *declared_paths),
        )
    except TypeError:
        snapshot_download(
            repo_id=repo_id,
            repo_type="model",
            local_dir=str(target),
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to download {repo_id} from Hugging Face: {exc}") from exc
    _raise_if_cancelled(cancel_event)
    _report_progress(progress_callback, 80)


def _download_unvalidated_model(
    repo_id: str,
    target: Path,
    *,
    cancel_event: threading.Event | None = None,
    progress_callback: Any = None,
) -> None:
    """Temporary complete download, independent of config.yaml's schema."""
    from huggingface_hub import list_repo_files

    paths = list_repo_files(repo_id, repo_type="model")
    _raise_if_cancelled(cancel_event)
    if not paths:
        raise RuntimeError("Model repository contains no files.")
    for index, path in enumerate(paths):
        # Use the existing streaming downloader for cancellation and path checks.
        _download_huggingface_file(
            repo_id, path, target / path, required=True,
            cancel_event=cancel_event, progress_callback=progress_callback,
            progress_start=5 + round(75 * index / len(paths)),
            progress_end=5 + round(75 * (index + 1) / len(paths)),
        )


def _download_huggingface_model_via_http(
    repo_id: str,
    target: Path,
    *,
    cancel_event: threading.Event | None = None,
    progress_callback: Any = None,
) -> None:
    _download_huggingface_file(
        repo_id,
        MODEL_MANIFEST,
        target / MODEL_MANIFEST,
        required=True,
        cancel_event=cancel_event,
        progress_callback=progress_callback,
        progress_start=5,
        progress_end=15,
    )
    payload = _load_yaml(target / MODEL_MANIFEST)
    if not isinstance(payload, dict):
        raise ModelPackageValidationError("Downloaded model package is missing a readable EIDORA manifest.")
    _validate_download_manifest_preflight(target / MODEL_MANIFEST)
    required_paths = list(_declared_package_asset_paths(payload))
    total = max(1, len(required_paths))
    for index, path in enumerate(required_paths, start=1):
        start = 15 + round(60 * (index - 1) / total)
        end = 15 + round(60 * index / total)
        _download_huggingface_file(
            repo_id,
            path,
            target / path,
            required=True,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
            progress_start=start,
            progress_end=end,
        )
    optional_paths = (
        "README.md",
        "LICENSE",
        "LICENSE.txt",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "vocab.txt",
        "vocab.json",
        "merges.txt",
    )
    for path in optional_paths:
        _download_huggingface_file(
            repo_id,
            path,
            target / path,
            required=False,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
            progress_start=75,
            progress_end=80,
        )
    _report_progress(progress_callback, 80)


def _download_huggingface_file(
    repo_id: str,
    remote_path: str,
    destination: Path,
    *,
    required: bool,
    cancel_event: threading.Event | None = None,
    progress_callback: Any = None,
    progress_start: int = 0,
    progress_end: int = 100,
) -> None:
    _raise_if_cancelled(cancel_event)
    if not _is_relative_package_path(remote_path):
        if required:
            raise RuntimeError(f"Model package path escapes the package directory: {remote_path}")
        return
    safe_repo = quote(repo_id, safe="/")
    safe_path = quote(str(remote_path).strip().lstrip("/"), safe="/")
    url = f"https://huggingface.co/{safe_repo}/resolve/main/{safe_path}"
    try:
        with _open_huggingface_url(url, timeout=10.0) as response:
            total = _response_content_length(response)
            destination.parent.mkdir(parents=True, exist_ok=True)
            downloaded = 0
            with destination.open("wb") as handle:
                while True:
                    _raise_if_cancelled(cancel_event)
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if total > 0:
                        span = max(0, int(progress_end) - int(progress_start))
                        value = int(progress_start) + round(span * downloaded / total)
                        _report_progress(progress_callback, value)
    except Exception as exc:
        if destination.exists():
            destination.unlink(missing_ok=True)
        if isinstance(exc, ModelInstallCancelled):
            raise
        if required:
            raise RuntimeError(f"Failed to download required model file {remote_path}: {exc}") from exc
        return
    _raise_if_cancelled(cancel_event)
    _report_progress(progress_callback, progress_end)


def _read_package_readme(directory: Path, label: str) -> str:
    readme = directory / "README.md"
    if not readme.exists():
        raise FileNotFoundError(f"README.md is not available for {label}.")
    try:
        return readme.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return readme.read_text(encoding="utf-8", errors="replace")


def _read_huggingface_text_file(repo_id: str, remote_path: str) -> str:
    if not _is_relative_package_path(remote_path):
        raise RuntimeError(f"Model package path escapes the package directory: {remote_path}")
    safe_repo = quote(repo_id, safe="/")
    safe_path = quote(str(remote_path).strip().lstrip("/"), safe="/")
    url = f"https://huggingface.co/{safe_repo}/resolve/main/{safe_path}"
    try:
        with _open_huggingface_url(url, timeout=10.0) as response:
            payload = response.read()
    except Exception as exc:
        raise RuntimeError(f"README.md is not available for {repo_id}: {exc}") from exc
    return payload.decode("utf-8", errors="replace")


def _response_content_length(response: Any) -> int:
    headers = getattr(response, "headers", None)
    if headers is None:
        return 0
    try:
        return int(headers.get("Content-Length") or headers.get("content-length") or 0)
    except Exception:
        return 0


def _is_relative_package_path(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    path = Path(text)
    return not path.is_absolute() and ".." not in path.parts


def _declared_package_asset_paths(payload: dict[str, Any]) -> tuple[str, ...]:
    if payload.get("schema_version") == 3:
        try:
            return tuple(artifact.path for artifact in parse_model_package(payload).artifacts)
        except ModelPackageError:
            return ()
    paths: list[str] = []
    runtime = payload.get("runtime")
    if isinstance(runtime, dict):
        _append_path(paths, runtime.get("model_path"))
    artifact = payload.get("artifact")
    if isinstance(artifact, dict):
        _append_path(paths, artifact.get("path") or artifact.get("model_path"))
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, dict):
        primary = artifacts.get("primary")
        if isinstance(primary, dict):
            _append_path(paths, primary.get("path") or primary.get("model_path") or primary.get("primary_path"))
        _append_path(paths, artifacts.get("path") or artifacts.get("model_path") or artifacts.get("primary_path"))
        by_modality = artifacts.get("by_modality")
        if isinstance(by_modality, dict):
            for value in by_modality.values():
                if isinstance(value, dict):
                    _append_path(paths, value.get("path") or value.get("model_path"))
                else:
                    _append_path(paths, value)
        tokenizer_artifact = artifacts.get("tokenizer")
        if isinstance(tokenizer_artifact, dict):
            _append_tokenizer_paths(paths, tokenizer_artifact.get("path"))
    _append_path(paths, payload.get("model_path"))
    tokenizer = payload.get("tokenizer")
    if isinstance(tokenizer, dict):
        _append_tokenizer_paths(paths, tokenizer.get("path"))
    contract = _raw_preprocessing_contract(payload)
    if isinstance(contract, dict):
        assets = contract.get("assets")
        if isinstance(assets, dict):
            for value in assets.values():
                _append_path(paths, value)
        pipelines = contract.get("pipelines")
        if isinstance(pipelines, dict):
            for pipeline in pipelines.values():
                if not isinstance(pipeline, dict):
                    continue
                for step in pipeline.get("steps", []):
                    if not isinstance(step, dict):
                        continue
                    for key in ("asset", "path"):
                        value = step.get(key)
                        # Named assets are expanded above; literal paths are
                        # package files that must be downloaded and verified.
                        if isinstance(assets, dict) and str(value) in assets:
                            continue
                        _append_path(paths, value)
    return tuple(_dedupe_strings(paths))


def _append_path(paths: list[str], value: Any) -> None:
    text = str(value or "").strip().lstrip("/")
    if text:
        paths.append(text)


def _dedupe_hashes(values: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str]] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _append_tokenizer_paths(paths: list[str], value: Any) -> None:
    text = str(value or "").strip().strip("/")
    if not text:
        return
    if Path(text).suffix:
        paths.append(text)
        return
    for filename in ("tokenizer.json", "tokenizer_config.json", "special_tokens_map.json"):
        paths.append(f"{text}/{filename}")


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _validate_installed_package(directory: Path, manifest: Path) -> None:
    if not manifest.exists():
        raise RuntimeError("Installed model package is missing config.yaml.")
    payload = _load_yaml(manifest)
    if not isinstance(payload, dict):
        raise RuntimeError("Installed model package has an unreadable config.yaml.")
    if payload.get("schema_version") != 3:
        raise RuntimeError("Model package requires repackaging with schema_version: 3.")
    try:
        spec = parse_model_package(payload)
        _validate_package_compatibility({"compatibility": {"min_eidora_version": spec.minimum_eidora_version}})
        validate_package_files(directory, spec)
        validate_onnx_graph(directory, spec)
    except ModelPackageError as exc:
        raise RuntimeError(str(exc)) from exc
    if _descriptor_from_config(manifest) is None:
        raise RuntimeError("Installed model package could not be parsed by EIDORA.")
    return


def _validate_download_manifest_preflight(manifest: Path) -> None:
    payload = _load_yaml(manifest)
    if not isinstance(payload, dict):
        raise ModelPackageValidationError("Model package config.yaml is unreadable or is not a mapping.")
    try:
        _validate_download_manifest_payload(manifest.parent, payload, require_assets=False)
    except RuntimeError as exc:
        raise ModelPackageValidationError(f"Model package rejected before download: {exc}") from exc


def _validate_download_manifest_payload(directory: Path, payload: dict[str, Any], *, require_assets: bool) -> None:
    """Validate declarations available from config.yaml alone.

    Existence and checksums remain post-download checks; paths, contracts,
    compatibility, adapters, and declared preprocessing assets are preflighted.
    """
    if payload.get("schema_version") == 3:
        try:
            spec = parse_model_package(payload)
            _validate_package_compatibility({"compatibility": {"min_eidora_version": spec.minimum_eidora_version}})
            if require_assets:
                validate_package_files(directory, spec)
        except ModelPackageError as exc:
            raise RuntimeError(str(exc)) from exc
        return
    raise RuntimeError("Model package requires repackaging with schema_version: 3.")


def _validate_package_compatibility(payload: dict[str, Any]) -> None:
    compatibility = payload.get("compatibility")
    if compatibility is None:
        return
    if not isinstance(compatibility, dict):
        raise RuntimeError("Model package compatibility must be a mapping.")
    minimum = str(compatibility.get("min_eidora_version") or "").strip()
    if minimum and _version_key(minimum) > _version_key(EIDORA_VERSION):
        raise RuntimeError(
            f"Model package requires EIDORA {minimum} or later; this installation is {EIDORA_VERSION}."
        )


def _version_key(value: str) -> tuple[int, ...]:
    match = re.match(r"^v?(\d+(?:\.\d+)*)", value.strip())
    if not match:
        raise RuntimeError(f"Invalid EIDORA compatibility version: {value!r}.")
    parts = [int(part) for part in match.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _preprocessing_size(step: dict[str, Any]) -> tuple[int, int]:
    size = step.get("size")
    if isinstance(size, list) and len(size) == 2:
        return int(size[0]), int(size[1])
    return int(step.get("width", size)), int(step.get("height", size))


def _tokenizer_dir_from_config(directory: Path, payload: dict[str, Any]) -> Path:
    raw = payload.get("tokenizer_path")
    if raw:
        path = _resolve_package_path(directory, raw)
        if path is not None:
            return path if path.is_dir() else path.parent
    tokenizer = payload.get("tokenizer")
    if isinstance(tokenizer, dict):
        raw = tokenizer.get("path")
        if raw:
            path = _resolve_package_path(directory, raw)
            if path is not None:
                return path if path.is_dir() else path.parent
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, dict):
        raw_tokenizer = artifacts.get("tokenizer")
        raw = raw_tokenizer.get("path") if isinstance(raw_tokenizer, dict) else raw_tokenizer
        if raw:
            path = _resolve_package_path(directory, raw)
            if path is not None:
                return path if path.is_dir() else path.parent
    return directory


def _infer_modality_labels(pipeline_tag: str) -> tuple[str, ...]:
    normalized = pipeline_tag.strip().casefold()
    if not normalized:
        return ("image",)
    if "text" in normalized:
        return ("text",)
    if "audio" in normalized:
        return ("audio",)
    return ("image",)


def _collection_api_url(collection_url: str) -> str:
    parsed = urlparse(collection_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3 or parts[0] != "collections":
        raise ValueError(f"Unsupported Hugging Face collection URL: {collection_url}")
    return f"https://huggingface.co/api/collections/{parts[1]}/{parts[2]}"


@lru_cache(maxsize=1)
def _fetch_huggingface_account_groups(
    account: str,
    collection_urls: tuple[str, ...],
    installed_ids: frozenset[str],
) -> list[DiscoverableModelGroup]:
    account = account.strip() or HUGGINGFACE_ACCOUNT
    groups = _fetch_collection_groups_from_http_api(account, installed_ids)
    if groups:
        return groups
    groups = _fetch_collection_groups_from_hub_api(account, installed_ids)
    if groups:
        return groups
    urls = tuple(_discover_collection_urls_from_pages(account)) or collection_urls
    return _fetch_huggingface_groups_from_urls(urls, installed_ids)


def _fetch_collection_groups_from_hub_api(
    account: str,
    installed_ids: frozenset[str],
) -> list[DiscoverableModelGroup]:
    try:
        from huggingface_hub import HfApi
    except Exception:
        return []

    api = HfApi()
    collections = None
    for kwargs in ({"owner": account}, {"author": account}):
        try:
            collections = list(api.list_collections(**kwargs))
            break
        except TypeError:
            continue
        except Exception:
            return []
    if collections is None:
        return []

    groups: list[DiscoverableModelGroup] = []
    for collection in collections:
        payload = _object_payload(collection)
        detail_payload = payload
        # list_collections() intentionally truncates items to four entries.
        # Always fetch the collection detail so models moved later in the
        # collection (for example after a delete/re-upload) are not hidden.
        slug = str(payload.get("slug") or payload.get("id") or "").strip()
        if slug:
            try:
                candidate = _object_payload(api.get_collection(slug))
                if isinstance(candidate.get("items"), list):
                    detail_payload = candidate
            except Exception:
                detail_payload = payload
        group = _group_from_collection_payload(detail_payload, "", installed_ids)
        if group is not None:
            groups.append(group)
    return groups


def _fetch_collection_groups_from_http_api(
    account: str,
    installed_ids: frozenset[str],
) -> list[DiscoverableModelGroup]:
    encoded = quote(account)
    for api_url in (
        f"https://huggingface.co/api/collections?owner={encoded}",
        f"https://huggingface.co/api/collections?author={encoded}",
    ):
        try:
            with _open_huggingface_url(api_url, timeout=HUGGINGFACE_COLLECTION_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            continue
        raw_collections = payload.get("collections") if isinstance(payload, dict) else payload
        if not isinstance(raw_collections, list):
            continue
        groups: list[DiscoverableModelGroup] = []
        for raw in raw_collections:
            if not isinstance(raw, dict):
                continue
            detail = raw
            # Collection-list responses may contain only a truncated item
            # list. Prefer the detail endpoint whenever the collection has a
            # resolvable slug, while retaining the list response as fallback.
            api_detail_url = _collection_detail_api_url(raw)
            if api_detail_url:
                try:
                    with _open_huggingface_url(
                        api_detail_url,
                        timeout=HUGGINGFACE_COLLECTION_TIMEOUT_SECONDS,
                    ) as response:
                        candidate = json.loads(response.read().decode("utf-8"))
                    if isinstance(candidate, dict) and isinstance(candidate.get("items"), list):
                        detail = candidate
                except Exception:
                    detail = raw
            group = _group_from_collection_payload(detail, _collection_source_url(detail, account), installed_ids)
            if group is not None:
                groups.append(group)
        if groups:
            return groups
    return []


def _fetch_huggingface_groups_from_urls(
    collection_urls: tuple[str, ...],
    installed_ids: frozenset[str],
) -> list[DiscoverableModelGroup]:
    groups: list[DiscoverableModelGroup] = []
    for collection_url in collection_urls:
        try:
            api_url = _collection_api_url(collection_url)
            with _open_huggingface_url(api_url, timeout=HUGGINGFACE_COLLECTION_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            continue
        group = _group_from_collection_payload(payload, collection_url, installed_ids)
        if group is not None:
            groups.append(group)
    return groups


def _discover_collection_urls_from_pages(account: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    encoded = quote(account)
    page_urls = (
        f"https://huggingface.co/{encoded}/collections",
        f"https://huggingface.co/collections?search={encoded}",
    )
    for page_url in page_urls:
        try:
            with _open_huggingface_url(page_url, timeout=HUGGINGFACE_COLLECTION_TIMEOUT_SECONDS) as response:
                html = response.read().decode("utf-8", errors="replace")
        except Exception:
            continue
        for match in re.finditer(r'href=["\'](/collections/[^"\']+)["\']', html):
            path = match.group(1)
            parts = [part for part in path.split("/") if part]
            if len(parts) < 3 or parts[0] != "collections":
                continue
            if parts[1].casefold() != account.casefold():
                continue
            url = f"https://huggingface.co{path}"
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def _group_from_collection_payload(
    payload: dict[str, Any],
    source_url: str,
    installed_ids: frozenset[str],
) -> DiscoverableModelGroup | None:
    title = str(payload.get("title") or payload.get("name") or "").strip()
    slug = str(payload.get("slug") or payload.get("id") or title).strip().replace("/", "_")
    if not title:
        title = slug.rsplit("/", 1)[-1] if slug else "Collection"
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return None
    entries = tuple(_entry_from_collection_item(raw, installed_ids) for raw in raw_items)
    entries = tuple(entry for entry in entries if entry is not None)
    if not entries:
        return None
    return DiscoverableModelGroup(
        id=slug or title,
        label=title,
        source_url=source_url or _collection_source_url(payload, HUGGINGFACE_ACCOUNT),
        models=entries,
    )


def _entry_from_collection_item(raw: Any, installed_ids: frozenset[str]) -> ModelCatalogEntry | None:
    item = _object_payload(raw)
    if not item:
        return None
    item_type = str(
        item.get("type") or item.get("item_type") or item.get("itemType") or item.get("repoType") or "model"
    ).strip().casefold()
    if item_type and item_type != "model":
        return None
    if isinstance(item.get("item"), dict):
        nested = item["item"]
        item = {**item, **nested}
    model_id = str(
        item.get("id")
        or item.get("modelId")
        or item.get("item_id")
        or item.get("itemId")
        or item.get("repo_id")
        or item.get("repoId")
        or ""
    ).strip()
    if not model_id:
        return None
    card_data = item.get("cardData") if isinstance(item.get("cardData"), dict) else {}
    pipeline_tag = str(item.get("pipeline_tag") or item.get("pipelineTag") or card_data.get("pipeline_tag") or "").strip()
    tags = tuple(str(tag).strip() for tag in item.get("tags", ()) if str(tag).strip()) if isinstance(item.get("tags"), list) else ()
    modality_labels = _modality_labels_from_item(pipeline_tag, tags)
    label = str(item.get("title") or item.get("label") or item.get("name") or "").strip() or _display_model_label(model_id)
    safetensors = item.get("safetensors") if isinstance(item.get("safetensors"), dict) else {}
    size_bytes = _coerce_optional_int(item.get("size") or item.get("sizeBytes") or safetensors.get("total"))
    # Collection discovery must remain a single request path. Fetching each
    # package manifest here delays every row until all model requests finish.
    # The Hub's collection metadata supplies a size when it is available.
    description = str(item.get("description") or card_data.get("summary") or f"https://huggingface.co/{model_id}").strip()
    return ModelCatalogEntry(
        id=model_id,
        label=label,
        modality_labels=modality_labels,
        backend=str(item.get("library_name") or card_data.get("library_name") or "huggingface"),
        description=description,
        dimensions=_coerce_optional_int(card_data.get("embedding_dimensions") or card_data.get("dimensions")),
        size_bytes=size_bytes,
        install_state=ModelInstallState.INSTALLED if _model_id_key(model_id) in installed_ids else ModelInstallState.AVAILABLE,
        is_installed=_model_id_key(model_id) in installed_ids,
    )


@lru_cache(maxsize=256)
def _fetch_model_package_size_bytes(model_id: str) -> int | None:
    """Read the package-owned size declaration used by the Model Zoo UI."""
    try:
        text = _read_huggingface_text_file(model_id, MODEL_MANIFEST)
        import yaml

        payload = yaml.safe_load(text)
    except Exception:
        return None
    return _package_size_bytes_from_config(payload)


def _package_size_bytes_from_config(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    artifact = payload.get("artifact") if isinstance(payload.get("artifact"), dict) else {}
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    candidates = (
        payload.get("file_size_bytes"),
        payload.get("package_size_bytes"),
        artifact.get("file_size_bytes"),
        artifact.get("package_size_bytes"),
        artifacts.get("file_size_bytes"),
        artifacts.get("package_size_bytes"),
        payload.get("file_size"),
        artifact.get("file_size"),
        artifacts.get("file_size"),
    )
    for value in candidates:
        parsed = _size_bytes(value)
        if parsed is not None:
            return parsed
    return None


def _size_bytes(value: Any) -> int | None:
    numeric = _coerce_optional_int(value)
    if numeric is not None and numeric >= 0:
        return numeric
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?i?b)\s*$", str(value or ""), flags=re.IGNORECASE)
    if match is None:
        return None
    amount = float(match.group(1))
    unit = match.group(2).casefold()
    multipliers = {
        "b": 1,
        "kb": 1_000,
        "mb": 1_000_000,
        "gb": 1_000_000_000,
        "tb": 1_000_000_000_000,
        "kib": 1024,
        "mib": 1024**2,
        "gib": 1024**3,
        "tib": 1024**4,
    }
    return round(amount * multipliers[unit])


def _modality_labels_from_item(pipeline_tag: str, tags: tuple[str, ...]) -> tuple[str, ...]:
    labels: list[str] = []
    for tag in tags:
        normalized = tag.strip().casefold()
        if normalized.startswith("modality:"):
            value = normalized.split(":", 1)[1].strip()
            if value and value not in labels:
                labels.append(value)
    if labels:
        return tuple(labels)
    return _infer_modality_labels(pipeline_tag)


def _object_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    payload: dict[str, Any] = {}
    for name in (
        "id",
        "item",
        "slug",
        "title",
        "name",
        "items",
        "type",
        "item_type",
        "itemType",
        "repoType",
        "repo_id",
        "repoId",
        "item_id",
        "item_object_id",
        "itemId",
        "modelId",
        "pipeline_tag",
        "pipelineTag",
        "tags",
        "description",
        "cardData",
        "library_name",
    ):
        if hasattr(value, name):
            payload[name] = getattr(value, name)
    return payload


def _collection_detail_api_url(payload: dict[str, Any]) -> str:
    slug = str(payload.get("slug") or payload.get("id") or "").strip()
    if not slug:
        return ""
    if slug.startswith("http"):
        return _collection_api_url(slug)
    parts = [part for part in slug.split("/") if part]
    if len(parts) >= 2:
        return f"https://huggingface.co/api/collections/{parts[0]}/{parts[1]}"
    owner = str(payload.get("owner") or payload.get("author") or payload.get("namespace") or HUGGINGFACE_ACCOUNT).strip()
    return f"https://huggingface.co/api/collections/{owner}/{slug}"


def _collection_source_url(payload: dict[str, Any], account: str) -> str:
    slug = str(payload.get("slug") or payload.get("id") or "").strip()
    if slug.startswith("http"):
        return slug
    parts = [part for part in slug.split("/") if part]
    if len(parts) >= 2:
        return f"https://huggingface.co/collections/{parts[0]}/{parts[1]}"
    if slug:
        return f"https://huggingface.co/collections/{account}/{slug}"
    return ""


def _display_model_label(model_id: str) -> str:
    owner, separator, name = model_id.partition("/")
    if separator == "/" and owner.casefold() == HUGGINGFACE_ACCOUNT.casefold() and name.strip():
        return name.strip()
    return model_id
