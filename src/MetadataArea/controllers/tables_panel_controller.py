"""Small binder for named tables and explicit record editing actions."""
from PySide6 import QtCore, QtGui, QtWidgets

from shared.qt.icons import current_icon_color, current_icon_theme, tinted_icon_pixmap
from shared.qt.theme import THEMES

from MetadataArea.table_repository import MetadataTableRepository, list_tables
from MetadataArea.service import MetadataService
from MetadataArea.relationship_service import MetadataRelationshipService
from shared.qt.widgets import require_child


class _MetadataTabStyle(QtWidgets.QProxyStyle):
    """Keep tab labels close to their icons without replacing native tab painting."""

    def drawItemText(self, painter, rect, flags, palette, enabled, text, text_role=QtGui.QPalette.ColorRole.NoRole):
        # This proxy is scoped to the tab bar. Reduce Qt's icon-to-label gap.
        offset = 4 if self.parent().layoutDirection() == QtCore.Qt.LayoutDirection.RightToLeft else -4
        super().drawItemText(painter, rect.translated(offset, 0), flags, palette, enabled, text, text_role)


class MetadataTablesPanelController(QtCore.QObject):
    def __init__(self, owner):
        super().__init__(owner._table_panel)
        self._linked_table_ids = set()
        self.owner = owner
        self.widget = owner._table_panel
        self.tabs = require_child(self.widget, QtWidgets.QTabWidget, 'tabs_metadata')
        self._tab_style = _MetadataTabStyle()
        self._tab_style.setParent(self.tabs.tabBar())
        self.tabs.tabBar().setStyle(self._tab_style)
        QtWidgets.QApplication.instance().paletteChanged.connect(self._refresh_tab_icons)
        self.content = require_child(self.widget, QtWidgets.QFrame, 'panel_metadata_table')
        self.first_page = require_child(self.widget, QtWidgets.QWidget, 'page_metadata_table')
        self.link_button = require_child(self.widget, QtWidgets.QPushButton, 'button_link_tables')
        self.link_button.clicked.connect(self._open_links)
        self.tabs.currentChanged.connect(self._selected)
        self.tabs.tabBar().setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabs.tabBar().customContextMenuRequested.connect(self._tab_menu)

    @QtCore.Slot()
    def _refresh_tab_icons(self):
        size = self.tabs.iconSize()
        icons = []
        for name, color in (("metadata_items", current_icon_color()),
                            ("metadata_link", current_icon_color()),
                            ("metadata_unlinked", THEMES[current_icon_theme().value].error_text)):
            pixmap = tinted_icon_pixmap(name, size, color)
            icon = QtGui.QIcon()
            for mode in (QtGui.QIcon.Mode.Normal, QtGui.QIcon.Mode.Active, QtGui.QIcon.Mode.Selected):
                for state in (QtGui.QIcon.State.Off, QtGui.QIcon.State.On):
                    icon.addPixmap(pixmap, mode, state)
            icons.append(icon)
        items, linked, unlinked = icons
        self.tabs.setTabIcon(0, items)
        for index in range(1, self.tabs.count()):
            table_id = self.tabs.tabBar().tabData(index)
            self.tabs.setTabIcon(index, linked if table_id in self._linked_table_ids else unlinked)

    def _open_links(self):
        self._run(self._link_tables)

    def connect_records(self):
        self.owner._table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.owner._table.customContextMenuRequested.connect(self._record_menu)

    def _menu(self, parent, position, actions):
        menu = QtWidgets.QMenu(parent)
        callbacks = {}
        for label, callback in actions:
            if callback in (self._link_tables, self._link) and not self.link_button.isEnabled():
                continue
            action = menu.addAction(label)
            if callback == self._unlink:
                action.setEnabled(self.owner._project is not None
                                  and self.relationships.can_unlink(self.owner._table_id))
            callbacks[action] = callback
        selected = menu.exec(parent.mapToGlobal(position))
        if selected in callbacks:
            self._run(callbacks[selected])

    def _tab_menu(self, position):
        index = self.tabs.tabBar().tabAt(position)
        if index < 0:
            return
        self.tabs.setCurrentIndex(index)
        if self.owner._table_id == 'metadata':
            actions = [('Unlink all', self._unlink), ('Delete all', self._delete),
                       ('Export', self.owner._on_export_metadata)]
        else:
            actions = [('Link tables…', self._link_tables), ('Unlink', self._unlink),
                       ('Rename table…', self._rename), ('Delete table…', self._delete),
                       ('Export table…', self.owner._on_export_metadata)]
        self._menu(self.tabs.tabBar(), position, actions)

    def _record_menu(self, position):
        self._menu(self.owner._table, position, [('Set value…', self._set_value),
            ('Assign source…', self._assign_source), ('Link records…', self._link),
            ('Link tables…', self._link_tables)])

    def _run(self, action):
        if self.owner._project is None or self.owner._media_publication_active:
            return
        try:
            action()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self.widget, 'Metadata', str(exc))

    @property
    def store(self):
        return MetadataTableRepository(self.owner._project.paths.metadata_database)

    def update_actions(self):
        available = self.owner._project is not None and not self.owner._media_publication_active
        self.tabs.setEnabled(available)
        can_link = (self.owner._project is not None and self.owner._table_id != 'metadata'
                    and self.relationships.can_link())
        self.link_button.setEnabled(available and can_link)

    @property
    def relationships(self):
        return MetadataRelationshipService(self.store.database)

    def refresh(self):
        self.tabs.setEnabled(self.owner._project is not None and not self.owner._media_publication_active)
        self.tabs.blockSignals(True)
        # The first page and all visible controls are Designer-owned. Additional
        # pages are runtime data; a single shared table avoids duplicate models.
        self.content.setParent(self.widget)
        self.first_page.layout().addWidget(self.content)
        while self.tabs.count() > 1:
            page = self.tabs.widget(1)
            self.tabs.removeTab(1)
            page.deleteLater()
        tables = list_tables(self.owner._project.paths.metadata_database) if self.owner._project else ()
        self._linked_table_ids = self.store.linked_table_ids() if self.owner._project else set()
        self.tabs.setTabText(0, 'Items')
        self.tabs.tabBar().setTabData(0, tables[0].id if tables else 'metadata')
        from MetadataArea.source_repository import MetadataSourceRepository
        sources = MetadataSourceRepository(self.owner._project.paths.metadata_database).sources() if self.owner._project else {}
        selected = 0
        for index, table in enumerate(tables):
            if index:
                page = QtWidgets.QWidget()
                layout = QtWidgets.QVBoxLayout(page)
                layout.setContentsMargins(self.first_page.layout().contentsMargins())
                self.tabs.addTab(page, table.name)
                self.tabs.tabBar().setTabData(index, table.id)
                status = "Linked to Items" if table.id in self._linked_table_ids else "Not linked to Items"
                self.tabs.setTabToolTip(index, f'{sources.get(table.id, (table.name, ""))[0]}\n{status}')
                self.tabs.tabBar().setAccessibleTabName(index, f"{table.name}: {status}")
            if table.id == self.owner._table_id:
                selected = index
        self.tabs.setCurrentIndex(selected)
        self.tabs.currentWidget().layout().addWidget(self.content)
        self.content.show()
        self.tabs.blockSignals(False)
        self._refresh_tab_icons()
        self.update_actions()

    def _selected(self, index):
        if index >= 0:
            self.tabs.widget(index).layout().addWidget(self.content)
            self.content.show()
            self.owner._table_id = self.tabs.tabBar().tabData(index)
            self.owner._clear_search()
            self.owner.refresh()

    def _text(self, title, label, value=''):
        text, accepted = QtWidgets.QInputDialog.getText(self.widget, title, label, text=value)
        return text.strip() if accepted else None

    def _changed(self):
        self.owner.refresh()
        self.owner._notify_metadata_changed()

    def _rename(self):
        if self.owner._table_id == 'metadata':
            self.owner._show_status('The first tab is always named Items.')
            return
        name = self._text('Rename table', 'Table name', self.tabs.tabText(self.tabs.currentIndex()))
        if name:
            self.store.rename(self.owner._table_id, name)
            self._changed()

    def _ids(self):
        selection = self.owner._table.selectionModel()
        return [self.owner._table_model.row_id_at(index.row()) for index in selection.selectedRows()] if selection else []

    def _set_value(self):
        ids = self._ids()
        if not ids:
            raise ValueError('Select records first.')
        columns = [column.name for column in self.store.repository(self.owner._table_id).list_columns() if column.name != 'id']
        column, accepted = QtWidgets.QInputDialog.getItem(self.widget, 'Set value', 'Field', columns, 0, False)
        if accepted and column:
            value = self._text('Set value', f'Value for {len(ids):,} selected records')
            if value is not None:
                from MetadataArea.controllers.operation_dialog_controller import run_operation
                store, table_id = self.store, self.owner._table_id
                service = MetadataService(store.repository(table_id))
                run_operation(lambda: store.set_values(table_id, ids, column, service.prepare_cell_value(column, value)),
                              'Updating selected records…')
                self._changed()

    def _assign_source(self):
        from AssetsArea.controllers.record_link_dialog_controller import assign_source_dialog
        if assign_source_dialog(self.widget, self.owner._project, self._ids()):
            self._changed()

    def _link(self):
        if not self.link_button.isEnabled():
            return
        ids = self._ids()
        if not ids:
            raise ValueError('Select records first.')
        from MetadataArea.controllers.record_picker_controller import select_records
        targets = select_records(self.store.repository('metadata'))
        if targets:
            self.relationships.link_records(self.owner._table_id, ids, 'metadata', targets, 'Items')
            self._changed()

    def _unlink(self):
        if not self.relationships.can_unlink(self.owner._table_id):
            return
        from MetadataArea.controllers.operation_dialog_controller import run_operation
        service, table_id = self.relationships, self.owner._table_id
        count = run_operation(lambda: service.unlink(table_id), 'Unlinking metadata…')
        self._changed()
        self.owner._show_status(f'{count:,} record links removed; table records retained')

    def _link_tables(self):
        if not self.link_button.isEnabled():
            return
        from MetadataArea.controllers.link_tables_dialog_controller import LinkTablesDialogController
        dialog = LinkTablesDialogController(self.store.database, self.owner._table_id)
        linked = dialog.exec()
        if linked:
            self._changed()
            self.owner._show_status(f'{linked:,} related records linked')

    def _delete(self):
        from MetadataArea.table_deletion_service import MetadataTableDeletionService
        service = MetadataTableDeletionService(self.owner._project)
        from MetadataArea.controllers.operation_dialog_controller import run_operation
        table_id = self.owner._table_id
        plan = run_operation(lambda: service.preview(table_id), 'Checking table dependencies…')
        message = (f'Delete all {plan.row_count:,} Items?' if table_id == 'metadata'
                   else f'Delete this table and its {plan.row_count:,} records?')
        if plan.descriptions:
            message += '\n\nThis also unlinks or removes:\n' + '\n'.join(plan.descriptions)
        reply = QtWidgets.QMessageBox.question(self.widget, 'Delete metadata table', message,
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Cancel)
        if reply == QtWidgets.QMessageBox.StandardButton.Yes:
            manager = getattr(self.owner._activity_manager, 'processes', None)
            if manager:
                manager.begin_metadata_publication()
            try:
                run_operation(lambda: service.apply(plan), 'Removing the table and selected dependencies…')
            finally:
                if manager:
                    manager.end_metadata_publication()
            tables = list_tables(self.store.database)
            self.owner._table_id = tables[0].id if tables else 'metadata'
            self._changed()
