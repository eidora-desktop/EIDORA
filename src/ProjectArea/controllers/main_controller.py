from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtWidgets

from DiscoveryArea.thumbnails.repository import ThumbnailVariantRepository
from EmbeddingsArea.session import EmbeddingsAreaSession
from ProjectArea.controllers.media_sources_panel_controller import MediaSourcesPanelController
from ProjectArea.controllers.thumbnails_panel_controller import ThumbnailsPanelController
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import add_widget_to_container, require_child

if TYPE_CHECKING:
    from project.domain import Project
    from shared.qt.activity import ActivityManager


class ProjectAreaController:
    """Own the active project's media and thumbnail panels."""

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        status_callback: Callable[[str], None] | None = None,
        activity_manager: ActivityManager | None = None,
    ) -> None:
        self._widget = load_ui("ProjectArea", "ui/project_area.ui", parent)
        self._project: Project | None = None
        self._status_callback = status_callback
        self._media_changed_callback: Callable[[], None] | None = None
        self._metadata_changed_callback: Callable[[], None] | None = None
        self._media_operation_callback = None
        self._media_busy_callback = None
        self._thumbnail_variants_changed_callback: Callable[[], None] | None = None
        self._switch_project_callback: Callable[[], None] | None = None
        self._rename_project_callback: Callable[[Project], None] | None = None
        self._export_project_callback: Callable[[Project], None] | None = None
        self._delete_project_callback: Callable[[Project], None] | None = None
        self._thumbnail_repository: ThumbnailVariantRepository | None = None
        self._external_source_in_use: Callable[[str], bool] | None = None

        self._label_name = require_child(self._widget, QtWidgets.QLabel, "label_project_name")
        self._label_path = require_child(self._widget, QtWidgets.QLabel, "label_project_path")
        self._switch_button = require_child(self._widget, QtWidgets.QAbstractButton, "button_switch_project")
        self._actions_button = require_child(self._widget, QtWidgets.QToolButton, "button_project_actions")

        self._media_sources_panel = MediaSourcesPanelController(
            parent=self._widget,
            status_callback=self._show_status,
            activity_manager=activity_manager,
        )
        self._thumbnails_panel = ThumbnailsPanelController(
            parent=self._widget,
            status_callback=self._show_status,
            activity_manager=activity_manager,
        )
        add_widget_to_container(
            require_child(self._widget, QtWidgets.QWidget, "panel_media_sources_host"),
            self._media_sources_panel.widget,
        )
        add_widget_to_container(
            require_child(self._widget, QtWidgets.QWidget, "panel_thumbnails_host"),
            self._thumbnails_panel.widget,
        )
        self._media_sources_panel.set_media_changed_callback(self._on_media_changed)
        self._media_sources_panel.set_metadata_changed_callback(self._on_metadata_changed)
        self._media_sources_panel.set_operation_finished_callback(self._on_media_operation)
        self._media_sources_panel.set_busy_callback(self._on_media_busy)
        self._thumbnails_panel.set_changed_callback(self._on_thumbnail_variants_changed)
        self._media_sources_panel.set_thumbnail_creation_callbacks(
            can_create=self._thumbnails_panel.can_process_source,
            create=self._thumbnails_panel.open_for_source,
        )
        self._media_sources_panel.set_source_in_use_callback(self._source_in_use)
        self._thumbnails_panel.set_media_source_busy_callback(self._media_sources_panel.source_busy)

        horizontal_splitter = require_child(
            self._widget,
            QtWidgets.QSplitter,
            "splitter_hz",
        )
        horizontal_splitter.setStretchFactor(0, 1)
        horizontal_splitter.setStretchFactor(1, 1)
        horizontal_splitter.setCollapsible(0, False)
        horizontal_splitter.setCollapsible(1, False)
        horizontal_splitter.setSizes([500, 500])
        self._switch_button.clicked.connect(self._request_project_switch)
        self._rebuild_actions_menu()

    @property
    def widget(self) -> QtWidgets.QWidget:
        return self._widget

    def set_project_actions(
        self,
        *,
        switch: Callable[[], None],
        rename: Callable[[Project], None],
        export: Callable[[Project], None],
        delete: Callable[[Project], None],
    ) -> None:
        self._switch_project_callback = switch
        self._rename_project_callback = rename
        self._export_project_callback = export
        self._delete_project_callback = delete
        self._rebuild_actions_menu()

    def set_media_changed_callback(self, callback: Callable[[], None] | None) -> None:
        self._media_changed_callback = callback

    def set_metadata_changed_callback(self, callback: Callable[[], None] | None) -> None:
        self._metadata_changed_callback = callback

    def set_media_operation_callback(self, callback) -> None:
        self._media_operation_callback = callback

    def set_media_busy_callback(self, callback) -> None:
        self._media_busy_callback = callback

    def _on_media_busy(self, busy):
        if self._media_busy_callback:
            self._media_busy_callback(busy)

    def _on_media_operation(self, result):
        self._thumbnails_panel.refresh()
        if self._media_operation_callback:
            self._media_operation_callback(result)
        elif result.metadata_changed and self._metadata_changed_callback:
            self._metadata_changed_callback()
        elif self._media_changed_callback:
            self._media_changed_callback()

    def set_thumbnail_variants_changed_callback(self, callback: Callable[[], None] | None) -> None:
        self._thumbnail_variants_changed_callback = callback

    def set_source_in_use_callback(
        self, callback: Callable[[str], bool] | None
    ) -> None:
        self._external_source_in_use = callback

    def _source_in_use(self, source_id: str) -> bool:
        if self._thumbnails_panel.source_in_use(source_id):
            return True
        return bool(
            self._external_source_in_use is not None
            and self._external_source_in_use(source_id)
        )

    def media_source_busy(self, source_id: str) -> bool:
        return self._media_sources_panel.source_busy(source_id)

    def set_project(self, project: Project | None) -> None:
        self._project = project
        session = EmbeddingsAreaSession.for_project(project) if project is not None else None
        self._thumbnail_repository = (
            ThumbnailVariantRepository(project.paths.root)
            if project is not None
            else None
        )
        self._media_sources_panel.set_session(session)
        self._thumbnails_panel.set_session(session)
        self._media_sources_panel.set_project(project)
        self._thumbnails_panel.set_project(project, repository=self._thumbnail_repository)
        self._label_name.setText(project.name if project is not None else "No project loaded")
        self._label_path.setText(str(project.root) if project is not None else "")
        self._label_path.setToolTip(str(project.root) if project is not None else "")
        self._actions_button.setEnabled(project is not None)

    def refresh(self) -> None:
        self._media_sources_panel.refresh()
        self._thumbnails_panel.refresh()

    def refresh_media_sources(self) -> None:
        self._media_sources_panel.refresh()

    def refresh_thumbnail_variants(self) -> None:
        self._thumbnails_panel.refresh()

    def open_thumbnail_creation_dialog(self) -> None:
        self._thumbnails_panel.open_create_dialog()

    def shutdown(self) -> None:
        self._media_sources_panel.shutdown()
        self._thumbnails_panel.shutdown()

    def _request_project_switch(self) -> None:
        if self._switch_project_callback is not None:
            QtCore.QTimer.singleShot(0, self._switch_project_callback)

    def _rebuild_actions_menu(self) -> None:
        old_menu = self._actions_button.menu()
        if old_menu is not None:
            old_menu.deleteLater()
        menu = QtWidgets.QMenu(self._actions_button)
        rename_action = menu.addAction("Rename Project…")
        export_action = menu.addAction("Export Bundle…")
        menu.addSeparator()
        delete_action = menu.addAction("Delete From Disk…")
        rename_action.triggered.connect(lambda: self._invoke_project_action(self._rename_project_callback))
        export_action.triggered.connect(lambda: self._invoke_project_action(self._export_project_callback))
        delete_action.triggered.connect(lambda: self._invoke_project_action(self._delete_project_callback))
        self._actions_button.setMenu(menu)

    def _invoke_project_action(self, callback: Callable[[Project], None] | None) -> None:
        if callback is not None and self._project is not None:
            callback(self._project)

    def _on_media_changed(self) -> None:
        self._thumbnails_panel.refresh()
        if self._media_changed_callback is not None:
            self._media_changed_callback()

    def _on_metadata_changed(self) -> None:
        self._thumbnails_panel.refresh()
        if self._metadata_changed_callback is not None:
            self._metadata_changed_callback()

    def _on_thumbnail_variants_changed(self) -> None:
        if self._thumbnail_variants_changed_callback is not None:
            self._thumbnail_variants_changed_callback()

    def _show_status(self, message: str) -> None:
        if self._status_callback is not None:
            self._status_callback(message)
