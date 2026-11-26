"""Persistent save failures and a cancellable close/switch guard."""
from PySide6 import QtCore, QtWidgets

from shared.qt.widgets import require_child


class ProjectSavePanelController(QtCore.QObject):
    def __init__(self, window):
        super().__init__(window)
        self._window = window
        self._areas = []
        self._checking = False
        self._closed = False
        self._discard_allowed = False
        self._app = QtWidgets.QApplication.instance()
        self._retry = require_child(window, QtWidgets.QPushButton, "button_retry_project_save")
        self._retry.clicked.connect(self.retry)
        self._retry.hide()
        self._app.installEventFilter(self)

    def register_area(self, area):
        if not hasattr(area, "save_state") or area in self._areas:
            return
        self._areas.append(area)
        area.save_state.changed.connect(self.refresh)
        self.refresh()

    def _pending(self):
        return {name: error for area in self._areas for name, error in area.save_state.pending.items()}

    def refresh(self):
        failures = {name: error for name, error in self._pending().items() if error}
        details = "\n".join(f"{name}: {error}" for name, error in failures.items())
        self._retry.setToolTip("Changes have not been saved:\n" + details if failures else "")
        self._retry.setAccessibleDescription(details)
        self._retry.setVisible(bool(failures))

    def retry(self):
        for area in self._areas:
            self._flush_area(area)
        self.refresh()

    @staticmethod
    def _flush_area(area):
        try:
            area.flush_pending_saves()
        except Exception as error:
            # An exception escaping a Qt close-event filter must not let the
            # window close and destroy the only copy of pending edits.
            area.save_state.failed("Project changes", error)
        else:
            area.save_state.saved("Project changes")

    def confirm_leave(self):
        if self._closed or self._discard_allowed:
            return True
        if self._checking:
            return False
        self._checking = True
        try:
            self.retry()
            if not self._pending():
                return True
            # An asynchronous save must finish before the owning UI is discarded.
            # The nested Qt loop remains responsive and Cancel keeps the project open.
            dialog = QtWidgets.QMessageBox()
            dialog.setWindowTitle("Unsaved Project Changes")
            dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
            buttons = QtWidgets.QMessageBox.StandardButton
            dialog.setStandardButtons(buttons.Retry | buttons.Discard | buttons.Cancel)
            dialog.setDefaultButton(buttons.Cancel)
            dialog.button(buttons.Discard).setText("Discard Unsaved Changes")
            timer = QtCore.QTimer(dialog)

            def update():
                # A collection can finish loading while this dialog is open.
                # Flush newly pending work, but leave failed writes for Retry.
                for area in self._areas:
                    if area.save_state.pending and not any(area.save_state.pending.values()):
                        self._flush_area(area)
                pending = self._pending()
                if not pending:
                    dialog.done(int(buttons.Ok))
                    return
                failures = {name: error for name, error in pending.items() if error}
                dialog.setText(
                    "Some changes could not be saved. Retry after restoring access to the project folder, "
                    "or cancel to keep working. Discarding loses unsaved changes."
                    if failures else "Saving project changes… Cancel to keep the project open."
                )
                dialog.setInformativeText("\n".join(pending))
                dialog.button(buttons.Retry).setEnabled(bool(failures))
                dialog.button(buttons.Discard).setEnabled(bool(failures))

            timer.timeout.connect(update)
            timer.start(50)
            try:
                while True:
                    update()
                    if not self._pending():
                        return True
                    answer = dialog.exec()
                    if answer == buttons.Retry:
                        self.retry()
                    elif answer == buttons.Discard:
                        for area in self._areas:
                            area.stop_pending_autosaves()
                        self._discard_allowed = True
                        return True
                    else:
                        return answer == buttons.Ok
            finally:
                timer.stop()
                timer.timeout.disconnect(update)
        finally:
            self._checking = False

    def eventFilter(self, watched, event):
        leaving = (watched is self._window and event.type() == QtCore.QEvent.Type.Close) or (
            watched is self._app and event.type() == QtCore.QEvent.Type.Quit
        )
        if leaving and not self.confirm_leave():
            event.ignore()
            return True
        return False

    def discard_if_confirmed(self):
        if self._discard_allowed:
            for area in self._areas:
                area.discard_pending_saves()
        self._discard_allowed = False

    def cancel_leave(self):
        # Cancelling the chooser returns to the same live document. Its edits
        # remain pending until the user actually switches projects or quits.
        self._discard_allowed = False
        self.refresh()

    def shutdown(self):
        self._closed = True
        self._app.removeEventFilter(self)
