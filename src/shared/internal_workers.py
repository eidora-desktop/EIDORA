"""Launch isolated workers in source checkouts and frozen applications."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from collections.abc import Sequence


INTERNAL_WORKER_FLAG = "--internal-worker"
_WORKER_NAMES = frozenset({"discovery", "gif", "native-probe", "video-thumbnail"})


def internal_worker_arguments(name: str, *arguments: object) -> list[str]:
    """Return arguments for launching one worker through ``sys.executable``."""
    if name not in _WORKER_NAMES:
        raise ValueError(f"Unknown internal worker: {name}")
    values = [str(argument) for argument in arguments]
    if getattr(sys, "frozen", False):
        return [INTERNAL_WORKER_FLAG, name, *values]
    return ["-m", "shared.internal_workers", name, *values]


def internal_worker_command(name: str, *arguments: object) -> list[str]:
    return [sys.executable, *internal_worker_arguments(name, *arguments)]


def read_worker_result(path: Path) -> dict[str, object]:
    """Read a completed worker's reply without depending on console streams."""
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Missing or invalid worker result: {path.name}") from exc
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise RuntimeError(f"Invalid worker result object: {path.name}")
    return result


def dispatch_internal_worker(argv: Sequence[str]) -> int:
    args = list(argv)
    if not args or args[0] not in _WORKER_NAMES:
        worker = args[0] if args else ""
        print(f"Unknown internal worker: {worker or '<missing>'}", file=sys.stderr)
        return 2

    name, worker_args = args[0], args[1:]
    if name == "discovery":
        from DiscoveryArea.discovery_methods.process_runner import main

        return main(worker_args)
    if name == "gif":
        from FiguresArea.animation_gif_worker import main

        return main(worker_args)
    if name == "video-thumbnail":
        from ProjectArea.video_thumbnail_worker import main

        return main(worker_args)
    return _run_native_probe(worker_args)


def _run_native_probe(argv: Sequence[str]) -> int:
    if len(argv) != 2:
        print("usage: native-probe <import-paths-json> <result.json>", file=sys.stderr)
        return 2
    try:
        from PySide6 import QtCore, QtGui, QtQml

        paths = json.loads(argv[0])
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            raise ValueError("Native probe import paths must be a JSON string array.")
        app = QtGui.QGuiApplication.instance() or QtGui.QGuiApplication([])
        engine = QtQml.QQmlEngine()
        for path in paths:
            engine.addImportPath(path)
        component = QtQml.QQmlComponent(engine)
        component.setData(
            b"import QtQuick; import DiscoveryRenderer 1.0; DiscoveryRenderItem {}",
            QtCore.QUrl(),
        )
        if component.isError():
            raise RuntimeError("; ".join(error.toString() for error in component.errors()))
        item = component.create()
        if item is None:
            raise RuntimeError("; ".join(error.toString() for error in component.errors()))
        result = {"ok": True, "api_version": int(item.rendererApiVersion())}
    except Exception as error:
        result = {"ok": False, "error": str(error)}
    Path(argv[1]).write_text(json.dumps(result), encoding="utf-8")
    return 0 if result["ok"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    return dispatch_internal_worker(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())
