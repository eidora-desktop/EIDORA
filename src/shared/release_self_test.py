"""Packaged-application smoke test used by release automation."""

from __future__ import annotations

import faulthandler
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path


def run(output_path: Path, *, require_package_identity: bool = False) -> int:
    started_at = time.perf_counter()
    report: dict[str, object] = {"schema_version": 1, "ok": False}
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def checkpoint(stage: str) -> None:
        report["stage"] = stage
        report["elapsed_seconds"] = round(time.perf_counter() - started_at, 3)
        output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    trace_file = output_path.with_suffix(".trace.log").open("w", encoding="utf-8")
    faulthandler.enable(file=trace_file)
    faulthandler.dump_traceback_later(90, file=trace_file)
    checkpoint("imports")
    controller = None
    application = None
    temporary = tempfile.TemporaryDirectory(prefix="eidora-release-check-")
    try:
        from shared.windows_package import current_package_full_name
        from shared.paths import default_workspace_root

        report["windows_package_full_name"] = current_package_full_name()
        if require_package_identity and not report["windows_package_full_name"]:
            raise RuntimeError("Installed MSIX validation requires Windows package identity")
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("MPLCONFIGDIR", str(Path(temporary.name) / "matplotlib"))
        os.environ.setdefault("XDG_CACHE_HOME", str(Path(temporary.name) / "cache"))
        from PySide6 import QtCore, QtWidgets

        from DiscoveryArea.canvas.native.runtime import (
            detect_native_renderer_runtime,
            probe_native_qml_api,
        )
        from DiscoveryArea.method_catalog import DiscoveryMethodCatalog
        from EmbeddingsArea.exchange.formats import FORMATS
        from MainWindow.controllers import MainWindowController
        from MainWindow.tour_repository import TourPreferences
        from project.service import ProjectService

        checkpoint("create-application")
        application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        application.setOrganizationName("EIDORA")
        application.setOrganizationDomain("eidora.app")
        application.setApplicationName("EIDORA Desktop")
        application.setQuitOnLastWindowClosed(False)
        report["default_workspace_root"] = str(default_workspace_root())

        checkpoint("multimedia-player")
        from PySide6 import QtMultimedia, QtMultimediaWidgets
        media_player = QtMultimedia.QMediaPlayer(application)
        media_audio = QtMultimedia.QAudioOutput(application)
        media_video = QtMultimediaWidgets.QVideoWidget()
        media_player.setAudioOutput(media_audio)
        media_player.setVideoOutput(media_video)
        media_player.stop()
        media_player.setVideoOutput(None)
        media_player.setAudioOutput(None)
        media_video.close()
        report["multimedia_player_ready"] = True

        checkpoint("create-project")
        workspace = Path(temporary.name) / "workspace"
        project_service = ProjectService(workspace)
        project = project_service.create_empty_project("Release self test")
        from MainWindow.controllers.starter_collection_controller import prepare_bundled_starter
        from project.starter_ui_audit import choose_starter, check_starter_view
        # Source checkouts do not ship the release asset. Packaged checks must
        # always install it, including failing when a resource is missing.
        check_starter = bool(getattr(sys, "frozen", False))
        if check_starter:
            checkpoint("install-starter")
            project = prepare_bundled_starter(project_service)
            if project is None:
                raise RuntimeError("Starter was not installed in a clean workspace.")
            choose_starter(project_service, project)
            if prepare_bundled_starter(project_service) is not None:
                raise RuntimeError("Starter was installed twice.")
        checkpoint("create-main-window")
        controller = MainWindowController(
            project=project,
            project_service=project_service,
            tour_preferences=TourPreferences(Path(temporary.name) / "tour.json"),
        )
        checkpoint("show-main-window")
        controller.show()
        QtCore.QCoreApplication.processEvents()
        if check_starter:
            if not controller._tour_controller.is_active:
                raise RuntimeError("The first-launch example tour did not start.")
            report["welcome_tour_started"] = True
            controller._tour_controller.close()
            checkpoint("restore-starter-snapshot")
            report["starter_collection"] = check_starter_view(controller, project)
        report["main_window_ready_seconds"] = round(time.perf_counter() - started_at, 3)

        checkpoint("load-discovery-methods")
        methods = DiscoveryMethodCatalog().list_methods()
        available_methods = [
            method.method_id for method in methods if method.status == "available"
        ]
        catalog = DiscoveryMethodCatalog()
        for method_id in available_methods:
            checkpoint(f"load-method:{method_id}")
            catalog.create(method_id)

        checkpoint("clustering-subprocess")
        from EmbeddingsArea.clustering.process_runner import probe_runtime
        report["clustering_methods"] = probe_runtime()

        checkpoint("export-formats")
        from shared.export_runtime_audit import audit_export_runtimes
        report["export_formats"] = audit_export_runtimes(
            Path(temporary.name) / "export-formats"
        )

        checkpoint("video-thumbnail-worker")
        from shared.video_runtime_audit import audit_video_thumbnail_runtime
        report["video_thumbnail_codecs"] = audit_video_thumbnail_runtime(
            Path(temporary.name) / "video-thumbnails",
            checkpoint=checkpoint,
            diagnostic_prefix=output_path.with_suffix(".video"),
        )

        checkpoint("detect-native-renderer")
        native = detect_native_renderer_runtime()
        if not native.available:
            raise RuntimeError(f"Native renderer is unavailable: {native.reason}")
        checkpoint("probe-native-renderer")
        native_api_ok, native_api_reason = probe_native_qml_api(native.import_paths)
        if not native_api_ok:
            raise RuntimeError(f"Native renderer QML probe failed: {native_api_reason}")

        report.update(
            {
                "application_version": _application_version(),
                "area_names": list(controller.area_names),
                "available_discovery_methods": available_methods,
                "embedding_exchange_formats": sorted(FORMATS),
                "native_renderer": {
                    "reason": native.reason,
                    "library": str(native.library_path),
                    "api_probe": native_api_reason,
                },
                "workspace_writable": workspace.is_dir(),
                "project_created": project.root.is_dir(),
            }
        )
    except Exception as error:
        report.update(
            {
                "error": f"{type(error).__name__}: {error}",
                "traceback": traceback.format_exc(),
            }
        )
    finally:
        try:
            checkpoint("shutdown")
            if controller is not None:
                controller.shutdown()
                controller.window.close()
            if application is not None:
                application.quit()
            checkpoint("temporary-cleanup")
            temporary.cleanup()
        except Exception as error:
            report["error"] = f"Cleanup failed: {type(error).__name__}: {error}"
            report["traceback"] = traceback.format_exc()
        finally:
            report["ok"] = "error" not in report
            checkpoint("complete")
            faulthandler.cancel_dump_traceback_later()
            faulthandler.disable()
            trace_file.close()
    return 0 if report["ok"] else 1


def _application_version() -> str:
    from eidora_version import __version__

    return __version__


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--require-package-identity", action="store_true")
    arguments = parser.parse_args(argv)
    return run(arguments.report, require_package_identity=arguments.require_package_identity)


if __name__ == "__main__":
    raise SystemExit(main())
