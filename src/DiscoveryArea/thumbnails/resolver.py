from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import sqlite3

from EmbeddingsArea.repository import MediaLinkRepository, MediaRepository
from DiscoveryArea.thumbnails.repository import ThumbnailVariantItem, ThumbnailVariantRepository
from project.domain import Project


@dataclass(frozen=True)
class ThumbnailRef:
    metadata_id: int
    path: Path
    fingerprint: str
    source: str
    mtime_ns: int | None


class ThumbnailResolver:
    def __init__(self, project: Project) -> None:
        self._project = project
        self._media_repo = MediaRepository(project.paths.media / "media_index.db")
        self._link_repo = MediaLinkRepository(project.paths.media / "media_index.db")
        self._variant_repo = ThumbnailVariantRepository(project.paths.root)

    def source_catalog_token(self, variant_id: str | None = None) -> tuple[object, ...]:
        """Return a cheap invalidation token for cached viewport source misses."""

        paths = (
            self._project.paths.metadata_database,
            self._project.paths.root / "thumbnails",
            self._project.paths.root / "thumbnails" / "variants.db",
            self._project.paths.media / "media_index.db",
        )
        values: list[object] = [str(variant_id or "")]
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                values.extend((0, 0))
            else:
                values.extend((int(stat.st_mtime_ns), int(stat.st_size)))
        return tuple(values)

    def resolve(self, metadata_id: int, variant_id: str | None = None, target_px: int = 128) -> ThumbnailRef | None:
        if variant_id:
            item = self._resolve_variant_item(variant_id=variant_id, metadata_id=metadata_id, target_px=target_px)
            if item is not None:
                candidate = self._variant_repo.thumbnails_root / item.relative_path
                if candidate.exists():
                    return self._make_ref(metadata_id, candidate, source=f"variant:{variant_id}")
        asset_ref = self._resolve_media_asset(metadata_id)
        if asset_ref is not None:
            return asset_ref
        return None

    def resolve_original(
        self,
        metadata_id: int,
        variant_id: str | None = None,
        source_asset_id: str = "",
    ) -> ThumbnailRef | None:
        """Resolve the original image behind a generated thumbnail variant."""

        if source_asset_id:
            assets = self._media_repo.assets_for_items(
                [metadata_id], include_inherited=True,
            ).get(int(metadata_id), [])
            asset = next((asset for asset in assets if asset.id == source_asset_id), None)
            if asset is None or not asset.absolute_path.is_file():
                return None
            return self._make_ref(metadata_id, asset.absolute_path, source="media")

        if variant_id:
            item = self._resolve_variant_item(
                variant_id=str(variant_id),
                metadata_id=int(metadata_id),
                target_px=1,
            )
            if item is not None and item.source_path:
                candidate = Path(item.source_path)
                if candidate.is_file():
                    return self._make_ref(
                        metadata_id,
                        candidate,
                        source=f"variant_source:{variant_id}",
                    )
        return self._resolve_media_asset(metadata_id)

    def resolve_many(
        self,
        metadata_ids: list[int] | tuple[int, ...],
        *,
        variant_id: str | None = None,
        target_px: int = 128,
    ) -> dict[int, ThumbnailRef]:
        """Resolve a viewport batch without scanning every item in a variant."""

        ordered_ids = list(dict.fromkeys(int(value) for value in metadata_ids))
        if not ordered_ids:
            return {}
        output: dict[int, ThumbnailRef] = {}
        if variant_id:
            try:
                variant_items = self._variant_repo.resolve_items_for_ids(
                    str(variant_id),
                    ordered_ids,
                    target_px,
                )
            except sqlite3.Error:
                variant_items = {}
            for metadata_id, item in variant_items.items():
                candidate = self._variant_repo.thumbnails_root / item.relative_path
                if candidate.exists():
                    output[metadata_id] = self._make_ref(
                        metadata_id,
                        candidate,
                        source=f"variant:{variant_id}",
                    )
        unresolved = [metadata_id for metadata_id in ordered_ids if metadata_id not in output]
        if unresolved:
            try:
                assets = self._media_repo.linked_assets_for_metadata_ids(unresolved)
            except sqlite3.Error:
                assets = {}
            for metadata_id, asset in assets.items():
                if asset.absolute_path.exists():
                    output[metadata_id] = self._make_ref(
                        metadata_id,
                        asset.absolute_path,
                        source="media",
                    )
        return output

    def _resolve_variant_item(
        self,
        *,
        variant_id: str,
        metadata_id: int,
        target_px: int,
    ) -> ThumbnailVariantItem | None:
        try:
            return self._variant_repo.resolve_item(
                variant_id=str(variant_id),
                metadata_id=int(metadata_id),
                target_px=max(1, int(target_px)),
            )
        except sqlite3.Error:
            return None

    def _resolve_media_asset(self, metadata_id: int) -> ThumbnailRef | None:
        assets = self._media_repo.linked_assets_for_metadata_ids([metadata_id])
        asset = assets.get(int(metadata_id))
        if asset is None or not asset.absolute_path.is_file():
            return None
        return self._make_ref(metadata_id, asset.absolute_path, source="media")

    def _make_ref(self, metadata_id: int, path: Path, source: str) -> ThumbnailRef:
        stat = path.stat()
        payload = f"{path}:{stat.st_mtime_ns}:{stat.st_size}".encode("utf-8")
        return ThumbnailRef(
            metadata_id=metadata_id,
            path=path,
            fingerprint=hashlib.sha1(payload).hexdigest(),
            source=source,
            mtime_ns=int(stat.st_mtime_ns),
        )
