from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from shared.qt.ui_loader import load_ui
from shared.qt.icons import apply_icon_color, current_icon_theme
from shared.qt.theme import THEMES
from shared.qt.widgets import require_child


def _format_count(value: int) -> str:
    return f"{value:,}".replace(",", ".")


class MediaSourceRowController(QtCore.QObject):
    """Bind one source row, keeping progress separate from its name and actions."""

    def __init__(self, table, source_id):
        self.widget = load_ui("AssetsArea", "ui/media_source_row.ui", table)
        super().__init__(self.widget)
        self._table = table
        self.item = QtWidgets.QTableWidgetItem()
        self.item.setData(QtCore.Qt.ItemDataRole.UserRole, source_id)
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, self.item)
        table.setCellWidget(row, 0, self.widget)
        self._name = require_child(self.widget, QtWidgets.QLabel, "label_name")
        self._path = require_child(self.widget, QtWidgets.QLabel, "label_path")
        self._count = require_child(self.widget, QtWidgets.QLabel, "label_file_count")
        self._matched = require_child(self.widget, QtWidgets.QLabel, "label_matched")
        self._status = require_child(self.widget, QtWidgets.QLabel, "label_status")
        self._progress_panel = require_child(self.widget, QtWidgets.QWidget, "panel_progress")
        self._progress = require_child(self.widget, QtWidgets.QProgressBar, "progress_source")
        self.link = require_child(self.widget, QtWidgets.QPushButton, "button_link")
        self._linked_label = require_child(self.widget, QtWidgets.QLabel, "label_linked")
        link_width = self.link.sizeHint().width()
        self.link.setFixedWidth(link_width)
        self._linked_label.setFixedWidth(link_width)
        self.cancel = require_child(self.widget, QtWidgets.QPushButton, "button_cancel")
        self.options = require_child(self.widget, QtWidgets.QPushButton, "button_options")
        self._body = require_child(self.widget, QtWidgets.QWidget, "panel_body")
        self._actions = require_child(self.widget, QtWidgets.QWidget, "panel_actions")
        self._linked = False
        self._icon_state = None
        self._texts = {}
        QtWidgets.QApplication.instance().paletteChanged.connect(self._refresh_status_icon)
        self._body.installEventFilter(self)
        for label in (self._name, self._path, self._count, self._status):
            label.installEventFilter(self)

    @property
    def source_id(self):
        return str(self.item.data(QtCore.Qt.ItemDataRole.UserRole))

    def set_source_id(self, source_id):
        self.item.setData(QtCore.Qt.ItemDataRole.UserRole, source_id)

    def set_inspection(self, path):
        self._set_identity(path.name or str(path), path)

    def set_summary(self, summary, column_labels=None):
        self._set_identity(summary.source.label, summary.source.path)
        count = summary.asset_count
        self._set_elided(self._count,
            f"{_format_count(summary.linked_count)}/{_format_count(count)} "
            f"{summary.source.content_type.value} files linked")
        available = max(0, count - summary.missing_count - summary.error_count)
        self._count.setToolTip(
            f"{summary.linked_count:,} of {count:,} files linked to Items\n"
            f"{available:,} available · {summary.missing_count:,} missing · {summary.error_count:,} errors"
        )
        matched = (
            "All matched" if count > 0 and summary.linked_count >= count
            else f"{_format_count(summary.linked_count)}/{_format_count(count)} matched"
        )
        labels = column_labels or {}
        columns = [labels.get(name, name) for name in summary.metadata_columns]
        if columns:
            detail = "Linked Items column: " if len(columns) == 1 else "Linked Items columns: "
            matched += "\n" + detail + ", ".join(columns)
        else:
            matched += "\nLinked to Items"
        self._matched.setToolTip(matched)

    def _set_identity(self, name, path):
        self.item.setData(QtCore.Qt.ItemDataRole.AccessibleTextRole, name)
        self.item.setToolTip(str(path))
        self._set_elided(self._name, name)
        self._set_elided(self._path, str(path))

    def set_status(self, progress, active, *, has_matches=False, can_match=False):
        self._progress_panel.setVisible(active)
        self._status.setVisible(active)
        self._count.setVisible(not active)
        self.cancel.setVisible(active)
        self.cancel.setEnabled(progress.stage not in {"Cancelling", "Saving"})
        self.link.setVisible(not active and not has_matches)
        self._linked_label.setVisible(not active and has_matches)
        self.link.setEnabled(can_match and not has_matches)
        self._linked = has_matches
        self._matched.setVisible(True)
        if not has_matches:
            self._matched.setToolTip("Not linked to Items")
        self._refresh_status_icon()
        if active:
            text = progress.stage
            if progress.total:
                self._progress.setRange(0, 100)
                self._progress.setValue(min(99, int(100 * progress.completed / progress.total)))
                text += f" · {_format_count(progress.completed)}/{_format_count(progress.total)}"
            else:
                self._progress.setRange(0, 0)
                if progress.completed:
                    text += f" · {_format_count(progress.completed)} files found"
            self._set_elided(self._status, text)
        self._fit_info()
        self._body.layout().activate()
        self.widget.layout().activate()
        self._table.setRowHeight(self.item.row(), max(64, self.widget.sizeHint().height()))

    def _set_elided(self, label, text):
        self._texts[label] = text
        label.setToolTip(text)
        self._elide(label)

    def _elide(self, label):
        mode = QtCore.Qt.TextElideMode.ElideMiddle if label is self._path else QtCore.Qt.TextElideMode.ElideRight
        label.setText(label.fontMetrics().elidedText(self._texts[label], mode, label.contentsRect().width()))

    def _refresh_status_icon(self, *_):
        color = None if self._linked else THEMES[current_icon_theme().value].error_text
        state = (self._linked, color, current_icon_theme())
        if state == self._icon_state:
            return
        self._icon_state = state
        self._matched.setProperty("iconName", "metadata_link" if self._linked else "metadata_unlinked")
        self._matched.setProperty("iconColorOverride", color)
        apply_icon_color(self.widget)

    def _fit_info(self):
        header = self._body.findChild(QtWidgets.QHBoxLayout, "layout_header")
        available = max(0, self._body.width() - self._actions.sizeHint().width()
                        - self._matched.sizeHint().width() - 3 * header.spacing())
        full_name = self._name.fontMetrics().horizontalAdvance(self._texts.get(self._name, "")) + 2
        reserve = min(full_name, self._name.fontMetrics().horizontalAdvance("MMMMMMMM"))
        info = self._status if not self._status.isHidden() else self._count
        info_width = info.fontMetrics().horizontalAdvance(self._texts.get(info, "")) + 2
        info.setFixedWidth(min(info_width, max(0, available - reserve)))
        self._name.setFixedWidth(min(full_name, max(0, available - info.width())))

    def eventFilter(self, watched, event):
        if watched is self._body and event.type() in {QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.FontChange}:
            self._fit_info()
        if watched in self._texts and event.type() in {QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.FontChange}:
            self._elide(watched)
        return super().eventFilter(watched, event)
