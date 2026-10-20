from __future__ import annotations

from typing import Mapping
from shared.methods_report import (
    PROVENANCE_SCHEMA_VERSION, CitationRecord, MethodsReport, MethodsReportExporter,
    citation_payloads, checksum_payload, deduplicate_citations, eidora_citation_payload,
    _citation_records, _mapping, _value, _humanize, _implementation_text, setting_rows,
)


class ProjectionProvenanceReportService:
    """Render immutable projection provenance as manuscript-friendly text."""

    def build(self, label: str, provenance: Mapping[str, object]) -> MethodsReport:
        if int(provenance.get("schema_version") or 0) != PROVENANCE_SCHEMA_VERSION:
            raise ValueError("This projection predates methods and citation provenance; rerun it to create a report.")
        method = _mapping(provenance.get("method"))
        embedding = _mapping(provenance.get("embedding"))
        selection = _mapping(provenance.get("selection"))
        normalization = _mapping(provenance.get("normalization"))
        parameters = _mapping(method.get("effective_parameters"))
        citations = deduplicate_citations(
            [*_citation_records(provenance.get("citations")), *_citation_records(embedding.get("citations"))]
        )
        item_count = int(selection.get("item_count") or 0)
        model_name = str(embedding.get("model_name") or embedding.get("model_id") or "recorded embeddings")
        model_version = str(embedding.get("model_version") or "").strip()
        has_embedding = bool(embedding.get("model_name") or embedding.get("model_id") or embedding.get("preprocessing"))
        method_name = str(method.get("name") or method.get("id") or "projection")
        if has_embedding:
            prose = f"Embeddings for {item_count:,} items were generated using {model_name}"
            if model_version:
                prose += f" {model_version}"
            prose += f" and projected to two dimensions in EIDORA {provenance.get('eidora_version') or ''} using {method_name}."
        else:
            prose = f"Coordinates for {item_count:,} items were processed in EIDORA {provenance.get('eidora_version') or ''} using {method_name}."
        salient_keys = (
            "distance", "metric", "n_neighbors", "perplexity", "min_dist",
            "pca_dimensions", "iterations", "random_state",
        )
        salient_items = [(key, parameters[key]) for key in salient_keys if key in parameters][:5]
        salient = ", ".join(f"{_humanize(key)}={_value(value)}" for key, value in salient_items)
        if salient:
            prose += f" Effective projection settings were {salient}."
        repeatability = str(method.get("repeatability") or "unknown")
        prose += f" Repeatability was classified as {repeatability.replace('_', ' ')}."
        if normalization:
            prose += " EIDORA linearly normalized the resulting coordinates for canvas storage."

        rows: list[tuple[str, str]] = []
        if has_embedding:
            rows.append(("Embedding model", f"{model_name}{' ' + model_version if model_version else ''}"))
        rows.append(("Projection method", method_name))
        rows.extend((_humanize(key), _value(value)) for key, value in parameters.items())
        for step in method.get("derived_steps", []) if isinstance(method.get("derived_steps"), list) else []:
            rows.append(("Derived step", str(step)))
        if embedding.get("parameters"):
            rows.extend(setting_rows("Embedding run settings", embedding["parameters"]))
        preprocessing = embedding.get("preprocessing")
        if preprocessing:
            rows.extend(setting_rows("Embedding preprocessing", preprocessing))
        postprocessing = embedding.get("postprocessing")
        if postprocessing:
            rows.extend(setting_rows("Embedding postprocessing", postprocessing))
        filter_snapshot = _mapping(selection.get("filter"))
        if filter_snapshot:
            rows.append(("Filter", str(filter_snapshot.get("label") or "Recorded filter")))
            if filter_snapshot.get("rules"):
                rows.extend(setting_rows("Projection input filter", filter_snapshot["rules"]))

        reproducibility = [
            ("EIDORA version", str(provenance.get("eidora_version") or "not recorded")),
            ("Projection implementation", _implementation_text(method)),
        ]
        if has_embedding:
            reproducibility.extend([
                ("Embedding model", f"{model_name}{' ' + model_version if model_version else ''}"),
                ("Model package fingerprint", str(embedding.get("package_fingerprint") or "not recorded")),
                ("Embedding input signature", str(embedding.get("input_signature") or "not recorded")),
                ("Execution providers", _value(embedding.get("execution_providers") or []) or "not recorded"),
                ("Final execution mode", str(embedding.get("final_execution_mode") or "not recorded")),
            ])
        reproducibility.extend([
            ("Item count", f"{item_count:,}"),
            ("Item checksum", str(selection.get("metadata_checksum") or "not recorded")),
            ("Repeatability", repeatability.replace("_", " ")),
            ("Coordinate normalization", _value(normalization) if normalization else "not recorded"),
        ])
        warnings = tuple(str(value) for value in provenance.get("missing", []) if str(value).strip())
        return MethodsReport(
            title=f"{label} — Methods and Citations",
            methods_text=prose,
            parameter_rows=tuple(rows),
            references=citations,
            reproducibility_rows=tuple(reproducibility),
            warnings=warnings,
        )

    save_pair = staticmethod(MethodsReportExporter.save_pair)
