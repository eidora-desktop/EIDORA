"""Reduction workflow: validate, compute privately, then publish."""
from dataclasses import replace
from pathlib import Path
import tempfile
import uuid

from EmbeddingsArea.reduction.domain import ReductionResult
from EmbeddingsArea.reduction.parameters import ReductionCancelled, validate_request
from EmbeddingsArea.reduction.repository import fingerprint, inspect_source, publish
from EmbeddingsArea.reduction.process_runner import run_isolated
from EmbeddingsArea.repository import EmbeddingRunRepository


class ReductionService:
    def __init__(self, project):
        self.project = project
        self.runs = EmbeddingRunRepository(project.paths.embeddings / "embeddings.db")

    def execute(self, source_id, request, *, output_id=None, cancelled=lambda: False, status=lambda value: None):
        source = self.runs.get_run(source_id)
        if source is None or not source.artifact_path:
            raise ValueError("Select published vector data to reduce.")
        stamp = fingerprint(source.artifact_path)
        _, shape, _ = inspect_source(source.artifact_path)
        validate_request(request, *shape)
        label = self.runs.validate_run_label(request.label, exclude_run_id=source.id if request.replace_original else "")
        request = replace(request, label=label)
        output_id = source.id if request.replace_original else output_id or f"run_{uuid.uuid4().hex}"
        if not request.replace_original and self.runs.get_run(output_id) is not None:
            raise ValueError("The reduction destination already exists.")
        with tempfile.TemporaryDirectory(prefix=".reduction-", dir=self.project.paths.embeddings) as directory:
            if cancelled():
                raise ReductionCancelled("Reduction cancelled.")
            recipe = run_isolated(source.artifact_path, directory, request, source.id, cancelled, status, source_label=source.label)
            if cancelled():
                raise ReductionCancelled("Reduction cancelled; original vectors are unchanged.")
            status("Publishing reduced vector data…")
            run, warning = publish(self.project, self.runs, source, output_id, Path(directory) / "result.npz",
                                   request, stamp)
        return ReductionResult(run.id, recipe["variance_retained"], request.dimensions, warning, recipe)
