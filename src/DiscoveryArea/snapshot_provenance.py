"""Assemble snapshot methods from saved analysis records, never live UI state."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re

from DiscoveryArea.provenance import ProjectionProvenanceReportService
from DiscoveryArea.repository import DiscoveryRepository
from DiscoveryArea.snapshot_repository import SnapshotRepository
from DiscoveryArea.settings_provenance import settings_report
from EmbeddingsArea.clustering.repository import ClusteringRepository
from EmbeddingsArea.provenance import clustering_report, saved_embedding_report
from shared.methods_report import MethodsReport, combine_reports, _value


class SnapshotMethodsService:
    def load(self, project, path: Path) -> MethodsReport:
        snapshot = SnapshotRepository(project.root, project.paths.snapshot_presets).load(path)
        if snapshot is None:
            raise ValueError("Snapshot could not be read.")
        if snapshot.methods_report:
            return MethodsReport.from_payload(snapshot.methods_report)
        return self.build(project, snapshot, legacy=True)

    def freeze(self, project, snapshot):
        # An update is a new capture; do not retain the previous report.
        report = self.build(project, snapshot)
        return replace(snapshot, methods_report=report.to_payload())

    def build(self, project, snapshot, *, legacy=False) -> MethodsReport:
        reports, warnings = [], []
        if legacy:
            warnings.append("This snapshot predates frozen methods reports; available source records are used below.")
        option = next((option for option in DiscoveryRepository().list_discovery_options(project)
                       if option.key == snapshot.discovery_key
                       or (snapshot.discovery_ref and str(option.discovery_file) == snapshot.discovery_ref)), None)
        if option is not None and option.provenance:
            reports.append(ProjectionProvenanceReportService().build(option.label, option.provenance))
        else:
            name = option.label if option else ("Random" if snapshot.discovery_key == "random" else Path(snapshot.discovery_key).stem)
            reports.append(MethodsReport("Projection", f"The snapshot uses {name} coordinates.",
                (("Projection settings", _value(option.parameters)),) if option else (), (), ()))
            warnings.append("Complete projection and embedding provenance was not recorded for this snapshot.")
            source_key = str(option.source.get("key") or "") if option else ""
            if source_key.startswith("run:"):
                try:
                    reports.append(saved_embedding_report(project, source_key.removeprefix("run:")))
                except (OSError, ValueError, RuntimeError) as exc:
                    warnings.append(str(exc))
        projection_selection = option.provenance.get("selection", {}) if option else {}
        states = (snapshot.appearance_state, snapshot.filters_state, snapshot.slicer_state, projection_selection)
        reports.extend(self.clustering_reports(project, states))
        reports.append(settings_report(snapshot.name, filters=snapshot.filters_state, slicer=snapshot.slicer_state,
                                       transform=snapshot.projection.transform if snapshot.projection else snapshot.transform_state))
        reports.append(MethodsReport("Snapshot", "", (), (), (
            ("Snapshot created", snapshot.created_at),
            ("Snapshot item count", str(snapshot.metadata_signature.count)),
            ("Snapshot item checksum", snapshot.metadata_signature.checksum),
        )))
        return combine_reports(snapshot.name, reports, warnings=warnings)

    @staticmethod
    def clustering_reports(project, states) -> list[MethodsReport]:
        fields = set()
        def visit(value):
            if isinstance(value, dict):
                for child in value.values():
                    visit(child)
            elif isinstance(value, (tuple, list)):
                for child in value:
                    visit(child)
            elif isinstance(value, str):
                fields.update(re.findall(r"group-set:([A-Za-z0-9_-]+)", value))
        visit(states)
        if not fields:
            return []
        repository = ClusteringRepository(project.paths.metadata_groups_database)
        reports, seen_runs = [], set()
        for record in repository.list():
            if record.get("group_set_id") not in fields or record.get("state") != "completed":
                continue
            report = clustering_report(record)
            run_id = str(record.get("run_id") or "")
            if run_id and run_id not in seen_runs:
                seen_runs.add(run_id)
                try:
                    reports.append(saved_embedding_report(project, run_id))
                except (OSError, ValueError, RuntimeError) as exc:
                    report = replace(report, warnings=(*report.warnings, str(exc)))
            reports.append(report)
        return reports
