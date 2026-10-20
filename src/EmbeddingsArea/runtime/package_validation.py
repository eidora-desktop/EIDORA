"""Pipeline entry point: validate packages using the actual Desktop runtime."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def validate(directory, request):
    from EmbeddingsArea.model_package import load_model_package, validate_package_files, validate_onnx_graph, resolve_package_path
    from EmbeddingsArea.preparation_contract import resolve_contract, PREPARATION_VERSION
    from EmbeddingsArea.runtime.adapters.base import AdapterRunConfig
    from EmbeddingsArea.runtime.adapters.onnx import OnnxAdapter, _DEVICE_PROVIDERS
    from EmbeddingsArea.runtime.contracts import RuntimeInput
    from eidora_version import __version__

    spec = load_model_package(directory / "config.yaml")
    validate_package_files(directory, spec)
    report = dict(desktop_version=__version__, schema_version=3, preparation_version=PREPARATION_VERSION, providers={})
    if request.get("metadata_only"):
        report["publication_ready"] = False
        return report
    import numpy as np
    import onnxruntime as ort
    from EmbeddingsArea.runtime.preparers import prepare_batch
    fixtures = request.get("fixtures", [])
    if not fixtures:
        raise ValueError("Publication validation requires real-media fixtures.")
    validate_onnx_graph(directory, spec)
    import platform
    report["environment"] = dict(onnxruntime=ort.__version__, platform=platform.platform(), python=sys.version)
    available = ort.get_available_providers()
    requested = request.get("providers", list(spec.execution_providers))
    if any(provider not in spec.execution_providers for provider in requested):
        raise ValueError("Cannot certify a provider not declared by the package.")
    for provider in requested:
        if provider not in available:
            report["providers"][provider] = dict(status="unverified", reason="Provider unavailable in selected Desktop runtime")
            continue
        device = next(key for key, value in _DEVICE_PROVIDERS.items() if value == provider)
        tested = 0
        for component in spec.components or (spec,):
            matching = [f for f in fixtures if f.get("modality", spec.modality) == component.modality]
            if not matching:
                raise ValueError(f"Missing {component.modality} real-media fixture.")
            if not any(f.get("expected_tensors") and f.get("expected_vectors") for f in matching):
                raise ValueError(f"{component.modality} certification requires source-reference tensors and vectors.")
            # Defaults plus each enum choice, supplemented by
            # fixture overrides for conditional recipes and reference tensors.
            combinations = [{}]
            definitions = component.preparation.get("parameters", {})
            for key, definition in definitions.items():
                from EmbeddingsArea.preparation_contract import OPTIONS
                values = definition.get("choices", OPTIONS[key].get("choices", []))
                combinations.extend({key: value} for value in values)
            for fixture in matching:
                for override in [*combinations, fixture.get("parameters", {})]:
                    contract = resolve_contract(component.contract(), override)
                    item = RuntimeInput(0, "fixture", str(fixture.get("identifier", "fixture")),
                                        resolve_package_path(directory, fixture["path"]), component.modality)
                    feed = prepare_batch([item], contract, package_directory=directory)
                    # Source-model reference tensors are explicit, named, and
                    # never inferred from ONNX output shape alone.
                    if override == fixture.get("parameters", {}) and fixture.get("expected_tensors"):
                        for name, path in fixture["expected_tensors"].items():
                            if set(fixture["expected_tensors"]) != set(feed):
                                raise ValueError("Reference tensors must cover every graph input.")
                            np.testing.assert_allclose(feed[name], np.load(resolve_package_path(directory, path), allow_pickle=False),
                                                       rtol=fixture.get("rtol", 1e-4), atol=fixture.get("atol", 1e-5))
                    adapter = OnnxAdapter()
                    try:
                        adapter.setup(AdapterRunConfig(model_path=resolve_package_path(directory, component.graph_path),
                                                       package_directory=directory, runtime_contract=contract, requested_device=device))
                        if adapter.execution_providers()[0] != provider:
                            raise ValueError("Certification session substituted a different provider.")
                        vectors = adapter.embed_batch([item])
                        if override == fixture.get("parameters", {}) and fixture.get("expected_vectors"):
                            np.testing.assert_allclose(vectors, np.load(resolve_package_path(directory, fixture["expected_vectors"]), allow_pickle=False),
                                                       rtol=fixture.get("rtol", 1e-4), atol=fixture.get("atol", 1e-5))
                        batch = contract["inputs"][0]["shape"][0]
                        if batch != 1:
                            adapter.embed_batch([item, item])
                        tested += 1
                    finally:
                        adapter.close()
        report["providers"][provider] = dict(status="passed", fixture_runs=tested,
                                             scope="session provider chain; CPU graph-node support allowed")
    report["publication_ready"] = all(report["providers"].get(p, {}).get("status") == "passed" for p in spec.execution_providers)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("request", type=Path)
    parser.add_argument("result", type=Path)
    args = parser.parse_args()
    try:
        result = validate(args.package, json.loads(args.request.read_text(encoding="utf-8")))
    except Exception as exc:
        args.result.write_text(json.dumps({"error": str(exc)}), encoding="utf-8")
        return 1
    args.result.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
