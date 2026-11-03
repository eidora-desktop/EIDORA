"""Load the selected CUDA runtime before creating an inference session."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys


_DLL_DIRECTORIES = []
_LOADED_LIBRARIES = []


def preload_cuda(ort) -> None:
    from EmbeddingsArea.runtime.cuda_install import installed_runtime

    optional = installed_runtime()
    if optional is not None:
        if str(ort.__version__) != "1.26.0":
            raise RuntimeError("The optional CUDA runtime requires ONNX Runtime 1.26.0.")
        if not Path(ort.__file__).resolve().is_relative_to((optional / "python/onnxruntime").resolve()):
            raise RuntimeError("The optional ONNX GPU package must be loaded in a fresh CUDA worker.")
        if sys.platform == "win32":
            _DLL_DIRECTORIES.append(os.add_dll_directory(str(optional)))
        linker = optional / ("nvJitLink_120_0.dll" if sys.platform == "win32" else "libnvJitLink.so.12")
        _LOADED_LIBRARIES.append(ctypes.CDLL(str(linker)))
        ort.preload_dlls(directory=str(optional))
        return
    if getattr(sys, "frozen", False):
        raise RuntimeError("NVIDIA GPU support has not been downloaded. Use Enable NVIDIA GPU first.")
    # In source installs ORT finds NVIDIA wheels relative to site-packages. Load
    # nvJitLink first because cuFFT can require it from a different wheel folder.
    root = Path(ort.__file__).resolve().parent.parent
    library = "nvJitLink_120_0.dll" if sys.platform == "win32" else "libnvJitLink.so.12"
    path = root / "nvidia/nvjitlink" / ("bin" if sys.platform == "win32" else "lib") / library
    if path.is_file():
        _LOADED_LIBRARIES.append(ctypes.CDLL(str(path)))
    ort.preload_dlls(directory="")
