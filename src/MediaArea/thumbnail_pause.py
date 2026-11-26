from __future__ import annotations

from collections.abc import Callable
import threading


class ThumbnailPause:
    """Pause the coordinator between batches, never a decoder or database read."""

    def __init__(self, cancelled: threading.Event, on_paused: Callable[[], None]) -> None:
        self._cancelled = cancelled
        self._on_paused = on_paused
        self._condition = threading.Condition()
        self._requested = False
        self._paused = False

    @property
    def requested(self) -> bool:
        with self._condition:
            return self._requested

    @property
    def paused(self) -> bool:
        with self._condition:
            return self._paused

    def request(self) -> None:
        with self._condition:
            self._requested = True

    def resume(self) -> None:
        with self._condition:
            self._requested = self._paused = False
            self._condition.notify_all()

    def checkpoint(self) -> None:
        with self._condition:
            if not self._requested or self._cancelled.is_set():
                return
            while self._requested and not self._cancelled.is_set():
                # Resume can be followed by another Pause before this thread
                # wakes. Acknowledge that new request rather than waiting with
                # the GUI still showing "Pausing".
                if not self._paused:
                    self._paused = True
                    self._on_paused()
                self._condition.wait(timeout=0.1)
            self._paused = False
