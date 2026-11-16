"""Worker entry points for media operations; no catalog or embedding session setup."""
from EmbeddingsArea.domain import MediaOperationResult
from EmbeddingsArea.media_library import MediaLibraryService
from EmbeddingsArea.media_linking import MediaLinkService
from EmbeddingsArea.media_sources import inspect_media_source
from EmbeddingsArea.repository import MediaRepository, MediaLinkRepository
from MetadataArea.repository import MetadataRepository


def services(project, table_id="metadata"):
    media = MediaRepository(project.paths.media / "media_index.db")
    links = MediaLinkRepository(media.database_path)
    metadata = MetadataRepository(project.paths.metadata_database, project.paths.metadata_columns, table_id=table_id)
    return (MediaLibraryService(project, media, metadata, links),
            MediaLinkService(media, metadata, links), metadata)


def inspect_source(path, recursive, progress, should_cancel):
    return inspect_media_source(
        path,
        recursive=recursive,
        stage_callback=progress,
        should_cancel=should_cancel,
    )


def scan_source(project, source_id, progress, should_cancel, registration=None, inspection=None):
    library, _, _ = services(project)
    if registration is not None:
        registered = library.register_source(
            registration.path,
            registration.content_type,
            recursive=registration.recursive,
        )
        source_id = registered.id
    summary = library.scan_source(
        source_id,
        stage_callback=progress,
        should_cancel=should_cancel,
        inspection=inspection,
    )
    if summary.source.error_message:
        raise ValueError(summary.source.error_message)
    return MediaOperationResult(summary, catalog_changed=True)


def _validate_link_request(request):
    """Validate the small, explicit set of reviewed source-matching rules."""
    if request.create_missing:
        raise ValueError("Only embedding publication can create items. Keep unmatched media available.")
    if (request.source_field not in {"relative_path", "identifier"}
            or request.comparison not in {"exact", "filename", "stem", "stem_suffix"}):
        raise ValueError("Choose a supported source field and matching rule.")


def preview_links(project, request, progress, should_cancel):
    _validate_link_request(request)
    _, linking, _ = services(project, request.table_id)
    return linking.preview_plan(request, progress=progress, should_cancel=should_cancel)


def suggest_links(project, source_id, replace_existing, progress, should_cancel):
    _, linking, _ = services(project)
    return linking.suggest_plan(source_id, replace_existing=replace_existing,
                                progress=progress, should_cancel=should_cancel)


def apply_links(project, plan, progress, should_cancel):
    _validate_link_request(plan.request)
    _, linking, _ = services(project, plan.request.table_id)
    return linking.apply_plan(plan, progress=progress, should_cancel=should_cancel)


def create_metadata(project, source_id, progress, should_cancel):
    raise ValueError("Import or calculate embeddings to create items.")


def relocate_source(project, source_id, path, progress, should_cancel):
    library, _, _ = services(project)
    summary = library.relocate_source(source_id, path, progress=progress, should_cancel=should_cancel)
    return MediaOperationResult(summary, catalog_changed=True)
