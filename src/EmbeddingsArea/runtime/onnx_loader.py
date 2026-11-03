"""Select the optional GPU package before ONNX is imported in a fresh worker."""
from __future__ import annotations

import importlib
from pathlib import Path
import sys


def activate_cuda_runtime() -> None:
    from EmbeddingsArea.runtime.cuda_install import installed_runtime

    directory = installed_runtime()
    if directory is not None:
        _activate_directory(directory / "python")


def _activate_directory(directory: Path) -> None:
    """Never unload/replace an already imported native runtime in this process."""
    package = directory / "onnxruntime"
    if not (package / "__init__.py").is_file():
        raise RuntimeError("The optional ONNX GPU runtime is incomplete. Run GPU setup again.")
    loaded = sys.modules.get("onnxruntime")
    if loaded is not None:
        location = Path(getattr(loaded, "__file__", "")).resolve()
        if location.is_relative_to(package.resolve()):
            return
        raise RuntimeError("CUDA requires a fresh worker; the CPU ONNX Runtime is already loaded.")
    # A normal filesystem package takes precedence over PyInstaller's bundled
    # package. Its native extension and providers remain next to one another,
    # as required by ORT's native provider loader on Windows and Linux.
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    importlib.invalidate_caches()
