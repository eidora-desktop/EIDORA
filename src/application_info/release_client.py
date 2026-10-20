from __future__ import annotations

import json

from PySide6 import QtCore, QtNetwork

from application_info.service import LATEST_RELEASE_API
from eidora_version import __version__


class ReleaseClient(QtCore.QObject):
    """Asynchronous, public GitHub release lookup; never downloads or installs code."""

    received = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._manager = QtNetwork.QNetworkAccessManager(self)
        self._reply = None
        self._timeout = QtCore.QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._timed_out)

    def check(self) -> None:
        if self._reply is not None:
            return
        request = QtNetwork.QNetworkRequest(QtCore.QUrl(LATEST_RELEASE_API))
        request.setRawHeader(b"Accept", b"application/vnd.github+json")
        request.setRawHeader(b"User-Agent", f"EIDORA/{__version__}".encode())
        request.setRawHeader(b"X-GitHub-Api-Version", b"2022-11-28")
        request.setTransferTimeout(15000)
        self._reply = self._manager.get(request)
        self._reply.finished.connect(self._finished)
        self._timeout.start(20000)

    def cancel(self) -> None:
        self._timeout.stop()
        reply, self._reply = self._reply, None
        if reply is not None:
            reply.finished.disconnect(self._finished)
            reply.abort()
            reply.deleteLater()

    def _timed_out(self) -> None:
        self.cancel()
        self.failed.emit("The update check timed out. Please try again.")

    def _finished(self) -> None:
        reply, self._reply = self._reply, None
        self._timeout.stop()
        if reply is None:
            return
        try:
            status = reply.attribute(QtNetwork.QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            if status == 404:
                self.failed.emit("No public release is available, or the release repository is unavailable.")
            elif status in (403, 429):
                self.failed.emit("GitHub temporarily refused the update check. Please try again later.")
            elif reply.error() != QtNetwork.QNetworkReply.NetworkError.NoError:
                self.failed.emit("Could not check for updates. Check your connection and try again.")
            else:
                try:
                    payload = json.loads(bytes(reply.readAll()))
                except (ValueError, UnicodeError):
                    self.failed.emit("The update server returned an invalid response.")
                else:
                    self.received.emit(payload)
        finally:
            reply.deleteLater()
