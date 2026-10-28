from __future__ import annotations

import platform
import sys

from PySide6 import QtCore, QtGui, QtWidgets

from application_info.release_client import ReleaseClient
from application_info.service import DOWNLOADS_URL, parse_release, platform_key
from eidora_version import __version__


class UpdatePanelController:
    """Bind the About dialog's explicit update check to the website catalog."""

    def __init__(self, dialog: QtWidgets.QDialog):
        self._client = ReleaseClient(dialog)
        self._check = dialog.findChild(QtWidgets.QPushButton, "button_check_updates")
        self._status = dialog.findChild(QtWidgets.QLabel, "label_update_status")
        self._downloads = dialog.findChild(QtWidgets.QPushButton, "button_view_downloads")
        self._notes = dialog.findChild(QtWidgets.QPushButton, "button_release_notes")
        self._download_url = DOWNLOADS_URL
        self._notes_url = ""
        self._busy = False
        self._connections = [
            (self._check.clicked, self.check),
            (self._downloads.clicked, self._open_downloads),
            (self._notes.clicked, self._open_notes),
            (self._client.received, self._received),
            (self._client.failed, self._failed),
        ]
        for signal, slot in self._connections:
            signal.connect(slot)
        self._status.hide()
        self._downloads.hide()
        self._notes.hide()

    def check(self) -> None:
        if self._busy:
            return
        self._download_url = DOWNLOADS_URL
        try:
            key = platform_key(sys.platform, platform.machine())
        except ValueError as exc:
            self._failed(str(exc))
            return
        self._download_url += f"#{key}"
        self._busy = True
        self._check.setEnabled(False)
        self._check.setText("Checking…")
        self._status.setText("Checking eidora.org for updates…")
        self._status.show()
        self._downloads.hide()
        self._notes.hide()
        self._client.check()

    def _finish(self, message: str) -> None:
        self._busy = False
        self._check.setText("Check for Updates")
        self._check.setEnabled(True)
        self._status.setText(message)
        self._status.show()
        self._downloads.setText("Downloads")
        self._downloads.show()

    def _received(self, payload: object) -> None:
        try:
            release = parse_release(payload, __version__, sys.platform, platform.machine())
        except ValueError as exc:
            self._failed(str(exc))
            return
        if release is None:
            self._finish("No public release is available for this platform yet.")
            return
        if release.newer:
            message = f"EIDORA {release.version} is available."
        elif release.version == __version__:
            message = "You have the latest available version of EIDORA."
        else:
            message = f"Your version is newer than the available release ({release.version})."
        message += f"\n{release.channel.capitalize()} release · {release.published_at}"
        if sys.platform == "win32":
            message += "\nWindows updates are installed through Microsoft Store."
        elif release.newer:
            message += "\nDownload and install the update from eidora.org."
        self._finish(message)
        self._download_url, self._notes_url = release.download_url, release.notes_url
        self._downloads.setText("View Update" if release.newer else "Downloads")
        self._notes.show()

    def _failed(self, message: str) -> None:
        self._notes.hide()
        self._finish(message)

    def _open_downloads(self) -> None:
        self._open_url(self._download_url)

    def _open_notes(self) -> None:
        self._open_url(self._notes_url)

    def _open_url(self, url: str) -> None:
        if not QtGui.QDesktopServices.openUrl(QtCore.QUrl(url)):
            self._status.setText("Could not open your browser. Please visit eidora.org/download/.")

    def dispose(self) -> None:
        self._client.cancel()
        for signal, slot in self._connections:
            signal.disconnect(slot)
        self._connections.clear()
