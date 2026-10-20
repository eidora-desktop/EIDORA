from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtWidgets

from MainWindow.controllers.help_panel_controller import HelpPanelController
from MainWindow.controllers.project_chooser_controller import ProjectChooserController
from MainWindow.navigation import NavigationItem
from project.service import ProjectService
from shared.qt.activity import ActivityManager
from shared.qt.message_dialogs import show_error
from shared.qt.ui_loader import load_ui

if TYPE_CHECKING:
    from MetadataArea.controllers import MetadataAreaController
    from ProjectArea.controllers import ProjectAreaController
    from project.domain import Project


class MainWindowController:
    """Application shell and cross-workspace coordinator."""

    def __init__(
        self,
        project: Project | None = None,
        project_service: ProjectService | None = None,
        *,
        defer_secondary_areas: bool = False,
    ) -> None:
        self.window = load_ui("MainWindow", "ui/main_window.ui")
        self._base_window_title = self.window.windowTitle() or "EIDORA"
        self._project = project
        self._project_service = project_service or ProjectService()
        self._button_group = QtWidgets.QButtonGroup(self.window)
        self._button_group.setExclusive(True)
        self._stack = self._ensure_area_stack()
        self._status_bar = self._find_required(QtWidgets.QStatusBar, "status_bar")
        self._activity = ActivityManager(self._status_bar)
        self._areas: list[object] = []
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
        self._help_controller = HelpPanelController(self.window)
        self._build_areas()
        self.set_project(project)
        self._show_area(0)
        self._show_status("Ready")

    @property
    def area_names(self) -> tuple[str, ...]:
        return tuple(item.title for item in self._navigation_items)

    def show(self) -> None:
        self.window.show()
        if self._defer_secondary_areas and len(self._areas) < len(self._navigation_items):
            self._show_status("Loading remaining workspaces…")
            self._deferred_load_timer.start(50)

    def show_project_chooser(self) -> bool:
        """Show the short-lived chooser without changing state on cancellation."""
        if self._chooser_open:
            return False
        self._chooser_open = True
        chooser = self._create_project_chooser()
        try:
            accepted = chooser.exec() == QtWidgets.QDialog.DialogCode.Accepted
            project = chooser.selected_project
            status = chooser.selection_status
        finally:
            chooser.dispose()
            self._chooser_open = False
        if not accepted or project is None:
            return False
        self._activate_project(project)
        self._show_status(status)
        return True

    def _create_project_chooser(self) -> ProjectChooserController:
        """Build one chooser instance; kept separate for focused UI testing."""
        return ProjectChooserController(
            self._project_service,
            remove_project_callback=lambda project: self._delete_project(
                project,
                confirm=False,
            ),
        )

    def set_project(self, project: Project | None) -> None:
        message = f"Loading project {project.name}…" if project is not None else "Updating project…"
        with self._activity.busy(message):
            self._project = project
            self._discovery_has_items = False
            self._figures_has_snapshots = False
            self._pending_media_refresh.clear()
            self.window.setWindowTitle(
                f"{self._base_window_title} - {project.name}" if project is not None else self._base_window_title
            )
            self._set_project_navigation_enabled(project is not None)
            for area in self._areas:
                area.set_project(project)
            self._refresh_discovery_availability()
            self._refresh_figures_availability()

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self._deferred_load_timer.stop()
        self._help_controller.shutdown()
        for area in self._areas:
            area.shutdown()
        self._activity.shutdown()

    def _build_areas(self) -> None:
        if self._defer_secondary_areas:
            self._build_project_area_first()
            return

        # Keep the shell import light; area imports pull rendering and compute code.
        from DiscoveryArea.controllers import DiscoveryAreaController
        from EmbeddingsArea.controllers import EmbeddingsAreaController
        from FiguresArea.controllers import FiguresAreaController
        from MetadataArea.controllers import MetadataAreaController
        from ProjectArea.controllers import ProjectAreaController

        area_specs = [
            ("Project", "button_project_area", ProjectAreaController),
            ("Metadata", "button_metadata_area", MetadataAreaController),
            ("Embeddings", "button_embeddings_area", EmbeddingsAreaController),
            ("Discovery", "button_discovery_area", DiscoveryAreaController),
            ("Figures", "button_figures_area", FiguresAreaController),
        ]
        project_area: ProjectAreaController | None = None
        metadata_area: MetadataAreaController | None = None
        embeddings_area: EmbeddingsAreaController | None = None
        discovery_area: DiscoveryAreaController | None = None
        figures_area: FiguresAreaController | None = None

        for index, (title, button_name, controller_type) in enumerate(area_specs):
            button = self._find_required(QtWidgets.QAbstractButton, button_name)
            controller = controller_type(
                parent=self.window,
                status_callback=self._show_status,
                activity_manager=self._activity,
            )
            if isinstance(controller, ProjectAreaController):
                project_area = controller
            elif isinstance(controller, MetadataAreaController):
                metadata_area = controller
            elif isinstance(controller, EmbeddingsAreaController):
                embeddings_area = controller
            elif isinstance(controller, DiscoveryAreaController):
                discovery_area = controller
            elif isinstance(controller, FiguresAreaController):
                figures_area = controller
            self._areas.append(controller)
            self._stack.addWidget(controller.widget)
            self._button_group.addButton(button, index)
            self._navigation_items.append(NavigationItem(title, button, controller.widget))
            button.clicked.connect(lambda _checked=False, page=index: self._show_area(page))

        if not all((project_area, metadata_area, embeddings_area, discovery_area, figures_area)):
            raise RuntimeError("Main window did not construct every workspace")

        self._wire_area_dependencies(
            project_area,
            metadata_area,
            embeddings_area,
            discovery_area,
            figures_area,
        )

    def _build_project_area_first(self) -> None:
        """Build only the visible startup workspace and lightweight placeholders."""

        from ProjectArea.controllers import ProjectAreaController

        area_specs = (
            ("Project", "button_project_area"),
            ("Metadata", "button_metadata_area"),
            ("Embeddings", "button_embeddings_area"),
            ("Discovery", "button_discovery_area"),
            ("Figures", "button_figures_area"),
        )
        project_area = ProjectAreaController(
            parent=self.window,
            status_callback=self._show_status,
            activity_manager=self._activity,
        )

        for index, (title, button_name) in enumerate(area_specs):
            button = self._find_required(QtWidgets.QAbstractButton, button_name)
            page = project_area.widget if index == 0 else self._loading_page(title)
            self._stack.addWidget(page)
            self._button_group.addButton(button, index)
            self._navigation_items.append(NavigationItem(title, button, page))
            button.clicked.connect(lambda _checked=False, page_index=index: self._show_area(page_index))

        self._areas.append(project_area)
        project_area.set_project_actions(
            switch=self.show_project_chooser,
            rename=self._rename_project,
            export=self._export_project,
            delete=self._delete_project,
        )
        project_area.set_media_changed_callback(self._refresh_loaded_secondary_areas)
        project_area.set_metadata_changed_callback(self._refresh_loaded_secondary_areas)
        project_area.set_thumbnail_variants_changed_callback(self._refresh_loaded_secondary_areas)
        project_area.set_media_busy_callback(self._set_media_busy)

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
        controller.set_project(self._project)
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
        self._show_status("All workspaces ready")

    @staticmethod
    def _deferred_controller_type(index: int):
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
        project_area,
        metadata_area,
        embeddings_area,
        discovery_area,
        figures_area,
    ) -> None:
        project_area.set_project_actions(
            switch=self.show_project_chooser,
            rename=self._rename_project,
            export=self._export_project,
            delete=self._delete_project,
        )

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

        project_area.set_media_changed_callback(refresh_after_project_media)
        project_area.set_metadata_changed_callback(refresh_after_project_metadata)
        def refresh_after_media_operation(result):
            self._queue_media_refresh(2, "inputs", embeddings_area.refresh_embedding_inputs)
            if result.metadata_changed:
                self._refresh_discovery_availability()
                self._queue_media_refresh(1, "metadata", metadata_area.refresh)
                self._queue_media_refresh(3, "metadata", discovery_area.refresh_metadata)

        project_area.set_media_operation_callback(refresh_after_media_operation)
        project_area.set_media_busy_callback(self._set_media_busy)
        project_area.set_thumbnail_variants_changed_callback(discovery_area.refresh_thumbnail_variants)
        project_area.set_source_in_use_callback(embeddings_area.source_in_use)
        embeddings_area.set_media_source_busy_callback(project_area.media_source_busy)

        def refresh_after_metadata() -> None:
            self._refresh_discovery_availability()
            project_area.refresh_media_sources()
            project_area.refresh_thumbnail_variants()
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
            project_area.refresh_media_sources()
            project_area.refresh_thumbnail_variants()
            discovery_area.refresh_metadata()
            discovery_area.refresh_thumbnail_variants()

        embeddings_area.set_metadata_changed_callback(refresh_after_embedding_metadata)

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
            project_area.refresh_media_sources()
            project_area.refresh_thumbnail_variants()
            embeddings_area.refresh_embedding_inputs()

        discovery_area.set_metadata_changed_callback(refresh_after_discovery_metadata)

        def refresh_after_discovery_groups() -> None:
            metadata_area.refresh()
            embeddings_area.refresh_clustering_groups()

        discovery_area.set_groups_changed_callback(refresh_after_discovery_groups)
        discovery_area.set_create_thumbnails_requested_callback(
            lambda: self._open_thumbnail_creation(project_area)
        )
        discovery_area.set_open_metadata_callback(
            lambda metadata_id: self._open_metadata_row(metadata_area, metadata_id)
        )
        def refresh_after_snapshots() -> None:
            self._refresh_figures_availability()
            figures_area.refresh_snapshots()

        discovery_area.set_snapshots_changed_callback(refresh_after_snapshots)

    def _open_metadata_row(self, metadata_area: MetadataAreaController, metadata_id: int) -> None:
        self._show_area(1)
        if not metadata_area.select_metadata_row(metadata_id):
            self._show_status(f"Metadata item {int(metadata_id)} is no longer available")

    def _open_thumbnail_creation(self, project_area: ProjectAreaController) -> None:
        self._show_area(0)
        QtCore.QTimer.singleShot(0, project_area.open_thumbnail_creation_dialog)

    def _show_area(self, index: int) -> None:
        if not 0 <= index < len(self._navigation_items):
            return
        if index > 0 and self._project is None:
            self._show_area(0)
            self._show_status("Open or create a project first")
            return
        if index >= len(self._areas):
            self._show_status(f"Loading {self._navigation_items[index].title}…")
            return
        if index == 3 and not self._discovery_has_items:
            self._show_status("Import metadata or run an analysis on assets to enable Discovery")
            return
        if index == 4 and not self._figures_has_snapshots:
            self._show_status("Create a snapshot to enable Figures")
            return
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
        self._show_status(f"Showing {self._navigation_items[index].title}")

    def _match_figures_sidebar_width(self) -> None:
        discovery = self._areas[3].widget.findChild(
            QtWidgets.QSplitter, "splitter_discovery_sidebar"
        )
        figures = self._areas[4].widget.findChild(
            QtWidgets.QSplitter, "splitter_figures_canvas"
        )
        if discovery is None or figures is None:
            return
        width = max(discovery.width(), discovery.minimumSizeHint().width())
        figures.widget(1).setMinimumWidth(discovery.minimumSizeHint().width())
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
        for index, item in enumerate(self._navigation_items):
            available = index == 0 or (enabled and index < len(self._areas))
            if index == 3:
                available = available and self._discovery_has_items
                item.button.setToolTip("Discovery" if self._discovery_has_items else
                                       "Import metadata or run an analysis on assets to enable Discovery")
            if index == 4:
                available = available and self._figures_has_snapshots
                item.button.setToolTip("Figures" if self._figures_has_snapshots else
                                       "Create a snapshot to enable Figures")
            item.button.setEnabled(available)

    def _refresh_figures_availability(self) -> None:
        from DiscoveryArea.presets_repository import PresetRepository

        self._figures_has_snapshots = bool(
            self._project and PresetRepository(self._project.paths.snapshot_presets).has_entries()
        )
        self._set_project_navigation_enabled(self._project is not None)
        if not self._figures_has_snapshots and self._stack.currentIndex() == 4:
            self._show_area(0)

    def _refresh_discovery_availability(self) -> None:
        if self._media_jobs_busy:
            return
        from MetadataArea.repository import MetadataRepository

        self._discovery_has_items = bool(
            self._project and MetadataRepository.has_items(self._project.paths.metadata_database)
        )
        self._set_project_navigation_enabled(self._project is not None)
        if not self._discovery_has_items and self._stack.currentIndex() == 3:
            self._show_area(0)

    def _export_project(self, project: Project) -> None:
        default_path = str(Path.home() / f"{project.name}.zip")
        bundle_path, _selected_filter = QtWidgets.QFileDialog.getSaveFileName(
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
            self.set_project(None)
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
        try:
            renamed_project = self._project_service.rename_project(project, project_name)
        except Exception as exc:
            show_error(self.window, "Rename Project Failed", str(exc))
            self._show_status("Rename project failed")
            return
        if self._project is not None and self._same_project_root(self._project.root, renamed_project.root):
            self.set_project(renamed_project)
        self._show_status(f"Renamed project {project.name} to {renamed_project.name}")

    def _activate_project(self, project: Project) -> None:
        self.set_project(project)
        self._show_area(0)

    def _confirm_delete_project(self, project: Project) -> bool:
        message = (
            f'This will permanently delete project "{project.name}" and all data stored in its folder.\n\n'
            f"{project.root}\n\n"
            "This includes metadata databases, embeddings, discovery, thumbnails, and figures."
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
