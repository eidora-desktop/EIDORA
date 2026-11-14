"""Column-based matching and orchestration, independent of Qt and embedding runtime."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import PurePosixPath
import unicodedata
import re

from EmbeddingsArea.domain import (
    MediaLinkRequest, MediaLinkPlan, MediaLinkPreview, MediaMatchStrategy,
    MediaOperationResult, MediaProgress, MediaContentType,
)


def match_value(value: str, request: MediaLinkRequest, *, record=False) -> str:
    value = unicodedata.normalize("NFC", str(value or "").strip())
    if request.source_field == "relative_path" or request.comparison != "exact":
        value = value.replace("\\", "/")
    if request.comparison in {"filename", "stem", "stem_suffix"}:
        value = value.rsplit("/", 1)[-1]
    if request.comparison in {"stem", "stem_suffix"}:
        value = PurePosixPath(value).stem
    if record and request.comparison == "stem_suffix":
        value = re.sub(r"[_-][0-9]+$", "", value)
    return value.casefold() if request.ignore_case else value


def _check_cancel(should_cancel):
    if should_cancel and should_cancel():
        raise RuntimeError("Media operation cancelled.")


def build_media_link_plan(request, metadata_values, assets, existing_links, *, source_revision=0,
                          metadata_revision=0, progress=None, should_cancel=None):
    """Build compact associations in O(metadata rows + assets), with bounded examples."""
    if request.comparison not in {"exact", "filename", "stem", "stem_suffix"}:
        raise ValueError("Choose a valid comparison.")
    by_value = {}
    for index, (row_id, raw) in enumerate(metadata_values):
        if index % 2000 == 0:
            _check_cancel(should_cancel)
            if progress:
                progress(MediaProgress("Reading metadata", index, len(metadata_values)))
        value = match_value(raw, request, record=True)
        if value:
            by_value.setdefault(value, []).append(int(row_id))
    records = []
    counts = Counter()
    for index, (asset_id, raw) in enumerate(assets):
        if index % 2000 == 0:
            _check_cancel(should_cancel)
            if progress:
                progress(MediaProgress("Reading source", index))
        key = match_value(raw, request)
        records.append((asset_id, raw, key))
        if key:
            counts[key] += 1
    old_pairs = set(existing_links)
    old_by_asset = {}
    for asset, row in existing_links:
        old_by_asset.setdefault(asset, []).append(row)
    occupied = {row for _, row in existing_links} if not request.replace_existing else set()
    matches, new_rows = [], []
    examples = {name: [] for name in ("matched", "unmatched", "ambiguous")}
    preserved = unmatched = ambiguous = blank = 0

    def sample(kind, value, row_id, detail):
        if len(examples[kind]) < 25:
            examples[kind].append((value, row_id, detail))

    for index, (asset_id, raw, key) in enumerate(records):
        if index % 2000 == 0:
            _check_cancel(should_cancel)
            if progress:
                progress(MediaProgress("Matching", index, len(records)))
        row_ids = by_value.get(key, [])
        row_id = row_ids[0] if row_ids else None
        if not request.replace_existing and not row_ids and asset_id in old_by_asset:
            for identity in old_by_asset[asset_id]:
                sample("matched", raw, identity, "Already linked")
            continue
        if not key:
            blank += 1
            sample("unmatched", raw, None, "Blank identifier")
        elif counts[key] > 1:
            ambiguous += 1
            sample("ambiguous", raw, row_id, "Duplicate key or metadata row already linked")
        elif row_id is not None:
            for identity in row_ids:
                if not request.replace_existing and (asset_id, identity) in old_pairs:
                    sample("matched", raw, identity, "Already linked")
                    continue
                if not request.replace_existing and identity in occupied:
                    ambiguous += 1
                    sample("ambiguous", raw, identity, "Record already linked to another source asset")
                    continue
                matches.append((asset_id, identity, key))
                sample("matched", raw, identity, "Link")
        else:
            unmatched += 1
            if request.create_missing:
                new_rows.append((asset_id, raw))
            sample("unmatched", raw, None, "Create row" if request.create_missing else "Keep unlinked")
    _check_cancel(should_cancel)
    if not request.replace_existing:
        preserved = len(existing_links)
    removed = len(existing_links) if request.replace_existing else 0
    covered_rows = occupied | {row_id for _, row_id, _ in matches}
    sampled_ids = {row_id for rows in examples.values() for _, row_id, _ in rows if row_id is not None}
    example_values = {row_id: str(raw) for row_id, raw in metadata_values if row_id in sampled_ids}
    return MediaLinkPlan(request, source_revision, metadata_revision, tuple(matches), tuple(new_rows),
                         preserved, removed, unmatched, ambiguous, blank,
                         {kind: tuple(rows) for kind, rows in examples.items()},
                         max(0, len(metadata_values) - len(covered_rows)),
                         len(set(existing_links) - {(asset, row) for asset, row, _ in matches}) if request.replace_existing else 0,
                         example_values)


class MediaLinkService:
    def __init__(self, media_repository, metadata_repository, link_repository):
        self._media_repository = media_repository
        self._metadata_repository = metadata_repository
        self._link_repository = link_repository

    def list_metadata_columns(self):
        from MetadataArea.service import MetadataService
        return [column.name for column in MetadataService(self._metadata_repository).list_columns() if column.name != "id"]

    def has_metadata_rows(self):
        return self._metadata_repository.is_initialized() and self._metadata_repository.count_rows() > 0

    def saved_request(self, source_id):
        return self._link_repository.saved_request(source_id)

    def _metadata_values(self, column, should_cancel=None):
        if self._metadata_repository.table_id == "metadata":
            from MetadataArea.related_fields import discovery_columns
            from MetadataArea.resolution import resolve_overview_field
            from MetadataArea.values import scalar_values
            from project.item_repository import ItemRepository
            _, aliases = discovery_columns(self._metadata_repository.database_path)
            ids = ItemRepository(self._metadata_repository.database_path).active_ids()
            cells = resolve_overview_field(self._metadata_repository.database_path,
                column, aliases, ids)
            values = []
            for index, identity in enumerate(ids):
                if index % 2000 == 0:
                    _check_cancel(should_cancel)
                cell = cells.get(identity)
                keys = {str(value) for record in cell.records for value in scalar_values(record.value)
                        if value is not None and str(value).strip()} if cell else set()
                # Shared metadata may contain several candidate keys; do not guess.
                values.append((identity, next(iter(keys)) if len(keys) == 1 else ""))
        else:
            values = self._metadata_repository.column_items(column, should_cancel=should_cancel)
        return values

    def suggest_plan(self, source_id, *, replace_existing=False, progress=None, should_cancel=None):
        """Rank real matches, preferring less permissive rules when coverage ties."""
        _check_cancel(should_cancel)
        source = self._media_repository.get_source(source_id)
        if source is None:
            raise ValueError("This media source no longer exists.")
        revision = self._metadata_repository.revision()
        fields = ["relative_path"]
        if source.content_type == MediaContentType.TEXT:
            fields.insert(0, "identifier")
        assets = {field: list(self._link_repository.matching_records(source_id, field, should_cancel))
                  for field in fields}
        best_request, best_score = None, (0, 0, 0)
        columns = self.list_metadata_columns()
        for index, column in enumerate(columns):
            _check_cancel(should_cancel)
            if progress:
                progress(MediaProgress("Finding best match", index, len(columns)))
            values = self._metadata_values(column, should_cancel)
            for field in fields:
                comparisons = ("exact",) if field == "identifier" else ("exact", "filename", "stem", "stem_suffix")
                for rank, comparison in enumerate(comparisons):
                    request = MediaLinkRequest(source_id, field, column, comparison, replace_existing=True,
                                               table_id=self._metadata_repository.table_id)
                    candidate = build_media_link_plan(request, values, assets[field], [],
                                                      should_cancel=should_cancel)
                    score = (len(candidate.matches), -rank, -candidate.ambiguous_count)
                    if score > best_score:
                        best_request, best_score = request, score
        self._check_revision(source, revision)
        if best_request is None:
            return None
        return self.preview_plan(replace(best_request, replace_existing=replace_existing),
                                 progress=progress, should_cancel=should_cancel)

    def _check_revision(self, source, revision):
        current = self._media_repository.get_source(source.id)
        if current is None or current.source_revision != source.source_revision or revision != self._metadata_repository.revision():
            raise ValueError("Data changed during preview. Find matches again.")

    def preview_plan(self, request: MediaLinkRequest, *, progress=None, should_cancel=None):
        _check_cancel(should_cancel)
        if request.metadata_column not in self.list_metadata_columns():
            raise ValueError("Choose an existing metadata column.")
        source = self._media_repository.get_source(request.source_id)
        if source is None:
            raise ValueError("This media source no longer exists.")
        revision = self._metadata_repository.revision()
        values = self._metadata_values(request.metadata_column, should_cancel)
        plan = build_media_link_plan(
            request, values, self._link_repository.matching_records(request.source_id, request.source_field, should_cancel),
            self._link_repository.linked_pairs(request.source_id), source_revision=source.source_revision,
            metadata_revision=revision, progress=progress, should_cancel=should_cancel,
        )
        self._check_revision(source, revision)
        return plan

    def apply_plan(self, plan, *, progress=None, should_cancel=None):
        _check_cancel(should_cancel)
        if plan.new_rows:
            raise ValueError("Media matching cannot create records. Import or calculate embeddings first.")
        self._link_repository.publish_plan(plan, self._metadata_repository, updated_at=_now(),
                                          progress=progress, should_cancel=should_cancel)
        summary = self.summary(plan.request.source_id)
        return MediaOperationResult(summary, links_changed=True, metadata_changed=bool(plan.new_rows),
                                    created_count=len(plan.new_rows), linked_count=len(plan.matches)+len(plan.new_rows))

    def create_initial_metadata(self, source_id, *, progress=None, should_cancel=None):
        raise ValueError("Import or calculate embeddings to create items.")
    def summary(self, source_id):
        return next(item for item in self._media_repository.list_source_summaries() if item.source.id == source_id)

    # Compatibility for embedding preparation and existing callers. The dialog uses
    # explicit fields and comparisons; only this old API interprets its legacy enum.
    def _legacy_request(self, source_id, column, strategy):
        field = "relative_path" if strategy == MediaMatchStrategy.RELATIVE_PATH else "identifier"
        comparison = "exact"
        if strategy in {MediaMatchStrategy.FILENAME, MediaMatchStrategy.FILENAME_STEM}:
            field, comparison = "filename", "stem" if strategy == MediaMatchStrategy.FILENAME_STEM else "filename"
        elif column.casefold() in {"filename", "file", "file_name", "filepath", "file_path", "path", "name"}:
            field, comparison = "filename", "filename"
        return MediaLinkRequest(source_id, field, column, comparison)

    @staticmethod
    def _legacy_preview(plan, strategy):
        return MediaLinkPreview(plan.request.source_id, plan.request.metadata_column, strategy,
            len(plan.matches)+plan.preserved_count, plan.unmatched_count+plan.ambiguous_count+plan.blank_count,
            plan.unmatched_metadata_count, tuple(row[0] for row in plan.examples["unmatched"]))

    def preview_links(self, source_id, metadata_column, strategy=MediaMatchStrategy.IDENTIFIER_EQUALS):
        return self._legacy_preview(self.preview_plan(self._legacy_request(source_id, metadata_column, strategy)), strategy)

    def apply_links(self, source_id, metadata_column, *, strategy=MediaMatchStrategy.IDENTIFIER_EQUALS):
        plan = self.preview_plan(self._legacy_request(source_id, metadata_column, strategy))
        self.apply_plan(plan)
        return self._legacy_preview(plan, strategy)

    def bootstrap_filename_metadata(self, source_id):
        if self.has_metadata_rows():
            raise ValueError("Metadata already contains rows. Choose a metadata column.")
        result = self.create_initial_metadata(source_id)
        if not result.created_count:
            raise ValueError("This media source has no indexed files to link.")
        return MediaLinkPreview(source_id, "filename", MediaMatchStrategy.MANUAL, result.created_count, 0, 0)

    def create_missing_metadata_rows(self, source_id, metadata_column, *, strategy=MediaMatchStrategy.IDENTIFIER_EQUALS):
        from dataclasses import replace
        request = replace(self._legacy_request(source_id, metadata_column, strategy), create_missing=True)
        return self.apply_plan(self.preview_plan(request)).created_count

    def dismiss_unmatched_assets(self, *args, **kwargs):
        raise ValueError("Unmatched files remain indexed. Remove the source explicitly if it is no longer needed.")

    def assets_with_optional_links(self, source_ids):
        assets = self._media_repository.ready_assets_for_sources(source_ids)
        links = self._link_repository.links_for_assets([asset.id for asset in assets])
        return [(asset, link) for asset in assets for link in links.get(asset.id, [None])]

    def linked_assets_for_sources(self, source_ids):
        assets = self._media_repository.ready_assets_for_sources(source_ids)
        links = self._link_repository.links_for_assets([asset.id for asset in assets])
        return [(asset, link) for asset in assets for link in links.get(asset.id, [])]


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
