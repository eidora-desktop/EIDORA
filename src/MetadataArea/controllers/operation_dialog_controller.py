"""Run a finite metadata operation without blocking the GUI event loop."""
from concurrent.futures import ThreadPoolExecutor
from PySide6 import QtCore, QtWidgets
from shared.qt.dialog_windows import install_dialog_window_policy


def run_operation(function, message):
    dialog = QtWidgets.QProgressDialog(message, "", 0, 0)
    dialog.setCancelButton(None)
    dialog.setWindowTitle("Metadata")
    dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
    dialog.setMinimumDuration(0)
    install_dialog_window_policy(QtWidgets.QApplication.instance())
    dialog.setWindowFlag(QtCore.Qt.WindowType.WindowCloseButtonHint,False)
    executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='metadata-operation')
    future=executor.submit(function)
    class Guard(QtCore.QObject):
        def eventFilter(self, watched, event):
            return not future.done() and (event.type()==QtCore.QEvent.Type.Close or
                (event.type()==QtCore.QEvent.Type.KeyPress and event.key()==QtCore.Qt.Key.Key_Escape))
    guard=Guard(dialog);dialog.installEventFilter(guard)
    timer=QtCore.QTimer(dialog);timer.setInterval(75)
    def poll():
        if future.done():dialog.accept()
    timer.timeout.connect(poll);timer.start()
    try:
        dialog.exec()
        return future.result()
    finally:
        timer.stop();timer.timeout.disconnect(poll)
        executor.shutdown(wait=True)
