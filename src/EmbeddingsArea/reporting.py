"""Readable vector-data summaries, independent of Qt and computation libraries."""
from datetime import datetime


def human_size(value):
    if value is None:
        return "Not recorded"
    size = float(value)
    for suffix in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or suffix == "TB":
            return f"{size:.0f} {suffix}" if suffix == "B" or size >= 10 else f"{size:.1f} {suffix}"
        size /= 1000


def method_name(recipe):
    return "Incremental PCA" if recipe.get("method") == "incremental_pca" else "PCA"


def variance_text(recipe):
    if "variance_retained" not in recipe:
        return "Not recorded"
    value = recipe.get("variance_retained")
    return "Undefined (constant input)" if value is None else f"{value:.2%} of immediate source variance"


def reduction_report(recipe, state="completed"):
    source = recipe.get('source_label') or 'Not recorded'
    lines = []
    if recipe.get("output_mode") == "copy":
        action = "Imported" if state == "completed" else "Importing"
        lines.append(f"{action} embeddings from: {source}")
    lines.extend(["Operation: PCA reduction", f"Method: {method_name(recipe)}"])
    if recipe.get("output_mode") != "copy":
        lines.append(f"Source: {source}")
    if recipe.get("item_count") is not None:
        lines.append(f"Vectors: {recipe['item_count']:,}")
    original = recipe.get("source_dimensions")
    reduced = recipe.get("dimensions")
    original = f"{original:,}" if original is not None else "Not recorded"
    reduced = f"{reduced:,}" if reduced is not None else "Not recorded"
    lines.append(f"Dimensions: {original} → {reduced}")
    if recipe.get("output_mode"):
        lines.append("Output: " + ("Replace original" if recipe["output_mode"] == "replace" else "Create new vector data"))
    if recipe.get("output_label"):
        lines.append(f"Name: {recipe['output_label']}")
    lines.append(f"Status: {state.replace('_', ' ').capitalize()}")
    if state == "completed":
        lines.append(f"Variance retained: {variance_text(recipe)}")
        if recipe.get("duration_seconds") is not None:
            lines.append(f"Duration: {recipe['duration_seconds']:.1f} seconds")
    return "\n".join(lines)


def readable_date(value):
    if not value:
        return "Not recorded"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%d %b %Y, %H:%M")
    except ValueError:
        return value


def vector_info_rows(run, dataset, summary, source_labels, model_label, parameters):
    provenance = summary.get("provenance") or {}
    recipe = provenance.get("reduction") or {}
    origin = "PCA-reduced" if recipe else "Generated" if run.selected_model_id else "Imported"
    model = summary.get("model_name") or model_label or summary.get("model_id")
    if model in {"Imported", "Imported embeddings"}:
        model = None
    source = (recipe.get("source_label") or "Not recorded") if recipe else (source_labels or provenance.get("import_sources") or "Not recorded")
    normalized = summary.get("l2_normalized")
    rows = [("Origin:", origin), ("Model:", model or "Not recorded"), ("Source:", source),
            ("Vectors:", f"{dataset.item_count:,}" if dataset else "Not yet saved"),
            ("Dimensions:", dataset.dimensions if dataset else "Not yet saved"),
            ("Storage:", human_size(summary.get("size_bytes", dataset.size_bytes if dataset else None))),
            ("Precision:", summary.get("dtype") or "Not recorded"),
            ("Normalized:", "Yes" if normalized is True else "No" if normalized is False else "Not recorded"),
            ("Created:", readable_date(run.created_at))]
    if recipe:
        rows.append(("Reduction", {"Method:": method_name(recipe),
            "Dimensions:": f"{recipe.get('source_dimensions', '?')} → {recipe.get('dimensions', '?')}",
            "Variance retained:": variance_text(recipe)}))
    if run.failed_items:
        rows.append(("Warning:", f"{run.failed_items:,} items failed during generation. See Progress for details."))
    elif run.error_message:
        rows.append(("Warning:", "The last operation reported a problem. See Progress for details."))
    settings = {}
    scopes = {"run": "Execution", "preprocess": "Preprocessing", "postprocess": "Postprocessing"}
    for value in parameters:
        scope = scopes.get(value.scope, value.scope.replace("_", " ").capitalize())
        settings.setdefault(scope, {})[value.parameter_id.replace("_", " ").capitalize()] = value.value
    technical = {"Run ID": run.id, "Artifact": run.artifact_path,
                 "Updated": readable_date(run.updated_at)}
    if settings:
        technical["Saved settings"] = settings
    for key, label in (("model_id", "Model ID"), ("input_modalities", "Input modalities"),
                       ("backend", "Backend"), ("preprocess", "Preprocessing"),
                       ("postprocess", "Postprocessing"), ("provenance", "Provenance")):
        if summary.get(key):
            technical[label] = summary[key]
    rows.append(("Technical details", technical))
    return rows
