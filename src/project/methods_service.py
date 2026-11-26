"""Find saved analysis reports independently of the active workspace."""
from pathlib import Path

from project.domain import Project
from shared.methods_report import MethodsReport, combine_reports


class ProjectMethodsService:
    def __init__(self, project: Project):
        self.project = project

    def sources(self) -> list[tuple[str, str]]:
        from DiscoveryArea.presets_repository import PresetRepository
        from DiscoveryArea.repository import DiscoveryRepository
        from EmbeddingsArea.clustering.repository import ClusteringRepository
        from EmbeddingsArea.domain import EmbeddingRunState
        from EmbeddingsArea.repository import EmbeddingRunRepository

        sources = []
        database = self.project.paths.embeddings / "embeddings.db"
        if database.exists():
            sources.extend((f"embedding:{run.id}", f"Embeddings: {run.label}")
                           for run in EmbeddingRunRepository(database).list_runs()
                           if run.state == EmbeddingRunState.COMPLETED and run.artifact_path)
        if self.project.paths.metadata_groups_database.exists():
            sources.extend((f"clustering:{record['id']}", f"Clustering: {record['label']}")
                           for record in ClusteringRepository(self.project.paths.metadata_groups_database).list()
                           if record.get("state") == "completed")
        sources.extend((f"projection:{option.key}", f"Projection: {option.label}")
                       for option in DiscoveryRepository().list_discovery_options(self.project)
                       if not option.is_random)
        sources.extend((f"snapshot:{entry.path}", f"Snapshot: {entry.label}")
                       for entry in PresetRepository(self.project.paths.snapshot_presets, create=False).list_entries())
        return sources

    def load(self, key: str) -> MethodsReport:
        from dataclasses import replace
        from DiscoveryArea.provenance import ProjectionProvenanceReportService
        from DiscoveryArea.repository import DiscoveryRepository
        from DiscoveryArea.snapshot_provenance import SnapshotMethodsService
        from EmbeddingsArea.clustering.repository import ClusteringRepository
        from EmbeddingsArea.provenance import clustering_report, saved_embedding_report

        kind, identity = key.split(":", 1)
        if kind == "embedding":
            return saved_embedding_report(self.project, identity)
        if kind == "clustering":
            record = ClusteringRepository(self.project.paths.metadata_groups_database).get(identity)
            if record.get("state") != "completed":
                raise ValueError("This clustering result has not completed.")
            cluster = clustering_report(record)
            try:
                embedding = saved_embedding_report(self.project, str(record["run_id"]))
            except (OSError, ValueError, RuntimeError) as exc:
                return replace(cluster, warnings=(*cluster.warnings, str(exc)))
            return replace(combine_reports(record["label"], [embedding, cluster]), scope=cluster.scope)
        if kind == "projection":
            option = next((option for option in DiscoveryRepository().list_discovery_options(self.project)
                           if option.key == identity), None)
            if option is None:
                raise ValueError("This projection is no longer available.")
            return ProjectionProvenanceReportService().build(option.label, option.provenance)
        if kind == "snapshot":
            return SnapshotMethodsService().load(self.project, Path(identity))
        raise ValueError("Unknown methods report source.")
