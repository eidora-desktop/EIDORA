"""Vector Data cards combining embedding work, outputs, and clustering."""
from __future__ import annotations

from pathlib import Path
from PySide6 import QtCore, QtWidgets

from EmbeddingsArea.controllers.clustering_dialog_controller import ClusteringDialogController
from EmbeddingsArea.controllers.vector_data_card_controller import VectorDataCardController
from EmbeddingsArea.domain import EmbeddingRunState
from shared.qt.item_views import configure_panel_item_view, minimum_panel_row_size
from shared.qt.panel_controller import UiPanelController
from shared.qt.widgets import require_child

_ACTIVE = {"queued", "running", "cancelling"}
_RECOVERABLE = {EmbeddingRunState.PAUSED, EmbeddingRunState.INTERRUPTED, EmbeddingRunState.FAILED}


class VectorDataPanelController(UiPanelController):
    package = "EmbeddingsArea"
    ui_path = "ui/vector_data_panel.ui"

    def __init__(self, *args, coordinator, **kwargs):
        self._session = None
        self._coordinator = coordinator
        self._groups_changed = self._show_groups = self._metadata_changed = None
        self._run_again_callback = None
        self._cards, self._items, self._dialogs = {}, {}, set()
        super().__init__(*args, **kwargs)

    def _wire(self):
        self._list = require_child(self._widget, QtWidgets.QListWidget, "list_vector_data_runs")
        self._import = require_child(self._widget, QtWidgets.QPushButton, "button_import_embeddings")
        configure_panel_item_view(self._list)
        self._import.clicked.connect(self._import_embeddings)
        self._coordinator.changed.connect(self.refresh)
        self._coordinator.progress.connect(self._progress)
        self._coordinator.output_ready.connect(self._output_ready)
        self._coordinator.groups_changed.connect(self._notify_groups_changed)
        self._import.setEnabled(False)
        self._render_empty("Open or create a project to view vector data.")

    def set_session(self, session): self._session = session
    def set_run_again_callback(self, callback): self._run_again_callback = callback
    def set_groups_changed_callback(self, callback): self._groups_changed = callback
    def set_show_groups_callback(self, callback): self._show_groups = callback
    def set_metadata_changed_callback(self, callback): self._metadata_changed = callback

    def set_project(self, project):
        super().set_project(project)
        self.refresh()

    def shutdown(self):
        for controller in tuple(self._dialogs):
            if hasattr(controller, "shutdown"): controller.shutdown()
        self._dialogs.clear()
        self._clear_cards()

    def refresh(self):
        enabled = self._project is not None and self._session is not None
        self._import.setEnabled(enabled)
        if not enabled:
            self._clear_cards(); self._render_empty("Open or create a project to view vector data."); return
        try:
            datasets = {value.run_id: value for value in self._session.datasets.list_datasets()}
            runs = [
                run for run in self._session.runs.list_runs()
                if run.state != EmbeddingRunState.DRAFT or self._coordinator.state("embedding", run.id)
            ]
            runs.sort(key=lambda run: self._sort_key(run, datasets.get(run.id)))
        except Exception as exc:  # noqa: BLE001
            self._clear_cards(); self._render_empty(f"Vector data could not be loaded: {exc}"); return
        if not runs:
            self._clear_cards(); self._render_empty("No vector data yet. Select a model on the left and click Run, or import an existing dataset."); return
        self._remove_empty()
        current = {run.id for run in runs}
        for identity in tuple(self._cards):
            if identity not in current: self._remove_card(identity)
        for position, run in enumerate(runs):
            card = self._cards.get(run.id) or self._create_card(run.id)
            self._place_card(run.id, position)
            dataset = datasets.get(run.id)
            records = list(self._session.clusters.list_cluster_results(run.id)) if dataset else []
            records.sort(key=lambda value: str(value.get("created_at", "")), reverse=True)
            card.update(run, dataset, records, self._model_label(run.selected_model_id), lambda key=run.id: self._info_rows(key))
            self._resize_card(run.id)

    def select_output(self, kind, identity):
        cluster_id = identity if kind == "clustering" else None
        if cluster_id and self._session:
            record = self._session.clusters.repository.get(identity)
            run_id = str(record.get("run_id")) if record else ""
        else: run_id = identity
        card = self._cards.get(run_id)
        if card is None: self.refresh(); card = self._cards.get(run_id)
        if card:
            if cluster_id: card.select_cluster(cluster_id)
            self._list.scrollToItem(self._items[run_id])

    def open_clustering(self, run_id=None, result_id=None, duplicate=False):
        if self._session is None: return
        if run_id is None:
            datasets = self._session.datasets.list_datasets()
            if not datasets: self._show_status("Create or import vector data before running clustering."); return
            labels = [value.name for value in datasets]
            label, accepted = QtWidgets.QInputDialog.getItem(self._widget, "Add Clustering", "Vector data", labels, 0, False)
            if not accepted: return
            run_id = datasets[labels.index(label)].run_id
        created = result_id is None or duplicate
        try:
            if result_id is None: record = self._session.clusters.create_draft(run_id)
            else:
                source = self._session.clusters.repository.get(result_id)
                record = self._session.clusters.create_draft(run_id, source=source) if duplicate else source
        except Exception as exc:  # noqa: BLE001
            self._show_status(str(exc)); return
        controller = ClusteringDialogController(self._coordinator, record)
        try:
            accepted = controller.widget.exec() == QtWidgets.QDialog.DialogCode.Accepted
        finally:
            controller.dispose()
        if created and not accepted:
            try:
                self._session.clusters.delete_result(str(record["id"]))
            except (KeyError, ValueError):
                pass
        self.refresh()

    def refresh_clustering_groups(self): self.refresh()

    def _create_card(self, run_id):
        card = VectorDataCardController(self._list, run_id, coordinator=self._coordinator, on_action=self._card_action, on_cluster_action=self._cluster_action)
        item = QtWidgets.QListWidgetItem(); item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable)
        self._list.addItem(item); self._list.setItemWidget(item, card.widget)
        card.resized.connect(lambda key=run_id: self._resize_card(key))
        self._cards[run_id], self._items[run_id] = card, item
        return card

    def _place_card(self, run_id, position):
        item = self._items[run_id]; current = self._list.row(item)
        if current == position: return
        if current < 0: return
        # Removing and reinserting a QListWidgetItem also schedules its item
        # widget for deletion. Move the model row instead so the stable card
        # and every control owned by it remain alive while active runs reorder.
        destination = position if current > position else position + 1
        self._list.model().moveRows(
            QtCore.QModelIndex(), current, 1,
            QtCore.QModelIndex(), destination,
        )

    def _resize_card(self, run_id):
        card, item = self._cards.get(run_id), self._items.get(run_id)
        if card and item: item.setSizeHint(minimum_panel_row_size(card.widget.sizeHint()))

    def _card_action(self, run_id, action, payload):
        if action == "pause": self._coordinator.pause_embedding(run_id)
        elif action == "cancel": self._coordinator.cancel("embedding", run_id)
        elif action == "cluster": self.open_clustering(run_id)
        elif action == "menu": self._run_menu(run_id, payload)

    def _run_menu(self, run_id, anchor):
        run = self._session.runs.require_run(run_id)
        dataset = next((v for v in self._session.datasets.list_datasets() if v.run_id == run_id), None)
        live = self._coordinator.state("embedding", run_id); active = live is not None and live.state in _ACTIVE
        clustering_active = self._run_has_active_clustering(run_id)
        checkpoint = self._session.runs.inspect_run_checkpoint(run_id)
        menu = QtWidgets.QMenu(anchor)
        resume = menu.addAction("Resume") if run.state in _RECOVERABLE and checkpoint.get("resumable") else None
        retry = menu.addAction("Retry…") if dataset is None and not active and resume is None else None
        again = menu.addAction("Run Again…") if dataset and run.selected_model_id else None
        if dataset: menu.addSeparator()
        rename = menu.addAction("Rename…"); export = menu.addAction("Export Data…") if dataset else None
        cite = menu.addAction("Citations…") if dataset else None
        menu.addSeparator()
        if dataset:
            remove_label = "Remove…"
        elif run.state in {EmbeddingRunState.FAILED, EmbeddingRunState.CANCELLED, EmbeddingRunState.INTERRUPTED} and not checkpoint.get("resumable"):
            remove_label = "Dismiss"
        else:
            remove_label = "Discard"
        remove = menu.addAction(remove_label)
        rename.setEnabled(not active)
        remove.setEnabled(not active and not clustering_active)
        if clustering_active:
            remove.setToolTip("This vector data is used by an active clustering run.")
        selected = menu.exec(anchor.mapToGlobal(QtCore.QPoint(0, anchor.height())))
        if selected == resume: self._submit_embedding(run_id)
        elif selected in {retry, again} and self._run_again_callback: self._run_again_callback(run_id)
        elif selected == rename: self._rename_dataset(run_id)
        elif selected == cite and cite is not None: self._cite_run(run_id)
        elif selected == export: self._export(run_id)
        elif selected == remove: self._delete_dataset(run_id, published=dataset is not None)

    def _cluster_action(self, identity, action, anchor):
        if action != "menu" or self._session is None: return
        record = self._session.clusters.repository.get(identity)
        if not record: return
        live = self._coordinator.state("clustering", identity); active = live is not None and live.state in _ACTIVE
        state = str(record.get("state", "draft")); menu = QtWidgets.QMenu(anchor)
        cancel = menu.addAction("Cancel") if active else None
        show = menu.addAction("Show Groups") if state == "completed" else None
        retry = menu.addAction("Run Again…" if state == "completed" else "Retry…") if not active and state != "draft" else None
        configure = menu.addAction("Configure…") if not active and state == "draft" else None
        cite = menu.addAction("Citations…") if state == "completed" else None
        rename = menu.addAction("Rename…"); menu.addSeparator()
        remove_label = "Dismiss" if state in {"failed", "aborted", "interrupted"} else "Remove"
        if record.get("group_set_id"):
            remove_label = "Remove and Keep Groups"
        remove = menu.addAction(remove_label)
        remove_groups = menu.addAction("Remove and Delete Groups") if record.get("group_set_id") else None
        rename.setEnabled(not active); remove.setEnabled(not active)
        if remove_groups: remove_groups.setEnabled(not active)
        selected = menu.exec(anchor.mapToGlobal(QtCore.QPoint(0, anchor.height())))
        if selected == cancel: self._coordinator.cancel("clustering", identity)
        elif selected == show and self._show_groups: self._show_groups(record.get("group_set_id"))
        elif selected == retry: self.open_clustering(str(record["run_id"]), identity, True)
        elif selected == configure: self.open_clustering(str(record["run_id"]), identity)
        elif selected == cite and cite is not None: self._cite_run(str(record["run_id"]), record)
        elif selected == rename: self._rename_cluster(record)
        elif selected == remove: self._remove_cluster(record, False)
        elif selected == remove_groups: self._remove_cluster(record, True)

    def _cite_run(self, run_id, cluster=None):
        from EmbeddingsArea.provenance import embedding_report, clustering_report
        from shared.methods_report import combine_reports
        from shared.qt.methods_citations_dialog import MethodsCitationsDialogController
        try:
            run = self._session.runs.require_run(run_id)
            reports = [embedding_report(run.label, self._session.datasets.get_dataset_summary(run_id))]
            if cluster is not None:
                reports.append(clustering_report(cluster))
            report = combine_reports(str(cluster["label"]) if cluster else run.label, reports)
        except (OSError, ValueError) as exc:
            self._show_status(f"Methods report unavailable: {exc}")
            return
        MethodsCitationsDialogController().exec(report)

    def _submit_embedding(self, run_id):
        configuration = self._session.service.get_embedding_run_configuration(run_id)
        thumbnails = any(v.scope == "run" and v.parameter_id == "create_thumbnails" and v.value.lower() in {"1", "true", "yes"} for v in configuration.parameter_values)
        try: self._coordinator.submit_embedding(run_id, create_thumbnails=thumbnails)
        except Exception as exc: self._show_status(str(exc))

    def _info_rows(self, run_id):
        run = self._session.runs.require_run(run_id); config = self._session.service.get_embedding_run_configuration(run_id)
        dataset = next((v for v in self._session.datasets.list_datasets() if v.run_id == run_id), None)
        sources = {v.source.id: v.source.label for v in self._session.service.list_media_source_summaries()}
        model = self._session.model_catalog.get_model(run.selected_model_id) if run.selected_model_id else None
        rows = [("Input sources", [sources.get(v, v) for v in run.selected_source_ids] or ["Imported file"])]
        if model is not None:
            rows.append(("Model", model.label))
        settings = {}
        scope_labels = {
            "run": "Execution",
            "preprocess": "Preprocessing",
            "postprocess": "Postprocessing",
        }
        for value in config.parameter_values:
            if value.parameter_id == "create_thumbnails":
                continue
            scope = scope_labels.get(value.scope, value.scope.replace("_", " ").title())
            parameter = value.parameter_id.replace("_", " ").capitalize()
            settings.setdefault(scope, {})[parameter] = value.value
        if settings: rows.append(("Settings", settings))
        if dataset:
            rows.extend((("Vectors", dataset.item_count), ("Dimensions", dataset.dimensions)))
            summary = self._session.datasets.get_dataset_summary(run_id)
            if summary.get("backend"):
                rows.append(("Backend", summary["backend"]))
            provenance = summary.get("provenance")
            if provenance:
                rows.append(("Device provenance", provenance))
        rows.extend((("Created", run.created_at), ("Updated", run.updated_at)))
        if run.failed_items: rows.append(("Failed items", run.failed_items))
        if run.error_message: rows.append(("Diagnostics", run.error_message))
        return rows

    def _model_label(self, model_id):
        if not model_id: return "Imported"
        model = self._session.model_catalog.get_model(model_id)
        return model.label if model else model_id

    def _rename_dataset(self, run_id):
        run = self._session.runs.require_run(run_id)
        value, accepted = QtWidgets.QInputDialog.getText(self._widget, "Rename Vector Data", "Name", text=run.label)
        if accepted and value.strip():
            try: self._session.datasets.rename_dataset(run_id, value.strip())
            except Exception as exc: self._show_status(str(exc))
            self.refresh()

    def _delete_dataset(self, run_id, *, published):
        run = self._session.runs.require_run(run_id)
        live = self._coordinator.state("embedding", run_id)
        if live is not None and live.state in _ACTIVE:
            self._show_status("Cancel or pause this embedding run before removing it.")
            return
        if self._run_has_active_clustering(run_id):
            self._show_status("Cancel clustering before removing its vector data.")
            return
        if published and QtWidgets.QMessageBox.question(self._widget, "Remove Vector Data", f"Remove '{run.label}' and its vector artifact?", QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No, QtWidgets.QMessageBox.StandardButton.No) != QtWidgets.QMessageBox.StandardButton.Yes: return
        try: self._session.datasets.delete_dataset(run_id)
        except Exception as exc: self._show_status(str(exc)); return
        self.refresh()

    def _run_has_active_clustering(self, run_id):
        for state in self._coordinator.states():
            if state.kind != "clustering" or state.state not in _ACTIVE:
                continue
            record = self._session.clusters.repository.get(state.id)
            if record is not None and str(record.get("run_id")) == run_id:
                return True
        return False

    def _rename_cluster(self, record):
        value, accepted = QtWidgets.QInputDialog.getText(self._widget, "Rename Clustering", "Name", text=str(record["label"]))
        if not accepted or not value.strip():
            return
        try:
            self._session.clusters.rename_cluster_result(str(record["run_id"]), str(record["id"]), value.strip())
        except Exception as exc:
            self._show_status(str(exc))
            return
        if record.get("group_set_id"):
            self._notify_groups_changed()
        self.refresh()

    def _remove_cluster(self, record, delete_groups):
        try: deleted = self._session.clusters.delete_result(str(record["id"]), delete_groups=delete_groups)
        except Exception as exc: self._show_status(str(exc)); return
        if deleted and self._groups_changed: self._groups_changed()
        self.refresh()

    def _export(self, run_id):
        from EmbeddingsArea.controllers.embedding_export_dialog_controller import EmbeddingExportDialogController
        controller = EmbeddingExportDialogController(self._session.service, run_id, activity_manager=self._activity_manager, finished_callback=self._export_finished)
        self._dialogs.add(controller); controller.exec()
        if not controller.detached_export: self._dialogs.discard(controller)

    def _export_finished(self, controller, _result, error):
        self._dialogs.discard(controller)
        if error: self._show_status(f"Embedding export failed: {error}")

    def _import_embeddings(self):
        if self._session is None: return
        from EmbeddingsArea.controllers.embedding_import_dialog_controller import EmbeddingImportDialogController
        from EmbeddingsArea.exchange.importer import IMPORT_FILE_FILTER
        source, _selected = QtWidgets.QFileDialog.getOpenFileName(self._widget, "Import Embeddings", "", IMPORT_FILE_FILTER)
        if source and EmbeddingImportDialogController(self._session.service, Path(source)).exec():
            self.refresh()
            if self._metadata_changed: self._metadata_changed()

    @QtCore.Slot(str, str, int, str)
    def _progress(self, kind, identity, value, detail):
        if kind == "embedding" and identity in self._cards:
            self._cards[identity].update_progress(value, detail)
        elif kind == "clustering":
            record = self._session.clusters.repository.get(identity) if self._session else None
            if record and str(record["run_id"]) in self._cards:
                self._cards[str(record["run_id"])].update_cluster_progress(identity, value, detail)

    @QtCore.Slot(str, str)
    def _output_ready(self, kind, identity):
        self.refresh(); self.select_output(kind, identity)
        if kind == "embedding" and self._metadata_changed: self._metadata_changed()

    @QtCore.Slot()
    def _notify_groups_changed(self):
        if self._groups_changed:
            self._groups_changed()

    @staticmethod
    def _sort_key(run, dataset):
        group = 0 if run.state in {EmbeddingRunState.QUEUED, EmbeddingRunState.RUNNING} else 1 if run.state in _RECOVERABLE else 2
        timestamp = dataset.updated_at if dataset else run.updated_at
        return group, tuple(-ord(char) for char in str(timestamp))

    def _render_empty(self, message):
        self._remove_empty(); item = QtWidgets.QListWidgetItem(); item.setData(QtCore.Qt.ItemDataRole.UserRole, "empty")
        item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEnabled & ~QtCore.Qt.ItemFlag.ItemIsSelectable)
        label = QtWidgets.QLabel(message, self._list); label.setWordWrap(True); label.setContentsMargins(6, 12, 6, 12)
        item.setSizeHint(minimum_panel_row_size(label.sizeHint())); self._list.addItem(item); self._list.setItemWidget(item, label)

    def _remove_empty(self):
        for index in range(self._list.count() - 1, -1, -1):
            if self._list.item(index).data(QtCore.Qt.ItemDataRole.UserRole) == "empty": self._list.takeItem(index)

    def _remove_card(self, run_id):
        item, card = self._items.pop(run_id, None), self._cards.pop(run_id, None)
        if item is not None and self._list.row(item) >= 0: self._list.takeItem(self._list.row(item))
        if card is not None: card.widget.deleteLater()

    def _clear_cards(self):
        for identity in tuple(self._cards): self._remove_card(identity)
        self._remove_empty()
