from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from PySide6 import QtCore, QtGui, QtWidgets

from EmbeddingsArea.domain import MediaContentType, MediaLinkRequest, MediaOperationResult, MediaProgress
from EmbeddingsArea.media_sources import cleanup_media_source_inspection, source_id_for_path
from EmbeddingsArea.session import EmbeddingsAreaSession
from ProjectArea.controllers.media_link_dialog_controller import MediaLinkDialogController
from ProjectArea.controllers.media_source_jobs_controller import MediaSourceJobsController
from ProjectArea import media_service
from shared.qt.item_views import configure_panel_item_view
from shared.qt.message_dialogs import show_error
from shared.qt.panel_controller import UiPanelController
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


def _format_count(value: int) -> str:
    return f"{value:,}".replace(",", ".")


class MediaSourcesPanelController(UiPanelController):
    package = "ProjectArea"
    ui_path = "ui/media_sources_panel.ui"

    def __init__(self, *args, **kwargs):
        self._media_changed_callback = self._metadata_changed_callback = None
        self._operation_callback = self._busy_callback = None
        self._can_create_thumbnails = self._create_thumbnails = self._source_in_use = None
        self._session = None
        self._link_dialog = None
        self._creation_dialog = None
        self._summaries = {}
        self._rows = {}
        self._states = {}
        self._pending_registrations = set()
        self._refresh_pending = False
        super().__init__(*args, **kwargs)
        self._jobs = MediaSourceJobsController(self._activity_manager, self._on_job_changed)
        self._widget.destroyed.connect(self._jobs.shutdown)

    def set_session(self, session):
        self._session = session

    def set_media_changed_callback(self, callback):
        self._media_changed_callback = callback

    def set_metadata_changed_callback(self, callback):
        self._metadata_changed_callback = callback

    def set_operation_finished_callback(self, callback):
        self._operation_callback = callback

    def set_busy_callback(self, callback):
        self._busy_callback = callback

    def set_thumbnail_creation_callbacks(self, *, can_create, create):
        self._can_create_thumbnails, self._create_thumbnails = can_create, create

    def set_source_in_use_callback(self, callback):
        self._source_in_use = callback

    def source_busy(self, source_id):
        return self._jobs.busy(source_id)

    def _wire(self):
        self._table = require_child(self._widget, QtWidgets.QTableWidget, "table_media_sources")
        configure_panel_item_view(self._table)
        self._file_button = require_child(self._widget, QtWidgets.QAbstractButton, "button_import_media_file")
        self._folder_button = require_child(self._widget, QtWidgets.QAbstractButton, "button_import_media_folder")
        self._file_button.clicked.connect(self._on_import_media_file)
        self._folder_button.clicked.connect(self._on_import_media_folder)
        self._table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._open_row_context_menu)
        self._configure_table_columns()
        self._render_no_project()

    def set_project(self, project):
        if self._creation_dialog is not None:
            self._creation_dialog.reject()
        if self._link_dialog is not None:
            self._link_dialog.dialog.reject()
        self._jobs.reset()
        self._states.clear()
        self._pending_registrations.clear()
        self._rows.clear()
        self._summaries.clear()
        self._refresh_pending = False
        super().set_project(project)
        if project is not None and self._session is None:
            self._session = EmbeddingsAreaSession.for_project(project)
        if self._busy_callback:
            self._busy_callback(False)
        self.refresh()

    def refresh(self):
        if self._jobs.busy():
            self._refresh_pending = True
            return
        if self._project is None or self._session is None:
            self._render_no_project()
            return
        try:
            summaries = self._session.media_library.list_source_summaries()
        except Exception as exc:
            self._show_status(f"Media sources could not be loaded: {exc}")
            return
        self._summaries = {summary.source.id: summary for summary in summaries}
        self._render_summaries(summaries)

    def _render_summaries(self, summaries, **_):
        selected = self._selected_source_id()
        wanted = {summary.source.id for summary in summaries}
        # Add/update replacements before removing old or placeholder rows.
        for summary in summaries:
            self._update_source(summary)
        for row in reversed(range(self._table.rowCount())):
            item = self._table.item(row, 0)
            source_id = str(item.data(QtCore.Qt.ItemDataRole.UserRole) or "") if item else ""
            if source_id not in wanted:
                self._table.removeRow(row)
                self._rows.pop(source_id, None)
        if not summaries:
            self._render_empty("No media sources imported")
        elif selected in wanted:
            self._select_source_row(selected)
        else:
            self._table.setCurrentCell(0, 0)

    def _update_source(self, summary):
        source_id = summary.source.id
        self._summaries[source_id] = summary
        if source_id not in self._rows:
            row = self._table.rowCount()
            self._table.insertRow(row)
            for column in range(self._table.columnCount()):
                self._table.setItem(row, column, QtWidgets.QTableWidgetItem())
            item = self._table.item(row, 0)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, source_id)
            status = load_ui("ProjectArea", "ui/media_source_status.ui", self._table)
            self._table.setCellWidget(row, 3, status)
            self._rows[source_id] = (item, status)
            status.findChild(QtWidgets.QPushButton, "button_link").clicked.connect(
                lambda _checked=False, identity=source_id: self._on_link_source(identity))
            status.findChild(QtWidgets.QPushButton, "button_cancel").clicked.connect(
                lambda _checked=False, identity=source_id: self._jobs.cancel_source(identity))
        item, _status = self._rows[source_id]
        row = item.row()
        path = str(summary.source.path)
        for column, text in ((0, summary.source.label), (1, summary.source.content_type.value),
                             (2, _format_count(summary.asset_count)),
                             (4, path if len(path)<=120 else "..."+path[-120:])):
            self._table.item(row, column).setText(text)
        self._table.item(row, 0).setToolTip(path)
        self._table.item(row, 4).setToolTip(path)
        self._update_status(source_id)

    def _update_status(self, source_id):
        if source_id not in self._rows:
            return
        _, status = self._rows[source_id]
        summary = self._summaries[source_id]
        progress, active = self._states.get(source_id, (MediaProgress(""), False))
        bar = status.findChild(QtWidgets.QProgressBar, "progress_source")
        label = status.findChild(QtWidgets.QLabel, "label_status")
        cancel = status.findChild(QtWidgets.QPushButton, "button_cancel")
        link = status.findChild(QtWidgets.QPushButton, "button_link")
        bar.setVisible(active)
        cancel.setVisible(active)
        cancel.setEnabled(progress.stage not in {"Cancelling", "Saving"})
        if active:
            label.setVisible(True)
            link.setVisible(False)
            label.setText(progress.stage)
            if progress.total:
                bar.setRange(0, 100)
                bar.setValue(min(99, int(100*progress.completed/progress.total)))
                bar.setFormat(
                    f"{_format_count(progress.completed)}/{_format_count(progress.total)}"
                )
            else:
                bar.setRange(0, 0)
                label.setText(
                    progress.stage
                    + (f" · {_format_count(progress.completed)}" if progress.completed else "")
                )
        else:
            has_matches = summary.linked_count > 0
            label.setVisible(has_matches)
            link.setVisible(not has_matches)
            label.setText(
                "all"
                if summary.asset_count > 0 and summary.linked_count >= summary.asset_count
                else f"{_format_count(summary.linked_count)}/{_format_count(summary.asset_count)}"
            )
            link.setEnabled(summary.asset_count > 0 and not self._jobs.busy())

    def _on_job_changed(self, source_id, progress, active):
        if not active and self._jobs.busy(source_id):
            return
        self._states[source_id] = (progress, active)
        if not active and source_id in self._pending_registrations:
            self._pending_registrations.discard(source_id)
            self._refresh_pending = True
        for identity in self._rows:
            self._update_status(identity)
        if self._busy_callback:
            self._busy_callback(self._jobs.busy())
        if not self._jobs.busy() and self._refresh_pending:
            self._refresh_pending = False
            self.refresh()

    def _on_operation_finished(self, result):
        self._pending_registrations.discard(result.summary.source.id)
        self._update_source(result.summary)
        if self._operation_callback:
            self._operation_callback(result)
        elif result.metadata_changed and self._metadata_changed_callback:
            self._metadata_changed_callback()
        elif self._media_changed_callback:
            self._media_changed_callback()
        if result.cancelled or result.error_message:
            detail = "cancelled" if result.cancelled else f"failed: {result.error_message}"
            self._show_status(f"{result.summary.source.label}: indexed; metadata creation {detail}")
        else:
            self._show_status(f"{result.summary.source.label}: {result.summary.linked_count:,} / {result.summary.asset_count:,} linked")

    def _failed(self, message, cancelled):
        if cancelled:
            self._show_status("Media operation cancelled")
        else:
            self._show_status(f"Media operation failed: {message}")
        # Restore cached source actions after a failed or cancelled operation.
        self._refresh_pending = True

    def _in_use(self, source_id):
        return self._jobs.busy(source_id) or bool(self._source_in_use and self._source_in_use(source_id))

    def _on_import_media_folder(self):
        self._import_media_source(self._choose_media_folder_path, recursive=True)

    def _on_import_media_file(self):
        self._import_media_source(self._choose_media_file_path, recursive=False)

    def _import_media_source(self, chooser, *, recursive):
        if self._project is None or self._session is None:
            self._show_status("Open or create a project before importing media.")
            return
        path = chooser()
        if path is None:
            return
        resolved_path = path.resolve()
        existing_sources = [
            summary.source
            for summary in self._summaries.values()
            if summary.source.path == resolved_path
        ]
        if path.is_dir() or path.suffix.lower() == ".zip":
            project = self._project
            source_id = source_id_for_path(path)
            self._jobs.submit(
                source_id,
                "Inspecting media",
                lambda progress, cancel: media_service.inspect_source(path, recursive, progress, cancel),
                lambda inspection: self._on_source_inspected(project, path, recursive, inspection),
                self._failed,
            )
            return
        existing = existing_sources[0] if existing_sources else None
        if existing and self._in_use(existing.id):
            self._show_status("This source is used by a queued or running process.")
            return
        try:
            source = self._session.media_library.prepare_source(
                path,
                existing.content_type if existing else None,
                recursive=recursive,
                existing=existing,
            )
            self._begin_source_scan(source)
        except Exception as exc:
            self._failed(str(exc), False)

    def _on_source_inspected(self, project, path, recursive, inspection):
        if self._project is not project or self._session is None:
            cleanup_media_source_inspection(inspection)
            return
        registered_types = {
            summary.source.content_type
            for summary in self._summaries.values()
            if summary.source.path == path.resolve()
        }
        available_counts = tuple(
            (content_type, count)
            for content_type, count in inspection.counts
            if content_type not in registered_types
        )
        content_types = tuple(
            content_type for content_type, count in available_counts if count > 0
        )
        if not content_types:
            cleanup_media_source_inspection(inspection)
            if any(count for _content_type, count in inspection.counts):
                message = "All detected media types from this source are already imported."
            else:
                message = "No supported image, video, audio, or text files were found."
                show_error(self._widget, "Import Media Source", message)
            self._show_status(message)
            return
        content_type = content_types[0]
        if len(content_types) > 1:
            content_type = self._choose_detected_media_type(available_counts)
            if content_type is None:
                cleanup_media_source_inspection(inspection)
                self._show_status("Media import cancelled")
                return
        try:
            source = self._session.media_library.prepare_source(
                path,
                content_type,
                recursive=recursive,
            )
            self._begin_source_scan(source, inspection=inspection)
        except Exception as exc:
            cleanup_media_source_inspection(inspection)
            self._failed(str(exc), False)

    def _begin_source_scan(self, source, *, inspection=None):
        from EmbeddingsArea.domain import MediaSourceSummary
        self._update_source(self._summaries.get(source.id) or MediaSourceSummary(source, 0, 0, 0, 0, 0))
        self._render_summaries(list(self._summaries.values()))
        self._select_source_row(source.id)
        self._pending_registrations.add(source.id)
        self._start_scan_worker(source.id, source.label, registration=source, inspection=inspection)

    def _start_scan_worker(self, source_id, label, *, registration=None, inspection=None):
        if self._jobs.busy(source_id):
            return
        project = self._project
        def finished(result):
            self._on_operation_finished(result)
        def failed(message, cancelled):
            self._failed(message, cancelled)
        self._jobs.submit(source_id, "Discovering files",
            lambda progress, cancel: media_service.scan_source(
                project, source_id, progress, cancel, registration, inspection,
            ),
            finished, failed,
            cleanup=inspection.cleanup if inspection is not None else None)

    def _on_rescan_selected_source(self):
        source_id = self._selected_source_id()
        if source_id and not self._in_use(source_id):
            self._start_scan_worker(source_id, self._summaries[source_id].source.label)

    def _on_link_selected_source(self):
        self._on_link_source(self._selected_source_id())

    def _on_link_source(self, source_id, *, relink=False):
        if not source_id or not self._session or self._jobs.busy() or self._in_use(source_id):
            self._show_status("Select an available source before linking metadata.")
            return
        self._select_source_row(source_id)
        self._prompt_link_dialog(source_id, relink=relink)

    def _prompt_link_dialog(self, source_id, *, relink=False):
        project = self._project
        links = self._session.media_links
        if not links.has_metadata_rows():
            if self._confirm_metadata_creation() and self._project is project and not self._in_use(source_id):
                self._jobs.submit(source_id, "Creating metadata",
                    lambda progress, cancel: media_service.create_metadata(project, source_id, progress, cancel),
                    self._on_operation_finished, self._failed)
            return
        saved_request = links.saved_request(source_id)
        if saved_request is None and self._session.link_repository.list_match_rules(source_id):
            # Unsupported legacy rules require an explicit choice, not new defaults.
            saved_request = MediaLinkRequest(source_id, "", "")
        controller = MediaLinkDialogController(project, self._summaries[source_id].source,
            links.list_metadata_columns(), saved_request, self._jobs, relink=relink)
        self._link_dialog = controller
        try:
            plan = controller.exec()
        finally:
            self._link_dialog = None
        if plan is not None and self._project is project:
            if self._in_use(source_id):
                self._show_status("This source is used by a queued or running process. Refresh its linking preview when it finishes.")
                return
            self._jobs.submit(source_id, "Linking",
                lambda progress, cancel: media_service.apply_links(project, plan, progress, cancel),
                self._on_operation_finished, self._failed)

    def _confirm_metadata_creation(self):
        dialog = load_ui("ProjectArea", "ui/media_create_rows_dialog.ui", None)
        self._creation_dialog = dialog
        dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        buttons = require_child(dialog, QtWidgets.QDialogButtonBox, "button_box")
        create = buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        create.setText("Create Rows and Link")
        create.setDefault(False)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setDefault(True)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        try:
            return dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted
        finally:
            buttons.accepted.disconnect(dialog.accept)
            buttons.rejected.disconnect(dialog.reject)
            self._creation_dialog = None

    def _notify_media_changed(self):
        if self._media_changed_callback:
            self._media_changed_callback()

    def shutdown(self):
        self._jobs.shutdown()

    def _configure_table_columns(self) -> None:
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self._table.setColumnWidth(0, 200)

    def _choose_media_file_path(self) -> Path | None:
        file_path, _filter = QtWidgets.QFileDialog.getOpenFileName(
            self._widget,
            "Import Media File",
            "",
            "Media files (*.jpg *.jpeg *.png *.bmp *.gif *.tif *.tiff *.webp *.mp4 *.m4v *.mov *.mkv *.webm *.avi *.mpg *.mpeg *.ts *.mts *.m2ts *.wav *.flac *.mp3 *.m4a *.aac *.ogg *.opus *.aiff *.txt *.md *.json *.jsonl *.ndjson *.csv *.tsv *.xml *.zip);;All files (*)",
        )
        return Path(file_path) if file_path else None

    def _choose_detected_media_type(self, counts) -> MediaContentType | None:
        dialog = load_ui("ProjectArea", "ui/media_type_dialog.ui", None)
        dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        prompt = require_child(dialog, QtWidgets.QLabel, "label_media_type_prompt")
        combo = require_child(dialog, QtWidgets.QComboBox, "combo_media_type")
        buttons = require_child(dialog, QtWidgets.QDialogButtonBox, "button_box_media_type")
        prompt.setText("This source contains several media types. Choose one type to import:")
        labels = {
            MediaContentType.IMAGE: "Images",
            MediaContentType.VIDEO: "Videos",
            MediaContentType.AUDIO: "Audio",
            MediaContentType.TEXT: "Text",
        }
        for content_type, count in counts:
            if count:
                combo.addItem(f"{labels[content_type]} ({count:,})", content_type.value)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        try:
            if dialog.exec() != int(QtWidgets.QDialog.DialogCode.Accepted):
                return None
            return MediaContentType(str(combo.currentData()))
        finally:
            buttons.accepted.disconnect(dialog.accept)
            buttons.rejected.disconnect(dialog.reject)

    def _choose_media_folder_path(self) -> Path | None:
        directory = QtWidgets.QFileDialog.getExistingDirectory(self._widget, "Import Media Folder")
        return Path(directory) if directory else None

    def _selected_source_id(self) -> str:
        item = self._table.item(self._table.currentRow(), 0)
        if item is None:
            return ""
        return str(item.data(QtCore.Qt.ItemDataRole.UserRole) or "").strip()

    def _on_remove_selected_source(self) -> None:
        if self._session is None:
            return
        source_id = self._selected_source_id()
        if not source_id:
            self._show_status("Select a media source before removing it.")
            return
        if self._jobs.busy() or self._in_use(source_id):
            self._show_status("This media source is used by a queued or running process.")
            return
        source = self._session.media_library.require_source(source_id)
        decision = QtWidgets.QMessageBox.question(
            self._widget,
            "Remove Media Source",
            f"Remove '{source.label}' from the media library?\n\n"
            "EIDORA-generated files and extracted copies used only by this source "
            "will also be removed. Original external files will be kept.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if decision != QtWidgets.QMessageBox.StandardButton.Yes:
            self._show_status("Media source removal cancelled")
            return
        try:
            self._session.service.remove_media_source(source_id)
        except (OSError, ValueError) as exc:
            show_error(self._widget, "Remove Media Source", str(exc))
            return
        self.refresh()
        self._notify_media_changed()
        self._show_status(f"Removed {source.label}")

    def _on_rename_selected_source(self) -> None:
        if self._session is None:
            return
        source_id = self._selected_source_id()
        if not source_id or self._jobs.busy() or self._in_use(source_id):
            self._show_status("Select an available media source before renaming it.")
            return
        source = self._session.media_library.require_source(source_id)
        label, accepted = QtWidgets.QInputDialog.getText(
            self._widget,
            "Rename Media Source",
            "Name:",
            QtWidgets.QLineEdit.EchoMode.Normal,
            source.label,
        )
        if not accepted:
            return
        try:
            renamed = self._session.service.rename_media_source(source_id, label)
        except Exception as exc:  # noqa: BLE001
            show_error(self._widget, "Rename Media Source Failed", str(exc))
            return
        self.refresh()
        self._select_source_row(source_id)
        self._notify_media_changed()
        self._show_status(f"Renamed media source to {renamed.label}")

    def _open_row_context_menu(self, position: QtCore.QPoint) -> None:
        item = self._table.itemAt(position)
        if item is None:
            return
        source_item = self._table.item(item.row(), 0)
        source_id = str(source_item.data(QtCore.Qt.ItemDataRole.UserRole) or "").strip()
        if not source_id:
            return
        self._table.setCurrentCell(item.row(), 0)
        can_create = not self._in_use(source_id) and self._can_create_thumbnails is not None and self._can_create_thumbnails(source_id)
        menu = QtWidgets.QMenu(self._widget)
        action_link = menu.addAction("Link…")
        action_link.setEnabled(not self._jobs.busy() and not self._in_use(source_id))
        action_relink = menu.addAction("Relink…")
        action_relink.setEnabled(
            action_link.isEnabled() and self._summaries[source_id].linked_count > 0
        )
        menu.addSeparator()
        action_thumbnails = menu.addAction("Make Thumbnails…")
        action_thumbnails.setEnabled(can_create)
        action_rescan = menu.addAction("Rescan")
        action_rescan.setEnabled(not self._in_use(source_id))
        action_rename = menu.addAction("Rename…")
        action_rename.setEnabled(not self._jobs.busy() and not self._in_use(source_id))
        menu.addSeparator()
        action_remove = menu.addAction("Remove…")
        if self._jobs.busy() or self._in_use(source_id):
            action_remove.setEnabled(False)
            action_remove.setToolTip("This source is used by a queued or running process.")
        selected = self._exec_row_context_menu(menu, self._table.viewport().mapToGlobal(position))
        if selected == action_link:
            self._on_link_source(source_id)
        elif selected == action_relink:
            self._on_link_source(source_id, relink=True)
        elif selected == action_thumbnails and self._create_thumbnails is not None:
            self._create_thumbnails(source_id)
        elif selected == action_rescan:
            self._on_rescan_selected_source()
        elif selected == action_rename:
            self._on_rename_selected_source()
        elif selected == action_remove:
            self._on_remove_selected_source()

    @staticmethod
    def _exec_row_context_menu(
        menu: QtWidgets.QMenu,
        global_position: QtCore.QPoint,
    ) -> QtGui.QAction | None:
        return menu.exec(global_position)

    def _select_source_row(self, source_id: str) -> None:
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item is None:
                continue
            if str(item.data(QtCore.Qt.ItemDataRole.UserRole) or "").strip() == source_id:
                self._table.setCurrentCell(row, 0)
                return

    def _render_no_project(self) -> None:
        self._render_empty("Open or create a project to import media.")

    def _render_empty(self, text: str) -> None:
        self._table.setRowCount(0)
        self._table.insertRow(0)
        item = QtWidgets.QTableWidgetItem(text)
        item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable & ~QtCore.Qt.ItemFlag.ItemIsEnabled)
        self._table.setItem(0, 0, item)
