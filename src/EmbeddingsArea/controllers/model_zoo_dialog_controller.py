from __future__ import annotations

import threading
from collections.abc import Callable

from PySide6 import QtCore, QtGui, QtWidgets

from EmbeddingsArea.controllers.model_install_coordinator import ModelInstallCoordinator
from EmbeddingsArea.domain import ModelCatalogEntry
from EmbeddingsArea.model_catalog import DiscoverableModelGroup, ModelCatalog
from EmbeddingsArea.service import EmbeddingCatalogService
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


_MODEL_ID_ROLE = int(QtCore.Qt.ItemDataRole.UserRole)
_MODEL_KIND_ROLE = _MODEL_ID_ROLE + 1


class _CatalogSignals(QtCore.QObject):
    groups_loaded = QtCore.Signal(int, object)
    card_loaded = QtCore.Signal(int, str, object, object)


class ModelZooDialogController:
    """Browse EIDORA Hugging Face collections and install their models."""

    def __init__(
        self,
        *,
        install_coordinator: ModelInstallCoordinator,
        status_callback: Callable[[str], None] | None = None,
        models_changed_callback: Callable[[], None] | None = None,
    ) -> None:
        widget = load_ui("EmbeddingsArea", "ui/model_zoo_dialog.ui")
        if not isinstance(widget, QtWidgets.QDialog):
            raise TypeError("model_zoo_dialog.ui must define a QDialog")
        self.dialog = widget
        self.dialog.setWindowModality(QtCore.Qt.WindowModality.NonModal)
        self._install_coordinator = install_coordinator
        self._status_callback = status_callback
        self._models_changed_callback = models_changed_callback
        self._session = None
        self._fallback_catalog = EmbeddingCatalogService(ModelCatalog())
        self._signals = _CatalogSignals()
        self._signals.groups_loaded.connect(self._apply_groups)
        self._signals.card_loaded.connect(self._apply_card)
        self._catalog_generation = 0
        self._card_generation = 0
        self._catalog_loading = False
        self._groups: list[DiscoverableModelGroup] = []
        self._entries: dict[str, ModelCatalogEntry] = {}
        self._card_cache: dict[str, str] = {}
        self._size_cache: dict[str, int] = {}
        self._selected_model_id = ""
        self._loading_card_key = ""
        self._force_card_reload = False
        self._shutdown = False
        self._initial_size_applied = False

        self.tree = require_child(self.dialog, QtWidgets.QTreeWidget, "tree_model_zoo")
        self.splitter = require_child(self.dialog, QtWidgets.QSplitter, "splitter_model_zoo")
        self.card = require_child(self.dialog, QtWidgets.QTextBrowser, "browser_model_card")
        self.size_label = require_child(self.dialog, QtWidgets.QLabel, "label_download_size")
        self.status_label = require_child(self.dialog, QtWidgets.QLabel, "label_model_zoo_status")
        self.install_button = require_child(self.dialog, QtWidgets.QPushButton, "button_install_model")
        self.close_button = require_child(self.dialog, QtWidgets.QPushButton, "button_close_model_zoo")

        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([260, 700])
        self.card.setOpenExternalLinks(True)
        self.tree.currentItemChanged.connect(self._selection_changed)
        self.install_button.clicked.connect(self._install_selected)
        self.close_button.clicked.connect(self.dialog.hide)
        self._install_coordinator.changed.connect(self._install_changed)
        self._install_coordinator.completed.connect(self._install_completed)

    @property
    def widget(self) -> QtWidgets.QDialog:
        return self.dialog

    def set_session(self, session) -> None:
        self._session = session
        self._groups = []
        self._entries = {}
        self._card_cache = {}
        self._size_cache = {}
        self._selected_model_id = ""
        self._loading_card_key = ""
        self._catalog_generation += 1
        self._card_generation += 1
        self._populate_status("Open the Model Zoo to load EIDORA collections.")

    def show(self) -> None:
        if self._shutdown:
            return
        if not self._groups and not self._catalog_loading:
            self.refresh()
        if not self._initial_size_applied:
            screen = self.dialog.screen() or QtGui.QGuiApplication.primaryScreen()
            available = screen.availableGeometry() if screen is not None else QtCore.QRect(0, 0, 960, 640)
            self.dialog.resize(min(960, available.width()), min(640, available.height()))
            self._initial_size_applied = True
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def refresh(self, *, force: bool = False) -> None:
        if self._shutdown or self._catalog_loading:
            return
        if force:
            self._force_card_reload = True
        self._catalog_generation += 1
        generation = self._catalog_generation
        catalog = self._catalog()
        self._set_catalog_loading(True)
        if not self._groups:
            self._populate_status("Loading EIDORA collections…")
        threading.Thread(
            target=self._load_groups,
            args=(generation, catalog, force),
            name="model-zoo-catalog",
            daemon=True,
        ).start()

    def shutdown(self) -> None:
        self._shutdown = True
        self._catalog_generation += 1
        self._card_generation += 1
        self.dialog.hide()

    def _catalog(self):
        return self._session.catalog if self._session is not None else self._fallback_catalog

    @staticmethod
    def _key(model_id: str) -> str:
        return str(model_id).strip().casefold()

    def _set_catalog_loading(self, loading: bool) -> None:
        self._catalog_loading = loading

    def _load_groups(self, generation: int, catalog: object, force: bool) -> None:
        try:
            try:
                groups = catalog.list_discoverable_model_groups(force_refresh=force)
            except TypeError:
                groups = catalog.list_discoverable_model_groups()
        except Exception as exc:  # noqa: BLE001
            self._signals.groups_loaded.emit(generation, exc)
            return
        self._signals.groups_loaded.emit(generation, groups)

    def _apply_groups(self, generation: int, result: object) -> None:
        if self._shutdown or generation != self._catalog_generation:
            return
        self._set_catalog_loading(False)
        if isinstance(result, Exception):
            self._force_card_reload = False
            self.status_label.setText(f"Could not refresh the Model Zoo: {result}")
            if not self._groups:
                self._populate_status("Could not load EIDORA collections. Close and reopen to try again.")
            return
        if not isinstance(result, list):
            return
        groups = [group for group in result if isinstance(group, DiscoverableModelGroup) and group.models]
        self._groups = groups
        previous = self._selected_model_id
        self._entries = {}
        self.tree.clear()
        first_model_item = None
        selected_item = None
        for group in groups:
            heading = QtWidgets.QTreeWidgetItem([group.label])
            heading.setData(0, _MODEL_KIND_ROLE, "collection")
            heading.setToolTip(0, group.source_url)
            heading.setFlags(heading.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable)
            font = heading.font(0)
            font.setBold(True)
            heading.setFont(0, font)
            self.tree.addTopLevelItem(heading)
            for entry in group.models:
                key = self._key(entry.id)
                self._entries[key] = entry
                item = QtWidgets.QTreeWidgetItem([self._model_label(entry)])
                item.setData(0, _MODEL_ID_ROLE, entry.id)
                item.setData(0, _MODEL_KIND_ROLE, "model")
                self._apply_model_item_state(item, entry)
                heading.addChild(item)
                first_model_item = first_model_item or item
                if key == self._key(previous):
                    selected_item = item
            heading.setExpanded(True)
        if not groups:
            self._populate_status("No EIDORA models are currently available.")
            return
        self.status_label.setText("Select a model to read its model card.")
        self.tree.setCurrentItem(selected_item or first_model_item)

    def _model_label(self, entry: ModelCatalogEntry) -> str:
        pending = self._install_coordinator.pending(entry.id)
        if pending is not None:
            return f"{entry.label} — {'cancelling' if pending.abort_requested else pending.state}"
        if entry.is_installed or self._installed_locally(entry.id):
            return f"{entry.label} (installed)"
        return entry.label

    def _apply_model_item_state(
        self,
        item: QtWidgets.QTreeWidgetItem,
        entry: ModelCatalogEntry,
    ) -> None:
        installed = entry.is_installed or self._installed_locally(entry.id)
        font = item.font(0)
        font.setItalic(installed)
        item.setFont(0, font)
        if installed:
            palette = self.tree.palette()
            item.setForeground(
                0,
                palette.brush(
                    QtGui.QPalette.ColorGroup.Disabled,
                    QtGui.QPalette.ColorRole.Text,
                ),
            )
        else:
            item.setForeground(0, QtGui.QBrush())

    def _selection_changed(self, current: QtWidgets.QTreeWidgetItem | None, _previous) -> None:
        if current is None or current.data(0, _MODEL_KIND_ROLE) != "model":
            return
        model_id = str(current.data(0, _MODEL_ID_ROLE) or "")
        entry = self._entries.get(self._key(model_id))
        if entry is None:
            return
        self._selected_model_id = entry.id
        self._show_entry(entry)

    def _show_entry(self, entry: ModelCatalogEntry) -> None:
        key = self._key(entry.id)
        self._refresh_install_controls(entry)
        cached = self._card_cache.get(key)
        if cached is not None and not self._force_card_reload:
            self.card.setMarkdown(cached)
            self.status_label.setText("")
            return
        if cached is None:
            self.card.setMarkdown(f"# {entry.label}\n\n{entry.description}\n\n_Loading model card…_")
        self.status_label.setText(f"Loading model card for {entry.label}…")
        self._force_card_reload = False
        if self._loading_card_key == key:
            return
        self._loading_card_key = key
        self._card_generation += 1
        generation = self._card_generation
        catalog = self._catalog()
        threading.Thread(
            target=self._load_card,
            args=(generation, entry, catalog),
            name="model-zoo-card",
            daemon=True,
        ).start()

    def _load_card(self, generation: int, entry: ModelCatalogEntry, catalog: object) -> None:
        try:
            local_model_id = self._local_model_id(entry.id, catalog)
            if local_model_id is not None:
                text = catalog.read_local_model_readme(local_model_id)
            else:
                text = catalog.fetch_discoverable_model_readme(entry)
        except Exception as exc:  # noqa: BLE001
            text = exc
        try:
            size = catalog.fetch_discoverable_model_size(entry)
        except Exception:  # noqa: BLE001
            size = None
        self._signals.card_loaded.emit(generation, entry.id, text, size)

    def _apply_card(self, generation: int, model_id: str, result: object, size: object) -> None:
        if self._shutdown:
            return
        key = self._key(model_id)
        if self._loading_card_key == key:
            self._loading_card_key = ""
        if generation != self._card_generation:
            return
        if isinstance(size, int):
            self._size_cache[key] = size
        entry = self._entries.get(key)
        if key != self._key(self._selected_model_id):
            return
        if entry is not None:
            self._refresh_install_controls(entry)
        if isinstance(result, Exception):
            cached = self._card_cache.get(key)
            if cached is not None:
                self.card.setMarkdown(cached)
                self.status_label.setText(f"Could not refresh the model card: {result}")
                return
            description = entry.description if entry is not None else ""
            self.card.setMarkdown(
                f"# {entry.label if entry else model_id}\n\n{description}\n\n_Model card unavailable._"
            )
            self.status_label.setText(str(result))
            return
        text = str(result)
        self._card_cache[key] = text
        self.card.setMarkdown(text)
        self.status_label.setText("")

    def _installed_locally(self, model_id: str, catalog: object | None = None) -> bool:
        return self._local_model_id(model_id, catalog) is not None

    def _local_model_id(self, model_id: str, catalog: object | None = None) -> str | None:
        source = catalog or self._catalog()
        try:
            model = source.get_model(model_id)
        except AttributeError:
            model = None
        if model is not None:
            return str(getattr(model, "id", model_id))
        try:
            installed_models = source.list_installed_models()
        except AttributeError:
            return None
        key = self._key(model_id)
        for installed in installed_models:
            descriptor = getattr(installed, "descriptor", None)
            installed_id = str(getattr(descriptor, "id", ""))
            if self._key(installed_id) == key:
                return installed_id
        return None

    def _refresh_install_controls(self, entry: ModelCatalogEntry) -> None:
        pending = self._install_coordinator.pending(entry.id)
        installed = entry.is_installed or self._installed_locally(entry.id)
        size = self._size_cache.get(self._key(entry.id), entry.size_bytes)
        self.size_label.setText(f"Download: {_human_size(size)}" if size is not None else "Download size unavailable")
        if installed:
            self.install_button.setText("Installed")
            self.install_button.setEnabled(False)
        elif pending is not None:
            self.install_button.setText("Installing…")
            self.install_button.setEnabled(False)
        else:
            self.install_button.setText("Install model")
            self.install_button.setEnabled(True)

    def _install_selected(self) -> None:
        entry = self._entries.get(self._key(self._selected_model_id))
        if entry is None:
            return
        if self._install_coordinator.pending(entry.id) is not None:
            return
        if self._install_coordinator.start(entry, self._catalog()):
            self._show_status(f"Installing {entry.label}…")

    def _install_changed(self, model_id: str) -> None:
        self._refresh_tree_labels(model_id)
        entry = self._entries.get(self._key(self._selected_model_id))
        if entry is not None:
            self._refresh_install_controls(entry)

    def _install_completed(self, entry_or_id: object, error: object) -> None:
        model_id = entry_or_id.id if isinstance(entry_or_id, ModelCatalogEntry) else str(entry_or_id)
        entry = self._entries.get(self._key(model_id))
        if error == "cancelled":
            message = f"Install cancelled for {entry.label if entry else model_id}"
        elif error:
            message = f"Could not install {entry.label if entry else model_id}: {error}"
        else:
            message = f"Installed {entry.label if entry else model_id}"
            if self._models_changed_callback is not None:
                self._models_changed_callback()
        self.status_label.setText(message)
        self._show_status(message)
        self._refresh_tree_labels(model_id)
        if entry is not None:
            self._refresh_install_controls(entry)

    def _refresh_tree_labels(self, model_id: str) -> None:
        key = self._key(model_id)
        entry = self._entries.get(key)
        if entry is None:
            return
        iterator = QtWidgets.QTreeWidgetItemIterator(self.tree)
        while iterator.value() is not None:
            item = iterator.value()
            if self._key(str(item.data(0, _MODEL_ID_ROLE) or "")) == key:
                item.setText(0, self._model_label(entry))
                self._apply_model_item_state(item, entry)
            iterator += 1

    def _populate_status(self, message: str) -> None:
        if not self._groups:
            self.tree.clear()
            item = QtWidgets.QTreeWidgetItem([message])
            item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable)
            self.tree.addTopLevelItem(item)
            self.card.clear()
            self.install_button.setEnabled(False)
        self.status_label.setText(message)

    def _show_status(self, message: str) -> None:
        if self._status_callback is not None:
            self._status_callback(message)


def _human_size(value: int) -> str:
    size = float(value)
    for suffix in ("B", "KB", "MB", "GB"):
        if size < 1000.0 or suffix == "GB":
            if suffix == "B":
                return f"{size:.0f} {suffix}"
            return f"{size:.0f} {suffix}" if size >= 10 else f"{size:.1f} {suffix}"
        size /= 1000.0
    return f"{value} B"
