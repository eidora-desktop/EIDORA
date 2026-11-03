"""Ship CPU ONNX Runtime; all CUDA binaries belong to the optional GPU runtime."""
from __future__ import annotations

from importlib.metadata import distribution, PackageNotFoundError
from pathlib import Path
import re
import sys

CUDA_DISTRIBUTIONS = (
    "nvidia-cublas-cu12", "nvidia-cuda-runtime-cu12", "nvidia-cuda-nvrtc-cu12",
    "nvidia-cufft-cu12", "nvidia-curand-cu12", "nvidia-cudnn-cu12", "nvidia-nvjitlink-cu12",
)
_NVIDIA_LIBRARY = re.compile(r"^(lib)?(cublas|cudnn|cufft|curand|cudart|nvrtc|nvjitlink|onnxruntime_providers_(cuda|tensorrt|nv_tensorrt))", re.I)


def remove_cuda_runtime_libraries(entries):
    """Defend against hooks collecting locally installed CUDA wheels or system DLLs."""
    return [entry for entry in entries
            if not entry[0].replace("\\", "/").startswith("nvidia/")
            and not _NVIDIA_LIBRARY.match(entry[0].replace("\\", "/").rsplit("/", 1)[-1])]


def cuda_bundle_files():
    if sys.platform not in {"win32", "linux"}:
        return [], []
    try:
        distribution("onnxruntime-gpu")
    except PackageNotFoundError:
        pass
    else:
        raise RuntimeError("Base builds require a clean environment without the onnxruntime-gpu distribution.")
    package = distribution("onnxruntime")
    if package.version != "1.26.0":
        raise RuntimeError("The CPU base requires the pinned ONNX Runtime 1.26.0.")
    data = [(str(package.locate_file(entry)), "licenses/onnxruntime")
            for entry in package.files or ()
            if "license" in entry.name.casefold() or "notice" in entry.name.casefold()]
    return [], data


def validate_cuda_bundle(bundle: Path) -> None:
    """Reject any GPU provider/vendor libraries in the CPU base bundle."""
    if sys.platform not in {"win32", "linux"}:
        return
    root = bundle / "_internal"
    if not list((root / "onnxruntime/capi").glob("onnxruntime_pybind11_state*")):
        raise RuntimeError("The CPU ONNX Runtime extension is missing.")
    if not (root / "EmbeddingsArea/runtime/cuda-downloads.json").is_file():
        raise RuntimeError("Optional CUDA download manifest is missing.")
    unexpected = [str(path.relative_to(root)) for path in root.rglob("*")
                  if path.is_file() and _NVIDIA_LIBRARY.match(path.name)]
    if unexpected:
        raise RuntimeError("NVIDIA runtime libraries must be downloaded separately: " + ", ".join(unexpected))
