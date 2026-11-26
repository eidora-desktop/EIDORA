"""Bind explicit table matching and its bounded preview to a Designer dialog."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from PySide6 import QtCore, QtWidgets

from MetadataArea.controllers.operation_dialog_controller import run_operation
from MetadataArea.domain import TableLinkRequest
from MetadataArea.relationship_service import MetadataRelationshipService, is_relationship_key
from MetadataArea.table_repository import MetadataTableRepository, list_tables
from shared.qt.dialog_windows import install_dialog_window_policy
from shared.qt.ui_loader import load_ui
from shared.qt.table_preview import set_preview_continuation
from shared.qt.widgets import require_child


class LinkTablesDialogController:
    def __init__(self, database, source_table):
        self.service = MetadataRelationshipService(database)
        if not self.service.can_link():
            raise ValueError('Create Items and add a metadata source before linking.')
        store = MetadataTableRepository(database)
        self.tables = list_tables(database)
        self.columns = {table.id: [column for column in store.repository(table.id).list_columns() if column.name != 'id' or table.id == 'metadata'] for table in self.tables}
        self.dialog = load_ui('MetadataArea', 'ui/link_tables_dialog.ui', None)
        self.dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        install_dialog_window_policy(QtWidgets.QApplication.instance())
        self.source_table = require_child(self.dialog, QtWidgets.QComboBox, 'combo_source_table')
        self.source_column = require_child(self.dialog, QtWidgets.QComboBox, 'combo_source_column')
        self.target_table = require_child(self.dialog, QtWidgets.QComboBox, 'combo_target_table')
        self.target_column = require_child(self.dialog, QtWidgets.QComboBox, 'combo_target_column')
        self.status = require_child(self.dialog, QtWidgets.QLabel, 'label_preview_status')
        self.preview_table = require_child(self.dialog, QtWidgets.QTableWidget, 'table_match_preview')
        self.preview_count = require_child(self.dialog, QtWidgets.QLabel, 'label_preview_count')
        self.preview_table.horizontalHeader().setStretchLastSection(True)
        self.preview_table.horizontalHeader().setDefaultAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        self.link_button = require_child(self.dialog, QtWidgets.QPushButton, 'button_link')
        self.preview_table.setColumnHidden(0, True)
        self.preview_table.setColumnHidden(2, True)
        self.accept_multiple = require_child(self.dialog, QtWidgets.QCheckBox, 'check_accept_multiple')
        self.options_button = require_child(self.dialog, QtWidgets.QToolButton, 'button_link_options')
        self.options_panel = require_child(self.dialog, QtWidgets.QWidget, 'panel_link_options')
        self.detect_button = require_child(self.dialog, QtWidgets.QPushButton, 'button_detect_match')
        self.match_summary = require_child(self.dialog, QtWidgets.QLabel, 'label_match_summary')
        self.coverage = require_child(self.dialog, QtWidgets.QLabel, 'label_match_coverage')
        self.preview_filter = require_child(self.dialog, QtWidgets.QComboBox, 'combo_preview_filter')
        self.preview = None
        self.linked_count = 0
        self._source_id = source_table
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='metadata-matching')
        self._future = None
        self._cancelled = Event()
        self._disposed = False
        self._debounce = QtCore.QTimer(self.dialog)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(150)
        self._debounce.timeout.connect(self._preview)
        self._poll_timer = QtCore.QTimer(self.dialog)
        self._poll_timer.setInterval(40)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start()
        self._connections = []
        for table in self.tables:
            self.source_table.addItem('Items' if table.id == 'metadata' else table.name, table.id)
        self.source_table.setCurrentIndex(max(0, self.source_table.findData(source_table)))
        self._source_changed()
        self._connect(self.source_table.currentIndexChanged, self._source_changed)
        self._connect(self.target_table.currentIndexChanged, self._target_changed)
        self._connect(self.source_column.currentIndexChanged, self._source_column_changed)
        self._connect(self.target_column.currentIndexChanged, self._columns_changed)
        self._connect(self.accept_multiple.toggled, self._columns_changed)
        self._connect(self.preview_filter.currentIndexChanged, self._invalidate)
        self._connect(self.options_button.toggled, self._toggle_options)
        self._connect(self.detect_button.clicked, self._suggest)
        self.detect_button.setEnabled(len(self.tables) > 1)
        self._connect(self.link_button.clicked, self._link)
        self._connect(require_child(self.dialog, QtWidgets.QPushButton, 'button_cancel').clicked, self.dialog.reject)

    def _toggle_options(self, expanded):
        self.options_panel.setVisible(expanded)
        self.options_button.setArrowType(
            QtCore.Qt.ArrowType.DownArrow if expanded else QtCore.Qt.ArrowType.RightArrow
        )

    def _set_status(self, text):
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def _show_match(self):
        self.match_summary.setText(
            f'{self.source_column.currentText()} → {self.target_column.currentText()}'
        )

    def _connect(self, signal, callback):
        signal.connect(callback)
        self._connections.append((signal, callback))

    def _fill_columns(self, combo, table_id, preferred=None):
        with QtCore.QSignalBlocker(combo):
            combo.clear()
            columns = [column for column in self.columns.get(table_id, ()) if column.value_type != "list"]
            for column in columns:
                combo.addItem(column.label, column.name)
            if preferred is None:
                preferred = next((column.name for column in columns if is_relationship_key(column.name)), None)
            index = combo.findData(preferred)
            combo.setCurrentIndex(index if index >= 0 else (1 if combo.count() > 1 else 0))

    def _source_changed(self, *_):
        self._fill_columns(self.source_column, self.source_table.currentData())
        previous = self.target_table.currentData()
        with QtCore.QSignalBlocker(self.target_table):
            self.target_table.clear()
            for table in self.tables:
                if (table.id != self.source_table.currentData()
                        and 'metadata' in (table.id, self.source_table.currentData())):
                    self.target_table.addItem('Items' if table.id == 'metadata' else table.name, table.id)
            self.target_table.setCurrentIndex(max(0, self.target_table.findData(previous)))
        self._target_changed()

    def _target_changed(self, *_):
        self._fill_columns(self.target_column, self.target_table.currentData(), self.source_column.currentData())
        self._columns_changed()

    def _source_column_changed(self, *_):
        index = self.target_column.findData(self.source_column.currentData())
        if index >= 0:
            with QtCore.QSignalBlocker(self.target_column):
                self.target_column.setCurrentIndex(index)
        self._columns_changed()

    def _columns_changed(self, *_):
        self._show_match()
        self._invalidate()

    def _invalidate(self, *_):
        self._cancelled.set()
        if self._future is not None:
            self._future.cancel()
            self._future = None
        self.preview = None
        self.link_button.setEnabled(False)
        self.link_button.setText("Link records")
        self.preview_table.setRowCount(0)
        self.preview_count.setText('Updating preview…')
        self.coverage.setText("Checking matches…")
        self._set_status('' if self.target_table.count() else 'Add a second metadata table before linking.')
        self._debounce.start()

    def _submit(self, operation, kind):
        self._debounce.stop()
        self._cancelled.set()
        if self._future is not None:
            self._future.cancel()
        self._cancelled = Event()
        self._kind = kind
        self._future = self._executor.submit(operation, self._cancelled)

    def _suggest(self):
        self._invalidate()
        self.match_summary.clear()
        # Automatic suggestions never carry a previous multiple-match approval.
        with QtCore.QSignalBlocker(self.accept_multiple):
            self.accept_multiple.setChecked(False)
        service, table_id = self.service, self._source_id
        self._submit(lambda cancelled: service.best_match(table_id, cancelled), 'suggestion')

    def _preview(self):
        self._debounce.stop()
        if self._disposed:
            return
        request = TableLinkRequest(self.source_table.currentData(), self.source_column.currentData(),
                                   self.target_table.currentData(), self.target_column.currentData(),
                                   f'{self.target_table.currentText()} ({self.source_column.currentText()} → {self.target_column.currentText()})',
                                   accept_multiple=self.accept_multiple.isChecked())
        if not request.target_table or not request.source_column or not request.target_column:
            self._set_status('Choose two tables and their matching columns.')
            self.options_button.setChecked(True)
            return
        service = self.service
        row_filter = ('all', 'unmatched', 'multiple')[self.preview_filter.currentIndex()]
        self._submit(lambda cancelled: service.preview(request, row_filter) if not cancelled.is_set() else None, 'preview')

    def _poll(self):
        if self._disposed or self._future is None or not self._future.done():
            return
        future, self._future = self._future, None
        try:
            result = future.result()
        except Exception as exc:
            self._show_match()
            self.preview_count.clear()
            self._set_status(str(exc))
            self.options_button.setChecked(True)
            return
        if self._kind == 'suggestion':
            if result is None:
                self.preview_count.clear()
                self.coverage.setText('No suggested match')
                self.match_summary.setText('Choose matching tables and columns in Link options.')
                self._set_status('No reliable exact match found.' if self.target_table.count()
                                 else 'Add a second metadata table before linking.')
                self.options_button.setChecked(True)
                return
            with QtCore.QSignalBlocker(self.source_table):
                self.source_table.setCurrentIndex(self.source_table.findData(result.source_table))
            self._source_changed()
            with QtCore.QSignalBlocker(self.source_column):
                self.source_column.setCurrentIndex(self.source_column.findData(result.source_column))
            with QtCore.QSignalBlocker(self.target_table):
                self.target_table.setCurrentIndex(self.target_table.findData(result.target_table))
            self._target_changed()
            with QtCore.QSignalBlocker(self.target_column):
                self.target_column.setCurrentIndex(self.target_column.findData(result.target_column))
            self._show_match()
            self._preview()
            return
        if result is None:
            return
        self.preview = result
        self._show_preview(result)

    def _show_preview(self, preview):
        total = preview.matched_count + preview.missing_count + preview.ambiguous_count
        full = total > 0 and preview.matched_count == total
        state = 'All Matched' if full else 'Partly Matched' if preview.matched_count else 'None Matched'
        self.coverage.setText(f'{state} ({preview.matched_count:,}/{total:,})')
        counts = (total, preview.missing_count, preview.ambiguous_count)
        with QtCore.QSignalBlocker(self.preview_filter):
            for index, label in enumerate(('All records', 'Unmatched', 'Multiple matches')):
                self.preview_filter.setItemText(index, f'{label} ({counts[index]:,})')
        self.preview_table.setHorizontalHeaderLabels([
            'Source record', f'{self.source_column.currentText()} ({self.source_table.currentText()})',
            'Target record', f'{self.target_column.currentText()} ({self.target_table.currentText()})'
        ])
        filtered_total = counts[self.preview_filter.currentIndex()]
        self.preview_count.setText(f'Preview: {len(preview.rows):,}/{filtered_total:,} rows')
        self._set_status('')
        already_linked = preview.existing_count > 0 and preview.linkable_count == 0
        self.link_button.setText('Already linked' if already_linked else 'Link records')
        self.preview_table.setRowCount(len(preview.rows))
        for row_index, row in enumerate(preview.rows):
            for column_index, value in enumerate(row):
                if column_index == 3:
                    if value == 'Matched':
                        value = preview.target_values[row_index]
                    elif value == 'Missing':
                        value = 'No match'
                    else:
                        value = f'{preview.match_counts[row_index]:,} matching records'
                self.preview_table.setItem(row_index, column_index,
                                          QtWidgets.QTableWidgetItem('' if value is None else str(value)))
        set_preview_continuation(self.preview_table, len(preview.rows), filtered_total, 1)
        self.preview_table.resizeColumnsToContents()
        self.preview_table.setColumnWidth(1, min(400, max(220, self.preview_table.columnWidth(1))))
        self.link_button.setEnabled(preview.linkable_count > 0)

    def _link(self):
        if self.preview is None or not self.link_button.isEnabled():
            return
        preview = self.preview
        try:
            self.linked_count = run_operation(lambda: self.service.link(preview), 'Linking related records…')
        except Exception as exc:
            self._invalidate()
            self._set_status(str(exc))
            self.options_button.setChecked(True)
            return
        self.dialog.accept()

    def exec(self):
        try:
            self._suggest()
            self.dialog.exec()
            return self.linked_count
        finally:
            self.dispose()

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        self._debounce.stop()
        self._poll_timer.stop()
        self._debounce.timeout.disconnect(self._preview)
        self._poll_timer.timeout.disconnect(self._poll)
        self._cancelled.set()
        if self._future is not None:
            self._future.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
        for signal, callback in self._connections:
            signal.disconnect(callback)
        self._connections.clear()
        self.dialog.close()
