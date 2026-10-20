from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from EmbeddingsArea.model_catalog import ModelCatalog
from EmbeddingsArea.repository import EmbeddingRunRepository, MediaLinkRepository, MediaRepository
from EmbeddingsArea.service import (
    EmbeddingCatalogService,
    EmbeddingDatasetService,
    EmbeddingRunService,
    EmbeddingsService,
    ClusterService,
    MediaLibraryService,
    MediaLinkService,
)
from MetadataArea.repository import MetadataRepository


_APP_STARTED_AT = datetime.now(timezone.utc)


@dataclass
class EmbeddingsAreaSession:
    service: EmbeddingsService
    media_library: MediaLibraryService
    media_links: MediaLinkService
    runs: EmbeddingRunService
    datasets: EmbeddingDatasetService
    clusters: ClusterService
    catalog: EmbeddingCatalogService
    media_repository: MediaRepository
    link_repository: MediaLinkRepository
    run_repository: EmbeddingRunRepository
    metadata_repository: MetadataRepository
    model_catalog: ModelCatalog

    @classmethod
    def for_project(cls, project) -> "EmbeddingsAreaSession":
        media_repository = MediaRepository(project.paths.media / "media_index.db")
        link_repository = MediaLinkRepository(project.paths.media / "media_index.db")
        run_repository = EmbeddingRunRepository(project.paths.embeddings / "embeddings.db")
        metadata_repository = MetadataRepository(project.paths.metadata_database, project.paths.metadata_columns)
        model_catalog = ModelCatalog()
        catalog = EmbeddingCatalogService(model_catalog)
        media_library = MediaLibraryService(project, media_repository, metadata_repository, link_repository)
        media_links = MediaLinkService(media_repository, metadata_repository, link_repository)
        runs = EmbeddingRunService(project, media_library, media_links, run_repository, catalog)
        runs.discard_restored_transient_runs(_APP_STARTED_AT)
        datasets = EmbeddingDatasetService(media_library, runs)
        clusters = ClusterService(
            project,
            datasets,
            run_repository,
            metadata_repository,
            model_catalog=model_catalog,
        )
        clusters.discard_restored_transient_results(_APP_STARTED_AT)
        service = EmbeddingsService(
            project,
            media_repository=media_repository,
            runs_repository=run_repository,
            metadata_repository=metadata_repository,
            model_catalog=model_catalog,
            link_repository=link_repository,
        )
        return cls(
            service=service,
            media_library=media_library,
            media_links=media_links,
            runs=runs,
            datasets=datasets,
            clusters=clusters,
            catalog=catalog,
            media_repository=media_repository,
            link_repository=link_repository,
            run_repository=run_repository,
            metadata_repository=metadata_repository,
            model_catalog=model_catalog,
        )
