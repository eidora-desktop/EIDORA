from __future__ import annotations

import json

from PySide6 import QtCore, QtNetwork

from application_info.service import UPDATE_CATALOG_URL
from eidora_version import __version__


class ReleaseClient(QtCore.QObject):
    """User-requested website catalog lookup; never downloads or installs code."""

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
        request = QtNetwork.QNetworkRequest(QtCore.QUrl(UPDATE_CATALOG_URL))
        request.setRawHeader(b"Accept", b"application/json")
        request.setRawHeader(b"User-Agent", f"EIDORA/{__version__}".encode())
        # Pages redirects the apex domain to the configured www domain.
        request.setAttribute(
            QtNetwork.QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QtNetwork.QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
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
                self.failed.emit("Update information is not available on eidora.org yet.")
            elif status in (403, 429):
                self.failed.emit("The website temporarily refused the update check. Please try again later.")
            elif reply.error() != QtNetwork.QNetworkReply.NetworkError.NoError:
                self.failed.emit("Could not check for updates. Check your connection and try again.")
            elif status != 200:
                self.failed.emit("The website returned an unexpected response. Please try again later.")
            else:
                try:
                    payload = json.loads(bytes(reply.readAll()))
                except (ValueError, UnicodeError):
                    self.failed.emit("The update server returned an invalid response.")
                else:
                    self.received.emit(payload)
        finally:
            reply.deleteLater()
