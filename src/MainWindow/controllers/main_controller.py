from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtWidgets

from shared.qt import file_dialogs

from MainWindow.controllers.help_panel_controller import HelpPanelController
from MainWindow.controllers.project_panel_controller import ProjectPanelController
from MainWindow.controllers.project_save_panel_controller import ProjectSavePanelController
from MainWindow.controllers.project_chooser_controller import ProjectChooserController
from MainWindow.controllers.workspace_tour_controller import WorkspaceTourController
from MainWindow.controllers.window_layout_controller import WindowLayoutController
from MainWindow.navigation import NavigationItem
from MainWindow.tour_repository import TourPreferences
from project.service import ProjectService
from shared.qt.activity import ActivityManager
from shared.qt.message_dialogs import show_error
from shared.qt.ui_loader import load_ui

if TYPE_CHECKING:
    from MetadataArea.controllers import MetadataAreaController
    from MediaArea.controllers import MediaAreaController
    from project.domain import Project


class MainWindowController:
    """Application shell and cross-workspace coordinator."""

    def __init__(
        self,
        project: Project | None = None,
        project_service: ProjectService | None = None,
        *,
        defer_secondary_areas: bool = False,
        prepare_workspaces: bool = True,
        tour_preferences: TourPreferences | None = None,
    ) -> None:
        self.window = load_ui("MainWindow", "ui/main_window.ui")
        self._window_layout = WindowLayoutController(self.window)
        self._base_window_title = self.window.windowTitle() or "EIDORA"
        self._project = project
        self._project_service = project_service or ProjectService()
        self._button_group = QtWidgets.QButtonGroup(self.window)
        self._button_group.setExclusive(True)
        self._button_group.setProperty("tabAllButtons", True)
        self._stack = self._ensure_area_stack()
        self._status_bar = self._find_required(QtWidgets.QStatusBar, "status_bar")
        self._activity = ActivityManager(self._status_bar)
        self._areas: list[object] = []
        self._project_saves = ProjectSavePanelController(self.window)
        self._pending_media_refresh = {}
        self._media_jobs_busy = False
        self._discovery_has_items = False
        self._figures_has_snapshots = False
        self._navigation_items: list[NavigationItem] = []
        self._defer_secondary_areas = defer_secondary_areas
        self._deferred_load_timer = QtCore.QTimer(self.window)
        self._deferred_load_timer.setSingleShot(True)
        self._deferred_load_timer.timeout.connect(self._load_next_deferred_area)
        self._chooser_open = False
        self._shutdown = False
        self._is_example_collection = False
        self._tour_preferences = tour_preferences or TourPreferences()
        self._welcome_tour_shown = self._tour_preferences.was_shown()
        self._welcome_tour_timer = QtCore.QTimer(self.window)
        self._welcome_tour_timer.setSingleShot(True)
        self._welcome_tour_timer.timeout.connect(self._start_welcome_tour)
        self._help_controller = HelpPanelController(self.window, open_settings=self.show_settings)
        self._tour_controller = WorkspaceTourController(
            self.window, self._help_controller.tour_action, self._navigation_items,
            self._show_area, self._show_status,
        )
        self._tour_controller.active_changed.connect(self._update_tour_navigation)
        self._tour_controller.started.connect(self._remember_tour_shown)
        self._project_panel = ProjectPanelController(
            self.window, switch=self.show_project_chooser, rename=self._rename_project,
            export=self._export_project, delete=self._delete_project,
            methods=self._show_project_methods,
        )
        if prepare_workspaces:
            for _ in self.initialize_workspaces():
                pass

    def initialize_workspaces(self) -> Iterator[None]:
        """Prepare the hidden shell, yielding between startup GUI operations."""
        try:
            yield from self._build_area_steps()
            yield from self._set_project_steps(self._project)
            self._show_area(0)
        except BaseException:
            self.shutdown()
            self.window.close()
            raise

    @property
    def area_names(self) -> tuple[str, ...]:
        return tuple(item.title for item in self._navigation_items)

    def show(self) -> None:
        self.window.show()
        self._welcome_tour_timer.start(0)
        if self._defer_secondary_areas and len(self._areas) < len(self._navigation_items):
            self._show_status("Loading remaining workspaces…")
            self._deferred_load_timer.start(50)

    def show_project_chooser(self) -> bool:
        """Close the shell while choosing, restoring the workspace on cancellation."""
        if self._chooser_open:
            return False
        self._tour_controller.close()
        self._chooser_open = True
        app = QtWidgets.QApplication.instance()
        quit_on_last_window_closed = app.quitOnLastWindowClosed()
        app.setQuitOnLastWindowClosed(False)
        try:
            if not self.window.close():
                return False
            chooser = self._create_project_chooser()
            try:
                accepted = chooser.exec() == QtWidgets.QDialog.DialogCode.Accepted
                project = chooser.selected_project
                status = chooser.selection_status
            finally:
                chooser.dispose()
            if not accepted or project is None:
                return False
            self._show_status(status)
            return True
        finally:
            self._chooser_open = False
            self._project_saves.cancel_leave()
            try:
                if self._project is not None:
                    self.show()
                else:
                    app.quit()
            finally:
                app.setQuitOnLastWindowClosed(quit_on_last_window_closed)

    def show_settings(self, *, tab: str = "general") -> None:
        from MainWindow.controllers.settings_dialog_controller import SettingsDialogController

        self._tour_controller.close()
        controller = SettingsDialogController(
            self._project_service,
            project_open=self._project is not None,
            current_project=self._project,
            storage_busy=lambda: bool(self._activity.processes.records),
            models_changed=self._refresh_model_locations,
            presets_changed=self._refresh_preset_menus,
            required_preset_paths=self._required_preset_paths,
            before_project_storage_change=self._close_for_project_storage_change,
        )
        controller.select_tab(tab)
        app = QtWidgets.QApplication.instance()
        quit_on_close = app.quitOnLastWindowClosed()
        app.setQuitOnLastWindowClosed(False)
        try:
            controller.exec()
            if controller.project_closed_for_storage_change and not self._chooser_open:
                self.show_project_chooser()
        finally:
            app.setQuitOnLastWindowClosed(quit_on_close)

    def _close_for_project_storage_change(self):
        """Detach all project consumers before storage moves; drain saves in the worker."""
        from FiguresArea.controllers import FiguresAreaController

        if not self.window.close():
            raise ValueError("Project storage changes cancelled; the project remains open.")
        if not self.set_project(None):
            self.show()
            raise ValueError("Project storage changes cancelled; the project remains open.")
        self._welcome_tour_timer.stop()
        self._deferred_load_timer.stop()
        waits = [area.storage_completion_waiter() for area in self._areas
                 if isinstance(area, FiguresAreaController)]
        def wait_for_saves():
            for wait in waits:
                wait()
        return wait_for_saves

    def _refresh_preset_menus(self, project) -> None:
        if self._project is None or project.root != self._project.root:
            return
        from DiscoveryArea.controllers import DiscoveryAreaController
        from FiguresArea.controllers import FiguresAreaController

        for area in self._areas:
            if isinstance(area, (DiscoveryAreaController, FiguresAreaController)):
                area.refresh_preset_menus()

    def _required_preset_paths(self, project):
        if self._project is None or project.root != self._project.root:
            return ()
        from FiguresArea.controllers import FiguresAreaController

        return tuple(path for area in self._areas if isinstance(area, FiguresAreaController)
                     for path in area.required_preset_paths())

    def _refresh_model_locations(self) -> None:
        from EmbeddingsArea.controllers import EmbeddingsAreaController

        for area in self._areas:
            if isinstance(area, EmbeddingsAreaController):
                area.refresh_model_locations()

    def _create_project_chooser(self) -> ProjectChooserController:
        """Build one chooser instance; kept separate for focused UI testing."""
        return ProjectChooserController(
            self._project_service,
            prepare_project=self._prepare_project_switch,
            open_settings=lambda tab: self.show_settings(tab=tab),
            storage_read_only=self._project is not None,
            remove_project_callback=lambda project: self._delete_project(
                project,
                confirm=False,
            ),
        )

    def set_project(self, project: Project | None) -> bool:
        if self._project is not None and not self._project_saves.confirm_leave():
            return False
        for _ in self._set_project_steps(project):
            pass
        return True

    def _set_project_steps(self, project: Project | None) -> Iterator[None]:
        self._project_saves.discard_if_confirmed()
        self._welcome_tour_timer.stop()
        self._tour_controller.close()
        self._is_example_collection = ProjectService.is_example_collection(project)
        message = f"Loading project {project.name}…" if project is not None else "Updating project…"
        with self._activity.busy(message):
            self._project = project
            self._project_panel.set_project(project)
            self._discovery_has_items = False
            self._figures_has_snapshots = False
            self._pending_media_refresh.clear()
            self.window.setWindowTitle(
                f"{self._base_window_title} - {project.name}" if project is not None else self._base_window_title
            )
            self._set_project_navigation_enabled(project is not None)
            for area in self._areas:
                area.set_project(project)
                self._window_layout.register_area(area.widget)
                yield
            self._refresh_discovery_availability()
            self._refresh_figures_availability()
        self._welcome_tour_timer.start(0)

    def _start_welcome_tour(self) -> None:
        if (
            self._shutdown or self._welcome_tour_shown or not self._is_example_collection
            or self._chooser_open or not self.window.isVisible() or self.window.isMinimized()
            or QtWidgets.QApplication.activeModalWidget() is not None
            or not self._help_controller.tour_action.isEnabled() or self._tour_controller.is_active
        ):
            return
        self._tour_controller.start()

    def _remember_tour_shown(self) -> None:
        # Count presentation, including cancellation, so onboarding never forces
        # a user to finish before it stops launching. Manual tours count too.
        self._welcome_tour_timer.stop()
        if self._welcome_tour_shown:
            return
        self._welcome_tour_shown = True
        try:
            self._tour_preferences.mark_shown()
        except OSError:
            from logging import getLogger

            getLogger(__name__).warning("Could not save workspace tour preference", exc_info=True)

    def _update_tour_navigation(self, active: bool) -> None:
        self._set_project_navigation_enabled(self._project is not None)
        current = self._stack.currentIndex()
        if not active and current >= 0 and not self._navigation_items[current].button.isEnabled():
            self._show_area(0)

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self._project_saves.discard_if_confirmed()
        self._project_saves.shutdown()
        self._window_layout.save()
        self._deferred_load_timer.stop()
        self._welcome_tour_timer.stop()
        self._tour_controller.shutdown()
        self._tour_controller.active_changed.disconnect(self._update_tour_navigation)
        self._tour_controller.started.disconnect(self._remember_tour_shown)
        self._help_controller.shutdown()
        self._project_panel.shutdown()
        for area in self._areas:
            area.shutdown()
        self._activity.shutdown()

    def _build_area_steps(self) -> Iterator[None]:
        if self._defer_secondary_areas:
            self._build_media_area_first()
            yield
            return

        area_specs = (
            ("Media", "button_media_area"),
            ("Metadata", "button_metadata_area"),
            ("Embeddings", "button_embeddings_area"),
            ("Discovery", "button_discovery_area"),
            ("Figures", "button_figures_area"),
        )
        for index, (title, button_name) in enumerate(area_specs):
            # Import separately from construction so paint/timer events can run
            # between both operations. All widgets still belong to the GUI thread.
            controller_type = self._deferred_controller_type(index)
            yield
            button = self._find_required(QtWidgets.QAbstractButton, button_name)
            controller = controller_type(
                parent=self.window,
                status_callback=self._show_status,
                activity_manager=self._activity,
            )
            self._areas.append(controller)
            self._project_saves.register_area(controller)
            self._stack.addWidget(controller.widget)
            self._button_group.addButton(button, index)
            self._navigation_items.append(NavigationItem(title, button, controller.widget))
            button.clicked.connect(lambda _checked=False, page=index: self._show_area(page))
            yield

        self._wire_area_dependencies(*self._areas)
        yield

    def _build_media_area_first(self) -> None:
        """Build only the visible startup workspace and lightweight placeholders."""

        from MediaArea.controllers import MediaAreaController

        area_specs = (
            ("Media", "button_media_area"),
            ("Metadata", "button_metadata_area"),
            ("Embeddings", "button_embeddings_area"),
            ("Discovery", "button_discovery_area"),
            ("Figures", "button_figures_area"),
        )
        media_area = MediaAreaController(
            parent=self.window,
            status_callback=self._show_status,
            activity_manager=self._activity,
        )

        for index, (title, button_name) in enumerate(area_specs):
            button = self._find_required(QtWidgets.QAbstractButton, button_name)
            page = media_area.widget if index == 0 else self._loading_page(title)
            self._stack.addWidget(page)
            self._button_group.addButton(button, index)
            self._navigation_items.append(NavigationItem(title, button, page))
            button.clicked.connect(lambda _checked=False, page_index=index: self._show_area(page_index))

        self._areas.append(media_area)
        media_area.set_media_changed_callback(self._refresh_loaded_secondary_areas)
        media_area.set_metadata_changed_callback(self._refresh_loaded_secondary_areas)
        media_area.set_thumbnail_variants_changed_callback(self._refresh_loaded_secondary_areas)
        media_area.set_media_busy_callback(self._set_media_busy)

    def _load_next_deferred_area(self) -> None:
        if self._shutdown or len(self._areas) >= len(self._navigation_items):
            return
        if self._media_jobs_busy:
            self._deferred_load_timer.start(100)
            return

        index = len(self._areas)
        controller_type = self._deferred_controller_type(index)
        controller = controller_type(
            parent=self.window,
            status_callback=self._show_status,
            activity_manager=self._activity,
        )
        placeholder = self._navigation_items[index].widget
        self._stack.removeWidget(placeholder)
        self._stack.insertWidget(index, controller.widget)
        self._navigation_items[index] = NavigationItem(
            self._navigation_items[index].title,
            self._navigation_items[index].button,
            controller.widget,
        )
        placeholder.deleteLater()
        self._areas.append(controller)
        self._project_saves.register_area(controller)
        controller.set_project(self._project)
        self._window_layout.register_area(controller.widget)
        metadata_callback = getattr(controller, "set_metadata_changed_callback", None)
        if callable(metadata_callback):
            metadata_callback(self._refresh_discovery_availability)
        snapshots_callback = getattr(controller, "set_snapshots_changed_callback", None)
        if callable(snapshots_callback):
            snapshots_callback(self._refresh_figures_availability)
        self._set_project_navigation_enabled(self._project is not None)

        if len(self._areas) < len(self._navigation_items):
            self._deferred_load_timer.start(10)
            return

        self._wire_area_dependencies(*self._areas)
        self._welcome_tour_timer.start(0)

    @staticmethod
    def _deferred_controller_type(index: int):
        if index == 0:
            from MediaArea.controllers import MediaAreaController

            return MediaAreaController
        if index == 1:
            from MetadataArea.controllers import MetadataAreaController

            return MetadataAreaController
        if index == 2:
            from EmbeddingsArea.controllers import EmbeddingsAreaController

            return EmbeddingsAreaController
        if index == 3:
            from DiscoveryArea.controllers import DiscoveryAreaController

            return DiscoveryAreaController
        if index == 4:
            from FiguresArea.controllers import FiguresAreaController

            return FiguresAreaController
        raise IndexError(f"No deferred workspace at index {index}")

    def _refresh_loaded_secondary_areas(self) -> None:
        self._refresh_discovery_availability()
        for area in self._areas[1:]:
            area.set_project(self._project)

    def _loading_page(self, title: str) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget(self.window)
        page.setObjectName(f"page_{title.lower()}_loading")
        page.setProperty("surfaceRole", "area")
        layout = QtWidgets.QVBoxLayout(page)
        label = QtWidgets.QLabel(f"Loading {title}…", page)
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        return page

    def _wire_area_dependencies(
        self,
        media_area,
        metadata_area,
        embeddings_area,
        discovery_area,
        figures_area,
    ) -> None:
        for area in (discovery_area, figures_area):
            area.set_open_settings_callback(lambda tab: self.show_settings(tab=tab))

        def refresh_after_project_media() -> None:
            metadata_area.refresh()
            embeddings_area.refresh_embedding_inputs()
            discovery_area.refresh_metadata()
            discovery_area.refresh_thumbnail_variants()

        def refresh_after_project_metadata() -> None:
            self._refresh_discovery_availability()
            metadata_area.refresh()
            embeddings_area.refresh_embedding_inputs()
            discovery_area.refresh_metadata()
            discovery_area.refresh_metadata_groups()
            discovery_area.refresh_thumbnail_variants()

        media_area.set_media_changed_callback(refresh_after_project_media)
        media_area.set_metadata_changed_callback(refresh_after_project_metadata)
        def refresh_after_media_operation(result):
            self._queue_media_refresh(2, "inputs", embeddings_area.refresh_embedding_inputs)
            if result.metadata_changed:
                self._refresh_discovery_availability()
                self._queue_media_refresh(1, "metadata", metadata_area.refresh)
            if result.metadata_changed or result.links_changed:
                self._queue_media_refresh(3, "metadata", discovery_area.refresh_metadata)

        media_area.set_media_operation_callback(refresh_after_media_operation)
        media_area.set_media_busy_callback(self._set_media_busy)
        media_area.set_thumbnail_variants_changed_callback(discovery_area.refresh_thumbnail_variants)
        media_area.set_source_in_use_callback(embeddings_area.source_in_use)
        embeddings_area.set_media_source_busy_callback(media_area.media_source_busy)

        def refresh_after_metadata() -> None:
            self._refresh_discovery_availability()
            media_area.refresh_media_sources()
            media_area.refresh_thumbnail_variants()
            embeddings_area.refresh_embedding_inputs()
            discovery_area.refresh_metadata()
            discovery_area.refresh_metadata_groups()
            discovery_area.refresh_thumbnail_variants()
            figures_area.refresh_snapshots()

        metadata_area.set_metadata_changed_callback(refresh_after_metadata)

        def refresh_group_dependents() -> None:
            discovery_area.refresh_metadata_groups()
            embeddings_area.refresh_clustering_groups()

        metadata_area.set_groups_changed_callback(refresh_group_dependents)

        def refresh_after_embedding_metadata() -> None:
            self._refresh_discovery_availability()
            metadata_area.refresh()
            media_area.refresh_media_sources()
            media_area.refresh_thumbnail_variants()
            discovery_area.refresh_metadata()
            discovery_area.refresh_thumbnail_variants()

        embeddings_area.set_metadata_changed_callback(refresh_after_embedding_metadata)

        def start_embedding_thumbnails(run_id: str) -> None:
            media_area.start_embedding_thumbnails(run_id)

        embeddings_area.set_thumbnails_requested_callback(start_embedding_thumbnails)

        def refresh_clustering_groups() -> None:
            metadata_area.refresh()
            discovery_area.refresh_metadata_groups()

        embeddings_area.set_groups_changed_callback(refresh_clustering_groups)

        def show_clustering_groups(group_set_id) -> None:
            self._show_area(1)
            metadata_area.show_group_set(group_set_id)

        embeddings_area.set_show_groups_callback(show_clustering_groups)

        def refresh_after_discovery_metadata() -> None:
            self._refresh_discovery_availability()
            metadata_area.refresh()
            media_area.refresh_media_sources()
            media_area.refresh_thumbnail_variants()
            embeddings_area.refresh_embedding_inputs()

        discovery_area.set_metadata_changed_callback(refresh_after_discovery_metadata)

        def refresh_after_discovery_groups() -> None:
            metadata_area.refresh()
            embeddings_area.refresh_clustering_groups()

        discovery_area.set_groups_changed_callback(refresh_after_discovery_groups)
        discovery_area.set_create_thumbnails_requested_callback(
            lambda: self._open_thumbnail_creation(media_area)
        )
        discovery_area.set_open_metadata_callback(
            lambda metadata_id: self._open_metadata_row(metadata_area, metadata_id)
        )
        def refresh_after_snapshots() -> None:
            self._refresh_figures_availability()
            figures_area.refresh_snapshots()

        discovery_area.set_snapshots_changed_callback(refresh_after_snapshots)

        def create_slicer_animation(snapshot_path, preset_path, payload, values) -> None:
            figures_area.create_slicer_animation(snapshot_path, preset_path, payload, values)
            self._refresh_figures_availability()
            self._show_area(4)

        discovery_area.set_create_slicer_animation_callback(create_slicer_animation)


    def _open_metadata_row(self, metadata_area: MetadataAreaController, metadata_id: int) -> None:
        self._show_area(1)
        if not metadata_area.select_metadata_row(metadata_id):
            self._show_status(f"Metadata item {int(metadata_id)} is no longer available")

    def _open_thumbnail_creation(self, media_area: MediaAreaController) -> None:
        self._show_area(0)
        QtCore.QTimer.singleShot(0, media_area.open_thumbnail_creation_dialog)

    def _show_area(self, index: int) -> bool:
        if not 0 <= index < len(self._navigation_items):
            return False
        if index > 0 and self._project is None and not self._tour_controller.is_active:
            self._show_area(0)
            self._show_status("Open or create a project first")
            return False
        if index >= len(self._areas):
            self._show_status(f"Loading {self._navigation_items[index].title}…")
            return False
        if index == 3 and not self._discovery_has_items and not self._tour_controller.is_active:
            self._show_status("Import metadata or run an analysis on media to enable Discovery")
            return False
        if index == 4 and not self._figures_has_snapshots and not self._tour_controller.is_active:
            self._show_status("Create a snapshot to enable Figures")
            return False
        self._stack.setCurrentIndex(index)
        if index == 4:
            self._match_figures_sidebar_width()
        self._flush_media_refresh(index)
        button = self._button_group.button(index)
        if button is not None:
            button.setChecked(True)
        activate = getattr(self._areas[index], "activate", None)
        if callable(activate):
            QtCore.QTimer.singleShot(0, activate)
        return True

    def _match_figures_sidebar_width(self) -> None:
        discovery = self._areas[3].widget.findChild(
            QtWidgets.QSplitter, "splitter_discovery_sidebar"
        )
        figures = self._areas[4].widget.findChild(
            QtWidgets.QSplitter, "splitter_figures_canvas"
        )
        if discovery is None or figures is None:
            return
        if self._window_layout.has_saved_splitter(figures):
            return
        sidebar = figures.widget(1)
        minimum_width = max(
            discovery.minimumSizeHint().width(),
            sidebar.minimumWidth(), sidebar.minimumSizeHint().width(),
        )
        width = max(discovery.width(), minimum_width)
        sidebar.setMinimumWidth(minimum_width)
        available = sum(figures.sizes())
        figures.setStretchFactor(0, 1)
        figures.setStretchFactor(1, 0)
        figures.setSizes([max(0, available - width), width])

    def _set_media_busy(self, busy):
        self._media_jobs_busy = busy
        if len(self._areas) > 1:
            self._areas[1].set_media_publication_active(busy)
        if not busy:
            self._refresh_discovery_availability()
            self._flush_media_refresh(self._stack.currentIndex())

    def _queue_media_refresh(self, index, key, callback):
        self._pending_media_refresh.setdefault(index, {})[key] = callback
        if index == self._stack.currentIndex():
            self._flush_media_refresh(index)

    def _flush_media_refresh(self, index):
        if self._media_jobs_busy:
            return
        for callback in self._pending_media_refresh.pop(index, {}).values():
            callback()

    def _set_project_navigation_enabled(self, enabled: bool) -> None:
        touring = self._tour_controller.is_active
        for index, item in enumerate(self._navigation_items):
            available = index == 0 or ((enabled or touring) and index < len(self._areas))
            if index == 3:
                available = available and (self._discovery_has_items or touring)
                item.button.setToolTip("" if self._discovery_has_items or touring else
                                       "Import metadata or run an analysis on media to enable Discovery")
            if index == 4:
                available = available and (self._figures_has_snapshots or touring)
                item.button.setToolTip("" if self._figures_has_snapshots or touring else
                                       "Create a snapshot to enable Figures")
            item.button.setEnabled(available)
        self._tour_controller.update_availability(not self._shutdown and len(self._areas) == len(self._navigation_items))

    def _refresh_figures_availability(self) -> None:
        from DiscoveryArea.presets_repository import PresetRepository

        self._figures_has_snapshots = bool(
            self._project and PresetRepository(self._project.paths.snapshot_presets).has_entries()
        )
        self._set_project_navigation_enabled(self._project is not None)
        if not self._figures_has_snapshots and self._stack.currentIndex() == 4 and not self._tour_controller.is_active:
            self._show_area(0)

    def _refresh_discovery_availability(self) -> None:
        if self._media_jobs_busy:
            return
        from MetadataArea.repository import MetadataRepository

        self._discovery_has_items = bool(
            self._project and MetadataRepository.has_items(self._project.paths.metadata_database)
        )
        self._set_project_navigation_enabled(self._project is not None)
        if not self._discovery_has_items and self._stack.currentIndex() == 3 and not self._tour_controller.is_active:
            self._show_area(0)

    def _show_project_methods(self, project: Project) -> None:
        from project.methods_service import ProjectMethodsService
        from shared.qt.methods_citations_dialog import MethodsCitationsDialogController

        service = ProjectMethodsService(project)
        try:
            sources = service.sources()
        except Exception as exc:  # Keep an unavailable saved store visible as a reporting error.
            self._show_status(f"Methods reports unavailable: {exc}")
            return
        MethodsCitationsDialogController().exec(sources=sources, load_report=service.load)

    def _export_project(self, project: Project) -> None:
        default_path = str(Path.home() / f"{project.name}.zip")
        bundle_path, _selected_filter = file_dialogs.get_save_file_name(
            self.window,
            "Export Project Bundle",
            default_path,
            "Project Bundles (*.zip);;All Files (*)",
        )
        if not bundle_path:
            self._show_status("Export project bundle cancelled")
            return
        activity = self._activity.progress("Preparing project bundle…", value=0, total=100)
        try:
            activity.update(message="Writing project bundle…", value=25)
            destination = self._project_service.export_project_bundle(project, bundle_path)
            activity.update(message="Project bundle export complete", value=100)
        except Exception as exc:
            activity.retain_failed(f"Project bundle export failed: {exc}")
            show_error(self.window, "Export Project Bundle Failed", str(exc))
            return
        finally:
            activity.finish()
        self._show_status(f"Exported project bundle to {destination}")

    def _delete_project(self, project: Project, *, confirm: bool = True) -> bool:
        if confirm and not self._confirm_delete_project(project):
            self._show_status("Delete project cancelled")
            return False
        is_active = self._project is not None and self._same_project_root(self._project.root, project.root)
        if is_active:
            if not self.set_project(None):
                return False
        try:
            deleted_project = self._project_service.delete_project(project)
        except Exception as exc:
            if is_active and project.root.exists():
                self.set_project(project)
            show_error(self.window, "Delete Project Failed", str(exc))
            self._show_status("Delete project failed")
            return False
        self._show_status(f"Deleted project {deleted_project.name}")
        if is_active and not self._chooser_open:
            QtCore.QTimer.singleShot(0, self._require_project_after_delete)
        return True

    def _require_project_after_delete(self) -> None:
        if self._project is None and not self.show_project_chooser():
            self.window.close()

    def _rename_project(self, project: Project) -> None:
        project_name, accepted = QtWidgets.QInputDialog.getText(
            self.window, "Rename Project", "Project name:", text=project.name
        )
        if not accepted:
            self._show_status("Rename project cancelled")
            return
        if self._project is not None and not self._project_saves.confirm_leave():
            return
        try:
            renamed_project = self._project_service.rename_project(project, project_name)
        except Exception as exc:
            self._project_saves.cancel_leave()
            show_error(self.window, "Rename Project Failed", str(exc))
            self._show_status("Rename project failed")
            return
        if self._project is not None and self._same_project_root(self._project.root, renamed_project.root):
            self.set_project(renamed_project)
        self._show_status(f"Renamed project {project.name} to {renamed_project.name}")

    def _prepare_project_switch(self, project: Project) -> Iterator[None]:
        """Keep the chooser's loader visible through workspace binding and show."""
        if not self._project_saves.confirm_leave():
            raise ValueError("Project switch cancelled; unsaved changes remain in the open project.")
        previous_project = self._project
        previous_area = self._stack.currentIndex()
        try:
            yield from self._set_project_steps(project)
        except Exception:
            # The chooser permits retry/cancel after an error. Restore the
            # existing shell before returning control to that dialog.
            yield from self._set_project_steps(previous_project)
            self._show_area(previous_area)
            raise
        self._show_area(0)
        self.show()

    def _confirm_delete_project(self, project: Project) -> bool:
        message = (
            f'This will permanently delete project "{project.name}" and all data stored in its folder.\n\n'
            f"{project.root}\n\n"
            "This includes metadata databases, embeddings, discovery, Preview Assets, and figures."
        )
        answer = QtWidgets.QMessageBox.warning(
            self.window,
            "Delete Project",
            message,
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        return answer == QtWidgets.QMessageBox.StandardButton.Yes

    @staticmethod
    def _same_project_root(left: Path, right: Path) -> bool:
        try:
            return left.resolve() == right.resolve()
        except OSError:
            return left == right

    def _show_status(self, message: str) -> None:
        self._status_bar.showMessage(message, 5000)

    def _ensure_area_stack(self) -> QtWidgets.QStackedWidget:
        existing = self.window.findChild(QtWidgets.QStackedWidget, "stack_main_areas")
        if existing is not None:
            return existing
        host = self._find_required(QtWidgets.QFrame, "panel_area_host")
        layout = host.layout()
        if layout is None:
            layout = QtWidgets.QVBoxLayout(host)
            layout.setContentsMargins(0, 0, 0, 0)
        stack = QtWidgets.QStackedWidget(host)
        stack.setObjectName("stack_main_areas")
        layout.addWidget(stack)
        return stack

    def _find_child(self, widget_type: type, *object_names: str):
        for object_name in object_names:
            widget = self.window.findChild(widget_type, object_name)
            if widget is not None:
                return widget
        return None

    def _find_required(self, widget_type: type, *object_names: str):
        widget = self._find_child(widget_type, *object_names)
        if widget is None:
            raise RuntimeError(f"main_window.ui is missing one of: {', '.join(object_names)}")
        return widget
