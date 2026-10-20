"""Column-based matching and orchestration, independent of Qt and embedding runtime."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import PurePosixPath
import unicodedata

from EmbeddingsArea.domain import (
    MediaLinkRequest, MediaLinkPlan, MediaLinkPreview, MediaMatchStrategy,
    MediaOperationResult, MediaProgress,
)


def match_value(value: str, request: MediaLinkRequest) -> str:
    value = unicodedata.normalize("NFC", str(value or "").strip())
    if request.source_field == "relative_path" or request.comparison != "exact":
        value = value.replace("\\", "/")
    if request.comparison in {"filename", "stem"}:
        value = value.rsplit("/", 1)[-1]
    if request.comparison == "stem":
        value = PurePosixPath(value).stem
    return value.casefold() if request.ignore_case else value


def _check_cancel(should_cancel):
    if should_cancel and should_cancel():
        raise RuntimeError("Media operation cancelled.")


def build_media_link_plan(request, metadata_values, assets, existing_links, *, source_revision=0,
                          metadata_revision=0, progress=None, should_cancel=None):
    """Build compact associations in O(metadata rows + assets), with bounded examples."""
    if request.comparison not in {"exact", "filename", "stem"}:
        raise ValueError("Choose a valid comparison.")
    by_value = {}
    for index, (row_id, raw) in enumerate(metadata_values):
        if index % 2000 == 0:
            _check_cancel(should_cancel)
            if progress:
                progress(MediaProgress("Reading metadata", index, len(metadata_values)))
        value = match_value(raw, request)
        if value:
            by_value[value] = None if value in by_value else int(row_id)
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
    old_assets = {asset for asset, _ in existing_links}
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
        if not request.replace_existing and asset_id in old_assets:
            continue
        row_id = by_value.get(key)
        if not key:
            blank += 1
            sample("unmatched", raw, None, "Blank identifier")
        elif counts[key] > 1 or (key in by_value and row_id is None) or row_id in occupied:
            ambiguous += 1
            sample("ambiguous", raw, row_id, "Duplicate key or metadata row already linked")
        elif row_id is not None:
            matches.append((asset_id, row_id, key))
            sample("matched", raw, row_id, "Link")
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
    return MediaLinkPlan(request, source_revision, metadata_revision, tuple(matches), tuple(new_rows),
                         preserved, removed, unmatched, ambiguous, blank,
                         {kind: tuple(rows) for kind, rows in examples.items()},
                         max(0, len(metadata_values) - len(covered_rows)),
                         len(set(existing_links) - {(asset, row) for asset, row, _ in matches}) if request.replace_existing else 0)


class MediaLinkService:
    def __init__(self, media_repository, metadata_repository, link_repository):
        self._media_repository = media_repository
        self._metadata_repository = metadata_repository
        self._link_repository = link_repository

    def list_metadata_columns(self):
        return [column.name for column in self._metadata_repository.list_columns() if column.name != "id"]

    def has_metadata_rows(self):
        return self._metadata_repository.is_initialized() and self._metadata_repository.count_rows() > 0

    def saved_request(self, source_id):
        return self._link_repository.saved_request(source_id)

    def preview_plan(self, request: MediaLinkRequest, *, progress=None, should_cancel=None):
        _check_cancel(should_cancel)
        if request.metadata_column not in self.list_metadata_columns():
            raise ValueError("Choose an existing metadata column.")
        source = self._media_repository.get_source(request.source_id)
        if source is None:
            raise ValueError("This media source no longer exists.")
        revision = self._metadata_repository.revision()
        values = self._metadata_repository.column_items(request.metadata_column, should_cancel=should_cancel)
        plan = build_media_link_plan(
            request, values, self._link_repository.matching_records(request.source_id, request.source_field, should_cancel),
            self._link_repository.linked_pairs(request.source_id), source_revision=source.source_revision,
            metadata_revision=revision, progress=progress, should_cancel=should_cancel,
        )
        current = self._media_repository.get_source(request.source_id)
        if current is None or current.source_revision != source.source_revision or revision != self._metadata_repository.revision():
            raise ValueError("Data changed during preview. Refresh the preview.")
        return plan

    def apply_plan(self, plan, *, progress=None, should_cancel=None):
        _check_cancel(should_cancel)
        self._link_repository.publish_plan(plan, self._metadata_repository, updated_at=_now(),
                                          progress=progress, should_cancel=should_cancel)
        summary = self.summary(plan.request.source_id)
        return MediaOperationResult(summary, links_changed=True, metadata_changed=bool(plan.new_rows),
                                    created_count=len(plan.new_rows), linked_count=len(plan.matches)+len(plan.new_rows))

    def create_initial_metadata(self, source_id, *, progress=None, should_cancel=None):
        _check_cancel(should_cancel)
        count = self._link_repository.create_initial_metadata(
            source_id, self._metadata_repository, updated_at=_now(), progress=progress, should_cancel=should_cancel)
        return MediaOperationResult(self.summary(source_id), links_changed=bool(count), metadata_changed=bool(count),
                                    created_count=count, linked_count=count)

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
        return [(asset, links.get(asset.id)) for asset in assets]

    def linked_assets_for_sources(self, source_ids):
        assets = self._media_repository.ready_assets_for_sources(source_ids)
        links = self._link_repository.links_for_assets([asset.id for asset in assets])
        return [(asset, links[asset.id]) for asset in assets if asset.id in links]


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
