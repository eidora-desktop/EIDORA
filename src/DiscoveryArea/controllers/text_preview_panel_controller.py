from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event

from PySide6 import QtCore, QtWidgets

from DiscoveryArea.text_preview_repository import TextPreview, read_text_preview
from shared.qt.widgets import require_child


class TextPreviewPanelController:
    """Poll plain-Python file reads on the GUI thread; never update closed views."""

    def __init__(self, root: QtWidgets.QWidget) -> None:
        self._text = require_child(root, QtWidgets.QPlainTextEdit, "edit_source_text")
        self._status = require_child(root, QtWidgets.QLabel, "label_source_text_status")
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="item-text-preview")
        self._future: Future[TextPreview] | None = None
        self._cancelled = Event()
        self._timer = QtCore.QTimer(root)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._poll)

    def show(self, path: Path, record_key: str = "") -> None:
        self.clear()
        self._status.setText("Loading text…")
        self._cancelled = Event()
        self._future = self._executor.submit(read_text_preview, path, record_key, self._cancelled)
        self._timer.start()

    def clear(self) -> None:
        self._timer.stop()
        self._cancelled.set()
        if self._future is not None:
            self._future.cancel()
            self._future = None
        self._text.clear()
        self._status.clear()

    def close(self) -> None:
        self.clear()
        self._timer.timeout.disconnect(self._poll)
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _poll(self) -> None:
        if self._future is None or not self._future.done():
            return
        future, self._future = self._future, None
        self._timer.stop()
        try:
            result = future.result()
        except Exception as exc:
            result = TextPreview(status=f"Text preview unavailable: {exc}")
        self._text.setPlainText(result.text)
        self._status.setText(result.status)
