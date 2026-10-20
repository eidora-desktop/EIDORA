"""Methods reports from saved embedding summaries and clustering records."""
from __future__ import annotations

from shared.methods_report import (
    CitationRecord, MethodsReport, _citation_records, _mapping, _value,
    deduplicate_citations, eidora_citation_payload, setting_rows, citation_payloads,
)


def embedding_citation_payloads(summary: dict) -> list[dict[str, object]]:
    """Prefer the package BibTeX frozen in the artifact; retain legacy support."""
    runtime = _mapping(summary.get("provenance"))
    metadata = _mapping(runtime.get("model_metadata"))
    model_provenance = _mapping(metadata.get("provenance"))
    raw = (runtime.get("bibtex") or metadata.get("citation") or model_provenance.get("citation")
           or runtime.get("citation") or summary.get("citation"))
    return citation_payloads(raw, default_role="model")


def embedding_report(label: str, summary: dict) -> MethodsReport:
    runtime = _mapping(summary.get("provenance"))
    citations = _citation_records(embedding_citation_payloads(summary))
    model = summary.get("model_name") or summary.get("model_id") or summary.get("selected_model_id")
    count = summary.get("num_items", summary.get("item_count", summary.get("prepared_input_count", "unrecorded")))
    text = f"Embeddings for {count} items were generated using {model}." if model else "Imported embeddings were used; the generating model was not recorded."
    rows = [("Embedding model", str(model or "not recorded"))]
    for key, name in (("preprocess", "Preprocessing"), ("postprocess", "Postprocessing"),
                      ("parameter_values", "Saved run settings"), ("dimensions", "Dimensions")):
        if summary.get(key) is not None:
            rows.extend(setting_rows(name, summary[key]))
    details = [("Run ID", str(summary.get("run_id") or "not recorded"))]
    for key in ("eidora_version", "model_version", "package_fingerprint", "input_signature", "execution_providers", "final_execution_mode"):
        details.append((key.replace("_", " ").capitalize(), _value(runtime.get(key, "not recorded"))))
    warnings = []
    if not citations:
        warnings.append("No model citation was recorded in the embedding artifact.")
    return MethodsReport(f"{label} — Methods and Citations", text, tuple(rows),
                         deduplicate_citations([CitationRecord.from_payload(eidora_citation_payload()), *citations]),
                         tuple(details), tuple(warnings))


# References to the actual scikit-learn implementations used by this application.
# These remain available offline; opening their URLs is an explicit user action.
CLUSTER_DOCUMENTATION = {
    "kmeans": ("K-means", "clustering.html#k-means"),
    "minibatch_kmeans": ("Mini Batch K-Means", "clustering.html#mini-batch-k-means"),
    "hdbscan": ("HDBSCAN", "clustering.html#hdbscan"),
    "agglomerative": ("Agglomerative clustering", "clustering.html#hierarchical-clustering"),
    "dbscan": ("DBSCAN", "clustering.html#dbscan"),
    "gaussian_mixture": ("Gaussian mixture models", "mixture.html#gaussian-mixture"),
}


CLUSTER_PAPERS = {
    "kmeans": {"id": "lloyd1982", "title": "Least squares quantization in PCM",
        "authors": ["Lloyd, Stuart P."], "year": "1982", "doi": "10.1109/TIT.1982.1056489"},
    "minibatch_kmeans": {"id": "sculley2010", "title": "Web-Scale K-Means Clustering",
        "authors": ["Sculley, D."], "year": "2010",
        "url": "https://www.ccs.neu.edu/home/vip/teach/DMcourse/2_cluster_EM_mixt/notes_slides/sculey_webscale_kmeans_approx.pdf"},
    "hdbscan": {"id": "campello2013", "title": "Density-Based Clustering Based on Hierarchical Density Estimates",
        "authors": ["Campello, Ricardo J. G. B.", "Moulavi, Davoud", "Sander, Joerg"],
        "year": "2013", "doi": "10.1007/978-3-642-37456-2_14"},
    "dbscan": {"id": "ester1996", "title": "A Density-Based Algorithm for Discovering Clusters in Large Spatial Databases with Noise",
        "authors": ["Ester, M.", "Kriegel, H. P.", "Sander, J.", "Xu, X."], "year": "1996",
        "url": "https://www.aaai.org/Papers/KDD/1996/KDD96-037.pdf"},
    "gaussian_mixture": {"id": "dempster1977", "title": "Maximum Likelihood from Incomplete Data Via the EM Algorithm",
        "authors": ["Dempster, A. P.", "Laird, N. M.", "Rubin, D. B."], "year": "1977",
        "doi": "10.1111/j.2517-6161.1977.tb01600.x"},
}


def clustering_report(record: dict) -> MethodsReport:
    method_id = str(record.get("method_id", "unknown"))
    documentation = CLUSTER_DOCUMENTATION.get(method_id)
    name = str(record.get("method_name") or (documentation[0] if documentation else method_id))
    completed = record.get("state") == "completed"
    parameters = record.get("parameters") if completed else None
    warnings = list(record.get("warnings") or [])
    if parameters is None:
        parameters = _mapping(record.get("method_settings")).get(method_id, {})
        warnings.append("Effective clustering parameters were not recorded; settings shown are requested values.")
    text = (f"{name} clustering produced {record.get('cluster_count', 'unrecorded')} clusters "
            f"from {record.get('matched_item_count', 'unrecorded')} items; "
            f"{record.get('noise_count', 'unrecorded')} items were classified as noise.") if completed else f"{name} clustering is configured but has not completed."
    rows = [("Clustering method", name), *setting_rows("Clustering parameters", parameters),
            *setting_rows("Clustering preprocessing", record.get("preprocessing", {}))]
    references = [CitationRecord.from_payload(eidora_citation_payload())]
    if method_id in CLUSTER_PAPERS:
        references.append(CitationRecord.from_payload(CLUSTER_PAPERS[method_id]))
    if documentation:
        references.append(CitationRecord.from_payload({"id": "sklearn-" + method_id, "role": "implementation",
            "title": f"scikit-learn: {documentation[0]}", "url": "https://scikit-learn.org/stable/modules/" + documentation[1]}))
    else:
        warnings.append(f"No citation is available for clustering method {name}.")
    details = [("Clustering run ID", str(record.get("id", "not recorded"))),
               ("Clustering implementation", f"{record.get('backend', 'not recorded')} {record.get('backend_version', '')}".strip()),
               ("Clustering source signature", str(record.get("source_signature", "not recorded")))]
    return MethodsReport(f"{record.get('label', name)} — Methods and Citations", text, tuple(rows),
                         tuple(references), tuple(details), tuple(warnings))


def saved_embedding_report(project, run_id: str) -> MethodsReport:
    """Read only the small saved summary, never the vector payload."""
    from pathlib import Path
    from EmbeddingsArea.repository import EmbeddingRunRepository
    from EmbeddingsArea.runtime.readers import load_embedding_summary

    repository = EmbeddingRunRepository(project.paths.embeddings / "embeddings.db")
    run = repository.get_run(run_id)
    if run is None or not run.artifact_path:
        raise ValueError(f"Source embedding run {run_id} is unavailable.")
    artifact = Path(run.artifact_path)
    if not artifact.is_absolute():
        artifact = project.root / artifact
    summary = load_embedding_summary(artifact)
    values = repository.list_run_parameter_values(run_id)
    summary.update(run_id=run_id, selected_model_id=run.selected_model_id,
                   parameter_values={scope: {v.parameter_id: v.value for v in values if v.scope == scope}
                                     for scope in ("run", "preprocess", "postprocess")})
    return embedding_report(run.label, summary)
