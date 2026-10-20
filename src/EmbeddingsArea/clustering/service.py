"""Clustering workflow and publication; Qt-independent."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import json
import re
import tempfile
import uuid
from MetadataArea.group_service import MetadataGroupService
from EmbeddingsArea.clustering.base import normalize_parameters
from EmbeddingsArea.clustering.parameters import PREPROCESSING, preprocessing_defaults
from EmbeddingsArea.clustering.registry import ClusteringRegistry
from EmbeddingsArea.clustering.repository import ClusteringRepository
from EmbeddingsArea.clustering.process_runner import ClusteringCancelled, run_isolated


def now():
    return datetime.now(timezone.utc).isoformat()


def _timestamp_precedes(value, cutoff):
    raw = str(value or "").strip()
    if not raw:
        return True
    try:
        timestamp = datetime.fromisoformat(
            raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        )
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return timestamp.astimezone(timezone.utc) < cutoff.astimezone(timezone.utc)


class ClusterService:
    def __init__(self, project, datasets, runs_repository, metadata_repository, registry=None, model_catalog=None):
        self._project = project
        self._metadata = metadata_repository
        self._runs = runs_repository
        self._model_catalog = model_catalog
        self._registry = registry or ClusteringRegistry()
        self.repository = ClusteringRepository(project.paths.metadata_groups_database)
        self.groups = MetadataGroupService(self.repository.groups, metadata_repository)

    def require_method(self, method_id):
        return self._registry.require_method(method_id)

    def list_methods(self):
        return self._registry.list_methods()

    def default_preprocessing(self, method_id):
        self.require_method(method_id)
        return normalize_parameters(PREPROCESSING, preprocessing_defaults(method_id))

    def list_cluster_results(self, run_id):
        return self.repository.list(run_id)

    def discard_restored_transient_results(self, app_started_at):
        """Remove unsuccessful attempts retained only for the previous session."""
        discarded = 0
        for record in self.repository.list():
            if record.get("state") not in {"draft", "aborted", "failed", "interrupted"}:
                continue
            if not _timestamp_precedes(record.get("created_at"), app_started_at):
                continue
            self.repository.delete(record["id"])
            discarded += 1
        return discarded

    def default_cluster_name(self, run_id, method_id="kmeans", *, exclude_result_id=""):
        method = self.require_method(method_id)
        algorithm = re.sub(r"\s+", "-", method.name.strip().casefold())
        embedding = self._embedding_name(run_id)
        return self._available_name(f"{algorithm}_{embedding}", exclude_result_id=exclude_result_id)

    def cluster_name_error(self, label, *, exclude_result_id=""):
        name = str(label or "").strip()
        if not name:
            return "Enter a clustering name."
        used = {
            str(record.get("label") or "").strip().casefold()
            for record in self.repository.list()
            if record["id"] != exclude_result_id
        }
        used.update(group_set.name.strip().casefold() for group_set in self.groups.list_group_sets())
        if name.casefold() in used:
            return "Clustering name already exists."
        return ""

    def _embedding_name(self, run_id):
        run = self._runs.get_run(run_id)
        label = "Embeddings"
        if run is not None:
            model = self._model_catalog.get_model(run.selected_model_id) if self._model_catalog is not None else None
            label = model.label if model is not None else run.selected_model_id or run.label or label
        label = re.sub(r"\s*\([^)]*\)\s*", " ", str(label)).strip()
        label = re.sub(r"^EIDORA/", "", label, flags=re.IGNORECASE)
        label = re.sub(r"(?:[_\s-](?:IN1k|ImageNet(?:-?1K)?))$", "", label, flags=re.IGNORECASE)
        label = re.sub(r"\s+Embeddings(?:\s+\d+)?$", "", label, flags=re.IGNORECASE)
        label = "-".join(label.split())
        return label or "Embeddings"

    def _available_name(self, base, *, exclude_result_id=""):
        used = {
            str(record.get("label") or "").strip().casefold()
            for record in self.repository.list()
            if record["id"] != exclude_result_id
        }
        used.update(group_set.name.strip().casefold() for group_set in self.groups.list_group_sets())
        if base.casefold() not in used:
            return base
        index = 2
        while f"{base}_{index}".casefold() in used:
            index += 1
        return f"{base}_{index}"

    def create_draft(self, run_id, label=None, source=None):
        auto_named = label is None
        method_id = source["method_id"] if source else "kmeans"
        if label is None:
            label = self.default_cluster_name(run_id, method_id)
        label = str(label).strip()
        if not label:
            raise ValueError("Enter a clustering name.")
        record = dict(id="cluster_" + uuid.uuid4().hex, run_id=run_id, label=label,
                      method_id=method_id, method_settings={}, preprocessing=self.default_preprocessing(method_id),
                      preprocessing_custom=False, state="draft", created_at=now(), warnings=[], auto_named=auto_named)
        if source:
            record.update(method_id=source["method_id"], method_settings=source.get("method_settings", {source["method_id"]: source.get("parameters", {})}),
                          preprocessing=normalize_parameters(PREPROCESSING, source.get("preprocessing", {})),
                          preprocessing_custom=True)
        return self.repository.save_unique_name(record)

    def update_draft(self, result_id, *, label, method_id, method_settings, preprocessing,
                     auto_named=None, preprocessing_custom=None):
        record = self.repository.get(result_id)
        if record["state"] in ("queued", "running", "saving", "completed"):
            raise ValueError("These clustering settings are locked.")
        self._registry.require_method(method_id)
        if not label.strip():
            raise ValueError("Enter a clustering name.")
        record.update(label=label.strip(), method_id=method_id, method_settings=method_settings,
                      preprocessing=normalize_parameters(PREPROCESSING, preprocessing))
        if auto_named is not None:
            record["auto_named"] = bool(auto_named)
        if preprocessing_custom is not None:
            record["preprocessing_custom"] = bool(preprocessing_custom)
        return self.repository.save_unique_name(record)

    def rename_cluster_result(self, run_id, cluster_id, label):
        record = self.repository.get(cluster_id)
        if record["run_id"] != run_id or not label.strip():
            raise ValueError("Enter a valid clustering name.")
        if record["label"] == label.strip():
            return record
        record["label"] = label.strip()
        record["auto_named"] = False
        return self.repository.save_unique_name(record)

    def delete_result(self, result_id, *, delete_groups=False):
        record = self.repository.get(result_id)
        group_set_id = str(record.get("group_set_id") or "")
        if delete_groups:
            self.repository.delete_with_group_set(result_id, group_set_id)
        else:
            self.repository.delete(result_id)
        return bool(delete_groups and group_set_id)

    def execute(self, result_id, *, isolated=True, cancelled=lambda: False, status=lambda value: None):
        import numpy as np
        from EmbeddingsArea.clustering.pipeline import compute, source_snapshot, validate_output

        record = self.repository.start(result_id)
        try:
            with tempfile.TemporaryDirectory(prefix="eidora-clustering-") as directory:
                result = (run_isolated(self._project, record, directory, cancelled, status) if isolated else
                          compute(self._project, record, directory, status))
                if cancelled():
                    raise ClusteringCancelled("Clustering aborted.")
                status("Validating results")
                labels = np.load(Path(directory) / "labels.npy", allow_pickle=False)
                ids = np.load(Path(directory) / "metadata_ids.npy", allow_pickle=False)
                strength_path = Path(directory) / "strengths.npy"
                strengths = np.load(strength_path, allow_pickle=False) if strength_path.exists() else None
                validate_output(labels, ids, strengths)
                status("Saving groups")
                self.repository.mark_saving(record)
                with self.repository.groups.transaction() as connection:
                    latest_row = connection.execute(
                        "SELECT payload FROM clustering_runs WHERE id=? AND deleted=0",
                        (result_id,),
                    ).fetchone()
                    if latest_row is None:
                        raise ValueError("Clustering result no longer exists.")
                    latest = json.loads(latest_row[0])
                    record["label"] = latest["label"]
                    if source_snapshot(self._project, record["run_id"])[2] != result["source_signature"]:
                        raise ValueError("Source embeddings or metadata links changed. Retry clustering.")
                    assignments = self._assignments(labels, ids)
                    group_set_id = self.groups.publish_exclusive_groups(connection, record["label"], record["id"], assignments)
                    record.update(result, state="completed", completed_at=now(), group_set_id=group_set_id, error="")
                    self.repository.write(connection, record)
                status("Completed")
            return record
        except Exception as exc:
            record.update(state="aborted" if isinstance(exc, ClusteringCancelled) else "failed", error=str(exc))
            self.repository.save(record)
            raise

    @staticmethod
    def _assignments(labels, ids):
        import numpy as np

        assignments = {}
        order = np.argsort(labels, kind="stable")
        sorted_labels, sorted_ids = labels[order], ids[order]
        unique, starts = np.unique(sorted_labels, return_index=True)
        ends = list(starts[1:]) + [len(labels)]
        cluster_number = 0
        for label, start, end in zip(unique, starts, ends, strict=True):
            if label == -1:
                name = "Noise"
            else:
                cluster_number += 1
                name = f"Cluster {cluster_number}"
            assignments[name] = sorted_ids[start:end].tolist()
        return assignments

    def create_clusters(self, run_id, *, method_id, label, parameters, progress_callback=None, preprocessing=None):
        record = self.create_draft(run_id, label or method_id)
        record.update(method_id=method_id, method_settings={method_id: parameters})
        record["preprocessing"] = (
            self.default_preprocessing(method_id)
            if preprocessing is None else normalize_parameters(PREPROCESSING, preprocessing)
        )
        record["preprocessing_custom"] = preprocessing is not None
        self.repository.save(record)
        if progress_callback:
            progress_callback(5)
        result = self.execute(record["id"], isolated=False)
        if progress_callback:
            progress_callback(100)
        return result

    def create_kmeans_clusters(self, run_id, *, label, cluster_count):
        return self.create_clusters(run_id, method_id="kmeans", label=label, parameters={"n_clusters": cluster_count})

    def initialize(self):
        """Recover interrupted jobs once on project activation."""
        self.repository.recover()
