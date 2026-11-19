"""GUI-owned runner; numerical work executes in a cancellable child process."""
import threading
from PySide6 import QtCore
from EmbeddingsArea.reduction.service import ReductionService


class ReductionThread(QtCore.QThread):
    progress = QtCore.Signal(str, int, str)
    result_ready = QtCore.Signal(str, object)

    def __init__(self, project, run_id, request, parent=None, *, output_id=None):
        super().__init__(parent)
        self.project, self.run_id, self.request = project, run_id, request
        self.output_id = output_id or run_id
        self.cancelled = threading.Event()

    def request_cancel(self):
        self.cancelled.set()

    def run(self):
        try:
            result = ReductionService(self.project).execute(self.run_id, self.request,
                output_id=self.output_id,
                cancelled=self.cancelled.is_set,
                status=lambda message: self.progress.emit(self.output_id, -1, message))
        except Exception as exc:
            result = exc
        self.result_ready.emit(self.output_id, result)
