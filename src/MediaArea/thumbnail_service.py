from __future__ import annotations

from MediaArea.thumbnail_generator import (
    CancellationCallback,
    ProgressCallback,
    ThumbnailGenerationCancelled,
    ThumbnailGenerationInput,
    ThumbnailGenerationResult,
    create_thumbnail_variant,
)
from DiscoveryArea.thumbnails.repository import ThumbnailVariantRepository
from EmbeddingsArea.domain import MediaContentType, MediaSourceSummary
from EmbeddingsArea.repository import MediaLinkRepository, MediaRepository
from pathlib import Path
from MediaArea.preview_names import default_preview_name
from MediaArea.thumbnail_pause import ThumbnailPause
from MediaArea.thumbnail_options import image_parameters


def create_source_thumbnails(
    repo: ThumbnailVariantRepository,
    media_repository: MediaRepository,
    link_repository: MediaLinkRepository,
    summary: MediaSourceSummary,
    *,
    label: str,
    resize_mode: str,
    video_frame_mode: str | None = None,
    preview_parameters: dict | None = None,
    progress_callback: ProgressCallback | None = None,
    should_cancel: CancellationCallback | None = None,
    pause: ThumbnailPause | None = None,
    variant_id: str | None = None,
) -> ThumbnailGenerationResult:
    """Resolve canonical metadata identities without passing UI state to workers."""
    def check_cancelled() -> None:
        if pause is not None:
            pause.checkpoint()
        if should_cancel is not None and should_cancel():
            raise ThumbnailGenerationCancelled("")

    check_cancelled()
    if progress_callback is not None:
        progress_callback("prepare", 0, 0, "Finding inputs for Preview Assets…")
    source_content_type = getattr(summary.source, "content_type", MediaContentType.IMAGE)
    from project.item_repository import ItemRepository
    from project.domain import ProjectStoragePaths

    metadata_path = ProjectStoragePaths(repo.thumbnails_root.parent).metadata_database
    inputs_by_metadata = {}
    if metadata_path.exists():
        item_ids = ItemRepository(metadata_path).active_ids()
        import time
        from concurrent.futures import CancelledError
        last_report = float("-inf")
        def resolved_progress(count, total):
            nonlocal last_report
            now = time.monotonic()
            if progress_callback is not None and (now - last_report >= 0.25 or count == total):
                progress_callback("prepare", count, total, f"Finding inputs for Preview Assets: {count:,}/{total:,} Items…")
                last_report = now
        try:
            resolved = media_repository.linked_assets_for_metadata_ids(item_ids,
                source_ids=(summary.source.id,), should_cancel=should_cancel,
                progress_callback=resolved_progress)
        except CancelledError as exc:
            raise ThumbnailGenerationCancelled("") from exc
        for item_id, asset in resolved.items():
            check_cancelled()
            if asset.content_type == source_content_type:
                inputs_by_metadata[item_id] = ThumbnailGenerationInput(
                    asset.absolute_path, item_id, asset.content_type.value, str(asset.id),
                    asset.text_record_key if source_content_type == MediaContentType.TEXT else "")
    if source_content_type == MediaContentType.TEXT:
        from MediaArea.text_generator import create_text_variant
        return create_text_variant(repo, label=label, source_dir=str(summary.source.path),
            identifier_column=summary.metadata_column or "metadata_row_id", inputs=inputs_by_metadata.values(),
            source_id=summary.source.id, parameters=preview_parameters or {},
            progress_callback=progress_callback, should_cancel=should_cancel, pause=pause,
            variant_id=variant_id, input_label=summary.source.label)
    if preview_parameters and preview_parameters.get("preview_kind") in {"audio_clip", "video_clip", "animated_gif"}:
        from MediaArea.clip_generator import create_clip_variant
        return create_clip_variant(repo, label=label, source_dir=str(summary.source.path),
            identifier_column=summary.metadata_column or "metadata_row_id", inputs=inputs_by_metadata.values(),
            source_id=summary.source.id, source_media_type=source_content_type.value,
            parameters=preview_parameters, progress_callback=progress_callback, should_cancel=should_cancel,
            pause=pause, variant_id=variant_id, input_label=summary.source.label)
    return create_thumbnail_variant(
        repo,
        label=label,
        source_dir=str(summary.source.path),
        identifier_column=summary.metadata_column or "metadata_row_id",
        inputs=inputs_by_metadata.values(),
        resize_mode=resize_mode,
        **image_parameters(preview_parameters or {}),
        source_id=summary.source.id,
        source_media_type=source_content_type.value,
        video_frame_mode=video_frame_mode,
        input_kind="media_source",
        input_label=str(getattr(summary.source, "label", summary.source.id)),
        progress_callback=progress_callback,
        should_cancel=should_cancel,
        pause=pause,
        variant_id=variant_id,
    )


def create_artifact_thumbnails(
    repo: ThumbnailVariantRepository,
    source_variant_id: str,
    *,
    label: str,
    resize_mode: str,
    preview_parameters: dict | None = None,
    progress_callback: ProgressCallback | None = None,
    should_cancel: CancellationCallback | None = None,
    pause: ThumbnailPause | None = None,
    variant_id: str | None = None,
) -> ThumbnailGenerationResult:
    """Create a variant from the largest stored image for each canonical ID."""
    if pause is not None:
        pause.checkpoint()
    if should_cancel is not None and should_cancel():
        raise ThumbnailGenerationCancelled("")
    source = next(
        (candidate for candidate in repo.list_variants() if candidate.variant_id == source_variant_id),
        None,
    )
    if source is None:
        raise ValueError("The selected artifact no longer exists.")
    largest_size = max(source.sizes, default=0)
    if largest_size <= 0:
        raise ValueError("The selected artifact has no usable image sizes.")
    items = repo.resolve_items(source_variant_id, largest_size)
    inputs = [
        ThumbnailGenerationInput(
            path=repo.absolute_variant_path(source_variant_id, item.size_px, metadata_id),
            metadata_id=metadata_id,
            media_type="image",
        )
        for metadata_id, item in items.items()
    ]
    return create_thumbnail_variant(
        repo,
        label=label,
        source_dir=str(Path(repo.variants_root) / source_variant_id),
        identifier_column=source.identifier_column,
        inputs=inputs,
        resize_mode=resize_mode,
        **image_parameters(preview_parameters or {}),
        source_id=source_variant_id,
        source_media_type="image",
        input_kind="artifact",
        input_label=source.label,
        progress_callback=progress_callback,
        should_cancel=should_cancel,
        pause=pause,
        variant_id=variant_id,
    )


def default_embedding_thumbnail_request(configuration, available_inputs, repo):
    """Use the completed embedding's visual source with preview defaults."""
    from uuid import uuid4
    from MediaArea.domain import ProcessInputKind, ProcessRequest
    from EmbeddingsArea.domain import EmbeddingRunState

    if configuration.run.state != EmbeddingRunState.COMPLETED or any(variant.preview_kind == "image" for variant in repo.list_variants()):
        return None
    source_ids = [binding.source_id for binding in configuration.input_bindings]
    if not source_ids:
        source_ids = configuration.run.selected_source_ids
    sources = {item.id: item for item in available_inputs if item.kind == ProcessInputKind.MEDIA_SOURCE and item.media_type in {"image", "video"}}
    source = next((sources[identity] for identity in source_ids if identity in sources), None)
    if source is None:
        return None
    return ProcessRequest(
        operation_id="create_thumbnails", input=source, output_id=uuid4().hex,
        output_name=default_preview_name(source.label, source.media_type),
        parameters={"resize_mode": "original_aspect",
                    **({"video_frame_mode": "middle"} if source.media_type == "video" else {})},
    )
