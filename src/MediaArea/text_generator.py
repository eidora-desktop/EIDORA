"""Prepare small text excerpts using the existing preview job/publication lifecycle."""
from datetime import datetime
from uuid import uuid4

from DiscoveryArea.thumbnails.repository import PreviewText, ThumbnailVariant
from MediaArea.text_options import select_excerpt, text_parameters
from MediaArea.text_source_repository import TextSourceReader
from MediaArea.thumbnail_generator import ThumbnailGenerationCancelled, ThumbnailGenerationResult, _emit


def create_text_variant(repo, *, label, source_dir, identifier_column, inputs, source_id,
                        parameters, progress_callback=None, should_cancel=None, pause=None,
                        variant_id=None, input_label=""):
    parameters = text_parameters(parameters)
    label = label.strip()
    if not label:
        raise ValueError("Enter a name for the Preview Assets.")
    variant_id = variant_id or uuid4().hex

    def checkpoint():
        if pause:
            pause.checkpoint()
        if should_cancel and should_cancel():
            raise ThumbnailGenerationCancelled(variant_id)

    checkpoint()
    # Group shared record files so each index is scanned only once.
    inputs = sorted(inputs, key=lambda item: (str(item.path), item.text_record_key, item.metadata_id))
    if not inputs:
        raise ValueError("No linked text sources are available for creating Preview Assets.")
    if len({item.metadata_id for item in inputs}) != len(inputs):
        raise ValueError("Duplicate Item identity in Preview Assets.")
    if any(item.media_type != "text" for item in inputs):
        raise ValueError("Text excerpts require a linked text source.")
    reader = TextSourceReader(checkpoint)
    previews, errors = [], []
    failed = missing = 0
    last_key = last_selection = None
    _emit(progress_callback, "start", len(inputs), 0, "Generating text excerpts…")
    with repo.stage_variant(variant_id) as staging:
        for done, item in enumerate(inputs, 1):
            checkpoint()
            key = (item.path, item.text_record_key, item.source_asset_id)
            try:
                if key != last_key:
                    text = reader.read(item.path, item.text_record_key)
                    identity = f"{source_id}:{item.source_asset_id or str(item.path)}:{item.text_record_key}"
                    excerpt, start, end = select_excerpt(text, parameters, identity)
                    if not excerpt:
                        raise ValueError("The selected text excerpt contains only whitespace.")
                    last_selection = (excerpt, start, end, len(text), len(excerpt.encode('utf-8')))
                    last_key = key
                excerpt, start, end, length, size = last_selection
                previews.append(PreviewText(variant_id, item.metadata_id, excerpt, item.source_asset_id,
                                           item.text_record_key, start, end, length, size))
            except (OSError, UnicodeError, ValueError) as exc:
                missing += isinstance(exc, FileNotFoundError)
                failed += not isinstance(exc, FileNotFoundError)
                if len(errors) < 10:
                    record = f" (record {item.text_record_key})" if item.text_record_key else ""
                    errors.append(f"{item.path.name}{record}: {exc}"[:1000])
            _emit(progress_callback, "step", done, failed + missing, f"Creating excerpts: {done}/{len(inputs)}")
        checkpoint()
        if not previews:
            raise RuntimeError(f"No text excerpts created: {failed} failed, {missing} missing.\n" + "\n".join(errors))
        variant = ThumbnailVariant(variant_id, label, source_dir, identifier_column, (),
            datetime.now().isoformat(timespec="seconds"), source_id=source_id, source_media_type="text",
            generated_files=len(previews), failed_files=failed, missing_files=missing, errors=tuple(errors),
            input_kind="media_source", input_label=input_label, preview_kind="text_excerpt", parameters=parameters,
            total_bytes=sum(preview.size_bytes for preview in previews))
        _emit(progress_callback, "index", len(previews), 0, "Saving text excerpts…")
        checkpoint()
        repo.publish_variant(variant, [], staging, texts=previews)
    _emit(progress_callback, "complete", len(previews), failed + missing, "Text excerpts are ready.")
    return ThumbnailGenerationResult(variant_id, label, len(previews), failed, missing, tuple(errors), variant.total_bytes)
