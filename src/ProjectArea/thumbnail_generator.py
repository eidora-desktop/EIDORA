from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
import datetime as dt
import json
import logging
import os
from pathlib import Path
import subprocess
import threading
import time
from uuid import uuid4

from PIL import Image, ImageOps

from ProjectArea.thumbnail_images import write_thumbnail_sizes
from shared.internal_workers import internal_worker_command, read_worker_result

from DiscoveryArea.thumbnails.repository import (
    DEFAULT_THUMBNAIL_SIZES,
    ThumbnailVariant,
    ThumbnailVariantItem,
    ThumbnailVariantRepository,
)


ProgressCallback = Callable[[str, int, int, str], None]
CancellationCallback = Callable[[], bool]
logger = logging.getLogger(__name__)


class ThumbnailGenerationCancelled(RuntimeError):
    def __init__(self, variant_id: str) -> None:
        super().__init__("Thumbnail generation was aborted.")
        self.variant_id = variant_id


@dataclass(frozen=True)
class ThumbnailGenerationInput:
    path: Path
    metadata_id: int
    media_type: str = "image"


@dataclass(frozen=True)
class ThumbnailGenerationResult:
    variant_id: str
    label: str
    generated_files: int
    failed_files: int
    missing_files: int = 0
    errors: tuple[str, ...] = ()


class _InvalidSource(RuntimeError):
    def __init__(self, path: Path, cause: Exception, *, missing: bool = False) -> None:
        super().__init__(f"{path}: {type(cause).__name__}: {cause}")
        self.missing = missing


def next_thumbnail_variant_label(repo: ThumbnailVariantRepository, base: str | None = "thumbnails") -> str:
    existing = {variant.label.casefold() for variant in repo.list_variants()}
    normalized = str(base or "").strip() or "thumbnails"
    candidate = normalized
    index = 2
    while candidate.casefold() in existing:
        candidate = f"{normalized} {index}"
        index += 1
    return candidate


def create_thumbnail_variant(
    repo: ThumbnailVariantRepository,
    *,
    label: str,
    source_dir: str,
    identifier_column: str,
    inputs: Iterable[ThumbnailGenerationInput],
    resize_mode: str = "original_aspect",
    progress_callback: ProgressCallback | None = None,
    should_cancel: CancellationCallback | None = None,
    source_id: str = "",
    source_media_type: str = "image",
    video_frame_mode: str | None = None,
    input_kind: str = "",
    input_label: str = "",
    variant_id: str | None = None,
) -> ThumbnailGenerationResult:
    variant_id = str(variant_id or uuid4().hex).strip()
    if not variant_id:
        raise ValueError("Thumbnail variant ID cannot be empty.")
    normalized_label = label.strip() or next_thumbnail_variant_label(repo)
    normalized_resize = str(resize_mode).strip().lower()
    if normalized_resize not in {"original_aspect", "square_cropped", "square", "square_squeezed"}:
        raise ValueError(f"Unsupported thumbnail resize mode: {resize_mode}")
    normalized_media_type = str(source_media_type or "image").strip().lower()
    if normalized_media_type not in {"image", "video"}:
        raise ValueError(f"Unsupported thumbnail media type: {source_media_type}")
    normalized_frame_mode = str(video_frame_mode or "middle").strip().lower()
    if normalized_frame_mode not in {"first", "middle", "last", "random"}:
        raise ValueError(f"Unsupported video frame mode: {video_frame_mode}")
    stopped = threading.Event()

    def check_cancelled() -> None:
        if stopped.is_set() or (should_cancel is not None and should_cancel()):
            raise ThumbnailGenerationCancelled(variant_id)

    matched: list[ThumbnailGenerationInput] = []
    seen_ids: set[int] = set()
    for item in inputs:
        check_cancelled()
        metadata_id = int(item.metadata_id)
        if metadata_id in seen_ids:
            raise ValueError(f"Duplicate thumbnail metadata ID: {metadata_id}")
        seen_ids.add(metadata_id)
        item_media_type = str(item.media_type or normalized_media_type).strip().lower()
        if item_media_type != normalized_media_type:
            raise ValueError("A thumbnail variant cannot mix image and video inputs.")
        matched.append(ThumbnailGenerationInput(Path(item.path), metadata_id, item_media_type))
    check_cancelled()
    if not matched:
        raise RuntimeError("No linked media files are available for thumbnail creation.")

    entries: list[ThumbnailVariantItem] = []
    errors: list[str] = []
    done = failed = missing = 0
    worker_limit = 2 if normalized_media_type == "video" else 8
    workers = min(worker_limit, max(1, os.cpu_count() or 4), len(matched))
    report_every = max(1, (len(matched) + 99) // 100)
    last_reported = 0
    last_reported_at = time.monotonic()
    current_file = ""
    _emit(progress_callback, "start", len(matched), 0, "Generating thumbnails…")

    with repo.stage_variant(variant_id) as staging:
        # Waiting for workers precedes staging cleanup, including on cancellation.
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="thumb-gen") as pool:
            input_iterator = iter(matched)
            pending = set()

            def submit_next() -> None:
                nonlocal current_file
                item = next(input_iterator, None)
                if item is not None:
                    current_file = item.path.name
                    _emit(
                        progress_callback,
                        "message",
                        done,
                        failed + missing,
                        f"Processing {current_file}…",
                    )
                    processor = _process_video if normalized_media_type == "video" else _process_image
                    pending.add(pool.submit(
                        processor,
                        repo,
                        staging,
                        variant_id,
                        item,
                        normalized_resize,
                        check_cancelled,
                        normalized_frame_mode,
                    ))

            try:
                for _ in range(min(len(matched), workers * 2)):
                    submit_next()
                while pending:
                    check_cancelled()
                    completed, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                    check_cancelled()
                    for future in completed:
                        done += 1
                        try:
                            entries.extend(future.result())
                        except _InvalidSource as exc:
                            missing += int(exc.missing)
                            failed += int(not exc.missing)
                            logger.warning("Thumbnail source skipped: %s", exc)
                            if len(errors) < 20:
                                errors.append(str(exc))
                        submit_next()
                    now = time.monotonic()
                    if done == len(matched) or done - last_reported >= report_every or now - last_reported_at >= 1.0:
                        detail = f" Current file: {current_file}." if current_file and done < len(matched) else ""
                        _emit(progress_callback, "step", done, failed + missing,
                              f"Processed {done}/{len(matched)} files ({failed + missing} skipped).{detail}")
                        last_reported = done
                        last_reported_at = now
            except BaseException:
                stopped.set()
                for future in pending:
                    future.cancel()
                raise

        check_cancelled()
        if not entries:
            raise RuntimeError(f"No thumbnails created: {failed} failed, {missing} missing.\n" + "\n".join(errors))
        generated = done - failed - missing
        variant = ThumbnailVariant(
            variant_id=variant_id,
            label=normalized_label,
            source_dir=str(source_dir),
            identifier_column=identifier_column,
            sizes=DEFAULT_THUMBNAIL_SIZES,
            created_at=dt.datetime.now().isoformat(timespec="seconds"),
            resize_mode=normalized_resize,
            source_id=str(source_id).strip(),
            source_media_type=normalized_media_type,
            video_frame_mode=normalized_frame_mode if normalized_media_type == "video" else "",
            generated_files=generated,
            failed_files=failed,
            missing_files=missing,
            errors=tuple(errors),
            input_kind=str(input_kind).strip(),
            input_label=str(input_label).strip(),
        )
        _emit(progress_callback, "index", len(entries), 0, "Saving the thumbnail index…")
        check_cancelled()
        # This is the completion boundary. Cancellation during commit does not
        # turn an already published result into an aborted job.
        repo.publish_variant(variant, entries, staging)

    _emit(progress_callback, "complete", generated, failed + missing, "Thumbnail variant is ready.")
    return ThumbnailGenerationResult(variant_id, normalized_label, generated, failed, missing, tuple(errors))


def _process_image(
    repo: ThumbnailVariantRepository,
    staging: Path,
    variant_id: str,
    item: ThumbnailGenerationInput,
    resize_mode: str,
    check_cancelled: Callable[[], None],
    _video_frame_mode: str = "middle",
) -> list[ThumbnailVariantItem]:
    check_cancelled()
    try:
        with Image.open(item.path) as source:
            ImageOps.exif_transpose(source, in_place=True)
            image = source.convert("RGB")
    except FileNotFoundError as exc:
        raise _InvalidSource(item.path, exc, missing=True) from exc
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise _InvalidSource(item.path, exc) from exc

    source_path = str(item.path.resolve())
    with image:
        outputs = _output_paths(repo, staging, variant_id, item.metadata_id)
        check_cancelled()
        # Storage failures abort the job; they are not bad input files.
        write_thumbnail_sizes(image, outputs, resize_mode)
    return [
            ThumbnailVariantItem(
                variant_id=variant_id, metadata_id=item.metadata_id, size_px=size_px,
                relative_path=str(repo.relative_variant_path(variant_id, size_px, item.metadata_id)),
                mtime_ns=output_path.stat().st_mtime_ns,
                status="ok", source_path=source_path,
            )
            for size_px, output_path in outputs.items()
        ]


def _output_paths(
    repo: ThumbnailVariantRepository,
    staging: Path,
    variant_id: str,
    metadata_id: int,
) -> dict[int, Path]:
    return {
        size_px: staging
        / repo.relative_variant_path(variant_id, size_px, metadata_id).relative_to(
            Path("variants") / variant_id
        )
        for size_px in DEFAULT_THUMBNAIL_SIZES
    }


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _process_video(
    repo: ThumbnailVariantRepository,
    staging: Path,
    variant_id: str,
    item: ThumbnailGenerationInput,
    resize_mode: str,
    check_cancelled: Callable[[], None],
    video_frame_mode: str,
) -> list[ThumbnailVariantItem]:
    check_cancelled()
    if not item.path.exists():
        raise _InvalidSource(item.path, FileNotFoundError(item.path), missing=True)
    outputs = _output_paths(repo, staging, variant_id, item.metadata_id)
    payload = {
        "source_path": str(item.path),
        "outputs": {str(size): str(path) for size, path in outputs.items()},
        "resize_mode": resize_mode,
        "frame_mode": video_frame_mode,
        "seed": f"{variant_id}:{item.metadata_id}",
    }
    # Each concurrent item owns its reply. Remove it before publishing staging.
    result_path = staging / f".video-result-{uuid4().hex}.json"
    try:
        process = subprocess.Popen(
            internal_worker_command("video-thumbnail", json.dumps(payload, separators=(",", ":")), result_path),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        started = time.monotonic()
        try:
            while process.poll() is None:
                check_cancelled()
                if time.monotonic() - started >= 120.0:
                    _stop_process(process)
                    raise _InvalidSource(item.path, TimeoutError("video decoding exceeded 120 seconds"))
                time.sleep(0.05)
            _stdout, stderr = process.communicate()
        except BaseException:
            _stop_process(process)
            raise

        try:
            result = read_worker_result(result_path)
        except RuntimeError as exc:
            detail = stderr.strip() or str(exc)
            raise _InvalidSource(item.path, RuntimeError(
                f"{detail}; worker exited with code {process.returncode} (0x{process.returncode & 0xffffffff:08x})"
            )) from exc
    finally:
        result_path.unlink(missing_ok=True)
    if not result.get("ok"):
        error = RuntimeError(str(result.get("error") or stderr.strip() or "video extraction failed"))
        if result.get("kind") == "output":
            raise error
        raise _InvalidSource(item.path, error, missing=result.get("kind") == "missing")
    if process.returncode != 0:
        raise _InvalidSource(item.path, RuntimeError(
            f"worker exited with code {process.returncode} (0x{process.returncode & 0xffffffff:08x})"
        ))
    if any(not path.is_file() for path in outputs.values()):
        raise RuntimeError(f"Video thumbnail worker did not write every requested size for {item.path}")

    timestamp = result.get("timestamp_seconds")
    ordinal = result.get("frame_ordinal")
    source_path = str(item.path.resolve())
    return [
        ThumbnailVariantItem(
            variant_id=variant_id,
            metadata_id=item.metadata_id,
            size_px=size_px,
            relative_path=str(repo.relative_variant_path(variant_id, size_px, item.metadata_id)),
            mtime_ns=output_path.stat().st_mtime_ns,
            status="ok",
            source_path=source_path,
            selected_frame_seconds=float(timestamp) if timestamp is not None else None,
            selected_frame_ordinal=int(ordinal) if ordinal is not None else None,
        )
        for size_px, output_path in outputs.items()
    ]


def _emit(callback: ProgressCallback | None, event: str, value1: int, value2: int, message: str) -> None:
    if callback is not None:
        try:
            callback(event, value1, value2, message)
        except Exception:
            logger.exception("Thumbnail progress callback failed")
