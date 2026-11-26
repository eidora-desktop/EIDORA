"""Paint startup feedback while project I/O and queued UI setup complete."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor

from PySide6 import QtCore, QtGui, QtWidgets

from project.domain import Project
from shared.qt.branding import branding_svg_pixmap
from shared.qt.ui_loader import load_ui


class RotatingSquare(QtWidgets.QWidget):
    def __init__(self, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self.setFixedSize(56, 56)
        self.setAccessibleName("Loading")
        self._elapsed = QtCore.QElapsedTimer()
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self.update)

    def showEvent(self, event) -> None:
        self._elapsed.start()
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.translate(self.rect().center())
        painter.rotate((self._elapsed.elapsed() % 1500) * 360 / 1500)
        pen = QtGui.QPen(self.palette().color(QtGui.QPalette.ColorRole.WindowText), 2)
        pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawRect(QtCore.QRectF(-14, -14, 28, 28))


class LoadingPanelController(QtCore.QObject):
    """One modal startup operation; only the project loader runs off-thread.

    Preparation yields between bounded GUI steps. The caller must keep its
    main window hidden until all steps, including the Quick host, are ready.
    """

    def __init__(self, *, owner: QtWidgets.QWidget | None = None) -> None:
        super().__init__()
        self.dialog = load_ui("MainWindow", "ui/loading_panel.ui")
        self.dialog.setWindowFlags(QtCore.Qt.WindowType.Dialog | QtCore.Qt.WindowType.FramelessWindowHint)
        self.dialog.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self._wordmark = self.dialog.findChild(QtWidgets.QLabel, "label_wordmark")
        self._refresh_wordmark()
        self.dialog.installEventFilter(self)
        host = self.dialog.findChild(QtWidgets.QWidget, "panel_animation")
        self.square = RotatingSquare(host)
        host.layout().addWidget(self.square)
        screen = owner.screen() if owner is not None else QtGui.QGuiApplication.screenAt(QtGui.QCursor.pos())
        screen = screen or QtGui.QGuiApplication.primaryScreen()
        center = owner.frameGeometry().center() if owner is not None else screen.availableGeometry().center()
        self.dialog.move(center - self.dialog.rect().center())

    def _refresh_wordmark(self) -> None:
        self._wordmark.setPixmap(branding_svg_pixmap(
            "eidora_wordmark.svg",
            QtCore.QSize(120, 23),
            device_pixel_ratio=self.dialog.devicePixelRatioF(),
            ink=self.dialog.palette().color(QtGui.QPalette.ColorRole.WindowText),
        ))

    def eventFilter(self, watched, event) -> bool:
        if event.type() in (
            QtCore.QEvent.Type.Show,
            QtCore.QEvent.Type.PaletteChange,
            QtCore.QEvent.Type.DevicePixelRatioChange,
        ):
            self._refresh_wordmark()
        if event.type() == QtCore.QEvent.Type.Show:
            # The chooser and main window are hidden during loading. Keep this
            # window independent of the application's normal dialog owner.
            self.dialog.windowHandle().setTransientParent(None)
        # Setup cannot be cancelled halfway through constructing Qt objects.
        if event.type() == QtCore.QEvent.Type.Close:
            event.ignore()
            return True
        if event.type() == QtCore.QEvent.Type.KeyPress and event.key() == QtCore.Qt.Key.Key_Escape:
            return True
        return super().eventFilter(watched, event)

    def run(
        self,
        load: Callable[[], Project | None],
        prepare: Callable[[Project], Iterator[None] | None] | None = None,
    ) -> Project | None:
        project = None
        steps = None
        error = None
        future = None
        finished = False
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="project-open")
        timer = QtCore.QTimer(self.dialog)
        timer.setInterval(10)
        timer.setSingleShot(True)
        complete = object()

        def advance() -> None:
            nonlocal project, steps, error, future, finished
            try:
                if future is None:
                    future = executor.submit(load)
                    return
                if not future.done():
                    return
                if steps is None:
                    project = future.result()
                    prepared = prepare(project) if project is not None and prepare is not None else None
                    steps = iter(()) if prepared is None else iter(prepared)
                if next(steps, complete) is complete:
                    finished = True
                    self.dialog.accept()
            except BaseException as exc:
                error = exc
                self.dialog.accept()
            finally:
                # Schedule after the step, not on a repeating deadline: an
                # overdue timer must not jump ahead of the spinner's paint.
                if not finished and error is None:
                    timer.start()

        timer.timeout.connect(advance)
        try:
            timer.start()
            self.dialog.exec()
        finally:
            timer.stop()
            timer.timeout.disconnect(advance)
            executor.shutdown(wait=False, cancel_futures=True)
            self.dialog.hide()
            self.dialog.removeEventFilter(self)
            if steps is not None and hasattr(steps, "close"):
                steps.close()
        if error is not None:
            raise error
        if not finished:
            raise RuntimeError("Application closed during startup")
        return project
