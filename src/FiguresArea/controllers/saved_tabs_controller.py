"""Lightweight saved pages; only the selected page acquires a figure controller."""
from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class SavedFigureTabsController:
    def __init__(self, tabs, build, status_callback):
        self._tabs = tabs
        self._build = build
        self._show_status = status_callback
        self.records = {}
        self._futures = {}
        self._prepared = {}
        self._failed = set()
        self._service = None
        self._timer = QtCore.QTimer(tabs)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._check_selected)

    def add(self, record, service):
        self._service = service
        add_page = require_child(self._tabs, QtWidgets.QWidget, "page_add_figure")
        index = self._tabs.indexOf(add_page)
        page = load_ui("FiguresArea", "ui/saved_figure_placeholder.ui", self._tabs)
        page.layout().setContentsMargins(0, 0, 0, 0)
        page.setProperty("figureKind", record["kind"])
        page.setProperty("figureTitle", record["title"])
        self.records[page] = record
        retry = require_child(page, QtWidgets.QPushButton, "button_retry_figure")
        retry.hide()
        retry.clicked.connect(lambda: self._retry(page))
        self._tabs.insertTab(index, page, record["title"])
        return page

    def select(self, page):
        # Cancel only work that has not started. Keep running/completed results
        # available if their tabs are selected again.
        for candidate, future in list(self._futures.items()):
            if candidate is not page and future.cancel():
                del self._futures[candidate]
        if page not in self.records or page in self._failed:
            self._timer.stop()
            return
        if page not in self._futures and page not in self._prepared:
            self._futures[page] = self._service.prepare_saved_tab(self.records[page])
        # Let Qt paint the placeholder before constructing the selected UI.
        self._timer.start()

    def _check_selected(self):
        page = self._tabs.currentWidget()
        if page not in self.records or page in self._failed:
            self._timer.stop()
            return
        future = self._futures.get(page)
        if future is not None and not future.done():
            return
        try:
            if future is not None:
                self._prepared[page] = future.result()
                del self._futures[page]
            record = self._prepared.get(page)
            if record is None:
                return
            self._build(page, record)
            self.discard(page)
        except Exception as error:
            self._failed.add(page)
            require_child(page, QtWidgets.QLabel, "label_figure_loading").setText(
                "Could not load this figure. Your saved figure is unchanged."
            )
            require_child(page, QtWidgets.QPushButton, "button_retry_figure").show()
            self._show_status(f"Failed to load figure: {error}")
        self._timer.stop()

    def _retry(self, page):
        self._failed.discard(page)
        self._futures.pop(page, None)
        self._prepared.pop(page, None)
        require_child(page, QtWidgets.QLabel, "label_figure_loading").setText("Loading figure…")
        require_child(page, QtWidgets.QPushButton, "button_retry_figure").hide()
        self.select(page)

    def discard(self, page):
        self.records.pop(page, None)
        self._prepared.pop(page, None)
        self._failed.discard(page)
        future = self._futures.pop(page, None)
        if future is not None:
            future.cancel()

    def clear(self):
        self._timer.stop()
        for page in list(self.records):
            self.discard(page)
        self._service = None
