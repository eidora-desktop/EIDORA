from __future__ import annotations

import shutil
from collections.abc import Callable

from PySide6 import QtCore, QtWidgets

from EmbeddingsArea.controllers.model_install_coordinator import (
    ModelInstallCoordinator,
    PendingModelInstall,
)
from EmbeddingsArea.controllers.model_row_controller import ModelRowController
from EmbeddingsArea.domain import InstalledModel, ModelCatalogEntry
from EmbeddingsArea.model_catalog import (
    DEVELOPMENT_PLACEHOLDER_MODEL_IDS,
    ModelCatalog,
)
from EmbeddingsArea.session import EmbeddingsAreaSession
from shared.qt.item_views import configure_panel_item_view
from shared.qt.panel_controller import UiPanelController
from shared.qt.widgets import delete_removed_widgets, require_child


class _BaseModelsPanelController(UiPanelController):
    def __init__(
        self,
        *args,
        install_coordinator: ModelInstallCoordinator | None = None,
        **kwargs,
    ) -> None:
        self._session: EmbeddingsAreaSession | None = None
        self._runs_changed_callback: Callable[[str | None], None] | None = None
        self._run_model_callback: Callable[[str], None] | None = None
        self._models_changed_callback: Callable[[], None] | None = None
        self._readme_dialogs: list[QtWidgets.QDialog] = []
        self._owns_install_coordinator = install_coordinator is None
        self._install_coordinator = install_coordinator or ModelInstallCoordinator()
        super().__init__(*args, **kwargs)

    def set_session(self, session: EmbeddingsAreaSession | None) -> None:
        self._session = session
        self.refresh()

    def set_runs_changed_callback(self, callback: Callable[[str | None], None] | None) -> None:
        self._runs_changed_callback = callback

    def set_run_model_callback(self, callback: Callable[[str], None] | None) -> None:
        self._run_model_callback = callback

    def set_models_changed_callback(self, callback: Callable[[], None] | None) -> None:
        self._models_changed_callback = callback

    def set_project(self, project) -> None:
        super().set_project(project)
        if project is not None and self._session is None:
            self._session = EmbeddingsAreaSession.for_project(project)
        self.refresh()

    def _catalog(self):
        return self._session.catalog if self._session is not None else EmbeddingCatalogServiceFallback()

    def _notify_models_changed(self) -> None:
        if self._models_changed_callback is not None:
            self._models_changed_callback()

    def _show_readme_dialog(self, title: str, text: str, source: str = "") -> None:
        dialog = QtWidgets.QDialog(self._widget)
        dialog.setWindowTitle(f"Model Info - {title}")
        dialog.setModal(False)
        layout = QtWidgets.QVBoxLayout(dialog)
        if source:
            label = QtWidgets.QLabel(source, dialog)
            label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(label)
        browser = QtWidgets.QTextBrowser(dialog)
        browser.setOpenExternalLinks(True)
        try:
            browser.setMarkdown(text)
        except Exception:
            browser.setPlainText(text)
        layout.addWidget(browser, 1)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close, dialog)
        buttons.rejected.connect(dialog.close)
        layout.addWidget(buttons)
        dialog.resize(760, 560)
        self._readme_dialogs.append(dialog)
        dialog.destroyed.connect(lambda: self._forget_readme_dialog(dialog))
        dialog.show()

    def _forget_readme_dialog(self, dialog: QtWidgets.QDialog) -> None:
        if dialog in self._readme_dialogs:
            self._readme_dialogs.remove(dialog)

    @staticmethod
    def _configure_tree(tree: QtWidgets.QTreeWidget) -> None:
        configure_panel_item_view(tree)
        tree.setRootIsDecorated(False)
        tree.setUniformRowHeights(False)
        tree.setColumnCount(1)
        tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)

    @staticmethod
    def _installed_tooltip(installed: InstalledModel) -> str:
        descriptor = installed.descriptor
        lines = [
            descriptor.id,
            descriptor.label,
            f"Backend: {descriptor.backend or 'unknown'}",
            f"Modalities: {', '.join(content_type.value for content_type in descriptor.compatible_content_types)}",
        ]
        if descriptor.output_dimensions is not None:
            lines.append(f"Dimensions: {descriptor.output_dimensions}")
        if descriptor.shared_space_id:
            lines.append(f"Shared space: {descriptor.shared_space_id} ({descriptor.shared_space_revision})")
        if installed.health_label != "installed":
            lines.append(f"Status: {installed.health_label}")
        if installed.size_bytes is not None:
            lines.append(f"Size: {_human_size(installed.size_bytes)}")
        if descriptor.description:
            lines.append(descriptor.description)
        return "\n".join(lines)


class LocalModelsPanelController(_BaseModelsPanelController):
    package = "EmbeddingsArea"
    ui_path = "ui/local_models_panel.ui"

    def _wire(self) -> None:
        self._local_models = require_child(self._widget, QtWidgets.QTreeWidget, "tree_local_models")
        self._options_button = self._widget.findChild(QtWidgets.QAbstractButton, "button_models_options")
        self._browse_button = require_child(
            self._widget, QtWidgets.QAbstractButton, "button_browse_model_zoo"
        )
        self._browse_callback: Callable[[], None] | None = None
        self._browse_button.clicked.connect(self._browse_model_zoo)
        self._install_coordinator.changed.connect(self.update_pending_install)
        self._install_coordinator.progress_changed.connect(self.update_pending_install)
        self._rows = {}
        self._configure_tree(self._local_models)
        self.refresh()

    def set_browse_model_zoo_callback(self, callback: Callable[[], None] | None) -> None:
        self._browse_callback = callback

    def _browse_model_zoo(self) -> None:
        if self._browse_callback is not None:
            self._browse_callback()

    def refresh(self) -> None:
        old_items = [self._local_models.topLevelItem(i) for i in range(self._local_models.topLevelItemCount())]
        old_widgets = [row.widget for row in self._rows.values()]
        for row in self._rows.values():
            row.dispose()
        self._local_models.clear()
        self._rows.clear()
        delete_removed_widgets(self._local_models, old_widgets)
        del old_items
        for pending in sorted(
            self._install_coordinator.pending_installs(), key=lambda item: item.label.casefold()
        ):
            row = ModelRowController(self._local_models, pending.id, "installing")
            self._rows[pending.id] = row
            row.set_summary(pending.label, ", ".join(pending.modality_labels) or "—",
                            "Installing from the EIDORA Model Zoo.")
            row.set_installing(pending)
            row.cancel.clicked.connect(lambda _checked=False, key=pending.id: self._on_abort_pending_install(key))
        for installed in self._catalog().list_installed_models():
            if installed.descriptor.id in DEVELOPMENT_PLACEHOLDER_MODEL_IDS:
                continue
            row = ModelRowController(self._local_models, installed.descriptor.id, "installed")
            self._rows[installed.descriptor.id] = row
            modalities = ", ".join(value.value for value in installed.descriptor.compatible_content_types) or "—"
            size = _human_size(installed.size_bytes) if installed.size_bytes is not None else "Size unavailable"
            row.set_summary(installed.descriptor.label, f"{modalities} · {size}", self._installed_tooltip(installed))
            row.set_installing(None)
            executable = installed.descriptor.package_status == "ready"
            row.run.setEnabled(executable)
            if not executable:
                row.status.setText("Requires repackaging")
                row.run.setToolTip("This legacy model package must be repackaged with the EIDORA schema v3 contract.")
            row.run.clicked.connect(lambda _checked=False, model=installed: self._on_run_installed_model(model))
            row.options.clicked.connect(lambda _checked=False, model=installed, button=row.options:
                                        self._open_local_more_menu(model, button))

    def update_pending_install(self, install_id: str) -> None:
        pending = self._install_coordinator.pending(install_id)
        row = self._rows.get(install_id)
        if pending is None or row is None:
            self.refresh()
            return
        row.set_installing(pending)

    def _on_abort_pending_install(self, install_id: str) -> None:
        pending = self._install_coordinator.pending(install_id)
        if pending is None:
            return
        self._install_coordinator.cancel(install_id)
        self._show_status(f"Aborting install of {pending.label}...")
        self.refresh()

    def shutdown(self) -> None:
        if self._owns_install_coordinator:
            self._install_coordinator.shutdown()

    def _on_remove_installed_model(self, installed: InstalledModel) -> None:
        if not installed.is_removable:
            self._show_status("This installed model cannot be removed from the app yet.")
            return
        decision = QtWidgets.QMessageBox.question(
            self._widget,
            "Remove Model",
            f"Remove '{installed.descriptor.label}' from local storage?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if decision != QtWidgets.QMessageBox.StandardButton.Yes:
            self._show_status("Model removal cancelled")
            return
        try:
            self._catalog().remove_local_model(installed.descriptor.id)
        except Exception as exc:  # noqa: BLE001
            self._show_status(str(exc))
            return
        self.refresh()
        self._notify_models_changed()
        self._show_status(f"Removed {installed.descriptor.label}")

    def _on_run_installed_model(self, installed: InstalledModel) -> None:
        if self._session is None:
            self._show_status("Open or create a project before adding an embedding run.")
            return
        if self._run_model_callback is not None:
            self._run_model_callback(installed.descriptor.id)
            return
        try:
            label = self._session.service.next_default_embedding_run_label(installed.descriptor.id)
            run = self._session.service.create_draft_embedding_run(label)
            updated = self._session.service.update_embedding_run_configuration(run.id, model_id=installed.descriptor.id)
        except Exception as exc:  # noqa: BLE001
            self._show_status(str(exc))
            return
        if self._runs_changed_callback is not None:
            self._runs_changed_callback(updated.id)
        self._show_status(f"Added {updated.label} with {installed.descriptor.label}")

    def _open_local_more_menu(self, installed: InstalledModel, anchor: QtWidgets.QWidget) -> None:
        menu = QtWidgets.QMenu(anchor)
        action_info = menu.addAction("Show Info…")
        menu.addSeparator()
        action_deinstall = menu.addAction("Remove…")
        action_deinstall.setEnabled(installed.is_removable)
        selected = menu.exec(anchor.mapToGlobal(QtCore.QPoint(0, anchor.height())))
        if selected == action_deinstall:
            self._on_remove_installed_model(installed)
        elif selected == action_info:
            self._on_info_installed_model(installed)

    def _on_info_installed_model(self, installed: InstalledModel) -> None:
        try:
            text = self._catalog().read_local_model_readme(installed.descriptor.id)
        except Exception as exc:  # noqa: BLE001
            self._show_status(str(exc))
            return
        source = ""
        if installed.descriptor.directory is not None:
            source = str(installed.descriptor.directory / "README.md")
        self._show_readme_dialog(installed.descriptor.label, text, source)


class EmbeddingCatalogServiceFallback:
    def __init__(self) -> None:
        self._catalog = ModelCatalog()

    def list_local_models(self):
        return self._catalog.list_local_models()

    def list_installed_models(self):
        return self._catalog.list_installed_models()

    def list_discoverable_models(self):
        return self._catalog.list_discoverable_models()

    def list_discoverable_model_groups(self, *, force_refresh: bool = False):
        return self._catalog.list_discoverable_model_groups(force_refresh=force_refresh)

    def fetch_discoverable_model_size(self, entry: ModelCatalogEntry) -> int | None:
        return self._catalog.fetch_discoverable_model_size(entry)

    def remove_local_model(self, model_id: str) -> None:
        installed = {model.descriptor.id: model for model in self.list_installed_models()}
        target = installed.get(model_id)
        if target is None or not target.is_removable or target.descriptor.directory is None:
            raise ValueError("This installed model cannot be removed from the app yet.")
        shutil.rmtree(target.descriptor.directory, ignore_errors=True)

    def install_discoverable_model(self, entry: ModelCatalogEntry, **kwargs):
        return self._catalog.install_discoverable_model(entry, **kwargs)

    def read_local_model_readme(self, model_id: str) -> str:
        return self._catalog.read_local_model_readme(model_id)

    def fetch_discoverable_model_readme(self, entry: ModelCatalogEntry) -> str:
        return self._catalog.fetch_discoverable_model_readme(entry)


def _human_size(value: int) -> str:
    size = float(value)
    for suffix in ("B", "KB", "MB", "GB"):
        if size < 1000.0 or suffix == "GB":
            if suffix == "B":
                return f"{size:.0f} {suffix}"
            return f"{size:.0f} {suffix}" if size >= 10 else f"{size:.1f} {suffix}"
        size /= 1000.0
    return f"{value} B"
