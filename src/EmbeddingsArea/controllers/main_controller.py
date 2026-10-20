from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6 import QtCore, QtWidgets

from EmbeddingsArea.controllers.execution_coordinator import ExecutionCoordinator
from EmbeddingsArea.controllers.model_execution_dialog_controller import ModelExecutionDialogController
from EmbeddingsArea.controllers.model_install_coordinator import ModelInstallCoordinator
from EmbeddingsArea.controllers.model_zoo_dialog_controller import ModelZooDialogController
from EmbeddingsArea.controllers.models_panel_controller import LocalModelsPanelController
from EmbeddingsArea.controllers.vector_data_panel_controller import VectorDataPanelController
from EmbeddingsArea.session import EmbeddingsAreaSession
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import add_widget_to_container, require_child

if TYPE_CHECKING:
    from project.domain import Project
    from shared.qt.activity import ActivityManager
    from shared.qt.panel_controller import UiPanelController


class EmbeddingsAreaController:
    """UI binder for the embeddings area."""

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        status_callback: Callable[[str], None] | None = None,
        activity_manager: ActivityManager | None = None,
    ) -> None:
        self._widget = load_ui("EmbeddingsArea", "ui/embeddings_area.ui", parent)
        self._project: Project | None = None
        self._session: EmbeddingsAreaSession | None = None
        self._status_callback = status_callback
        self._activity_manager = activity_manager
        self._metadata_changed_callback: Callable[[], None] | None = None
        self._panels: list[UiPanelController] = []
        self._vector_data_panel: VectorDataPanelController | None = None
        self._local_models_panel: LocalModelsPanelController | None = None
        self._model_install_coordinator = ModelInstallCoordinator(activity_manager)
        self._execution_coordinator = ExecutionCoordinator(activity_manager)
        self._model_zoo_dialog: ModelZooDialogController | None = None
        self._load_panels()
        QtCore.QTimer.singleShot(0, self._configure_layout_splitters)

    @property
    def widget(self) -> QtWidgets.QWidget:
        return self._widget

    def set_project(self, project: Project | None) -> None:
        if self._activity_manager is not None:
            with self._activity_manager.busy("Refreshing embeddings…"):
                self._set_project(project)
            return
        self._set_project(project)

    def _set_project(self, project: Project | None) -> None:
        self._project = project
        self._session = EmbeddingsAreaSession.for_project(project) if project is not None else None
        self._execution_coordinator.activate(project, self._session)
        if self._model_zoo_dialog is not None:
            self._model_zoo_dialog.set_session(self._session)
        for panel in self._panels:
            set_session = getattr(panel, "set_session", None)
            if callable(set_session):
                set_session(self._session)
            panel.set_project(project)
        label = self._widget.findChild(QtWidgets.QLabel, "label_project_status")
        if label is not None:
            label.setText(project.name if project is not None else "No project loaded")

    def shutdown(self) -> None:
        if self._model_zoo_dialog is not None:
            self._model_zoo_dialog.shutdown()
        self._model_install_coordinator.shutdown()
        self._execution_coordinator.shutdown()
        for panel in self._panels:
            panel.shutdown()

    def set_metadata_changed_callback(self, callback: Callable[[], None] | None) -> None:
        self._metadata_changed_callback = callback

    def refresh_clustering_groups(self):
        if self._vector_data_panel is not None:
            self._vector_data_panel.refresh_clustering_groups()

    def set_groups_changed_callback(self, callback):
        if self._vector_data_panel is not None:
            self._vector_data_panel.set_groups_changed_callback(callback)

    def set_show_groups_callback(self, callback):
        if self._vector_data_panel is not None:
            self._vector_data_panel.set_show_groups_callback(callback)

    def refresh_embedding_inputs(self) -> None:
        self._refresh_views()

    def set_media_source_busy_callback(self, callback) -> None:
        self._execution_coordinator.media_source_busy = callback

    def source_in_use(self, source_id: str) -> bool:
        if self._session is None:
            return False
        for state in self._execution_coordinator.states():
            if state.kind != "embedding" or state.state not in {"queued", "running", "cancelling"}:
                continue
            try:
                run = self._session.runs.require_run(state.id)
            except ValueError:
                continue
            if source_id in run.selected_source_ids:
                return True
        return False

    def _load_panels(self) -> None:
        panel_specs = [
            ("panel_local_models", LocalModelsPanelController),
            ("panel_vector_data_host", VectorDataPanelController),
        ]
        for container_name, controller_type in panel_specs:
            container = require_child(self._widget, QtWidgets.QWidget, container_name)
            panel = controller_type(
                parent=self._widget,
                status_callback=self._show_status,
                activity_manager=self._activity_manager,
                **(
                    {"install_coordinator": self._model_install_coordinator}
                    if controller_type is LocalModelsPanelController
                    else {"coordinator": self._execution_coordinator}
                ),
            )
            if isinstance(panel, LocalModelsPanelController):
                self._local_models_panel = panel
                panel.set_run_model_callback(self._open_model_execution_for_model)
                panel.set_models_changed_callback(self._on_models_changed)
                panel.set_browse_model_zoo_callback(self._show_model_zoo)
            if isinstance(panel, VectorDataPanelController):
                self._vector_data_panel = panel
                panel.set_metadata_changed_callback(self._notify_metadata_changed)
                panel.set_run_again_callback(
                    lambda run_id: self._open_model_execution(source_run_id=run_id)
                )
            add_widget_to_container(container, panel.widget)
            self._panels.append(panel)

    def _show_status(self, message: str) -> None:
        if self._status_callback is not None:
            self._status_callback(message)

    def _refresh_views(self, _identity: str | None = None) -> None:
        if self._vector_data_panel is not None:
            activity = (
                self._activity_manager.busy("Refreshing embedding runs…")
                if self._activity_manager is not None
                else None
            )
            try:
                if self._vector_data_panel is not None:
                    self._vector_data_panel.refresh()
            finally:
                if activity is not None:
                    activity.finish()

    def _on_models_changed(self) -> None:
        activity = (
            self._activity_manager.busy("Refreshing models…")
            if self._activity_manager is not None
            else None
        )
        try:
            if self._local_models_panel is not None:
                self._local_models_panel.refresh()
        finally:
            if activity is not None:
                activity.finish()

    def _show_model_zoo(self) -> None:
        if self._model_zoo_dialog is None:
            self._model_zoo_dialog = ModelZooDialogController(
                install_coordinator=self._model_install_coordinator,
                status_callback=self._show_status,
                models_changed_callback=self._on_models_changed,
            )
            self._model_zoo_dialog.set_session(self._session)
        self._model_zoo_dialog.show()

    def _open_model_execution_for_model(self, model_id: str | None = None) -> None:
        self._open_model_execution(model_id=str(model_id or ""))

    def _open_model_execution(
        self,
        run_id: str | None = None,
        model_id: str = "",
        source_run_id: str | None = None,
    ) -> None:
        if self._session is None or self._project is None:
            self._show_status("Open or create a project before running an embedding model.")
            return
        if run_id is None and source_run_id is None and not model_id:
            self._show_status("Select an installed model on the left and click Run.")
            return
        discard_on_cancel = False
        if source_run_id is not None:
            source = self._session.service.get_embedding_run_configuration(source_run_id)
            label = self._session.service.next_default_embedding_run_label(source.run.selected_model_id)
            copied = self._session.service.create_draft_embedding_run(label)
            self._session.service.update_embedding_run_configuration(
                copied.id,
                model_id=source.run.selected_model_id,
                input_bindings={value.input_id: value.source_id for value in source.input_bindings},
                parameter_values={(value.scope, value.parameter_id): value.value for value in source.parameter_values},
            )
            run_id = copied.id
            discard_on_cancel = True
        controller = ModelExecutionDialogController(
            self._session,
            self._project,
            run_id=run_id,
            model_id=model_id,
            discard_on_cancel=discard_on_cancel,
        )
        submitted = controller.exec()
        if submitted is None:
            self._refresh_views()
            return
        configuration = self._session.service.get_embedding_run_configuration(submitted)
        create_thumbnails = next(
            (
                value.value.lower() in {"1", "true", "yes"}
                for value in configuration.parameter_values
                if value.scope == "run" and value.parameter_id == "create_thumbnails"
            ),
            False,
        )
        try:
            self._execution_coordinator.submit_embedding(
                submitted,
                create_thumbnails=create_thumbnails,
            )
        except Exception as exc:  # noqa: BLE001
            self._show_status(str(exc))
        self._refresh_views()

    def _notify_metadata_changed(self) -> None:
        if self._metadata_changed_callback is not None:
            self._metadata_changed_callback()

    def _configure_layout_splitters(self) -> None:
        splitter = self._widget.findChild(QtWidgets.QSplitter, "splitter_embeddings_main")
        if splitter is None or splitter.count() < 2:
            return
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        total = max(splitter.size().width(), splitter.width(), 2)
        half = max(1, total // 2)
        splitter.setSizes([half, half])
