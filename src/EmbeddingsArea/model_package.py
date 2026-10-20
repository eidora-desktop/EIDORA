"""Strict, Qt-free contract for executable EIDORA ONNX model packages."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from shared.bibtex import parse_bibtex

from EmbeddingsArea.preparation_contract import (
    SCHEMA_VERSION, OPTIONS, resolve_preparation, resolve_contract, validate_providers,
)


SUPPORTED_MODALITIES = frozenset({"image", "text", "audio", "video"})
SUPPORTED_DTYPES = frozenset({"float32", "int32", "int64"})
SUPPORTED_TENSOR_SOURCES = {
    "image": frozenset({"pixels"}),
    "text": frozenset({"token_ids", "attention_mask", "token_type_ids"}),
    "audio": frozenset({"waveform", "valid_samples", "sample_mask"}),
    "video": frozenset({"frames"}),
}
_TOP_LEVEL = frozenset({
    "schema_version", "id", "name", "version", "modality", "license",
    "compatibility", "artifacts", "runtime", "preparation", "embedding", "metadata", "bibtex",
})
_RUNTIME = frozenset({"graph", "inputs", "output", "execution_providers"})
_INPUT = frozenset({"name", "source", "dtype", "shape"})
_OUTPUT = frozenset({"name", "dtype", "shape"})
_ARTIFACT = frozenset({"path", "sha256", "role"})
_EMBEDDING = frozenset({"dimensions", "normalized", "similarity", "shared_space"})
_SHARED_SPACE = frozenset({"id", "revision"})
_PREPARATION_FIELDS = {
    "image": frozenset({"width", "height", "layout", "resize", "interpolation", "rescale", "mean", "std", "alpha_background"}),
    "text": frozenset({"tokenizer_path", "max_length", "padding_side", "truncation_side", "add_special_tokens", "prefix", "suffix"}),
    "audio": frozenset({"sample_rate", "sample_count", "channels", "excerpt"}),
    "video": frozenset({"width", "height", "frame_count", "duration_seconds", "layout", "resize", "interpolation", "rescale", "mean", "std", "alpha_background", "excerpt"}),
}


class ModelPackageError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArtifactSpec:
    path: str
    sha256: str
    role: str


@dataclass(frozen=True)
class TensorInputSpec:
    name: str
    source: str
    dtype: str
    shape: tuple[object, ...]


@dataclass(frozen=True)
class TensorOutputSpec:
    name: str
    dtype: str
    shape: tuple[object, ...]


@dataclass(frozen=True)
class ModelPackageSpec:
    id: str
    name: str
    version: str
    modality: str
    license: str
    minimum_eidora_version: str
    artifacts: tuple[ArtifactSpec, ...]
    graph_path: str
    inputs: tuple[TensorInputSpec, ...]
    output: TensorOutputSpec
    preparation: dict[str, object]
    dimensions: int
    normalized: bool
    similarity: str
    shared_space_id: str = ""
    shared_space_revision: str = ""
    metadata: dict[str, object] | None = None
    components: tuple[ModelPackageSpec, ...] = ()
    execution_providers: tuple[str, ...] = ()
    bibtex: str = ""

    def contract(self) -> dict[str, object]:
        if self.components:
            return {
                "schema_version": SCHEMA_VERSION,
                "execution_providers": list(self.execution_providers),
                "id": self.id, "name": self.name, "version": self.version,
                "modality": "multimodal",
                "bibtex": self.bibtex,
                "metadata": dict(self.metadata or {}),
                "fusion": "normalized_mean",
                "components": {
                    item.modality: {**item.contract(), "graph": item.graph_path}
                    for item in self.components
                },
                "embedding": self.components[0].contract()["embedding"],
            }
        return {
            "schema_version": SCHEMA_VERSION,
            "execution_providers": list(self.execution_providers),
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "license": self.license,
            "bibtex": self.bibtex,
            "minimum_eidora_version": self.minimum_eidora_version,
            "modality": self.modality,
            "inputs": [
                {"name": item.name, "source": item.source, "dtype": item.dtype, "shape": list(item.shape)}
                for item in self.inputs
            ],
            "output": {"name": self.output.name, "dtype": self.output.dtype, "shape": list(self.output.shape)},
            "preparation": dict(self.preparation),
            "embedding": {
                "dimensions": self.dimensions,
                "normalized": self.normalized,
                "similarity": self.similarity,
                "shared_space": {"id": self.shared_space_id, "revision": self.shared_space_revision},
            },
            "artifacts": [artifact.__dict__ for artifact in self.artifacts],
            "metadata": dict(self.metadata or {}),
        }

    def fingerprint(self) -> str:
        raw = json.dumps(self.contract(), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(raw.encode("utf-8"))
        for artifact in self.artifacts:
            digest.update(artifact.path.encode("utf-8"))
            digest.update(artifact.sha256.encode("ascii"))
        return digest.hexdigest()


def load_model_package(config_path: Path) -> ModelPackageSpec:
    try:
        import yaml
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ModelPackageError(f"Could not read model package config: {exc}") from exc
    return parse_model_package(payload)


def parse_model_package(payload: object) -> ModelPackageSpec:
    if not isinstance(payload, dict):
        raise ModelPackageError("config.yaml must contain one mapping.")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ModelPackageError(f"schema_version must be {SCHEMA_VERSION}; this package requires repackaging.")
    _reject_unknown(payload, _TOP_LEVEL, "config")
    if payload.get("modality") == "multimodal":
        return _parse_multimodal_package(payload)
    model_id = _required_text(payload, "id")
    name = _required_text(payload, "name")
    version = _required_text(payload, "version")
    modality = _required_text(payload, "modality").lower()
    if modality not in SUPPORTED_MODALITIES:
        raise ModelPackageError(f"Unsupported modality: {modality!r}.")
    license_name = _required_text(payload, "license")
    compatibility = _mapping(payload.get("compatibility"), "compatibility")
    _reject_unknown(compatibility, frozenset({"min_eidora_version"}), "compatibility")
    minimum = _required_text(compatibility, "min_eidora_version")

    raw_artifacts = payload.get("artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise ModelPackageError("artifacts must be a nonempty list.")
    artifacts: list[ArtifactSpec] = []
    for index, raw in enumerate(raw_artifacts):
        item = _mapping(raw, f"artifacts[{index}]")
        _reject_unknown(item, _ARTIFACT, f"artifacts[{index}]")
        path = _package_path(_required_text(item, "path"))
        checksum = _required_text(item, "sha256").lower()
        if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
            raise ModelPackageError(f"artifacts[{index}].sha256 must be a hexadecimal SHA-256.")
        artifacts.append(ArtifactSpec(path, checksum, _required_text(item, "role")))
    if len({artifact.path for artifact in artifacts}) != len(artifacts):
        raise ModelPackageError("Artifact paths must be unique.")

    runtime = _mapping(payload.get("runtime"), "runtime")
    _reject_unknown(runtime, _RUNTIME, "runtime")
    try:
        providers = validate_providers(runtime.get("execution_providers"))
    except ValueError as exc:
        raise ModelPackageError(str(exc)) from exc
    graph = _package_path(_required_text(runtime, "graph"))
    if graph not in {artifact.path for artifact in artifacts} or not graph.lower().endswith(".onnx"):
        raise ModelPackageError("runtime.graph must reference a declared .onnx artifact.")
    raw_inputs = runtime.get("inputs")
    if not isinstance(raw_inputs, list) or not raw_inputs:
        raise ModelPackageError("runtime.inputs must be a nonempty list.")
    inputs: list[TensorInputSpec] = []
    for index, raw in enumerate(raw_inputs):
        item = _mapping(raw, f"runtime.inputs[{index}]")
        _reject_unknown(item, _INPUT, f"runtime.inputs[{index}]")
        source = _required_text(item, "source")
        if source not in SUPPORTED_TENSOR_SOURCES[modality]:
            raise ModelPackageError(f"Tensor source {source!r} is not supported for {modality} packages.")
        dtype = _dtype(item.get("dtype"), f"runtime.inputs[{index}].dtype")
        shape = _shape(item.get("shape"), f"runtime.inputs[{index}].shape")
        inputs.append(TensorInputSpec(_required_text(item, "name"), source, dtype, shape))
    if len({item.name for item in inputs}) != len(inputs):
        raise ModelPackageError("ONNX input names must be unique.")

    raw_output = _mapping(runtime.get("output"), "runtime.output")
    _reject_unknown(raw_output, _OUTPUT, "runtime.output")
    output = TensorOutputSpec(
        _required_text(raw_output, "name"),
        _dtype(raw_output.get("dtype"), "runtime.output.dtype"),
        _shape(raw_output.get("shape"), "runtime.output.shape"),
    )
    if output.dtype != "float32" or len(output.shape) != 2:
        raise ModelPackageError("runtime.output must be float32 with shape [batch, dimensions].")
    batch_dimensions = {item.shape[0] for item in inputs} | {output.shape[0]}
    if 1 in batch_dimensions and len(batch_dimensions) != 1:
        raise ModelPackageError("Fixed batch-1 inputs and output must all declare batch size 1.")

    preparation = _mapping(payload.get("preparation"), "preparation")
    _reject_unknown(preparation, _PREPARATION_FIELDS[modality] | {"parameters"} | {key for key, value in OPTIONS.items() if modality in value["modalities"]}, f"{modality} preparation")
    try:
        effective = resolve_preparation(modality, preparation)
        _validate_preparation(modality, effective, inputs)
    except ValueError as exc:
        raise ModelPackageError(str(exc)) from exc
    embedding = _mapping(payload.get("embedding"), "embedding")
    _reject_unknown(embedding, _EMBEDDING, "embedding")
    dimensions = _positive_int(embedding.get("dimensions"), "embedding.dimensions")
    if isinstance(output.shape[1], int) and output.shape[1] != dimensions:
        raise ModelPackageError("runtime.output shape disagrees with embedding.dimensions.")
    normalized = embedding.get("normalized")
    if not isinstance(normalized, bool):
        raise ModelPackageError("embedding.normalized must be true or false.")
    similarity = _required_text(embedding, "similarity").lower()
    if similarity not in {"cosine", "dot", "euclidean"}:
        raise ModelPackageError("embedding.similarity must be cosine, dot, or euclidean.")
    shared = embedding.get("shared_space", {})
    shared = _mapping(shared, "embedding.shared_space")
    _reject_unknown(shared, _SHARED_SPACE, "embedding.shared_space")
    shared_id = str(shared.get("id") or "").strip()
    shared_revision = str(shared.get("revision") or "").strip()
    if bool(shared_id) != bool(shared_revision):
        raise ModelPackageError("A shared space requires both id and revision.")
    try:
        parse_bibtex(payload.get("bibtex"))
    except ValueError as exc:
        raise ModelPackageError(str(exc)) from exc
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ModelPackageError("metadata must be a mapping.")
    spec = ModelPackageSpec(
        model_id, name, version, modality, license_name, minimum,
        tuple(artifacts), graph, tuple(inputs), output, dict(preparation), dimensions,
        normalized, similarity, shared_id, shared_revision, dict(metadata), execution_providers=providers, bibtex=payload["bibtex"].strip(),
    )

    try:
        resolve_contract(spec.contract())
    except ValueError as exc:
        raise ModelPackageError(str(exc)) from exc
    return spec


def _parse_multimodal_package(payload: dict[str, object]) -> ModelPackageSpec:
    """A package owns its encoders and explicitly declares their fusion rule."""
    runtime = _mapping(payload.get("runtime"), "runtime")
    _reject_unknown(runtime, frozenset({"components", "fusion", "execution_providers"}), "multimodal runtime")
    try:
        providers = validate_providers(runtime.get("execution_providers"))
    except ValueError as exc:
        raise ModelPackageError(str(exc)) from exc
    if runtime.get("fusion") != "normalized_mean":
        raise ModelPackageError("Multimodal runtime.fusion must be normalized_mean.")
    if "preparation" in payload:
        raise ModelPackageError("Multimodal preparation belongs to each component.")
    raw_components = _mapping(runtime.get("components"), "runtime.components")
    if len(raw_components) < 2:
        raise ModelPackageError("Multimodal packages require at least two modality components.")
    components = []
    for modality, raw in raw_components.items():
        if modality not in SUPPORTED_MODALITIES:
            raise ModelPackageError(f"Unsupported component modality: {modality!r}.")
        component = _mapping(raw, f"component {modality}")
        _reject_unknown(component, frozenset({"runtime", "preparation"}), f"component {modality}")
        component_runtime = _mapping(component.get("runtime"), "component runtime")
        if "execution_providers" in component_runtime and component_runtime["execution_providers"] != list(providers):
            raise ModelPackageError("Component execution providers must match the package declaration.")
        components.append(parse_model_package({
            **payload, "modality": modality,
            "runtime": {**component_runtime, "execution_providers": list(providers)}, "preparation": component.get("preparation"),
        }))
    first = components[0]
    if not first.shared_space_id or not first.normalized:
        raise ModelPackageError("Normalized mean requires normalized encoders in a declared shared space.")
    return replace(first, modality="multimodal", components=tuple(components))


def validate_package_files(directory: Path, spec: ModelPackageSpec, *, verify_hashes: bool = True) -> None:
    if not (directory / "README.md").is_file():
        raise ModelPackageError("Model package is missing required README.md model card.")
    for artifact in spec.artifacts:
        path = resolve_package_path(directory, artifact.path)
        if not path.is_file():
            raise ModelPackageError(f"Model package is missing artifact: {artifact.path}")
        if verify_hashes:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != artifact.sha256:
                raise ModelPackageError(f"Artifact checksum mismatch: {artifact.path}")
    for component in spec.components or (spec,):
        tokenizer = str(component.preparation.get("tokenizer_path") or "")
        if tokenizer and not resolve_package_path(directory, f"{tokenizer.rstrip('/')}/tokenizer.json").is_file():
            raise ModelPackageError("Text package is missing its declared tokenizer.json.")


def validate_onnx_graph(directory: Path, spec: ModelPackageSpec) -> None:
    """Load with the portable CPU provider and compare its public tensor contract."""
    if spec.components:
        for component in spec.components:
            validate_onnx_graph(directory, component)
        return
    try:
        import onnxruntime as ort
        session = ort.InferenceSession(
            str(resolve_package_path(directory, spec.graph_path)),
            providers=["CPUExecutionProvider"],
        )
    except Exception as exc:
        raise ModelPackageError(f"ONNX graph does not load with EIDORA's CPU runtime: {exc}") from exc
    actual_inputs = {str(item.name): item for item in session.get_inputs()}
    if set(actual_inputs) != {item.name for item in spec.inputs}:
        raise ModelPackageError("ONNX graph inputs do not exactly match runtime.inputs.")
    expected_types = {"float32": "tensor(float)", "int32": "tensor(int32)", "int64": "tensor(int64)"}
    for item in spec.inputs:
        actual = actual_inputs[item.name]
        if str(actual.type) != expected_types[item.dtype] or not _shapes_compatible(actual.shape, item.shape):
            raise ModelPackageError(f"ONNX input contract mismatch: {item.name}")
    outputs = {str(item.name): item for item in session.get_outputs()}
    actual_output = outputs.get(spec.output.name)
    if actual_output is None or str(actual_output.type) != "tensor(float)" or not _shapes_compatible(actual_output.shape, spec.output.shape):
        raise ModelPackageError(f"ONNX output contract mismatch: {spec.output.name}")


def _shapes_compatible(actual: object, expected: tuple[object, ...]) -> bool:
    values = list(actual or [])
    if len(values) != len(expected):
        return False
    for index, (found, wanted) in enumerate(zip(values, expected)):
        if index == 0 and wanted in {"batch", "dynamic"} and isinstance(found, int):
            return False
        if isinstance(wanted, int) and isinstance(found, int) and wanted != found:
            return False
    return True


def resolve_package_path(directory: Path, relative: str) -> Path:
    relative = _package_path(relative)
    root = directory.resolve()
    result = (root / relative).resolve()
    if root not in result.parents:
        raise ModelPackageError(f"Package path escapes its directory: {relative}")
    return result


def _validate_preparation(modality: str, prep: dict[str, Any], inputs: list[TensorInputSpec]) -> None:
    if modality in {"image", "video"}:
        _positive_int(prep.get("width"), "preparation.width")
        _positive_int(prep.get("height"), "preparation.height")
        layouts = {"image": {"NCHW", "NHWC"}, "video": {"BTCHW", "BCTHW", "BTHWC"}}[modality]
        if _required_text(prep, "layout") not in layouts:
            raise ModelPackageError(f"Unsupported {modality} layout.")
        if _required_text(prep, "resize") not in {"stretch", "fill_crop", "fit_pad", "short_edge_crop"}:
            raise ModelPackageError("preparation.resize must be stretch, fill_crop, fit_pad, or short_edge_crop.")
        if _required_text(prep, "interpolation") not in {"nearest", "bilinear", "bicubic", "lanczos"}:
            raise ModelPackageError("Unsupported image interpolation.")
        _number(prep.get("rescale"), "preparation.rescale")
        _rgb_triplet(prep.get("mean"), "preparation.mean")
        std = _rgb_triplet(prep.get("std"), "preparation.std")
        if any(value == 0 for value in std):
            raise ModelPackageError("preparation.std values must be nonzero.")
        background = prep.get("alpha_background", [0, 0, 0])
        if not isinstance(background, list) or len(background) != 3 or any(not isinstance(x, int) or not 0 <= x <= 255 for x in background):
            raise ModelPackageError("preparation.alpha_background must be three integers from 0 to 255.")
    if modality == "text":
        _package_path(_required_text(prep, "tokenizer_path"))
        _positive_int(prep.get("max_length"), "preparation.max_length")
        if _required_text(prep, "padding_side") not in {"left", "right"}:
            raise ModelPackageError("preparation.padding_side must be left or right.")
        if _required_text(prep, "truncation_side") not in {"left", "right"}:
            raise ModelPackageError("preparation.truncation_side must be left or right.")
        if not isinstance(prep.get("add_special_tokens"), bool):
            raise ModelPackageError("preparation.add_special_tokens must be true or false.")
    if modality == "audio":
        _positive_int(prep.get("sample_rate"), "preparation.sample_rate")
        _positive_int(prep.get("sample_count"), "preparation.sample_count")
        channels = prep.get("channels")
        if channels != "mono" and (not isinstance(channels, int) or isinstance(channels, bool) or channels < 0):
            raise ModelPackageError("preparation.channels must be mono or a zero-based channel index.")
    if modality == "video":
        _positive_int(prep.get("frame_count"), "preparation.frame_count")
        if _number(prep.get("duration_seconds"), "preparation.duration_seconds") <= 0:
            raise ModelPackageError("preparation.duration_seconds must be positive.")
    if modality in {"audio", "video"} and str(prep.get("excerpt") or "beginning") not in {"beginning", "center", "end", "custom"}:
        raise ModelPackageError("preparation.excerpt must be beginning, center, end, or custom.")
    if not any(item.source in SUPPORTED_TENSOR_SOURCES[modality] for item in inputs):
        raise ModelPackageError(f"No usable {modality} tensor input is declared.")
    batch = inputs[0].shape[0]
    if modality == "image":
        expected = (
            (batch, 3, prep["height"], prep["width"])
            if prep["layout"] == "NCHW"
            else (batch, prep["height"], prep["width"], 3)
        )
        _require_source_shape(inputs, "pixels", expected)
    elif modality == "text":
        expected = (batch, prep["max_length"])
        for item in inputs:
            if item.source in SUPPORTED_TENSOR_SOURCES["text"] and item.shape != expected:
                raise ModelPackageError(f"Text tensor '{item.name}' shape must be {list(expected)}.")
    elif modality == "audio":
        waveform = next((item for item in inputs if item.source == "waveform"), None)
        expected_waveforms = {(batch, prep["sample_count"]), (batch, 1, prep["sample_count"])}
        if waveform is None or waveform.shape not in expected_waveforms:
            raise ModelPackageError("Audio waveform shape must be [batch, samples] or [batch, 1, samples].")
        for item in inputs:
            expected = (batch,) if item.source == "valid_samples" else (batch, prep["sample_count"])
            if item.source in {"valid_samples", "sample_mask"} and item.shape != expected:
                raise ModelPackageError(f"Audio tensor '{item.name}' has an invalid shape.")
    elif modality == "video":
        shapes = {
            "BTCHW": (batch, prep["frame_count"], 3, prep["height"], prep["width"]),
            "BCTHW": (batch, 3, prep["frame_count"], prep["height"], prep["width"]),
            "BTHWC": (batch, prep["frame_count"], prep["height"], prep["width"], 3),
        }
        _require_source_shape(inputs, "frames", shapes[str(prep["layout"])])


def _require_source_shape(inputs: list[TensorInputSpec], source: str, expected: tuple[object, ...]) -> None:
    item = next((value for value in inputs if value.source == source), None)
    if item is None or item.shape != expected:
        raise ModelPackageError(f"Tensor source '{source}' shape must be {list(expected)}.")


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelPackageError(f"{field} must be a mapping.")
    return value


def _reject_unknown(value: dict[str, Any], allowed: frozenset[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ModelPackageError(f"Unknown {field} field(s): {', '.join(unknown)}.")


def _required_text(value: dict[str, Any], key: str) -> str:
    text = str(value.get(key) or "").strip()
    if not text:
        raise ModelPackageError(f"{key} is required.")
    return text


def _package_path(value: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or path == Path("."):
        raise ModelPackageError(f"Invalid package-relative path: {value!r}.")
    return path.as_posix()


def _dtype(value: object, field: str) -> str:
    dtype = str(value or "").strip().lower()
    if dtype not in SUPPORTED_DTYPES:
        raise ModelPackageError(f"{field} must be float32, int32, or int64.")
    return dtype


def _shape(value: object, field: str) -> tuple[object, ...]:
    if not isinstance(value, list) or not value:
        raise ModelPackageError(f"{field} must be a nonempty list.")
    result: list[object] = []
    for dimension in value:
        if isinstance(dimension, str) and dimension in {"batch", "dynamic"}:
            result.append(dimension)
        elif type(dimension) is int and dimension > 0:
            result.append(dimension)
        else:
            raise ModelPackageError(f"{field} contains an invalid dimension: {dimension!r}.")
    if result[0] not in {"batch", "dynamic", 1}:
        raise ModelPackageError(f"{field} must use dynamic batch or fixed batch 1.")
    return tuple(result)


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ModelPackageError(f"{field} must be a positive integer.")
    return value


def _number(value: object, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise ModelPackageError(f"{field} must be a number.")
    return float(value)


def _rgb_triplet(value: object, field: str) -> tuple[float, float, float]:
    if not isinstance(value, list) or len(value) != 3 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in value):
        raise ModelPackageError(f"{field} must contain three numbers.")
    return tuple(float(x) for x in value)
