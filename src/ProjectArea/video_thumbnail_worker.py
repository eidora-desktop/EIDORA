"""Decode one video still in an isolated process and write staged WebPs."""

from __future__ import annotations

from fractions import Fraction
from contextlib import redirect_stderr, redirect_stdout
import faulthandler
import hashlib
import json
from pathlib import Path
import sys
import traceback
from typing import Any, Iterable

from PIL import Image, ImageOps

from ProjectArea.thumbnail_images import write_thumbnail_sizes


FRAME_MODES = frozenset({"first", "middle", "last", "random"})


class VideoSourceError(RuntimeError):
    pass


def _ordinary_video_streams(container: Any) -> list[Any]:
    streams = []
    for stream in container.streams.video:
        disposition = getattr(stream, "disposition", None)
        if _disposition_enabled(disposition, "attached_pic"):
            continue
        streams.append(stream)
    return streams


def _select_stream(container: Any) -> Any:
    streams = _ordinary_video_streams(container)
    if not streams:
        raise VideoSourceError("The container has no decodable video stream.")
    for stream in streams:
        if _disposition_enabled(getattr(stream, "disposition", None), "default"):
            return stream
    return streams[0]


def _disposition_enabled(disposition: Any, name: str) -> bool:
    if disposition is None:
        return False
    flag = getattr(type(disposition), name, None)
    value = getattr(disposition, "value", 0)
    flag_value = getattr(flag, "value", 0)
    return bool(value & flag_value)


def _frame_seconds(frame: Any, stream: Any) -> float | None:
    if frame.pts is None:
        return None
    time_base = getattr(frame, "time_base", None) or getattr(stream, "time_base", None)
    if time_base is None:
        return None
    return float(frame.pts * time_base)


def _duration_seconds(container: Any, stream: Any) -> float | None:
    if getattr(stream, "duration", None) is not None and getattr(stream, "time_base", None) is not None:
        duration = float(stream.duration * stream.time_base)
        if duration > 0:
            return duration
    duration = getattr(container, "duration", None)
    if duration is not None:
        # PyAV container durations are expressed in AV_TIME_BASE units.
        value = float(duration) / 1_000_000.0
        if value > 0:
            return value
    return None


def _decode_frames(container: Any, stream: Any) -> Iterable[tuple[int, Any, float | None]]:
    for ordinal, frame in enumerate(container.decode(stream)):
        yield ordinal, frame, _frame_seconds(frame, stream)


def _seek_seconds(container: Any, stream: Any, seconds: float) -> None:
    time_base = getattr(stream, "time_base", None)
    if time_base is None:
        raise VideoSourceError("The video stream has no usable time base.")
    container.seek(max(0, int(seconds / float(time_base))), stream=stream, backward=True)


def _choose_at_time(container: Any, stream: Any, target: float) -> tuple[Any, float | None, int]:
    # Decode from the preceding keyframe and retain the frame displayed at the
    # requested presentation time rather than returning the keyframe itself.
    _seek_seconds(container, stream, target)
    previous: tuple[Any, float | None, int] | None = None
    for ordinal, frame, seconds in _decode_frames(container, stream):
        if previous is None:
            previous = (frame, seconds, ordinal)
        if seconds is None:
            previous = (frame, seconds, ordinal)
            continue
        if seconds > target:
            break
        previous = (frame, seconds, ordinal)
    if previous is None:
        raise VideoSourceError("No video frame could be decoded.")
    return previous


def _sequential_choice(container: Any, stream: Any, mode: str, seed: str) -> tuple[Any, float | None, int]:
    frame_count = 0
    for _ordinal, _frame, _seconds in _decode_frames(container, stream):
        frame_count += 1
    if not frame_count:
        raise VideoSourceError("No video frame could be decoded.")
    if mode == "first":
        wanted = 0
    elif mode == "last":
        wanted = frame_count - 1
    elif mode == "random":
        wanted = int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big") % frame_count
    else:
        wanted = (frame_count - 1) // 2
    container.seek(0, stream=stream, backward=True)
    for ordinal, frame, seconds in _decode_frames(container, stream):
        if ordinal == wanted:
            return frame, seconds, ordinal
    raise VideoSourceError("The selected video frame could not be decoded on a second pass.")


def _select_frame(container: Any, stream: Any, mode: str, seed: str) -> tuple[Any, float | None, int]:
    if mode == "first":
        for ordinal, frame, seconds in _decode_frames(container, stream):
            return frame, seconds, ordinal
        raise VideoSourceError("No video frame could be decoded.")

    duration = _duration_seconds(container, stream)
    if duration is None:
        return _sequential_choice(container, stream, mode, seed)
    if mode == "random":
        digest = int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big")
        target = duration * (digest / float(2**64))
    elif mode == "middle":
        target = duration / 2.0
    else:
        target = max(0.0, duration - min(1.0, duration * 0.02))

    if mode != "last":
        try:
            return _choose_at_time(container, stream, target)
        except (OSError, ValueError, VideoSourceError):
            container.seek(0, stream=stream, backward=True)
            return _sequential_choice(container, stream, mode, seed)

    # Seeking near EOF keeps long clips fast. Decode through EOF, then retry
    # progressively earlier because duration metadata is often imprecise.
    for fraction in (0.98, 0.9, 0.5, 0.0):
        last: tuple[Any, float | None, int] | None = None
        try:
            _seek_seconds(container, stream, duration * fraction)
            for ordinal, frame, seconds in _decode_frames(container, stream):
                last = (frame, seconds, ordinal)
        except (OSError, ValueError):
            last = None
        if last is not None:
            return last
    raise VideoSourceError("The final video frame could not be decoded.")


def _display_image(frame: Any, stream: Any) -> Image.Image:
    image = frame.to_image().convert("RGB")
    ratio = getattr(frame, "sample_aspect_ratio", None) or getattr(stream, "sample_aspect_ratio", None)
    try:
        ratio_value = float(Fraction(ratio)) if ratio is not None else 1.0
    except (TypeError, ValueError, ZeroDivisionError):
        ratio_value = 1.0
    if ratio_value > 0 and abs(ratio_value - 1.0) > 0.001:
        image = image.resize((max(1, round(image.width * ratio_value)), image.height), Image.Resampling.LANCZOS)

    metadata = getattr(stream, "metadata", {}) or {}
    try:
        rotation = int(round(float(getattr(frame, "rotation", 0) or metadata.get("rotate", 0)))) % 360
    except (TypeError, ValueError):
        rotation = 0
    if rotation:
        image = image.rotate(-rotation, expand=True)
    if str(metadata.get("mirror", "")).lower() in {"1", "true", "horizontal"}:
        image = ImageOps.mirror(image)
    return image


def process(payload: dict[str, Any]) -> dict[str, Any]:
    mode = str(payload.get("frame_mode", "middle")).lower()
    if mode not in FRAME_MODES:
        raise ValueError(f"Unsupported video frame mode: {mode}")
    try:
        import av
    except ImportError as exc:
        raise VideoSourceError("Video support is unavailable because PyAV is not installed.") from exc

    source = Path(str(payload["source_path"]))
    if not source.is_file():
        raise FileNotFoundError(source)
    with av.open(str(source), mode="r") as container:
        stream = _select_stream(container)
        try:
            stream.thread_type = "NONE"
            stream.codec_context.thread_count = 1
        except (AttributeError, ValueError):
            pass
        frame, seconds, ordinal = _select_frame(
            container,
            stream,
            mode,
            str(payload.get("seed", "")),
        )
        image = _display_image(frame, stream)
    try:
        outputs = {int(size): Path(path) for size, path in payload["outputs"].items()}
        with image:
            write_thumbnail_sizes(image, outputs, str(payload["resize_mode"]))
    except OSError as exc:
        return {"ok": False, "kind": "output", "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "timestamp_seconds": seconds, "frame_ordinal": ordinal}


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) == 4 and arguments[2] == "--diagnostics":
        # Windowed Windows bundles have no Python stdout/stderr. Write both
        # Python errors and native fault traces to an explicit audit artifact.
        with Path(arguments[3]).open("w", encoding="utf-8", buffering=1) as log:
            with redirect_stdout(log), redirect_stderr(log):
                faulthandler.enable(file=log)
                try:
                    print("Video thumbnail worker started", flush=True)
                    return _run_request(arguments[0], Path(arguments[1]))
                except Exception:
                    traceback.print_exc(file=log)
                    return 1
                finally:
                    faulthandler.disable()
    if len(arguments) != 2:
        print(json.dumps({"ok": False, "kind": "source", "error": "usage: video-thumbnail <request-json> <result.json> [--diagnostics <log-path>]"}))
        return 2
    return _run_request(arguments[0], Path(arguments[1]))


def _run_request(request: str, result_path: Path) -> int:
    try:
        result = process(json.loads(request))
    except FileNotFoundError as exc:
        result = {"ok": False, "kind": "missing", "error": str(exc)}
    except (VideoSourceError, OSError, ValueError, TypeError, KeyError) as exc:
        result = {"ok": False, "kind": "source", "error": f"{type(exc).__name__}: {exc}"}
    reply = json.dumps(result, separators=(",", ":"))
    result_path.write_text(reply, encoding="utf-8")
    print(reply)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
