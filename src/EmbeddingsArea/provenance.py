"""Methods reports from saved embedding summaries and clustering records."""
from __future__ import annotations

from shared.methods_report import (
    CitationRecord, MethodsReport, _citation_records, _mapping, _value,
    deduplicate_citations, eidora_citation_payload, setting_rows, citation_payloads, PCA_CITATION,
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
    if model in {"Imported", "Imported embeddings"}:
        model = None
    count = summary.get("num_items", summary.get("item_count", summary.get("prepared_input_count", "unrecorded")))
    version = str(runtime.get("model_version") or "")
    model_text = f"{model}{' ' + version if version else ''}"
    imported = summary.get("source") == "import"
    if not model:
        text = "Embeddings were used; the generating model was not recorded."
    elif imported:
        text = f"Imported embeddings for {count} items were attributed to {model_text}."
    else:
        text = f"Embeddings for {count} items were generated using {model_text}."
    preparation = preprocessing_summary(summary.get("preprocess"))
    if preparation:
        text += " " + preparation
    postprocessing = postprocessing_summary(summary.get("postprocess"))
    if postprocessing:
        text += " " + postprocessing
    if summary.get("dimensions"):
        text += f" The resulting vectors had {summary['dimensions']} dimensions."
    if summary.get("l2_normalized") is True and "L2-normalized" not in postprocessing:
        text += " Output vectors were L2-normalized."
    if any(isinstance(step, dict) and step.get("id") == "pca" for step in summary.get("postprocess") or []):
        citations.append(CitationRecord.from_payload(PCA_CITATION))
    rows = [("Embedding model", str(model or "not recorded"))]
    for key, name in (("preprocess", "Preprocessing"), ("postprocess", "Postprocessing"),
                      ("parameter_values", "Saved run settings"), ("dimensions", "Dimensions")):
        if summary.get(key) is not None:
            rows.extend(setting_rows(name, summary[key]))
    details = [("Run ID", str(summary.get("run_id") or "not recorded"))]
    for key in ("model_version", "package_fingerprint", "input_signature", "execution_providers", "final_execution_mode"):
        details.append((key.replace("_", " ").capitalize(), _value(runtime.get(key, "not recorded"))))
    warnings = []
    if not embedding_citation_payloads(summary):
        warnings.append("No model citation was recorded in the embedding artifact.")
    if not model:
        warnings.append("The generating model was not recorded.")
    if summary.get("failed_items"):
        warnings.append(f"{summary['failed_items']} inputs failed during embedding generation.")
    for key in ("l2_normalized", "input_modalities", "raw_dimensions", "dtype"):
        if summary.get(key) is not None:
            rows.extend(setting_rows(key.replace("_", " ").capitalize(), summary[key]))
    return MethodsReport(f"{label} — Methods and Citations", text, tuple(rows),
                         deduplicate_citations(citations),
                         tuple(details), tuple(warnings), scope=f"Embeddings: {label}")


def preprocessing_summary(value: object) -> str:
    """Describe only recorded, scientifically meaningful input preparation."""
    settings = _mapping(value)
    parts = []
    for key, child in settings.items():
        if isinstance(child, dict):
            summary = preprocessing_summary(child)
            if summary:
                parts.append(f"{key.capitalize()}: {summary}")
    if settings.get("width") and settings.get("height"):
        resize = str(settings.get("resize") or "recorded").replace("_", " ")
        parts.append(f"Images used {resize} resizing to {settings['width']} × {settings['height']} pixels.")
    if settings.get("crop"):
        parts.append(f"Images used {str(settings['crop']).replace('_', ' ')} cropping.")
    if settings.get("max_length"):
        parts.append(f"Text inputs used a maximum sequence length of {settings['max_length']} tokens.")
    if settings.get("sample_rate"):
        parts.append(f"Audio inputs used a sample rate of {settings['sample_rate']} Hz.")
    return " ".join(parts)


def postprocessing_summary(value: object) -> str:
    steps = value if isinstance(value, list) else []
    sentences = []
    for step in steps:
        if isinstance(step, dict) and step.get("id") == "pca":
            name = "Incremental PCA" if step.get("method") == "incremental_pca" else "PCA"
            dimensions = step.get("dimensions")
            sentence = f"{name} reduced the vectors to {dimensions} dimensions" if dimensions else f"{name} was applied to the vectors"
            if step.get("variance_retained") is not None:
                sentence += f", retaining {float(step['variance_retained']):.1%} of the immediate source variance"
            sentences.append(sentence + ".")
        elif isinstance(step, str) and step in {"l2", "l2_normalize", "l2_normalized"}:
            sentences.append("Vectors were L2-normalized.")
    return " ".join(sentences)


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
    preprocessing = _mapping(record.get("preprocessing"))
    if completed:
        preparation = []
        if preprocessing.get("normalize"):
            preparation.append("L2 normalization")
        if preprocessing.get("pca"):
            preparation.append(f"PCA to {preprocessing.get('components', 'unrecorded')} dimensions"
                               + (" with whitening" if preprocessing.get("whiten") else ""))
        text += " Clustering used embedding vectors" + (" after " + " and ".join(preparation) if preparation else "") + "."
        keys = {"kmeans": ("n_clusters",), "minibatch_kmeans": ("n_clusters",),
                "hdbscan": ("metric", "min_cluster_size", "min_samples", "cluster_selection_method"),
                "dbscan": ("metric", "eps", "min_samples"),
                "agglomerative": ("metric", "linkage", "n_clusters", "distance_threshold"),
                "gaussian_mixture": ("n_components", "covariance_type")}.get(method_id, ())
        settings = ", ".join(f"{key}={_value(parameters[key])}" for key in keys if parameters.get(key) is not None)
        if settings:
            text += f" Settings were {settings}."
    rows = [("Clustering method", name), *setting_rows("Clustering parameters", parameters),
            *setting_rows("Clustering preprocessing", record.get("preprocessing", {}))]
    references = [CitationRecord.from_payload(eidora_citation_payload())]
    if preprocessing.get("pca") and completed:
        references.append(CitationRecord.from_payload(PCA_CITATION))
    if method_id in CLUSTER_PAPERS:
        references.append(CitationRecord.from_payload(CLUSTER_PAPERS[method_id]))
    if not documentation:
        warnings.append(f"No citation is available for clustering method {name}.")
    details = [("Clustering run ID", str(record.get("id", "not recorded"))),
               ("Clustering implementation", f"{record.get('backend', 'not recorded')} {record.get('backend_version', '')}".strip()),
               ("Clustering source signature", str(record.get("source_signature", "not recorded")))]
    if documentation:
        details.append(("Implementation documentation", "https://scikit-learn.org/stable/modules/" + documentation[1]))
    if record.get("skipped_reasons"):
        details.extend(setting_rows("Excluded inputs", record["skipped_reasons"]))
    return MethodsReport(f"{record.get('label', name)} — Methods and Citations", text, tuple(rows),
                         tuple(references), tuple(details), tuple(warnings),
                         scope=f"Clustering: {record.get('label', name)} and its source embeddings")


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
    summary.update(run_id=run_id, selected_model_id=run.selected_model_id, failed_items=run.failed_items,
                   parameter_values={scope: {v.parameter_id: v.value for v in values if v.scope == scope}
                                     for scope in ("run", "preprocess", "postprocess")})
    return embedding_report_with_model_fallback(run.label, summary)


def embedding_report_with_model_fallback(label: str, summary: dict) -> MethodsReport:
    """Recover missing references by recorded model identity, without altering the artifact."""
    from dataclasses import replace

    if embedding_citation_payloads(summary):
        return embedding_report(label, summary)
    runtime = _mapping(summary.get("provenance"))
    model_id = str(summary.get("selected_model_id") or summary.get("model_id") or "").strip()
    version = str(runtime.get("model_version") or "").strip()
    if not model_id:
        return embedding_report(label, summary)
    from EmbeddingsArea.model_catalog import ModelCatalog

    matches = [model for model in ModelCatalog().list_local_models()
               if model.id.casefold() == model_id.casefold()
               and (not version or model.version == version)]
    if len(matches) != 1:
        return embedding_report(label, summary)
    model = matches[0]
    bibtex = model.runtime_contract.get("bibtex")
    recovered = {**summary, "provenance": {**runtime, "bibtex": bibtex}}
    if not embedding_citation_payloads(recovered):
        return embedding_report(label, summary)
    report = embedding_report(label, recovered)
    note = "References recovered from the installed model package; they were not saved with this run."
    if not version:
        note += " The original model version was not recorded."
    return replace(report, warnings=(*report.warnings, note),
                   reproducibility_rows=(*report.reproducibility_rows,
                                         ("Reference source", f"Installed model {model.id} {model.version}")))
