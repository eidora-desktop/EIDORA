"""Decode bundled synthetic videos through the isolated thumbnail worker.

Never import PyAV here: Qt Multimedia is already loaded in the self-test host.
Fixture encoding belongs to the offline preparation script, not the GUI process.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
import subprocess

from PIL import Image

from shared.internal_workers import internal_worker_command, read_worker_result


_CODECS = ("h264", "hevc", "vp9", "mpeg2", "mjpeg", "ffv1")
_FIXTURES = Path(__file__).resolve().parent / "resources" / "video-audit"


def _diagnostics(path: Path) -> str:
    if not path.is_file():
        return "No worker log was written."
    return path.read_text(encoding="utf-8", errors="replace")[-4000:]


def audit_video_thumbnail_runtime(
    root: Path,
    *,
    checkpoint: Callable[[str], None] | None = None,
    diagnostic_prefix: Path | None = None,
) -> dict[str, object]:
    root.mkdir(parents=True, exist_ok=True)
    diagnostic_prefix = diagnostic_prefix or root / "video"
    diagnostic_prefix.parent.mkdir(parents=True, exist_ok=True)
    results: dict[str, object] = {}
    for codec_label in _CODECS:
        if checkpoint is not None:
            checkpoint(f"video-thumbnail:{codec_label}")
        source = _FIXTURES / f"{codec_label}.mkv"
        if not source.is_file():
            raise RuntimeError(f"Missing bundled video fixture for {codec_label}: {source}")
        log_path = diagnostic_prefix.parent / f"{diagnostic_prefix.name}.{codec_label}.log"
        log_path.unlink(missing_ok=True)
        result_path = root / f"{codec_label}.json"
        result_path.unlink(missing_ok=True)
        outputs = {str(size): str(root / codec_label / f"{size}.webp") for size in (32, 64, 128, 256)}
        for path in outputs.values():
            Path(path).unlink(missing_ok=True)
        request = json.dumps(
            {
                "source_path": str(source),
                "outputs": outputs,
                "resize_mode": "original_aspect",
                "frame_mode": "middle",
                "seed": codec_label,
            },
            separators=(",", ":"),
        )
        try:
            completed = subprocess.run(
                internal_worker_command("video-thumbnail", request, result_path, "--diagnostics", log_path),
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Video worker timed out for {codec_label} after 60 seconds. "
                f"Log: {log_path}\n{_diagnostics(log_path)}"
            ) from exc
        if completed.returncode != 0:
            raise RuntimeError(
                f"Video worker failed for {codec_label}: exit {completed.returncode} "
                f"(0x{completed.returncode & 0xffffffff:08x}). "
                f"Log: {log_path}\n{_diagnostics(log_path)}\n"
                f"{completed.stderr.strip() or completed.stdout.strip()}"
            )
        try:
            reply = read_worker_result(result_path)
        except RuntimeError as exc:
            raise RuntimeError(f"Video worker returned no valid result for {codec_label}: {exc}") from exc
        if not reply["ok"]:
            raise RuntimeError(f"Video worker failed for {codec_label}: {reply.get('error')}")
        missing = [path for path in outputs.values() if not Path(path).is_file()]
        if missing:
            raise RuntimeError(f"Video worker omitted outputs for {codec_label}: {missing}")
        for size, path in outputs.items():
            with Image.open(path) as image:
                image.load()
                # All fixtures are 3:2 and their middle frame is green.
                with image.convert("RGB") as rgb:
                    red, green, blue = rgb.getpixel((image.width // 2, image.height // 2))
                width = min(int(size), 96)
                if (
                    image.format != "WEBP"
                    or image.size != (width, round(width * 2 / 3))
                    or green < max(red, blue) + 80
                ):
                    raise RuntimeError(f"Invalid middle-frame thumbnail for {codec_label}: {path}")
        results[codec_label] = {"fixture": source.name, "sizes": [32, 64, 128, 256]}
    return results
