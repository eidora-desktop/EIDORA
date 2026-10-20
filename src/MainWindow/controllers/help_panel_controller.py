from __future__ import annotations

import sys

from PySide6 import QtCore, QtGui, QtWidgets

from MainWindow.controllers.update_panel_controller import UpdatePanelController
from application_info.service import REPOSITORY_URL, WEBSITE_URL
from eidora_version import __version__
from shared.paths import application_data_root
from shared.qt.branding import branding_pixmap, branding_svg_pixmap
from shared.qt.ui_loader import load_ui


class _AboutPaletteFilter(QtCore.QObject):
    """Refresh palette-sensitive About artwork while the dialog is open."""

    def __init__(self, dialog: QtWidgets.QDialog, refresh) -> None:
        super().__init__(dialog)
        self._dialog = dialog
        self._refresh = refresh
        dialog.installEventFilter(self)

    def eventFilter(self, watched: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if watched is self._dialog and event.type() == QtCore.QEvent.Type.PaletteChange:
            self._refresh(self._dialog)
        return False


class HelpPanelController:
    """Shared Help actions and a short-lived About/update dialog."""

    def __init__(self, window: QtWidgets.QMainWindow) -> None:
        self._window = window
        self._dialog = None
        self._palette_filter = None
        self._native_menu = None
        self._update_panel = None
        self._about = QtGui.QAction("About EIDORA…", window)
        self._about.setMenuRole(QtGui.QAction.MenuRole.AboutRole)
        self._about.triggered.connect(self.show_about)
        self._updates = QtGui.QAction("Check for Updates…", window)
        self._updates.setMenuRole(QtGui.QAction.MenuRole.ApplicationSpecificRole)
        self._updates.triggered.connect(self.check_updates)
        button = window.findChild(QtWidgets.QPushButton, "button_help")
        if button is None:
            raise RuntimeError("main_window.ui is missing button_help")
        menu = QtWidgets.QMenu(button)
        menu.addAction(self._about)
        menu.addAction(self._updates)
        button.setMenu(menu)
        if sys.platform == "darwin":
            # A parentless native menu supplies application-menu actions without
            # adding a menu row to the main window.
            self._native_menu = QtWidgets.QMenuBar()
            self._native_menu.setNativeMenuBar(True)
            application_menu = self._native_menu.addMenu("EIDORA")
            application_menu.addAction(self._about)
            application_menu.addAction(self._updates)

    def show_about(self) -> None:
        self._show_dialog()

    def check_updates(self) -> None:
        self._show_dialog(check_updates=True)

    def _show_dialog(self, *, check_updates: bool = False) -> None:
        if self._dialog is not None:
            self._dialog.raise_()
            self._dialog.activateWindow()
            if check_updates and self._update_panel is not None:
                self._update_panel.check()
            return
        dialog = load_ui("MainWindow", "ui/about_dialog.ui")
        self._dialog = dialog
        dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        dialog.findChild(QtWidgets.QLabel, "label_version").setText(f"Version {__version__}")
        user_directory = QtCore.QDir.toNativeSeparators(str(application_data_root()))
        directory_edit = dialog.findChild(QtWidgets.QTextEdit, "edit_user_directory")
        directory_edit.setPlainText(user_directory)
        directory_edit.setReadOnly(True)
        self._refresh_about_theme(dialog)
        self._palette_filter = _AboutPaletteFilter(dialog, self._refresh_about_theme)
        self._update_panel = UpdatePanelController(dialog)
        try:
            if check_updates:
                self._update_panel.check()
            dialog.exec()
        finally:
            self._update_panel.dispose()
            self._update_panel = None
            dialog.removeEventFilter(self._palette_filter)
            self._palette_filter = None
            self._dialog = None

    def _refresh_about_theme(self, dialog: QtWidgets.QDialog) -> None:
        dark = dialog.palette().color(QtGui.QPalette.ColorRole.Window).lightness() < 128
        ratio = dialog.devicePixelRatioF()
        dialog.findChild(QtWidgets.QLabel, "label_logo").setPixmap(
            branding_pixmap(
                "eidora_logo_ui.png",
                QtCore.QSize(96, 96),
                device_pixel_ratio=ratio,
            )
        )
        dialog.findChild(QtWidgets.QLabel, "label_wordmark").setPixmap(
            branding_svg_pixmap(
                "eidora_wordmark.svg",
                QtCore.QSize(226, 40),
                device_pixel_ratio=ratio,
                ink="#F4F4F5" if dark else "#303830",
            )
        )
        link_color = "#7cb7ff" if dark else "#005bbb"
        dialog.findChild(QtWidgets.QLabel, "label_credits").setText(
            "Copyright © 2026 Tillmann Ohm · "
            f'<a style="color: {link_color}" href="{REPOSITORY_URL}/blob/main/LICENSE">MIT License</a>'
        )
        dialog.findChild(QtWidgets.QLabel, "label_links").setText(
            f'<a style="color: {link_color}" href="{WEBSITE_URL}">Website</a> · '
            f'<a style="color: {link_color}" href="{REPOSITORY_URL}">Source code</a>'
        )

    def shutdown(self) -> None:
        if self._dialog is not None:
            self._dialog.reject()
        if self._native_menu is not None:
            self._native_menu.clear()
            self._native_menu.deleteLater()
            self._native_menu = None
