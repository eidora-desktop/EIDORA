"""Versioned, Qt-free vocabulary shared with the ONNX packaging pipeline."""
from __future__ import annotations

from copy import deepcopy
import json
import math

SCHEMA_VERSION = 3
PREPARATION_VERSION = 2
PROVIDERS = ("CPUExecutionProvider", "CUDAExecutionProvider", "CoreMLExecutionProvider")
CROP_POSITIONS = ("top_left", "top", "top_right", "left", "center", "right", "bottom_left", "bottom", "bottom_right")

# Only these properties may be exposed by model packages. All other preparation
# fields remain fixed model requirements. Conditions are owned here, not by YAML.
OPTIONS = {
    "resize": dict(modalities=["image", "video"], kind="choice", choices=["stretch", "fill_crop", "fit_pad", "short_edge_crop"]),
    "resize_rounding": dict(modalities=["image", "video"], kind="choice", choices=["round", "truncate"], default="round", enabled_when={"resize": ["fill_crop", "fit_pad", "short_edge_crop"]}),
    "interpolation": dict(modalities=["image", "video"], kind="choice", choices=["nearest", "bilinear", "bicubic", "lanczos"]),
    "crop_position": dict(modalities=["image", "video"], kind="choice", choices=list(CROP_POSITIONS), default="center", enabled_when={"resize": ["fill_crop", "short_edge_crop"]}),
    "short_edge": dict(modalities=["image", "video"], kind="integer", minimum=1, maximum=16384, enabled_when={"resize": ["short_edge_crop"]}),
    "alpha_background": dict(modalities=["image", "video"], kind="rgb", default=[0, 0, 0]),
    "truncation_side": dict(modalities=["text"], kind="choice", choices=["left", "right"]),
    "prefix": dict(modalities=["text"], kind="text", default=""),
    "suffix": dict(modalities=["text"], kind="text", default=""),
    "channels": dict(modalities=["audio"], kind="channel", minimum=0, maximum=255),
    "excerpt": dict(modalities=["audio", "video"], kind="choice", choices=["beginning", "center", "end", "custom"], default="beginning"),
    "start_seconds": dict(modalities=["audio", "video"], kind="number", minimum=0, maximum=604800, default=0.0, enabled_when={"excerpt": ["custom"]}),
    "duration_seconds": dict(modalities=["video"], kind="number", minimum=0.001, maximum=86400),
}
OPERATIONS = {
    "image": ["first_page", "exif_orientation", "rgb_alpha_composite", "resize_crop_pad", "rescale_normalize", "layout", "batch"],
    "text": ["read_utf8", "prefix_suffix", "packaged_tokenizer", "truncate", "fixed_padding", "tensor_binding"],
    "audio": ["decode_first_stream", "channel_selection", "pyav_resample", "sample_accurate_excerpt", "right_zero_pad", "tensor_binding"],
    "video": ["decode_first_stream", "uniform_timestamp_sampling", "rgb", "resize_crop_pad", "rescale_normalize", "repeat_last_frame", "layout", "batch"],
}


def catalog_document():
    return dict(schema_version=SCHEMA_VERSION, preparation_version=PREPARATION_VERSION,
                bibtex={"required": True, "type": "string", "minimum_records": 1,
                        "required_fields": ["title"], "values": "self-contained literals"},
                execution_providers=list(PROVIDERS), options=deepcopy(OPTIONS), operations=OPERATIONS,
                semantics={"resize_rounding": "round: Python round (ties to even; default); truncate: discard fractional pixels using exact integer ratios", "crop_offsets": "integer floor; nine-position grid",
                           "interpolation": "Pillow RGB resize filters", "video_sampling": "first frame at or after start+k*duration/frame_count; endpoint excluded",
                           "audio_resampling": "PyAV fltp resampler; channel selection before resampling"})


def validate_providers(value):
    if not isinstance(value, list) or not value or any(not isinstance(x, str) or x not in PROVIDERS for x in value):
        raise ValueError("runtime.execution_providers must list supported ONNX Runtime providers.")
    if len(set(value)) != len(value) or PROVIDERS[0] not in value:
        raise ValueError("runtime.execution_providers must include CPUExecutionProvider without duplicates.")
    return tuple(value)


def _value(key, value, definition):
    kind = OPTIONS[key]["kind"]
    if kind == "choice":
        if not isinstance(value, str) or value not in OPTIONS[key]["choices"]:
            raise ValueError(f"Unsupported {key}: {value!r}.")
    elif kind in {"integer", "number", "channel"}:
        if kind == "channel" and value == "mono":
            if "choices" in definition and value not in definition["choices"]:
                raise ValueError("mono is not one of this model's channel choices.")
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{key} must be a finite number.")
        if kind in {"integer", "channel"} and not isinstance(value, int):
            raise ValueError(f"{key} must be an integer.")
        for bound, compare in (("minimum", lambda x, y: x < y), ("maximum", lambda x, y: x > y)):
            limit = definition.get(bound, OPTIONS[key].get(bound))
            if limit is not None and compare(value, limit):
                raise ValueError(f"{key} is outside its {bound} of {limit}.")
    elif kind == "rgb":
        if not isinstance(value, list) or len(value) != 3 or any(type(x) is not int or not 0 <= x <= 255 for x in value):
            raise ValueError(f"{key} must contain three RGB integers from 0 to 255.")
    elif not isinstance(value, str):
        raise ValueError(f"{key} must be text.")
    if "choices" in definition and value not in definition["choices"]:
        raise ValueError(f"{key} is not one of this model's choices.")
    return value


def parameter_definitions(modality, preparation):
    definitions = preparation.get("parameters", {})
    if not isinstance(definitions, dict):
        raise ValueError("preparation.parameters must be a mapping.")
    for key, definition in definitions.items():
        if key not in OPTIONS or modality not in OPTIONS[key]["modalities"]:
            raise ValueError(f"Unknown {modality} preparation parameter: {key}.")
        if not isinstance(definition, dict) or set(definition) - {"default", "choices", "minimum", "maximum"} or "default" not in definition:
            raise ValueError(f"Parameter {key} requires a default and only choices/minimum/maximum properties.")
        option = OPTIONS[key]
        for bound in ("minimum", "maximum"):
            if bound in definition:
                if bound not in option or isinstance(definition[bound], bool) or not isinstance(definition[bound], (float, int)) or not math.isfinite(definition[bound]):
                    raise ValueError(f"Invalid {key}.{bound}.")
                if option["kind"] in {"integer", "channel"} and type(definition[bound]) is not int:
                    raise ValueError(f"{key}.{bound} must be an integer.")
                if (bound == "minimum" and definition[bound] < option[bound]) or (bound == "maximum" and definition[bound] > option[bound]):
                    raise ValueError(f"{key} bounds cannot expand the global catalog.")
        if definition.get("minimum", option.get("minimum", 0)) > definition.get("maximum", option.get("maximum", float("inf"))):
            raise ValueError(f"Invalid bounds for {key}.")
        if "choices" in definition:
            choices = definition["choices"]
            if not isinstance(choices, list) or not choices:
                raise ValueError(f"{key}.choices must be nonempty.")
            for choice in choices:
                _value(key, choice, {k: v for k, v in definition.items() if k != "choices"})
        _value(key, definition["default"], definition)
    return definitions


def resolve_preparation(modality, preparation, overrides=None):
    definitions = parameter_definitions(modality, preparation)
    overrides = overrides or {}
    if set(overrides) - set(definitions):
        raise ValueError(f"Unknown preparation overrides: {sorted(set(overrides) - set(definitions))}.")
    result = deepcopy({k: v for k, v in preparation.items() if k != "parameters"})
    for key, option in OPTIONS.items():
        if modality in option["modalities"] and "default" in option:
            result.setdefault(key, deepcopy(option["default"]))
    for key, definition in definitions.items():
        value = overrides.get(key, definition["default"])
        if isinstance(value, str) and OPTIONS[key]["kind"] not in {"choice", "text"} and value != "mono":
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise ValueError(f"Invalid value for {key}.") from exc
        result[key] = _value(key, value, definition)
    for key in result.keys() & OPTIONS.keys():
        if modality in OPTIONS[key]["modalities"]:
            _value(key, result[key], {})
    if result.get("resize") == "short_edge_crop":
        if result.get("short_edge", 0) < max(result["width"], result["height"]):
            raise ValueError("short_edge must be at least the final width and height.")
    if result.get("excerpt") == "custom" and "start_seconds" not in result:
        raise ValueError("Custom excerpts require start_seconds.")
    return result


def resolve_contract(contract, overrides=None):
    """Return an isolated effective recipe; callers persist it before execution."""
    result = deepcopy(contract)
    overrides = dict(overrides or {})
    if result.get("components"):
        known = {f"{modality}.{key}" for modality, component in result["components"].items()
                 for key in component["preparation"].get("parameters", {})}
        if set(overrides) - known:
            raise ValueError("Unknown component preparation parameter.")
        for modality, component in result["components"].items():
            selected = {key.split(".", 1)[1]: value for key, value in overrides.items() if key.startswith(modality + ".")}
            result["components"][modality] = resolve_contract(component, selected)
    else:
        result["preparation"] = resolve_preparation(result["modality"], result["preparation"], overrides)
        sizes = {"float32": 4, "int32": 4, "int64": 8}
        size = sum(math.prod(t["shape"][1:]) * sizes[t["dtype"]] for t in result["inputs"])
        if size > 256 * 1024**2:
            raise ValueError("One prepared input exceeds the 256 MiB preparation budget.")
    result["preparation_version"] = PREPARATION_VERSION
    return result


def model_parameters(contract):
    from EmbeddingsArea.domain import EmbeddingModelParameter
    result = []
    components = contract.get("components") or {"": contract}
    for prefix, component in components.items():
        for key, definition in component["preparation"].get("parameters", {}).items():
            option = OPTIONS[key]
            encode = lambda value: value if isinstance(value, str) else json.dumps(value)
            result.append(EmbeddingModelParameter(
                id=f"{prefix}.{key}" if prefix else key, scope="preprocess",
                label=(prefix.title() + " · " if prefix else "") + key.replace("_", " ").capitalize(),
                kind=option["kind"], default=encode(definition["default"]), visible=True,
                choices=tuple(encode(x) for x in definition.get("choices", option.get("choices", []))),
                minimum=definition.get("minimum", option.get("minimum")), maximum=definition.get("maximum", option.get("maximum")),
                description="Recommended default: " + encode(definition["default"]),
            ))
    return tuple(result)
