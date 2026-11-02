"""Collect CUDA wheel libraries explicitly; provider plugins alone are insufficient."""
from __future__ import annotations

from importlib.metadata import distribution, PackageNotFoundError
from pathlib import Path
import sys


CUDA_DISTRIBUTIONS = (
    "nvidia-cublas-cu12", "nvidia-cuda-runtime-cu12", "nvidia-cuda-nvrtc-cu12",
    "nvidia-cufft-cu12", "nvidia-curand-cu12", "nvidia-cudnn-cu12",
    "nvidia-nvjitlink-cu12",
)


def remove_duplicate_cuda_libraries(entries, root_binaries):
    """Keep explicitly flattened CUDA libraries once after PyInstaller hooks run."""
    roots = {destination.replace("\\", "/"): Path(source).resolve()
             for destination, source, kind in root_binaries
             if kind == "BINARY" and "/" not in destination.replace("\\", "/")}
    result = []
    for entry in entries:
        destination, source, kind = entry
        destination = destination.replace("\\", "/")
        # Never remove a different library with the same name, or a library
        # whose flattened counterpart is absent. Retain licenses and modules.
        if (destination.startswith("nvidia/") and kind in {"BINARY", "DATA"}
                and roots.get(destination.rsplit("/", 1)[-1]) == Path(source).resolve()):
            continue
        result.append(entry)
    return result


def cuda_bundle_files() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Flatten runtime libraries into the bootloader's native library directory.

    Include every cuDNN engine and NVRTC companion, including dynamically loaded
    libraries that PyInstaller's dependency scanner cannot discover. Keep license
    files with their owning distribution. macOS continues to use standard ORT.
    """
    if sys.platform not in {"win32", "linux"}:
        return [], []
    try:
        distribution("onnxruntime")
    except PackageNotFoundError:
        pass
    else:
        raise RuntimeError("CUDA builds require a clean environment without the CPU onnxruntime distribution.")
    distribution("onnxruntime-gpu")
    import onnxruntime as ort

    if "CUDAExecutionProvider" not in ort.get_available_providers():
        raise RuntimeError("The build environment's ONNX Runtime has no CUDA provider.")
    binaries, data = [], []
    for name in ("onnxruntime-gpu", *CUDA_DISTRIBUTIONS):
        package = distribution(name)
        libraries = []
        for entry in package.files or ():
            source = Path(package.locate_file(entry))
            if "license" in source.name.casefold() or "notice" in source.name.casefold():
                data.append((str(source), f"licenses/{name}"))
            if name != "onnxruntime-gpu" and (source.suffix == ".dll" or ".so" in source.name):
                libraries.append((str(source), "."))
        if name != "onnxruntime-gpu" and not libraries:
            raise RuntimeError(f"No CUDA runtime libraries found in {name}.")
        binaries.extend(libraries)
    return binaries, data


def validate_cuda_bundle(bundle: Path) -> None:
    """Reject incomplete payloads even on build runners with no NVIDIA hardware."""
    if sys.platform not in {"win32", "linux"}:
        return
    binaries, data = cuda_bundle_files()
    root = bundle / "_internal"
    missing = [str(root / destination / Path(source).name)
               for source, destination in [*binaries, *data]
               if not (root / destination / Path(source).name).is_file()]
    provider = "onnxruntime_providers_cuda.dll" if sys.platform == "win32" else "libonnxruntime_providers_cuda.so"
    if not (root / "onnxruntime/capi" / provider).is_file():
        missing.append(provider)
    if missing:
        raise RuntimeError("CUDA bundle is incomplete: " + ", ".join(missing))
