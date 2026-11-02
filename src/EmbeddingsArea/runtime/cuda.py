"""Load the selected CUDA runtime before creating an inference session."""
from __future__ import annotations

import ctypes
from pathlib import Path
import sys


def preload_cuda(ort) -> None:
    # Frozen libraries live together in the bootloader's native search directory.
    # In source installs ORT finds NVIDIA wheels relative to site-packages. Load
    # nvJitLink first because cuFFT can require it from a different wheel folder.
    root = Path(getattr(sys, "_MEIPASS", Path(ort.__file__).resolve().parent.parent))
    frozen = bool(getattr(sys, "frozen", False))
    library = "nvJitLink_120_0.dll" if sys.platform == "win32" else "libnvJitLink.so.12"
    path = root / library if frozen else root / "nvidia/nvjitlink" / ("bin" if sys.platform == "win32" else "lib") / library
    if path.is_file():
        ctypes.CDLL(str(path))
    ort.preload_dlls(directory=str(root) if frozen else "")
