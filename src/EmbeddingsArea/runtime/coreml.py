"""Compatibility with ONNX Runtime's MLProgram default-padding conversion."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory


def _explicit_zero_padding(graph, onnx) -> int:
    """Spell out ONNX's zero-padding default without changing model semantics."""
    changed = 0
    weights = {tensor.name: tensor for tensor in graph.initializer}
    for node in graph.node:
        for attribute in node.attribute:
            if attribute.type == onnx.AttributeProto.GRAPH:
                changed += _explicit_zero_padding(attribute.g, onnx)
            elif attribute.type == onnx.AttributeProto.GRAPHS:
                changed += sum(_explicit_zero_padding(child, onnx) for child in attribute.graphs)
        if node.domain not in {"", "ai.onnx"} or node.op_type not in {"Conv", "ConvTranspose", "AveragePool", "MaxPool"}:
            continue
        attributes = {attribute.name: attribute for attribute in node.attribute}
        auto_pad = attributes.get("auto_pad")
        mode = auto_pad.s if auto_pad is not None else b"NOTSET"
        if mode not in {b"NOTSET", b"VALID"} or "pads" in attributes:
            continue
        kernel = attributes.get("kernel_shape")
        rank = len(kernel.ints) if kernel is not None else 0
        if not rank and node.op_type in {"Conv", "ConvTranspose"} and len(node.input) > 1:
            weight = weights.get(node.input[1])
            rank = len(weight.dims) - 2 if weight is not None else 0
        if rank <= 0:
            continue
        if auto_pad is not None:
            node.attribute.remove(auto_pad)
        node.attribute.append(onnx.helper.make_attribute("pads", [0] * (2 * rank)))
        changed += 1
    return changed


@contextmanager
def explicit_padding_model(model_path: Path):
    """Yield a temporary equivalent graph, leaving package files/checksums intact.

    Called only after the specific MLProgram missing-pad compiler failure.
    External tensors are resolved by ONNX and written alongside the temporary
    graph, so nested weight paths work without modifying or linking package files.
    """
    import onnx

    model = onnx.load_model(model_path)
    if not _explicit_zero_padding(model.graph, onnx):
        yield None
        return
    with TemporaryDirectory(prefix="eidora-coreml-") as directory:
        path = Path(directory) / "model.onnx"
        # Externalize large tensors before serialization to avoid protobuf's
        # 2 GiB message limit and release their Python storage before ORT loads.
        onnx.save_model(model, path, save_as_external_data=True,
                        all_tensors_to_one_file=True, location="weights.data",
                        size_threshold=1024, convert_attribute=True)
        del model
        yield path
