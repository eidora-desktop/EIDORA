from __future__ import annotations

import multiprocessing
import sys


def main(argv: list[str] | None = None) -> int:
    """Start the EIDORA desktop application."""
    multiprocessing.freeze_support()
    from shared.matplotlib_runtime import configure_matplotlib_cache

    configure_matplotlib_cache()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--internal-worker"]:
        from shared.internal_workers import dispatch_internal_worker

        return dispatch_internal_worker(arguments[1:])
    if arguments[:1] == ["--release-self-test"]:
        from shared.release_self_test import main as run_release_self_test

        return run_release_self_test(arguments[1:])

    from PySide6 import QtWidgets

    from MainWindow.controllers import MainWindowController, ProjectChooserController
    from project.service import ProjectService
    from eidora_version import __version__
    from shared.diagnostics.logging import configure_logging
    from shared.qt.dialog_windows import (
        install_dialog_window_policy,
        set_dialog_window_owner,
    )
    from shared.qt.branding import install_application_icon
    from shared.qt.theme import install_theme
    from shared.qt.icons import install_icon_palette_refresh

    configure_logging()

    app = QtWidgets.QApplication.instance()
    owns_app = app is None
    if app is None:
        application_arguments = sys.argv if argv is None else [sys.argv[0], *arguments]
        app = QtWidgets.QApplication(application_arguments)

    # QStandardPaths uses these values for AppDataLocation.  Set them before
    # services/controllers can resolve model or project storage paths so the
    # location is independent of whether EIDORA was launched by Python, an IDE,
    # or its installed console command.
    app.setOrganizationName("EIDORA")
    app.setOrganizationDomain("eidora.app")
    app.setApplicationName("EIDORA Desktop")
    app.setApplicationVersion(__version__)
    install_dialog_window_policy(app)
    install_theme(app)
    install_icon_palette_refresh(app)
    install_application_icon(app)

    project_service = ProjectService()
    set_dialog_window_owner(app, None)
    quit_on_last_window_closed = app.quitOnLastWindowClosed()
    app.setQuitOnLastWindowClosed(False)
    from MainWindow.controllers.starter_collection_controller import prepare_bundled_starter
    prepare_bundled_starter(project_service)
    chooser = ProjectChooserController(project_service)
    try:
        accepted = chooser.exec() == QtWidgets.QDialog.DialogCode.Accepted
        project = chooser.selected_project
    except BaseException:
        app.setQuitOnLastWindowClosed(quit_on_last_window_closed)
        raise
    finally:
        chooser.dispose()
    if not accepted or project is None:
        app.setQuitOnLastWindowClosed(quit_on_last_window_closed)
        return 0

    # Construct and reveal the application only after the launcher has closed.
    controller = MainWindowController(
        project=project,
        project_service=project_service,
        defer_secondary_areas=True,
    )
    app.aboutToQuit.connect(controller.shutdown)
    controller.show()
    set_dialog_window_owner(app, controller.window)
    app.setQuitOnLastWindowClosed(quit_on_last_window_closed)

    if owns_app:
        return int(app.exec())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
