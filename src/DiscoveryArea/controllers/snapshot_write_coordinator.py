from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from PySide6 import QtCore, QtWidgets


def run_snapshot_write_responsive(operation: Callable[[], object]) -> object:
    """Poll a plain Python write; all Qt objects stay on the GUI thread."""
    application = QtWidgets.QApplication.instance()
    if application is None or QtCore.QThread.currentThread() is not application.thread():
        return operation()
    loop = QtCore.QEventLoop()
    timer = QtCore.QTimer()
    timer.setInterval(20)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="snapshot-write") as executor:
        future = executor.submit(operation)
        timer.timeout.connect(lambda: loop.quit() if future.done() else None)
        timer.start()
        try:
            loop.exec()
            return future.result()
        finally:
            timer.stop()
            timer.timeout.disconnect()
