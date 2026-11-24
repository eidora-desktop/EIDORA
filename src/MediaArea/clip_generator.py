"""Generate compact clip sets through the existing staged preview repository."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
from uuid import uuid4

from DiscoveryArea.thumbnails.repository import ThumbnailVariant, ThumbnailVariantItem, PreviewClip
from MediaArea.clip_options import clip_parameters
from MediaArea.thumbnail_generator import ThumbnailGenerationCancelled, ThumbnailGenerationResult, _stop_process, _emit
from shared.internal_workers import internal_worker_command, read_worker_result


def create_clip_variant(repo, *, label, source_dir, identifier_column, inputs, source_id,
                        source_media_type, parameters, progress_callback=None, should_cancel=None,
                        pause=None, variant_id=None, input_label=""):
    parameters = clip_parameters(parameters, source_media_type)
    label = label.strip()
    if not label:
        raise ValueError("Enter a name for the Preview Assets.")
    variant_id = variant_id or uuid4().hex
    def check():
        if should_cancel and should_cancel():
            raise ThumbnailGenerationCancelled(variant_id)
    check()
    inputs = list(inputs)
    if not inputs:
        raise ValueError("No linked media files are available for creating Preview Assets.")
    if len({item.metadata_id for item in inputs}) != len(inputs):
        raise ValueError("Duplicate Item identity in Preview Assets.")
    if any(item.media_type != source_media_type for item in inputs):
        raise ValueError("Preview Assets must use the selected source's media type.")
    clips, items, errors = [], [], []
    failed = missing = 0
    reused = {}
    extension = {"audio_clip": ".m4a", "video_clip": ".mp4", "animated_gif": ".gif"}[parameters["preview_kind"]]
    _emit(progress_callback, "start", len(inputs), 0, "Generating Preview Assets…")
    with repo.stage_variant(variant_id) as staging:
        for done, item in enumerate(inputs, 1):
            if pause:
                pause.checkpoint()
            check()
            # Shared linked inputs retain distinct Item identities, with one encoded file.
            key = str(item.path.resolve())
            if key in reused:
                clip = replace(reused[key], metadata_id=item.metadata_id, source_asset_id=item.source_asset_id)
            else:
                identity = hashlib.sha256(key.encode()).hexdigest()[:24]
                directory = staging / identity
                directory.mkdir()
                output, poster = directory / ("clip" + extension), directory / "poster.webp"
                result_path, log_path = directory / "reply.json", directory / "worker.log"
                payload = dict(source=str(item.path), media_type=source_media_type, parameters=parameters,
                               seed=f"{source_id}:{item.source_asset_id or key}", output=str(output), poster=str(poster))
                with log_path.open("w") as log:
                    process = subprocess.Popen(internal_worker_command("preview-clip", json.dumps(payload), result_path),
                                               stdout=log, stderr=log)
                    started = time.monotonic()
                    try:
                        while process.poll() is None:
                            check()
                            if time.monotonic()-started > 180:
                                _stop_process(process)
                                break
                            time.sleep(0.05)
                    except BaseException:
                        _stop_process(process)
                        raise
                try:
                    result = read_worker_result(result_path)
                except RuntimeError:
                    result = dict(ok=False, kind="source", error=f"Clip worker failed or timed out (exit {process.returncode}).")
                if not result["ok"]:
                    if result.get("kind") in {"storage", "worker"}:
                        raise OSError(result["error"])
                    missing += result.get("kind") == "missing"
                    failed += result.get("kind") != "missing"
                    if len(errors) < 10:
                        errors.append(f"{item.path.name}: {result.get('error', 'Encoding failed')}"[:1000])
                    shutil.rmtree(directory)
                    _emit(progress_callback, "step", done, failed+missing, f"Creating previews: {done}/{len(inputs)}")
                    continue
                if process.returncode != 0 or not output.is_file() or not poster.is_file():
                    raise RuntimeError("Clip worker did not produce a complete preview.")
                result_path.unlink()
                log_path.unlink()
                prefix = Path("variants") / variant_id / identity
                clip = PreviewClip(variant_id, item.metadata_id, str(prefix/output.name), str(prefix/poster.name),
                                   item.source_asset_id, **{name: result[name] for name in
                                   ("start_seconds", "duration_seconds", "width", "height", "fps", "size_bytes")})
                reused[key] = clip
            clips.append(clip)
            poster = staging / Path(clip.poster_path).relative_to(Path("variants") / variant_id)
            items.append(ThumbnailVariantItem(variant_id, item.metadata_id, 256, clip.poster_path,
                         poster.stat().st_mtime_ns, "ok", str(item.path), clip.start_seconds))
            _emit(progress_callback, "step", done, failed+missing, f"Creating previews: {done}/{len(inputs)}")
        if pause:
            pause.checkpoint()
        check()
        if not clips:
            raise RuntimeError(f"No Preview Assets created: {failed} failed, {missing} missing.\n" + "\n".join(errors))
        variant = ThumbnailVariant(variant_id, label, source_dir, identifier_column, (256,),
            datetime.now().isoformat(timespec="seconds"), source_id=source_id,
            source_media_type=source_media_type, generated_files=len(clips), failed_files=failed,
            missing_files=missing, errors=tuple(errors), input_kind="media_source", input_label=input_label,
            preview_kind=parameters["preview_kind"], parameters=parameters,
            total_bytes=sum(path.stat().st_size for path in staging.rglob("*") if path.is_file()))
        _emit(progress_callback, "index", len(clips), 0, "Saving Preview Assets…")
        check()
        repo.publish_variant(variant, items, staging, clips=clips)
    _emit(progress_callback, "complete", len(clips), failed+missing, "Preview Assets are ready.")
    return ThumbnailGenerationResult(variant_id, label, len(clips), failed, missing, tuple(errors), variant.total_bytes)
