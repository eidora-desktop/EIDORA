from __future__ import annotations

from collections.abc import Callable

from PySide6 import QtCore


def defer_to_event_loop(callback: Callable[[], None]) -> None:
    """Run a callback on the next Qt event-loop turn."""
    QtCore.QTimer.singleShot(0, callback)

