from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6 import QtWidgets

from DiscoveryArea.thumbnails.repository import ThumbnailVariantRepository
from EmbeddingsArea.session import EmbeddingsAreaSession
from AssetsArea.controllers.media_sources_panel_controller import MediaSourcesPanelController
from AssetsArea.controllers.thumbnails_panel_controller import ThumbnailsPanelController
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import add_widget_to_container, require_child

if TYPE_CHECKING:
    from project.domain import Project
    from shared.qt.activity import ActivityManager


class AssetsAreaController:
    """Own the active project's media and thumbnail panels."""

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        status_callback: Callable[[str], None] | None = None,
        activity_manager: ActivityManager | None = None,
    ) -> None:
        self._widget = load_ui("AssetsArea", "ui/assets_area.ui", parent)
        self._project: Project | None = None
        self._status_callback = status_callback
        self._media_changed_callback: Callable[[], None] | None = None
        self._metadata_changed_callback: Callable[[], None] | None = None
        self._media_operation_callback = None
        self._media_busy_callback = None
        self._thumbnail_variants_changed_callback: Callable[[], None] | None = None
        self._thumbnail_repository: ThumbnailVariantRepository | None = None
        self._external_source_in_use: Callable[[str], bool] | None = None

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

    @property
    def widget(self) -> QtWidgets.QWidget:
        return self._widget

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

    def refresh(self) -> None:
        self._media_sources_panel.refresh()
        self._thumbnails_panel.refresh()

    def refresh_media_sources(self) -> None:
        self._media_sources_panel.refresh()

    def refresh_thumbnail_variants(self) -> None:
        self._thumbnails_panel.refresh()

    def start_embedding_thumbnails(self, run_id: str) -> bool:
        return self._thumbnails_panel.start_embedding_thumbnails(run_id)

    def open_thumbnail_creation_dialog(self) -> None:
        self._thumbnails_panel.open_create_dialog()

    def shutdown(self) -> None:
        self._media_sources_panel.shutdown()
        self._thumbnails_panel.shutdown()

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
