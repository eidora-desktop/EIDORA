"""Validate the small, portable contract for generated media previews."""
from __future__ import annotations

import hashlib
import math

PREVIEW_KINDS = {"audio_clip": "Audio clips", "video_clip": "Video clips", "animated_gif": "Animated GIFs"}
LOOP_MODES = {"once": "Play once", "forever": "Forever", "ping_pong": "Ping-Pong"}
VIDEO_QUALITY_CRF = {"compact": 32, "balanced": 26, "high": 20}
POSITIONS = {"first": "Beginning", "middle": "Middle", "last": "End", "random": "Random"}


def clip_parameters(parameters: dict, media_type: str) -> dict:
    kind = str(parameters.get("preview_kind", ""))
    allowed = {"audio_clip"} if media_type == "audio" else {"video_clip", "animated_gif"} if media_type == "video" else set()
    if kind not in allowed:
        raise ValueError("Choose a preview type supported by this media source.")
    duration = float(parameters.get("duration", 5))
    if not math.isfinite(duration) or not 1 <= duration <= 30:
        raise ValueError("Clip duration must be between 1 and 30 seconds.")
    position = str(parameters.get("position", "middle"))
    loop = str(parameters.get("loop", "forever")) if kind == "animated_gif" else "once"
    if position not in POSITIONS or loop not in LOOP_MODES:
        raise ValueError("Unsupported clip position or loop mode.")
    fps = int(parameters.get("gif_fps", 12))
    colors = int(parameters.get("gif_colors", 128))
    dither = str(parameters.get("gif_dither", "none"))
    if not 1 <= fps <= 24 or colors not in {64, 128, 256} or dither not in {"none", "floyd_steinberg"}:
        raise ValueError("Unsupported GIF compression settings.")
    video_quality = str(parameters.get("video_quality", "compact"))
    video_fps = int(parameters.get("video_fps", 24))
    max_edge = int(parameters.get("max_edge", 256))
    bitrate = int(parameters.get("audio_bitrate", 48))
    channels = str(parameters.get("audio_channels", "mono"))
    seed = int(parameters.get("random_seed", 0))
    if video_quality not in VIDEO_QUALITY_CRF or not 1 <= video_fps <= 24:
        raise ValueError("Unsupported video quality or frame rate.")
    if max_edge not in {128, 192, 256}:
        raise ValueError("Maximum edge must be 128, 192, or 256 pixels.")
    if bitrate not in {32, 48, 64, 96, 128} or channels not in {"mono", "stereo"}:
        raise ValueError("Unsupported audio bitrate or channel layout.")
    if not 0 <= seed <= 2147483647:
        raise ValueError("Random seed must be between 0 and 2147483647.")
    result = dict(preview_kind=kind, duration=duration, position=position,
                include_audio=bool(parameters.get("include_audio", True)),
                gif_fps=fps, gif_colors=colors, gif_dither=dither,
                video_quality=video_quality, video_fps=video_fps, max_edge=max_edge,
                audio_bitrate=bitrate, audio_channels=channels, random_seed=seed,
                encoding_profile="configurable-v1")
    if kind == "animated_gif":
        result["loop"] = loop
    return result


def excerpt_start(duration: float, length: float, position: str, seed: str) -> float:
    available = max(0.0, duration - length)
    if position == "first":
        return 0.0
    if position == "last":
        return available
    if position == "random":
        fraction = int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], "big") / 2**64
        return available * fraction
    return available / 2


def compression_description(parameters: dict) -> str:
    kind = parameters.get("preview_kind")
    channels = parameters.get("audio_channels", "mono")
    channels_label = "stereo when available" if channels == "stereo" else "mono"
    audio = f"AAC · {channels_label} · {parameters.get('audio_bitrate', 48)} kbit/s"
    if kind == "audio_clip":
        return audio
    edge = parameters.get("max_edge", 256)
    if kind == "animated_gif":
        dither = "Dithering on" if parameters.get("gif_dither") == "floyd_steinberg" else "Dithering off"
        return f"{edge} px maximum · {parameters.get('gif_fps', 12)} fps maximum · {parameters.get('gif_colors', 128)} colors · {dither}"
    quality = str(parameters.get("video_quality", "compact")).capitalize()
    audio = f"Audio included when available · {audio}" if parameters.get("include_audio", True) else "Muted"
    return f"{edge} px maximum · {parameters.get('video_fps', 24)} fps maximum · {quality} quality\n{audio}"
