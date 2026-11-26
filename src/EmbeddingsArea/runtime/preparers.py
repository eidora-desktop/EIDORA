"""Small, package-driven media-to-tensor preparers for ONNX inference."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from EmbeddingsArea.runtime.contracts import RuntimeInput


def prepare_batch(
    batch: list[RuntimeInput],
    contract: dict[str, object],
    *,
    package_directory: Path,
    text_preparer=None,
) -> dict[str, np.ndarray]:
    modality = str(contract["modality"])
    inputs = list(contract["inputs"])
    prep = dict(contract["preparation"])
    if modality == "image":
        values = [_prepare_image(item.path, prep) for item in batch]
        sources = {"pixels": np.stack(values)}
    elif modality == "text":
        from EmbeddingsArea.runtime.text import TextPreparer

        sources = (text_preparer or TextPreparer(prep, package_directory)).prepare(batch)
    elif modality == "audio":
        prepared = [_prepare_audio(item.path, prep) for item in batch]
        waveforms = np.stack([item[0] for item in prepared])
        valid = np.asarray([item[1] for item in prepared], dtype=np.int64)
        sources = {
            "waveform": waveforms,
            "valid_samples": valid,
            "sample_mask": (np.arange(waveforms.shape[-1])[None, :] < valid[:, None]).astype(np.int64),
        }
    elif modality == "video":
        sources = {"frames": np.stack([_prepare_video(item.path, prep) for item in batch])}
    else:  # strict package parsing should make this unreachable
        raise RuntimeError(f"Unsupported ONNX modality: {modality}")
    result: dict[str, np.ndarray] = {}
    for item in inputs:
        source = sources[str(item["source"])]
        if modality == "audio" and item["source"] == "waveform" and len(item["shape"]) == 3:
            source = source[:, None, :]
        target = np.dtype(str(item["dtype"]))
        result[str(item["name"])] = source.astype(target, copy=False)
    return result


def _prepare_image(path: Path, prep: dict[str, Any]) -> np.ndarray:
    try:
        from PIL import Image, ImageOps
    except Exception as exc:
        raise RuntimeError("Pillow is required for image and video model preparation.") from exc
    with Image.open(path) as opened:
        opened.seek(0)
        image = ImageOps.exif_transpose(opened)
        image.load()
        image = _rgb_image(image, prep, Image)
        image = _resize_image(image, prep, Image)
        return _image_array(image, prep)


def _rgb_image(image: Any, prep: dict[str, Any], image_module: Any) -> Any:
    if image.mode in {"RGBA", "LA"} or "transparency" in getattr(image, "info", {}):
        rgba = image.convert("RGBA")
        background = image_module.new("RGBA", rgba.size, tuple(prep.get("alpha_background", [0, 0, 0])) + (255,))
        return image_module.alpha_composite(background, rgba).convert("RGB")
    return image.convert("RGB")


def _resize_image(image: Any, prep: dict[str, Any], image_module: Any) -> Any:
    width, height = int(prep["width"]), int(prep["height"])
    resampling = {
        "nearest": image_module.Resampling.NEAREST,
        "bilinear": image_module.Resampling.BILINEAR,
        "bicubic": image_module.Resampling.BICUBIC,
        "lanczos": image_module.Resampling.LANCZOS,
    }[str(prep["interpolation"])]
    mode = str(prep["resize"])
    if mode == "stretch":
        return image.resize((width, height), resampling)
    if mode == "short_edge_crop":
        numerator, denominator = int(prep["short_edge"]), min(image.width, image.height)
    else:
        choose_scale = min if mode == "fit_pad" else max
        numerator, denominator = choose_scale(
            (width, image.width), (height, image.height), key=lambda ratio: ratio[0] / ratio[1],
        )
    if prep.get("resize_rounding", "round") == "truncate":
        # Exact ratios keep integer dimensions from losing a pixel to float error.
        size = (image.width * numerator // denominator, image.height * numerator // denominator)
    else:
        scale = numerator / denominator
        size = (round(image.width * scale), round(image.height * scale))
    if mode in {"fill_crop", "short_edge_crop"}:
        if size[0] < width or size[1] < height:
            raise RuntimeError("Resize does not cover the requested crop.")
        if size[0] * size[1] * 12 > 256 * 1024**2:
            raise RuntimeError("Intermediate resized image exceeds the 256 MiB preparation budget.")
        resized = image.resize(size, resampling)
        from EmbeddingsArea.preparation_contract import CROP_POSITIONS
        row, column = divmod(CROP_POSITIONS.index(prep.get("crop_position", "center")), 3)
        left, top = ((resized.width - width) * column // 2, (resized.height - height) * row // 2)
        return resized.crop((left, top, left + width, top + height))
    resized = image.resize((max(1, size[0]), max(1, size[1])), resampling)
    canvas = image_module.new("RGB", (width, height), tuple(prep.get("alpha_background", [0, 0, 0])))
    canvas.paste(resized, ((width - resized.width) // 2, (height - resized.height) // 2))
    return canvas


def _image_array(image: Any, prep: dict[str, Any]) -> np.ndarray:
    value = np.asarray(image, dtype=np.float32) * float(prep["rescale"])
    mean = np.asarray(prep["mean"], dtype=np.float32)
    std = np.asarray(prep["std"], dtype=np.float32)
    value = (value - mean) / std
    if str(prep["layout"]) in {"NCHW", "BTCHW"}:
        value = np.transpose(value, (2, 0, 1))
    return value


def _prepare_audio(path: Path, prep: dict[str, Any]) -> tuple[np.ndarray, int]:
    try:
        import av
    except Exception as exc:
        raise RuntimeError("PyAV is required for audio model preparation.") from exc
    rate, count = int(prep["sample_rate"]), int(prep["sample_count"])
    result = np.zeros(count, dtype=np.float32)
    valid = 0
    decoded = 0
    with av.open(str(path), mode="r") as container:
        streams = list(container.streams.audio)
        if not streams:
            raise RuntimeError(f"Audio file has no audio stream: {path.name}")
        stream = streams[0]
        start = _excerpt_start(_stream_duration_seconds(container, stream), count / rate,
                               str(prep.get("excerpt", "beginning")), float(prep.get("start_seconds", 0)))
        first = round(start * rate)
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=rate)

        def consume(frames):
            nonlocal decoded, valid
            for converted in frames:
                samples = converted.to_ndarray().reshape(-1)
                offset = max(0, first - decoded)
                length = min(max(0, len(samples) - offset), count - valid)
                if length:
                    result[valid:valid + length] = samples[offset:offset + length]
                    valid += length
                decoded += len(samples)

        # Decode continuously from the stream beginning so compressed-frame seek
        # rounding cannot shift the selected sample boundary. Retain only output.
        for frame in container.decode(stream):
            if prep["channels"] != "mono":
                raw = frame.to_ndarray()
                channels = len(frame.layout.channels)
                raw = raw.reshape(channels, -1) if frame.format.is_planar else raw.reshape(-1, channels).T
                index = int(prep["channels"])
                if index >= channels:
                    raise RuntimeError(f"Audio channel {index} is unavailable.")
                samples = raw[index].astype(np.float32)
                if np.issubdtype(raw.dtype, np.integer):
                    info = np.iinfo(raw.dtype)
                    if info.min == 0:
                        samples -= (info.max + 1) / 2
                    samples /= float(max(abs(info.min), (info.max + 1) / 2 if info.min == 0 else info.max))
                selected = av.AudioFrame.from_ndarray(np.ascontiguousarray(samples[None, :]), format="fltp", layout="mono")
                selected.sample_rate = frame.sample_rate
                frame = selected
            frame.pts = None
            consume(resampler.resample(frame))
            if valid == count:
                break
        if valid < count:
            consume(resampler.resample(None))
    if not valid:
        raise RuntimeError(f"Audio excerpt starts beyond available media or cannot be decoded: {path.name}")
    return result, valid


def _prepare_video(path: Path, prep: dict[str, Any]) -> np.ndarray:
    try:
        import av
        from PIL import Image
    except Exception as exc:
        raise RuntimeError("PyAV and Pillow are required for video model preparation.") from exc
    frame_count, excerpt = int(prep["frame_count"]), float(prep["duration_seconds"])
    with av.open(str(path), mode="r") as container:
        streams = list(container.streams.video)
        if not streams:
            raise RuntimeError(f"Video file has no video stream: {path.name}")
        stream = streams[0]
        duration = _stream_duration_seconds(container, stream)
        start = _excerpt_start(duration, excerpt, str(prep.get("excerpt") or "beginning"), float(prep.get("start_seconds", 0)))
        targets = np.linspace(start, start + excerpt, frame_count, endpoint=False)
        if start:
            container.seek(int(start * 1_000_000), backward=True, any_frame=False)
        selected: list[np.ndarray] = []
        target_index = 0
        last: np.ndarray | None = None
        image_prep = dict(prep)
        image_prep["layout"] = "NCHW" if str(prep["layout"]) in {"BTCHW", "BCTHW"} else "NHWC"
        for frame in container.decode(stream):
            seconds = float(frame.time or 0.0)
            if seconds < start:
                continue
            image = frame.to_image().convert("RGB")
            image = _resize_image(image, image_prep, Image)
            last = _image_array(image, image_prep)
            while target_index < len(targets) and seconds >= targets[target_index]:
                selected.append(last.copy())
                target_index += 1
            if target_index >= len(targets):
                break
    if last is None:
        raise RuntimeError(f"Video file could not be decoded: {path.name}")
    while len(selected) < frame_count:
        selected.append(last.copy())
    value = np.stack(selected[:frame_count])
    if str(prep["layout"]) == "BCTHW":
        value = np.transpose(value, (1, 0, 2, 3))
    return value


def _stream_duration_seconds(container: Any, stream: Any) -> float | None:
    if getattr(stream, "duration", None) is not None and getattr(stream, "time_base", None) is not None:
        return float(stream.duration * stream.time_base)
    if getattr(container, "duration", None) is not None:
        return float(container.duration) / 1_000_000
    return None


def _excerpt_start(duration: float | None, excerpt: float, mode: str, custom_start: float = 0.0) -> float:
    if mode in {"center", "end"}:
        if duration is None or duration <= 0:
            raise RuntimeError("Centered/end temporal excerpts require reliable media duration metadata.")
        return max(0.0, (duration - excerpt) / (2 if mode == "center" else 1))
    start = custom_start if mode == "custom" else 0.0
    if start < 0 or (duration is not None and start >= duration):
        raise RuntimeError("Excerpt starts beyond available media.")
    return start
