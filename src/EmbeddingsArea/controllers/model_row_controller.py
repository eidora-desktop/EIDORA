"""Designer-owned, non-expandable row for installed and installing models."""
from PySide6 import QtCore, QtWidgets
from shiboken6 import isValid

from shared.qt.panel_rows import PanelRowHeader, schedule_row_layout, section_widget
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class ModelRowController(QtCore.QObject):
    def __init__(self, tree, identity, kind):
        self.widget = load_ui("EmbeddingsArea", "ui/model_row.ui", tree)
        super().__init__(self.widget)
        self.item = QtWidgets.QTreeWidgetItem([""])
        self.item.setFlags(self.item.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable)
        self.item.setData(0, 32, identity)
        self.item.setData(0, 33, kind)
        tree.addTopLevelItem(self.item)
        tree.setItemWidget(self.item, 0, self.widget)
        self.name = require_child(self.widget, QtWidgets.QLabel, "label_name")
        self.status = require_child(self.widget, QtWidgets.QLabel, "label_status")
        self.summary = require_child(self.widget, QtWidgets.QLabel, "label_row_summary")
        self.progress = require_child(self.widget, QtWidgets.QProgressBar, "progress_model")
        self.progress_panel = section_widget(self.widget, "layout_progress")
        self.cancel = require_child(self.widget, QtWidgets.QPushButton, "button_cancel")
        self.run = require_child(self.widget, QtWidgets.QPushButton, "button_run")
        self.options = require_child(self.widget, QtWidgets.QPushButton, "button_options")
        self.header = PanelRowHeader(self.widget)
        self.header.resized.connect(self._resize)
        self._resize()

    def set_summary(self, name, summary, tooltip):
        self.item.setData(0, QtCore.Qt.ItemDataRole.AccessibleTextRole, name)
        self.header.set_text(self.name, name)
        self.header.set_text(self.summary, summary)
        self.name.setToolTip(tooltip)

    def set_installing(self, pending):
        active = pending is not None
        self.progress_panel.setVisible(active)
        self.summary.setVisible(not active)
        self.cancel.setVisible(active)
        self.run.setVisible(not active)
        self.options.setVisible(not active)
        if active:
            self.progress.setRange(0, 100)
            self.progress.setValue(max(0, min(100, int(pending.progress))))
            self.status.setText("Cancelling…" if pending.abort_requested else f"Installing {pending.progress}%")
            self.cancel.setEnabled(not pending.abort_requested)
        else:
            self.status.setText("Installed")

    def dispose(self):
        # Tree items die immediately; row widgets survive until deferred deletion.
        self.header.resized.disconnect(self._resize)

    def _resize(self):
        if not isValid(self.item):
            return
        tree = self.item.treeWidget()
        if tree is None:
            return
        height = self.header.collapsed_height
        self.widget.setMinimumHeight(height)
        self.widget.resize(self.widget.width(), height)
        if self.item.sizeHint(0).height() != height:
            self.item.setSizeHint(0, QtCore.QSize(0, height))
            schedule_row_layout(tree)
