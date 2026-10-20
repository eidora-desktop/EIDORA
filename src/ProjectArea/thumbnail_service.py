from __future__ import annotations

from ProjectArea.thumbnail_generator import (
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


def create_source_thumbnails(
    repo: ThumbnailVariantRepository,
    media_repository: MediaRepository,
    link_repository: MediaLinkRepository,
    summary: MediaSourceSummary,
    *,
    label: str,
    resize_mode: str,
    video_frame_mode: str | None = None,
    progress_callback: ProgressCallback | None = None,
    should_cancel: CancellationCallback | None = None,
    variant_id: str | None = None,
) -> ThumbnailGenerationResult:
    """Resolve canonical metadata identities without passing UI state to workers."""
    def check_cancelled() -> None:
        if should_cancel is not None and should_cancel():
            raise ThumbnailGenerationCancelled("")

    check_cancelled()
    source_content_type = getattr(summary.source, "content_type", MediaContentType.IMAGE)
    assets = {}
    for asset in media_repository.ready_assets_for_sources((summary.source.id,)):
        check_cancelled()
        if asset.content_type == source_content_type and asset.content_type in {
            MediaContentType.IMAGE,
            MediaContentType.VIDEO,
        }:
            assets[asset.id] = asset
    check_cancelled()
    inputs_by_metadata: dict[int, ThumbnailGenerationInput] = {}
    for link in link_repository.list_links_for_source(summary.source.id):
        check_cancelled()
        asset = assets.get(link.media_asset_id)
        if asset is not None:
            # Preserve the deterministic first-linked-asset policy.
            inputs_by_metadata.setdefault(
                int(link.metadata_row_id),
                ThumbnailGenerationInput(
                    asset.absolute_path,
                    int(link.metadata_row_id),
                    asset.content_type.value,
                ),
            )
    from project.item_repository import ItemRepository
    from project.domain import ProjectStoragePaths

    metadata_path = ProjectStoragePaths(repo.thumbnails_root.parent).metadata_database
    if metadata_path.exists():
        identities = ItemRepository(metadata_path).item_ids_for_assets(assets)
        for asset_id, item_id in identities.items():
            asset = assets[asset_id]
            inputs_by_metadata.setdefault(item_id, ThumbnailGenerationInput(
                asset.absolute_path, item_id, asset.content_type.value,
            ))
    return create_thumbnail_variant(
        repo,
        label=label,
        source_dir=str(summary.source.path),
        identifier_column=summary.metadata_column or "metadata_row_id",
        inputs=inputs_by_metadata.values(),
        resize_mode=resize_mode,
        source_id=summary.source.id,
        source_media_type=source_content_type.value,
        video_frame_mode=video_frame_mode,
        input_kind="media_source",
        input_label=str(getattr(summary.source, "label", summary.source.id)),
        progress_callback=progress_callback,
        should_cancel=should_cancel,
        variant_id=variant_id,
    )


def create_artifact_thumbnails(
    repo: ThumbnailVariantRepository,
    source_variant_id: str,
    *,
    label: str,
    resize_mode: str,
    progress_callback: ProgressCallback | None = None,
    should_cancel: CancellationCallback | None = None,
    variant_id: str | None = None,
) -> ThumbnailGenerationResult:
    """Create a variant from the largest stored image for each canonical ID."""
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
        source_id=source_variant_id,
        source_media_type="image",
        input_kind="artifact",
        input_label=source.label,
        progress_callback=progress_callback,
        should_cancel=should_cancel,
        variant_id=variant_id,
    )
