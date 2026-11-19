"""Stable two-line headers for the Media and Embeddings panel rows."""
from __future__ import annotations

from weakref import WeakSet

from PySide6 import QtCore, QtWidgets
from shiboken6 import isValid

from shared.qt.widgets import require_child


def schedule_row_layout(view: QtWidgets.QAbstractItemView) -> None:
    """Reposition item widgets after their layouts have accepted new size hints."""
    timer = view.findChild(QtCore.QTimer, "timer_panel_row_layout", QtCore.Qt.FindChildOption.FindDirectChildrenOnly)
    if timer is None:
        timer = QtCore.QTimer(view)
        timer.setObjectName("timer_panel_row_layout")
        timer.setSingleShot(True)
        timer.timeout.connect(view.doItemsLayout)
    timer.start(0)


def section_widget(row: QtWidgets.QWidget, layout_name: str) -> QtWidgets.QWidget:
    """Give a Designer layout a visibility/size boundary, with or without a wrapper.

    Section layout names are the binding contract. Designer may keep them in a
    QWidget or directly inside another layout; its margins, stretch and
    alignment remain authoritative in either representation.
    """
    layout = require_child(row, QtWidgets.QLayout, layout_name)
    parent = layout.parent()
    if isinstance(parent, QtWidgets.QWidget):
        return parent
    if not isinstance(parent, (QtWidgets.QBoxLayout, QtWidgets.QGridLayout, QtWidgets.QFormLayout)):
        raise RuntimeError(f"{layout_name} must belong to a widget, box, grid or form layout")
    index = next(i for i in range(parent.count()) if parent.itemAt(i).layout() is layout)
    alignment = parent.itemAt(index).alignment()
    position = parent.stretch(index) if isinstance(parent, QtWidgets.QBoxLayout) else parent.getItemPosition(index)
    parent.takeAt(index)
    layout.setParent(None)
    container = QtWidgets.QWidget(parent.parentWidget())
    container.setObjectName(f"{layout_name}_container")
    container.setLayout(layout)
    if isinstance(parent, QtWidgets.QBoxLayout):
        parent.insertWidget(index, container, position, alignment)
    elif isinstance(parent, QtWidgets.QGridLayout):
        parent.addWidget(container, *position, alignment)
    else:
        parent.setWidget(*position, container)
        parent.setAlignment(container, alignment)
    return container


class _PanelRowHeights(QtCore.QObject):
    """Application-owned measurements; rows retain ownership of their widgets."""

    def __init__(self, app):
        super().__init__(app)
        self.headers = WeakSet()
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.refresh)

    def refresh(self):
        headers = [header for header in self.headers if isValid(header)]
        if not headers:
            return
        line = max(header._metrics[0] for header in headers)
        height = max(64, max(header._metrics[1] for header in headers) + 2 * line)
        for header in headers:
            header._apply_size(line, height)

    def schedule(self):
        self._timer.start(0)


class PanelRowHeader(QtCore.QObject):
    resized = QtCore.Signal()

    def __init__(self, row: QtWidgets.QWidget) -> None:
        super().__init__(row)
        self._row = row
        self.card = require_child(row, QtWidgets.QLayout, "layout_card_root").parentWidget()
        self.widget = section_widget(row, "layout_row_header")
        self._title = section_widget(row, "layout_header")
        self._summary = section_widget(row, "layout_summary")
        self._texts: dict[QtWidgets.QLabel, str] = {}
        self._metrics = (0, 0)
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fit)
        app = QtWidgets.QApplication.instance()
        self._heights = app.findChild(_PanelRowHeights)
        if self._heights is None:
            self._heights = _PanelRowHeights(app)
        self._heights.headers.add(self)
        self.destroyed.connect(self._heights.schedule)
        for control in self.widget.findChildren(QtWidgets.QAbstractButton):
            if "options" in control.objectName():
                control.setFixedWidth(22)
        for progress in self._summary.findChildren(QtWidgets.QProgressBar):
            progress.setFixedHeight(6)
        for widget in [self.widget, *self.widget.findChildren(QtWidgets.QWidget)]:
            # Include native button/progress edges in layout allocation, rather
            # than letting style-specific item margins clip nested groups.
            widget.setAttribute(QtCore.Qt.WidgetAttribute.WA_LayoutUsesWidgetRect, True)
            widget.updateGeometry()
            widget.installEventFilter(self)
        self._fit()

    def set_text(self, label: QtWidgets.QLabel, text: str) -> None:
        if label not in self._texts:
            label.installEventFilter(self)
        self._texts[label] = text
        label.setToolTip(text)
        self._elide(label)

    def _elide(self, label: QtWidgets.QLabel) -> None:
        label.setText(label.fontMetrics().elidedText(
            self._texts[label], QtCore.Qt.TextElideMode.ElideRight,
            max(0, label.contentsRect().width()),
        ))

    def _fit(self) -> None:
        # Include hidden actions so running/paused/idle use the same reservation.
        # Never impose another control's size hint on a button.
        controls = self.widget.findChildren(QtWidgets.QAbstractButton) + self.widget.findChildren(QtWidgets.QLabel)
        line_height = max([self.widget.fontMetrics().height(), *(
            max(control.sizeHint().height(), control.minimumSizeHint().height(), control.minimumHeight())
            for control in controls
        )])
        line_height += max(
            section.layout().contentsMargins().top() + section.layout().contentsMargins().bottom()
            for section in (self._title, self._summary)
        )
        layout = self.widget.layout()
        margins = layout.contentsMargins()
        padding = margins.top() + margins.bottom() + layout.spacing()
        metrics = (line_height, padding)
        if metrics != self._metrics:
            self._metrics = metrics
            self._heights.refresh()

    def _apply_size(self, line_height: int, height: int) -> None:
        changed = self.widget.minimumHeight() != height or self._title.minimumHeight() != line_height
        self._title.setMinimumHeight(line_height)
        self._summary.setMinimumHeight(height - self._metrics[1] - line_height)
        self.widget.setMinimumHeight(height)
        if changed:
            self.resized.emit()

    @property
    def collapsed_height(self) -> int:
        margins = self._row.layout().contentsMargins()
        card_margins = self.card.contentsMargins()
        return (self.widget.minimumHeight() + card_margins.top() + card_margins.bottom()
                + margins.top() + margins.bottom())

    def eventFilter(self, watched, event):
        if event.type() in {
            QtCore.QEvent.Type.FontChange, QtCore.QEvent.Type.StyleChange, QtCore.QEvent.Type.LayoutRequest,
        }:
            self._timer.start(0)
        if watched in self._texts and event.type() in {
            QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.FontChange,
        }:
            self._elide(watched)
        return super().eventFilter(watched, event)
