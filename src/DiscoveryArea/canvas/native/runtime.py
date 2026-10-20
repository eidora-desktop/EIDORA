from __future__ import annotations

from dataclasses import dataclass
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from shared.internal_workers import internal_worker_command, read_worker_result


@dataclass(frozen=True)
class NativeRendererRuntime:
    available: bool
    reason: str
    library_path: Path | None
    import_paths: tuple[Path, ...]


def _renderer_libraries(module_dir: Path) -> tuple[Path, ...]:
    """Return native QML plugin names emitted by Unix and MSVC builds."""

    unix_libraries = tuple(sorted(module_dir.glob("libdiscovery_renderer.*")))
    windows_library = module_dir / "discovery_renderer.dll"
    if windows_library.is_file():
        return (*unix_libraries, windows_library)
    return unix_libraries


def probe_native_library_loadability(library_path: Path) -> tuple[bool, str]:
    """Ask the platform loader to resolve the plugin and all Qt dependencies."""

    try:
        # Load the exact Qt libraries used by the application first.  A plugin
        # that only loads in isolation can still be ABI-incompatible once Qt
        # Quick is resident in the process.
        from PySide6 import QtCore, QtGui, QtQml, QtQuick, QtQuickWidgets

        _ = QtCore, QtGui, QtQml, QtQuick, QtQuickWidgets
        if os.name == "nt":
            ctypes.WinDLL(str(library_path))
        else:
            ctypes.CDLL(str(library_path), mode=getattr(os, "RTLD_LOCAL", 0))
    except OSError as exc:
        return False, str(exc)
    return True, "native-library-loadable"


def probe_native_qml_api(
    import_paths: tuple[Path, ...],
    *,
    expected_api: int = 6,
    timeout_seconds: float = 15.0,
) -> tuple[bool, str]:
    """Instantiate the native QML type in an isolated PySide process."""

    paths = [str(path) for path in import_paths]
    environment = dict(os.environ)
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        with tempfile.TemporaryDirectory(prefix="eidora-native-probe-") as temporary:
            result_path = Path(temporary) / "result.json"
            completed = subprocess.run(
                internal_worker_command("native-probe", json.dumps(paths), result_path),
                check=False,
                capture_output=True,
                text=True,
                timeout=max(1.0, float(timeout_seconds)),
                env=environment,
            )
            try:
                result = read_worker_result(result_path)
            except RuntimeError as exc:
                detail = completed.stderr.strip() or str(exc)
                return False, f"{detail}; exit code {completed.returncode} (0x{completed.returncode & 0xffffffff:08x})"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if completed.returncode != 0 or not result["ok"]:
        detail = result.get("error") or completed.stderr.strip() or "Native probe failed"
        return False, f"{detail}; exit code {completed.returncode} (0x{completed.returncode & 0xffffffff:08x})"
    if result.get("api_version") != int(expected_api):
        return False, f"Native renderer API mismatch: expected {expected_api}, received {result.get('api_version')!r}"
    marker = f"EIDORA_NATIVE_API={int(expected_api)}"
    return True, marker


def detect_native_renderer_runtime() -> NativeRendererRuntime:
    """Detect optional native Qt Quick discovery renderer plugin."""

    native_root = Path(__file__).resolve().parent
    repo_root = native_root.parents[3]
    candidates = (
        native_root / "packaged" / "DiscoveryRenderer",
        native_root / "build" / "DiscoveryRenderer",
        repo_root / "build" / "discovery_renderer" / "DiscoveryRenderer",
        native_root / "qml" / "DiscoveryRenderer",
    )
    failures: list[str] = []
    for module_dir in candidates:
        qmldir = module_dir / "qmldir"
        libraries = _renderer_libraries(module_dir)
        if qmldir.exists() and libraries:
            library_path = libraries[0]
            loadable, reason = probe_native_library_loadability(library_path)
            if not loadable:
                failures.append(f"{library_path}: {reason}")
                continue
            return NativeRendererRuntime(
                available=True,
                reason="native-library-loadable",
                library_path=library_path,
                import_paths=(module_dir.parent,),
            )
    if failures:
        return NativeRendererRuntime(
            available=False,
            reason="native-library-load-failed: " + " | ".join(failures),
            library_path=None,
            import_paths=(),
        )
    return NativeRendererRuntime(
        available=False,
        reason="native-library-missing",
        library_path=None,
        import_paths=(),
    )
