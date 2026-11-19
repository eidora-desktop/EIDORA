from __future__ import annotations

import multiprocessing
import sys


def main(argv: list[str] | None = None) -> int:
    """Start the EIDORA desktop application."""
    multiprocessing.freeze_support()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:2] == ["--internal-worker", "diagnostic-trace"]:
        from shared.internal_workers import dispatch_internal_worker

        return dispatch_internal_worker(arguments[1:])
    from shared.matplotlib_runtime import configure_matplotlib_cache

    configure_matplotlib_cache()
    if arguments[:1] == ["--internal-worker"]:
        from shared.internal_workers import dispatch_internal_worker

        return dispatch_internal_worker(arguments[1:])
    if arguments[:1] == ["--release-self-test"]:
        from shared.release_self_test import main as run_release_self_test

        return run_release_self_test(arguments[1:])

    from PySide6 import QtWidgets

    from eidora_version import __version__
    from shared.diagnostics.logging import configure_logging
    from shared.qt.dialog_windows import (
        install_dialog_window_policy,
        set_dialog_window_owner,
    )
    from shared.qt.branding import install_application_icon
    from shared.qt.theme import install_theme
    from shared.qt.icons import install_icon_palette_refresh
    from shared.qt.keyboard_focus import install_keyboard_focus

    configure_logging()

    app = QtWidgets.QApplication.instance()
    owns_app = app is None
    if app is None:
        application_arguments = sys.argv if argv is None else [sys.argv[0], *arguments]
        app = QtWidgets.QApplication(application_arguments)

    from shared.qt.garbage_collection import install_gui_garbage_collection

    install_gui_garbage_collection(app)

    # QStandardPaths uses these values for AppDataLocation.  Set them before
    # services/controllers can resolve model or project storage paths so the
    # location is independent of whether EIDORA was launched by Python, an IDE,
    # or its installed console command.
    app.setOrganizationName("EIDORA")
    app.setOrganizationDomain("eidora.app")
    app.setApplicationName("EIDORA Desktop")
    app.setApplicationVersion(__version__)
    from shared.qt.diagnostics import DiagnosticsMonitor

    diagnostics = DiagnosticsMonitor(app)
    # Keep diagnostics installed before importing and constructing workspaces.
    from MainWindow.controllers import MainWindowController, ProjectChooserController
    from MainWindow.controllers.loading_panel_controller import LoadingPanelController
    from project.service import ProjectService
    from application_settings.repository import ApplicationPreferences

    install_dialog_window_policy(app)
    preferences = ApplicationPreferences().read()
    install_theme(app).set_preference(preferences["appearance"])
    install_keyboard_focus(app)
    install_icon_palette_refresh(app)
    install_application_icon(app)

    project_service = ProjectService()
    set_dialog_window_owner(app, None)
    quit_on_last_window_closed = app.quitOnLastWindowClosed()
    app.setQuitOnLastWindowClosed(False)
    controller = None

    def prepare_project(project):
        nonlocal controller
        # Construct every workspace (including Discovery's Quick host) while
        # hidden, yielding to the loading panel between GUI operations.
        controller = MainWindowController(
            project=project, project_service=project_service, prepare_workspaces=False,
        )
        try:
            yield
            yield from controller.initialize_workspaces()
            app.aboutToQuit.connect(controller.shutdown)
            controller.window.destroyed.connect(diagnostics.close)
            controller.show()
            set_dialog_window_owner(app, controller.window)
        except BaseException:
            controller.shutdown()
            controller.window.close()
            controller = None
            raise

    try:
        project = None
        if preferences["startup"] == "last_project":
            project = LoadingPanelController().run(project_service.load_last_project, prepare_project)
        if project is None and getattr(sys, "frozen", False):
            from MainWindow.controllers.starter_collection_controller import prepare_bundled_starter

            project = prepare_bundled_starter(project_service)
            if project is not None:
                LoadingPanelController().run(lambda: project, prepare_project)
        if project is None:
            chooser = ProjectChooserController(project_service, prepare_project=prepare_project)
            try:
                if chooser.exec() != QtWidgets.QDialog.DialogCode.Accepted:
                    controller = None
            finally:
                chooser.dispose()
    except BaseException:
        app.setQuitOnLastWindowClosed(quit_on_last_window_closed)
        diagnostics.close()
        raise
    if controller is None:
        app.setQuitOnLastWindowClosed(quit_on_last_window_closed)
        diagnostics.close()
        return 0

    app.setQuitOnLastWindowClosed(quit_on_last_window_closed)

    if owns_app:
        result = int(app.exec())
        diagnostics.close()
        return result
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
