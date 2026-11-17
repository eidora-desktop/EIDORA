"""Keep cyclic finalization of GUI objects on the application's thread."""
import gc

from PySide6 import QtCore


class GuiGarbageCollector(QtCore.QObject):
    """Replace allocation-triggered collection with periodic Qt timer work.

    Python's automatic cyclic collector runs on whichever thread allocates.
    Even plain-data workers can therefore finalize abandoned widget/controller
    cycles. Shiboken's pending-call deletion queue does not make that safe while
    Qt is dispatching events. Reference-counted cleanup remains unchanged.
    """

    def __init__(self, app):
        super().__init__(app)
        self._was_enabled = gc.isenabled()
        self._closed = False
        self._collecting = False
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._collect)
        app.destroyed.connect(self.close)
        if self._was_enabled:
            gc.disable()
            self._timer.start()

    @QtCore.Slot()
    def _collect(self):
        if self._closed or self._collecting or not self._was_enabled:
            return
        counts, thresholds = gc.get_count(), gc.get_threshold()
        if not thresholds[0]:
            return
        # Allocation bursts can end between ticks, bringing count[0] back below
        # its threshold. Always sweep young objects; use the generation counters
        # to keep scans of older, long-lived objects infrequent.
        generation = 0
        if counts[1] > thresholds[1]:
            generation = 2 if counts[2] > thresholds[2] else 1
        self._collecting = True
        try:
            gc.collect(generation)
        finally:
            self._collecting = False

    @QtCore.Slot()
    def close(self):
        if self._closed:
            return
        self._closed = True
        self._timer.stop()
        if self._was_enabled:
            gc.enable()


def install_gui_garbage_collection(app):
    """Install once, before constructing controllers or starting GUI workers."""
    collector = getattr(app, "_eidora_garbage_collector", None)
    if collector is None or collector._closed:
        collector = GuiGarbageCollector(app)
        app._eidora_garbage_collector = collector
    return collector
