"""Responsive, explicitly requested creation of a bundled example project."""
from concurrent.futures import ThreadPoolExecutor
from queue import Empty, SimpleQueue
import sys
from threading import Event

from PySide6 import QtCore, QtWidgets

from project.starter_collection import bundled_resources, install_starter
from shared.qt.background_processes import BackgroundProcessManager
from shared.qt.message_dialogs import show_error
from shared.qt.ui_loader import load_ui


def prepare_bundled_starter(service, *, recreate=False):
    if not recreate and (not getattr(sys, 'frozen', False) or (service.workspace_root / '.starter-collection.json').exists()):
        return
    dialog = load_ui('MainWindow', 'ui/starter_collection.ui', None)
    label = dialog.findChild(QtWidgets.QLabel, 'label_status')
    bar = dialog.findChild(QtWidgets.QProgressBar, 'progress_install')
    button = dialog.findChild(QtWidgets.QPushButton, 'button_cancel')
    cancel = Event()
    updates = SimpleQueue()
    result = []
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='starter-install')
    manager = BackgroundProcessManager(changed=lambda: None)
    def start():
        result.append(executor.submit(install_starter, service, bundled_resources(),
                                     progress=lambda value,text: updates.put((value,text)),
                                     cancelled=cancel.is_set, **({"recreate": True} if recreate else {})))
    admission = manager.submit(panel='Project', title='Prepare starter collection', start=start, cancel=cancel.set)
    def request_cancel():
        cancel.set()
        button.setEnabled(False)
        label.setText('Cancelling…')
    button.clicked.connect(request_cancel)
    # Closing/Escape requests cancellation but must not release the worker early.
    class CloseFilter(QtCore.QObject):
        def eventFilter(self, watched, event):
            if event.type() == QtCore.QEvent.Type.Close:
                request_cancel()
                event.ignore()
                return True
            if event.type() == QtCore.QEvent.Type.KeyPress and event.key() == QtCore.Qt.Key.Key_Escape:
                request_cancel()
                return True
            return False
    close_filter = CloseFilter(dialog)
    dialog.installEventFilter(close_filter)
    error = []
    def poll():
        while True:
            try:
                value,text = updates.get_nowait()
            except Empty:
                break
            bar.setValue(value)
            if not cancel.is_set():
                label.setText(text)
            if value >= 99:
                button.setEnabled(False)
        if result and result[0].done():
            try:
                result[0].result()
            except InterruptedError:
                pass
            except Exception as exc:
                error.append(str(exc))
            timer.stop()
            admission.finish()
            dialog.accept()
    timer = QtCore.QTimer(dialog)
    timer.timeout.connect(poll)
    timer.start(50)
    manager.dispatch()
    try:
        dialog.exec()
    finally:
        cancel.set()
        executor.shutdown(wait=True)
        timer.stop()
        timer.timeout.disconnect(poll)
        button.clicked.disconnect(request_cancel)
        dialog.removeEventFilter(close_filter)
        admission.finish()
    if error:
        show_error(None, 'Starter Collection', error[0] + '\n\nUse Create MET Example to try again.')
