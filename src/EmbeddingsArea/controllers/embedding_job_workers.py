from __future__ import annotations
from PySide6 import QtCore

class _EmbeddingRunThread(QtCore.QThread):
    """Run the plain Python job without a QObject owned by the worker thread."""

    result_ready = QtCore.Signal(str, str, str, str)
    failed = QtCore.Signal(str, str)
    progress = QtCore.Signal(str, int, str)
    message = QtCore.Signal(str, str)
    telemetry = QtCore.Signal(str, object)

    def __init__(self, project, run_id: str, *, create_thumbnails=False, parent=None):
        super().__init__(parent)
        from EmbeddingsArea.generation_service import EmbeddingGenerationJob
        self._job = EmbeddingGenerationJob(project, run_id, create_thumbnails=create_thumbnails,
            finished=self.result_ready.emit, failed=self.failed.emit, progress=self.progress.emit,
            message=self.message.emit, telemetry=self.telemetry.emit)

    def request_pause(self):
        job = self._job
        if job is not None:
            job.request_pause()

    def request_cancel(self):
        job = self._job
        if job is not None:
            job.request_cancel()

    def run(self):
        try:
            self._job.run()
        finally:
            # Release signal callbacks before QThread.finished permits GUI cleanup.
            self._job = None


class _EmbeddingRunBridge(QtCore.QObject):
    def __init__(
        self,
        *,
        finished,
        failed,
        progress,
        message,
        telemetry,
        parent: QtCore.QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._finished = finished
        self._failed = failed
        self._progress = progress
        self._message = message
        self._telemetry = telemetry

    @QtCore.Slot(str, object)
    def on_telemetry(self, run_id, event):
        self._telemetry(run_id, event)

    @QtCore.Slot(str, str, str, str)
    def on_finished(self, run_id: str, state: str, label: str, completion_log: str) -> None:
        self._finished(run_id, state, label, completion_log)

    @QtCore.Slot(str, str)
    def on_failed(self, run_id: str, message: str) -> None:
        self._failed(run_id, message)

    @QtCore.Slot(str, int, str)
    def on_progress(self, run_id: str, value: int, activity: str) -> None:
        self._progress(run_id, value, activity)

    @QtCore.Slot(str, str)
    def on_message(self, run_id: str, message: str) -> None:
        self._message(run_id, message)
