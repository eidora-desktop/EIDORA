from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
from pathlib import Path
import shutil
import sqlite3

from project.portable_paths import ProjectPaths

DEFAULT_THUMBNAIL_SIZES: tuple[int, ...] = (32, 64, 128, 256)
DEFAULT_ATLAS_THUMBNAIL_SIZES: tuple[int, ...] = (32, 64, 128)
DEFAULT_ATLAS_SIZE_PX = 2048


@dataclass(frozen=True)
class ThumbnailVariant:
    variant_id: str
    label: str
    source_dir: str
    identifier_column: str
    sizes: tuple[int, ...]
    created_at: str
    resize_mode: str = "original_aspect"
    source_id: str = ""
    source_media_type: str = "image"
    video_frame_mode: str = ""
    generated_files: int | None = None
    failed_files: int = 0
    missing_files: int = 0
    errors: tuple[str, ...] = ()
    input_kind: str = ""
    input_label: str = ""


@dataclass(frozen=True)
class ThumbnailVariantItem:
    variant_id: str
    metadata_id: int
    size_px: int
    relative_path: str
    mtime_ns: int
    status: str
    source_path: str = ""
    selected_frame_seconds: float | None = None
    selected_frame_ordinal: int | None = None


@dataclass(frozen=True)
class ThumbnailAtlasPage:
    variant_id: str
    size_px: int
    page_index: int
    relative_path: str
    width: int
    height: int
    mtime_ns: int


@dataclass(frozen=True)
class ThumbnailAtlasItem:
    variant_id: str
    metadata_id: int
    size_px: int
    page_index: int
    u: float
    v: float
    width: float
    height: float


@dataclass(frozen=True)
class ThumbnailTilePyramid:
    variant_id: str
    discovery_cache_id: str
    fingerprint: str
    bounds_json: str
    tile_size_px: int
    max_lod: int
    created_at: str


@dataclass(frozen=True)
class ThumbnailTilePage:
    variant_id: str
    discovery_cache_id: str
    lod: int
    tile_x: int
    tile_y: int
    relative_path: str
    mtime_ns: int
    status: str


class ThumbnailVariantRepository:
    def __init__(self, project_root: Path) -> None:
        self._paths = ProjectPaths(project_root)
        self._root = Path(project_root) / "thumbnails"
        self._db_path = self._root / "variants.db"
        self._root.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @property
    def thumbnails_root(self) -> Path:
        return self._root

    @property
    def variants_root(self) -> Path:
        path = self._root / "variants"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def list_variants(self) -> list[ThumbnailVariant]:
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT variant_id, label, source_dir, identifier_column, sizes_json, created_at,
                       resize_mode, source_id, source_media_type, video_frame_mode,
                       generated_files, failed_files, missing_files, errors_json,
                       input_kind, input_label
                FROM thumbnail_variants
                ORDER BY created_at DESC, lower(label)
                """
            ).fetchall()
        output: list[ThumbnailVariant] = []
        for row in rows:
            sizes = self._parse_sizes_json(str(row["sizes_json"] or "[]"))
            output.append(
                ThumbnailVariant(
                    variant_id=str(row["variant_id"]),
                    label=str(row["label"] or ""),
                    source_dir=self._paths.decode(row["source_dir"]),
                    identifier_column=str(row["identifier_column"] or ""),
                    sizes=sizes,
                    created_at=str(row["created_at"] or ""),
                    resize_mode=str(row["resize_mode"] or "original_aspect"),
                    source_id=str(row["source_id"] or ""),
                    source_media_type=str(row["source_media_type"] or "image"),
                    video_frame_mode=str(row["video_frame_mode"] or ""),
                    generated_files=row["generated_files"],
                    failed_files=int(row["failed_files"]),
                    missing_files=int(row["missing_files"]),
                    errors=tuple(json.loads(row["errors_json"])),
                    input_kind=str(row["input_kind"] or ""),
                    input_label=str(row["input_label"] or ""),
                )
            )
        return output

    def save_variant(self, variant: ThumbnailVariant) -> None:
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            self._insert_variant(connection, variant, replace=True)

    def _insert_variant(self, connection: sqlite3.Connection, variant: ThumbnailVariant, *, replace: bool = False) -> None:
        connection.execute(
            f"""
            INSERT {"OR REPLACE" if replace else ""} INTO thumbnail_variants (
                variant_id, label, source_dir, identifier_column, sizes_json, created_at,
                resize_mode, source_id, source_media_type, video_frame_mode,
                generated_files, failed_files, missing_files, errors_json,
                input_kind, input_label
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                variant.variant_id,
                variant.label,
                self._paths.encode(variant.source_dir),
                variant.identifier_column,
                json.dumps(list(variant.sizes)),
                variant.created_at,
                variant.resize_mode,
                variant.source_id,
                variant.source_media_type,
                variant.video_frame_mode,
                variant.generated_files,
                variant.failed_files,
                variant.missing_files,
                json.dumps(variant.errors),
                variant.input_kind,
                variant.input_label,
            ),
        )

    @contextmanager
    def stage_variant(self, variant_id: str) -> Iterator[Path]:
        """Own unpublished files; cleanup runs only after the image workers stop."""
        staging = self._root / ".staging" / variant_id
        staging.mkdir(parents=True, exist_ok=False)
        try:
            yield staging
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def publish_variant(self, variant: ThumbnailVariant, items: list[ThumbnailVariantItem], staging: Path) -> None:
        """Expose complete files and their index together, without replacing an existing ID."""
        destination = self.variants_root / variant.variant_id
        moved = False
        try:
            with closing(sqlite3.connect(self._db_path)) as connection, connection:
                self._insert_variant(connection, variant)
                self._insert_variant_items(connection, items)
                if destination.exists():
                    raise FileExistsError(f"Thumbnail directory already exists: {destination}")
                staging.rename(destination)
                moved = True
                # Commit follows the rename: readers never see a partially written variant.
        except BaseException:
            if moved:
                shutil.rmtree(destination)
            raise

    def rename_variant(self, variant_id: str, label: str) -> None:
        normalized_id = str(variant_id).strip()
        normalized_label = str(label).strip()
        if not normalized_id:
            raise ValueError("Choose a thumbnail variant to rename.")
        if not normalized_label:
            raise ValueError("Thumbnail name cannot be empty.")
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            duplicate = connection.execute(
                "SELECT 1 FROM thumbnail_variants WHERE lower(label) = lower(?) AND variant_id != ?",
                (normalized_label, normalized_id),
            ).fetchone()
            if duplicate is not None:
                raise ValueError(f"A thumbnail variant named '{normalized_label}' already exists.")
            cursor = connection.execute(
                "UPDATE thumbnail_variants SET label = ? WHERE variant_id = ?",
                (normalized_label, normalized_id),
            )
            if cursor.rowcount == 0:
                raise ValueError("The thumbnail variant no longer exists.")
            connection.commit()

    def delete_variant(self, variant_id: str) -> None:
        """Remove one variant's records and generated files."""

        normalized_id = str(variant_id).strip()
        if not normalized_id:
            return
        if Path(normalized_id).name != normalized_id:
            raise ValueError("Thumbnail variant IDs must not contain path separators.")
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            connection.execute("DELETE FROM thumbnail_tile_pages WHERE variant_id = ?", (normalized_id,))
            connection.execute("DELETE FROM thumbnail_tile_pyramids WHERE variant_id = ?", (normalized_id,))
            connection.execute("DELETE FROM thumbnail_atlas_items WHERE variant_id = ?", (normalized_id,))
            connection.execute("DELETE FROM thumbnail_atlas_pages WHERE variant_id = ?", (normalized_id,))
            connection.execute("DELETE FROM thumbnail_variant_items WHERE variant_id = ?", (normalized_id,))
            connection.execute("DELETE FROM thumbnail_variants WHERE variant_id = ?", (normalized_id,))
            connection.commit()
        for root in (self._root / "variants", self._root / "atlases", self._root / "tiles"):
            target = root / normalized_id
            if target.is_dir():
                shutil.rmtree(target)

    def discard_staged_variant(self, variant_id: str) -> None:
        """Remove files left by an unpublished thumbnail job."""

        normalized_id = str(variant_id).strip()
        if not normalized_id or Path(normalized_id).name != normalized_id:
            return
        staging = self._root / ".staging" / normalized_id
        if staging.is_dir():
            shutil.rmtree(staging)

    def replace_variant_items(self, variant_id: str, items: list[ThumbnailVariantItem]) -> None:
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            connection.execute("DELETE FROM thumbnail_variant_items WHERE variant_id = ?", (variant_id,))
            self._insert_variant_items(connection, items)

    def _insert_variant_items(self, connection: sqlite3.Connection, items: list[ThumbnailVariantItem]) -> None:
        connection.executemany(
            """
            INSERT INTO thumbnail_variant_items (
                variant_id, metadata_id, size_px, relative_path, mtime_ns, status,
                source_path, selected_frame_seconds, selected_frame_ordinal
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    item.variant_id,
                    int(item.metadata_id),
                    int(item.size_px),
                    item.relative_path,
                    int(item.mtime_ns),
                    item.status,
                    self._paths.encode(item.source_path),
                    item.selected_frame_seconds,
                    item.selected_frame_ordinal,
                )
                for item in items
            ),
        )

    def replace_atlas(
        self,
        *,
        variant_id: str,
        pages: list[ThumbnailAtlasPage],
        items: list[ThumbnailAtlasItem],
    ) -> None:
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            connection.execute("DELETE FROM thumbnail_atlas_items WHERE variant_id = ?", (variant_id,))
            connection.execute("DELETE FROM thumbnail_atlas_pages WHERE variant_id = ?", (variant_id,))
            if pages:
                connection.executemany(
                    """
                    INSERT INTO thumbnail_atlas_pages (
                        variant_id, size_px, page_index, relative_path, width, height, mtime_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            page.variant_id,
                            int(page.size_px),
                            int(page.page_index),
                            page.relative_path,
                            int(page.width),
                            int(page.height),
                            int(page.mtime_ns),
                        )
                        for page in pages
                    ],
                )
            if items:
                connection.executemany(
                    """
                    INSERT INTO thumbnail_atlas_items (
                        variant_id, metadata_id, size_px, page_index, u, v, width, height
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            item.variant_id,
                            int(item.metadata_id),
                            int(item.size_px),
                            int(item.page_index),
                            float(item.u),
                            float(item.v),
                            float(item.width),
                            float(item.height),
                        )
                        for item in items
                    ],
                )
            connection.commit()

    def save_tile_pyramid(self, pyramid: ThumbnailTilePyramid) -> None:
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO thumbnail_tile_pyramids (
                    variant_id, discovery_cache_id, fingerprint, bounds_json,
                    tile_size_px, max_lod, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pyramid.variant_id,
                    pyramid.discovery_cache_id,
                    pyramid.fingerprint,
                    pyramid.bounds_json,
                    int(pyramid.tile_size_px),
                    int(pyramid.max_lod),
                    pyramid.created_at,
                ),
            )
            connection.commit()

    def resolve_tile_pyramid(self, variant_id: str, discovery_cache_id: str) -> ThumbnailTilePyramid | None:
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT variant_id, discovery_cache_id, fingerprint, bounds_json,
                       tile_size_px, max_lod, created_at
                FROM thumbnail_tile_pyramids
                WHERE variant_id = ? AND discovery_cache_id = ?
                """,
                (variant_id, discovery_cache_id),
            ).fetchone()
        if row is None:
            return None
        return ThumbnailTilePyramid(
            variant_id=str(row["variant_id"]),
            discovery_cache_id=str(row["discovery_cache_id"]),
            fingerprint=str(row["fingerprint"]),
            bounds_json=str(row["bounds_json"] or "{}"),
            tile_size_px=int(row["tile_size_px"] or DEFAULT_ATLAS_SIZE_PX),
            max_lod=int(row["max_lod"] or 0),
            created_at=str(row["created_at"] or ""),
        )

    def invalidate_tile_pyramid(self, variant_id: str, discovery_cache_id: str) -> None:
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            connection.execute(
                "DELETE FROM thumbnail_tile_pages WHERE variant_id = ? AND discovery_cache_id = ?",
                (variant_id, discovery_cache_id),
            )
            connection.execute(
                "DELETE FROM thumbnail_tile_pyramids WHERE variant_id = ? AND discovery_cache_id = ?",
                (variant_id, discovery_cache_id),
            )
            connection.commit()

    def save_tile_page(self, page: ThumbnailTilePage) -> None:
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO thumbnail_tile_pages (
                    variant_id, discovery_cache_id, lod, tile_x, tile_y,
                    relative_path, mtime_ns, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    page.variant_id,
                    page.discovery_cache_id,
                    int(page.lod),
                    int(page.tile_x),
                    int(page.tile_y),
                    page.relative_path,
                    int(page.mtime_ns),
                    page.status,
                ),
            )
            connection.commit()

    def list_tile_pages(
        self,
        variant_id: str,
        discovery_cache_id: str,
        tiles: list[tuple[int, int, int]],
    ) -> list[ThumbnailTilePage]:
        if not tiles:
            return []
        output: list[ThumbnailTilePage] = []
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            for lod, tile_x, tile_y in tiles:
                row = connection.execute(
                    """
                    SELECT variant_id, discovery_cache_id, lod, tile_x, tile_y,
                           relative_path, mtime_ns, status
                    FROM thumbnail_tile_pages
                    WHERE variant_id = ? AND discovery_cache_id = ?
                      AND lod = ? AND tile_x = ? AND tile_y = ?
                      AND status = 'ok'
                    """,
                    (variant_id, discovery_cache_id, int(lod), int(tile_x), int(tile_y)),
                ).fetchone()
                if row is None:
                    continue
                output.append(
                    ThumbnailTilePage(
                        variant_id=str(row["variant_id"]),
                        discovery_cache_id=str(row["discovery_cache_id"]),
                        lod=int(row["lod"]),
                        tile_x=int(row["tile_x"]),
                        tile_y=int(row["tile_y"]),
                        relative_path=str(row["relative_path"]),
                        mtime_ns=int(row["mtime_ns"] or 0),
                        status=str(row["status"] or "ok"),
                    )
                )
        return output

    def relative_tile_path(self, variant_id: str, discovery_cache_id: str, lod: int, tile_x: int, tile_y: int) -> Path:
        return (
            Path("tiles")
            / str(variant_id)
            / str(discovery_cache_id)
            / str(int(lod))
            / f"{int(tile_x)}_{int(tile_y)}.png"
        )

    def absolute_tile_path(self, variant_id: str, discovery_cache_id: str, lod: int, tile_x: int, tile_y: int) -> Path:
        return self._root / self.relative_tile_path(variant_id, discovery_cache_id, lod, tile_x, tile_y)

    def list_atlas_pages(self, variant_id: str, target_px: int) -> list[ThumbnailAtlasPage]:
        size_px = self._nearest_atlas_size(variant_id, target_px)
        if size_px is None:
            return []
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT variant_id, size_px, page_index, relative_path, width, height, mtime_ns
                FROM thumbnail_atlas_pages
                WHERE variant_id = ? AND size_px = ?
                ORDER BY page_index
                """,
                (variant_id, int(size_px)),
            ).fetchall()
        return [
            ThumbnailAtlasPage(
                variant_id=str(row["variant_id"]),
                size_px=int(row["size_px"]),
                page_index=int(row["page_index"]),
                relative_path=str(row["relative_path"]),
                width=int(row["width"]),
                height=int(row["height"]),
                mtime_ns=int(row["mtime_ns"] or 0),
            )
            for row in rows
        ]

    def list_atlas_items(self, variant_id: str, metadata_ids: list[int], target_px: int) -> list[ThumbnailAtlasItem]:
        if not metadata_ids:
            return []
        size_px = self._nearest_atlas_size(variant_id, target_px)
        if size_px is None:
            return []
        output: list[ThumbnailAtlasItem] = []
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            for offset in range(0, len(metadata_ids), 900):
                chunk = [int(value) for value in metadata_ids[offset : offset + 900]]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT variant_id, metadata_id, size_px, page_index, u, v, width, height
                    FROM thumbnail_atlas_items
                    WHERE variant_id = ? AND size_px = ? AND metadata_id IN ({placeholders})
                    """,
                    [variant_id, int(size_px), *chunk],
                ).fetchall()
                for row in rows:
                    output.append(
                        ThumbnailAtlasItem(
                            variant_id=str(row["variant_id"]),
                            metadata_id=int(row["metadata_id"]),
                            size_px=int(row["size_px"]),
                            page_index=int(row["page_index"]),
                            u=float(row["u"]),
                            v=float(row["v"]),
                            width=float(row["width"]),
                            height=float(row["height"]),
                        )
                    )
        return output

    def has_atlas(self, variant_id: str, target_px: int) -> bool:
        return bool(self.list_atlas_pages(variant_id, target_px))

    def resolve_item(self, variant_id: str, metadata_id: int, target_px: int) -> ThumbnailVariantItem | None:
        if not variant_id:
            return None
        target = max(1, int(target_px))
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT variant_id, metadata_id, size_px, relative_path, mtime_ns, status,
                       source_path, selected_frame_seconds, selected_frame_ordinal
                FROM thumbnail_variant_items
                WHERE variant_id = ? AND metadata_id = ? AND status = 'ok'
                """,
                (variant_id, int(metadata_id)),
            ).fetchall()
        if not rows:
            return None
        choices = sorted(
            rows,
            key=lambda row: self._variant_item_rank(row, target),
        )
        row = choices[0]
        return ThumbnailVariantItem(
            variant_id=str(row["variant_id"]),
            metadata_id=int(row["metadata_id"]),
            size_px=int(row["size_px"]),
            relative_path=str(row["relative_path"]),
            mtime_ns=int(row["mtime_ns"] or 0),
            status=str(row["status"] or "ok"),
            source_path=self._paths.decode(row["source_path"]),
            selected_frame_seconds=row["selected_frame_seconds"],
            selected_frame_ordinal=row["selected_frame_ordinal"],
        )

    def resolve_items(self, variant_id: str, target_px: int) -> dict[int, ThumbnailVariantItem]:
        if not variant_id:
            return {}
        target = max(1, int(target_px))
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT variant_id, metadata_id, size_px, relative_path, mtime_ns, status,
                       source_path, selected_frame_seconds, selected_frame_ordinal
                FROM thumbnail_variant_items
                WHERE variant_id = ? AND status = 'ok'
                """,
                (variant_id,),
            ).fetchall()
        best_rows: dict[int, sqlite3.Row] = {}
        for row in rows:
            metadata_id = int(row["metadata_id"])
            current = best_rows.get(metadata_id)
            if current is None or self._variant_item_rank(row, target) < self._variant_item_rank(current, target):
                best_rows[metadata_id] = row
        return {
            metadata_id: ThumbnailVariantItem(
                variant_id=str(row["variant_id"]),
                metadata_id=metadata_id,
                size_px=int(row["size_px"]),
                relative_path=str(row["relative_path"]),
                mtime_ns=int(row["mtime_ns"] or 0),
                status=str(row["status"] or "ok"),
                source_path=self._paths.decode(row["source_path"]),
                selected_frame_seconds=row["selected_frame_seconds"],
                selected_frame_ordinal=row["selected_frame_ordinal"],
            )
            for metadata_id, row in best_rows.items()
        }

    def count_variant_items(self, variant_id: str) -> int:
        """Return the number of usable metadata items without loading their rows."""

        if not variant_id:
            return 0
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            row = connection.execute(
                """
                SELECT COUNT(DISTINCT metadata_id)
                FROM thumbnail_variant_items
                WHERE variant_id = ? AND status = 'ok'
                """,
                (variant_id,),
            ).fetchone()
        return int(row[0] or 0) if row is not None else 0

    def resolve_items_for_ids(
        self,
        variant_id: str,
        metadata_ids: list[int] | tuple[int, ...],
        target_px: int,
    ) -> dict[int, ThumbnailVariantItem]:
        """Resolve only requested rows instead of materializing the full variant."""

        if not variant_id or not metadata_ids:
            return {}
        ordered_ids = list(dict.fromkeys(int(value) for value in metadata_ids))
        target = max(1, int(target_px))
        best_rows: dict[int, sqlite3.Row] = {}
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            for offset in range(0, len(ordered_ids), 900):
                chunk = ordered_ids[offset : offset + 900]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT variant_id, metadata_id, size_px, relative_path, mtime_ns, status,
                           source_path, selected_frame_seconds, selected_frame_ordinal
                    FROM thumbnail_variant_items
                    WHERE variant_id = ? AND status = 'ok'
                      AND metadata_id IN ({placeholders})
                    """,
                    [variant_id, *chunk],
                ).fetchall()
                for row in rows:
                    metadata_id = int(row["metadata_id"])
                    current = best_rows.get(metadata_id)
                    if current is None or self._variant_item_rank(row, target) < self._variant_item_rank(
                        current,
                        target,
                    ):
                        best_rows[metadata_id] = row
        return {
            metadata_id: ThumbnailVariantItem(
                variant_id=str(row["variant_id"]),
                metadata_id=metadata_id,
                size_px=int(row["size_px"]),
                relative_path=str(row["relative_path"]),
                mtime_ns=int(row["mtime_ns"] or 0),
                status=str(row["status"] or "ok"),
                source_path=self._paths.decode(row["source_path"]),
                selected_frame_seconds=row["selected_frame_seconds"],
                selected_frame_ordinal=row["selected_frame_ordinal"],
            )
            for metadata_id, row in best_rows.items()
        }

    def variant_items_fingerprint(self, variant_id: str) -> str:
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            rows = connection.execute(
                """
                SELECT metadata_id, size_px, relative_path, mtime_ns
                FROM thumbnail_variant_items
                WHERE variant_id = ? AND status = 'ok'
                ORDER BY metadata_id, size_px, relative_path
                """,
                (variant_id,),
            ).fetchall()
        digest = hashlib.sha1()
        digest.update(str(len(rows)).encode("ascii"))
        for metadata_id, size_px, relative_path, mtime_ns in rows:
            digest.update(b"\0")
            digest.update(str(int(metadata_id)).encode("ascii"))
            digest.update(b":")
            digest.update(str(int(size_px)).encode("ascii"))
            digest.update(b":")
            digest.update(str(relative_path).encode("utf-8"))
            digest.update(b":")
            digest.update(str(int(mtime_ns or 0)).encode("ascii"))
        return digest.hexdigest()

    @staticmethod
    def shard_for_metadata_id(metadata_id: int) -> str:
        return f"{abs(int(metadata_id)) % 256:03d}"

    def relative_variant_path(self, variant_id: str, size_px: int, metadata_id: int) -> Path:
        shard = self.shard_for_metadata_id(metadata_id)
        return Path("variants") / variant_id / str(int(size_px)) / shard / f"{int(metadata_id)}.webp"

    def absolute_variant_path(self, variant_id: str, size_px: int, metadata_id: int) -> Path:
        return self._root / self.relative_variant_path(variant_id, size_px, metadata_id)

    def relative_atlas_path(self, variant_id: str, size_px: int, page_index: int) -> Path:
        return Path("atlases") / variant_id / str(int(size_px)) / f"page_{int(page_index):05d}.webp"

    def absolute_atlas_path(self, variant_id: str, size_px: int, page_index: int) -> Path:
        return self._root / self.relative_atlas_path(variant_id, size_px, page_index)

    def _ensure_schema(self) -> None:
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS thumbnail_variants (
                    variant_id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    source_dir TEXT NOT NULL,
                    identifier_column TEXT NOT NULL,
                    sizes_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    resize_mode TEXT NOT NULL DEFAULT 'original_aspect',
                    source_id TEXT NOT NULL DEFAULT '',
                    source_media_type TEXT NOT NULL DEFAULT 'image',
                    video_frame_mode TEXT NOT NULL DEFAULT '',
                    input_kind TEXT NOT NULL DEFAULT '',
                    input_label TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS thumbnail_variant_items (
                    variant_id TEXT NOT NULL,
                    metadata_id INTEGER NOT NULL,
                    size_px INTEGER NOT NULL,
                    relative_path TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'ok',
                    source_path TEXT NOT NULL DEFAULT '',
                    selected_frame_seconds REAL,
                    selected_frame_ordinal INTEGER,
                    PRIMARY KEY (variant_id, metadata_id, size_px)
                );

                CREATE INDEX IF NOT EXISTS idx_thumbnail_variant_items_metadata
                ON thumbnail_variant_items (variant_id, metadata_id);

                CREATE TABLE IF NOT EXISTS thumbnail_atlas_pages (
                    variant_id TEXT NOT NULL,
                    size_px INTEGER NOT NULL,
                    page_index INTEGER NOT NULL,
                    relative_path TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (variant_id, size_px, page_index)
                );

                CREATE TABLE IF NOT EXISTS thumbnail_atlas_items (
                    variant_id TEXT NOT NULL,
                    metadata_id INTEGER NOT NULL,
                    size_px INTEGER NOT NULL,
                    page_index INTEGER NOT NULL,
                    u REAL NOT NULL,
                    v REAL NOT NULL,
                    width REAL NOT NULL,
                    height REAL NOT NULL,
                    PRIMARY KEY (variant_id, metadata_id, size_px)
                );

                CREATE INDEX IF NOT EXISTS idx_thumbnail_atlas_items_metadata
                ON thumbnail_atlas_items (variant_id, size_px, metadata_id);

                CREATE TABLE IF NOT EXISTS thumbnail_tile_pyramids (
                    variant_id TEXT NOT NULL,
                    discovery_cache_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    bounds_json TEXT NOT NULL,
                    tile_size_px INTEGER NOT NULL,
                    max_lod INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (variant_id, discovery_cache_id)
                );

                CREATE TABLE IF NOT EXISTS thumbnail_tile_pages (
                    variant_id TEXT NOT NULL,
                    discovery_cache_id TEXT NOT NULL,
                    lod INTEGER NOT NULL,
                    tile_x INTEGER NOT NULL,
                    tile_y INTEGER NOT NULL,
                    relative_path TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'ok',
                    PRIMARY KEY (variant_id, discovery_cache_id, lod, tile_x, tile_y)
                );

                CREATE INDEX IF NOT EXISTS idx_thumbnail_tile_pages_lookup
                ON thumbnail_tile_pages (variant_id, discovery_cache_id, lod);
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(thumbnail_variant_items)")
            }
            if "source_path" not in columns:
                connection.execute(
                    "ALTER TABLE thumbnail_variant_items "
                    "ADD COLUMN source_path TEXT NOT NULL DEFAULT ''"
                )
            for name, declaration in {
                "selected_frame_seconds": "REAL",
                "selected_frame_ordinal": "INTEGER",
            }.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE thumbnail_variant_items ADD COLUMN {name} {declaration}"
                    )
            variant_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(thumbnail_variants)")
            }
            if "resize_mode" not in variant_columns:
                connection.execute(
                    "ALTER TABLE thumbnail_variants "
                    "ADD COLUMN resize_mode TEXT NOT NULL DEFAULT 'original_aspect'"
                )
            if "source_id" not in variant_columns:
                connection.execute(
                    "ALTER TABLE thumbnail_variants "
                    "ADD COLUMN source_id TEXT NOT NULL DEFAULT ''"
                )
            for name, declaration in {
                "source_media_type": "TEXT NOT NULL DEFAULT 'image'",
                "video_frame_mode": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in variant_columns:
                    connection.execute(
                        f"ALTER TABLE thumbnail_variants ADD COLUMN {name} {declaration}"
                    )
            for name, declaration in {
                "generated_files": "INTEGER",
                "failed_files": "INTEGER NOT NULL DEFAULT 0",
                "missing_files": "INTEGER NOT NULL DEFAULT 0",
                "errors_json": "TEXT NOT NULL DEFAULT '[]'",
                "input_kind": "TEXT NOT NULL DEFAULT ''",
                "input_label": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in variant_columns:
                    connection.execute(f"ALTER TABLE thumbnail_variants ADD COLUMN {name} {declaration}")
            connection.commit()

    def _nearest_atlas_size(self, variant_id: str, target_px: int) -> int | None:
        with closing(sqlite3.connect(self._db_path)) as connection, connection:
            rows = connection.execute(
                """
                SELECT DISTINCT size_px
                FROM thumbnail_atlas_pages
                WHERE variant_id = ?
                """,
                (variant_id,),
            ).fetchall()
        if not rows:
            return None
        target = max(1, int(target_px))
        return int(
            sorted(
                (int(row[0]) for row in rows),
                key=lambda value: (0 if value >= target else 1, value if value >= target else -value),
            )[0]
        )

    @staticmethod
    def _variant_item_rank(row: sqlite3.Row, target: int) -> tuple[int, int]:
        size_px = int(row["size_px"])
        return (
            0 if size_px >= target else 1,
            size_px if size_px >= target else -size_px,
        )

    @staticmethod
    def _parse_sizes_json(raw: str) -> tuple[int, ...]:
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return DEFAULT_THUMBNAIL_SIZES
        if not isinstance(decoded, list):
            return DEFAULT_THUMBNAIL_SIZES
        values = sorted({int(item) for item in decoded if isinstance(item, int) and item > 0})
        if not values:
            return DEFAULT_THUMBNAIL_SIZES
        return tuple(values)
