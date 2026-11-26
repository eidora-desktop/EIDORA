"""Small binder for one embedding run card."""
from __future__ import annotations

from pathlib import Path
from collections.abc import Callable
from collections.abc import Mapping, Sequence

from PySide6 import QtCore, QtWidgets

from EmbeddingsArea.domain import EmbeddingRunState
from shared.qt.icons import apply_icon_color
from shared.qt.background_tasks import BackgroundTasks
from shared.qt.item_views import configure_panel_item_view
from shared.qt.ui_loader import load_ui
from shared.qt.panel_rows import PanelRowHeader, section_widget
from shared.qt.widgets import require_child


_ACTIVE = {"queued", "running", "cancelling"}


def _human_size(value: int) -> str:
    size = float(value)
    for suffix in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000.0 or suffix == "TB":
            return f"{size:.0f} {suffix}" if suffix == "B" or size >= 10 else f"{size:.1f} {suffix}"
        size /= 1000.0


class VectorDataCardController(QtCore.QObject):
    """Render one stable run ID without owning jobs or persistence."""

    resized = QtCore.Signal()

    def __init__(
        self,
        parent: QtWidgets.QWidget,
        run_id: str,
        *,
        coordinator,
        on_action: Callable[[str, str, object | None], None],
        on_cluster_action: Callable[[str, str, object | None], None],
    ) -> None:
        super().__init__(parent)
        self.run_id = run_id
        self.coordinator = coordinator
        self.on_action = on_action
        self.on_cluster_action = on_cluster_action
        self.widget = load_ui("EmbeddingsArea", "ui/embedding_run_row.ui", parent)
        # Removing a row deletes its widget; retire its controller with it too.
        self.setParent(self.widget)
        self._run = None
        self._dataset = None
        self._records: dict[str, dict] = {}
        self._pending_records: list[dict] = []
        self._cluster_progress: dict[str, tuple[QtWidgets.QProgressBar, QtWidgets.QTreeWidgetItem]] = {}
        self._info_loaded = False
        self._info_revision = 0
        self._info_requested_revision = None
        self._info_values = None
        self._info_tasks = None
        self._active = False
        self._presented_execution_state = ""
        self._model_label = "Imported"
        self._progress_follow_latest = True
        self._adjusting_progress_scroll = False
        self._report_state = None
        self._rendered_report_end = 0
        self._last_diagnostics = ""
        self._last_execution_note = ""
        self._last_report_summary = ""
        self._last_activity = ""
        self._pending_activity = ""
        self._activity_timer = QtCore.QTimer(self)
        self._activity_timer.setSingleShot(True)
        self._activity_timer.setInterval(1000)
        self._activity_timer.timeout.connect(self._flush_activity)
        self._summary_key = None
        self._saved_summary = {}
        self._presented_reduction = None
        self._sizing = False
        self._name_text = ""
        self._info_loader: Callable[[], list[tuple[str, object]]] | None = None

        self.toggle = require_child(self.widget, QtWidgets.QToolButton, "button_toggle_details")
        self.tabs = require_child(self.widget, QtWidgets.QTabWidget, "tabs_run_details")
        self.name = require_child(self.widget, QtWidgets.QLabel, "label_run_name")
        self.status = require_child(self.widget, QtWidgets.QLabel, "label_status")
        self.progress = require_child(self.widget, QtWidgets.QProgressBar, "progress_run")
        self.progress_control = section_widget(self.widget, "layout_progress")
        self.pause = require_child(self.widget, QtWidgets.QPushButton, "button_pause_run")
        self.cancel = require_child(self.widget, QtWidgets.QPushButton, "button_cancel_run")
        self.header = PanelRowHeader(self.widget)
        self.summary = require_child(self.widget, QtWidgets.QLabel, "label_row_summary")
        self._body = section_widget(self.widget, "layout_row_details")
        self.header.resized.connect(self._resize)
        self.options = require_child(self.widget, QtWidgets.QPushButton, "button_run_options")
        self.info = require_child(self.widget, QtWidgets.QTreeWidget, "tree_run_info")
        self.progress_report = require_child(self.widget, QtWidgets.QPlainTextEdit, "edit_execution_report")
        self.progress_tab = require_child(self.widget, QtWidgets.QWidget, "tab_progress")
        self.progress_tab.installEventFilter(self)
        self.progress_control.installEventFilter(self)
        self.info_tab = require_child(self.widget, QtWidgets.QWidget, "tab_run_info")
        self.clusters_tab = require_child(self.widget, QtWidgets.QWidget, "tab_run_clusters")
        self.cluster_tree = require_child(self.widget, QtWidgets.QTreeWidget, "tree_clustering_runs")
        self.add_clustering = require_child(self.widget, QtWidgets.QPushButton, "button_add_clustering")

        from EmbeddingsArea.controllers.reduction_panel_controller import ReductionPanelController
        self.reduction_tab = require_child(self.widget, QtWidgets.QWidget, "tab_reduction")
        self.reduction = ReductionPanelController(self.reduction_tab, run_id, coordinator)

        configure_panel_item_view(self.info)
        configure_panel_item_view(self.cluster_tree)
        self.info.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.info.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.cluster_tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.cluster_tree.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.toggle.setChecked(False)
        self.toggle.toggled.connect(self._toggle_details)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.pause.clicked.connect(self._pause_run)
        self.cancel.clicked.connect(self._cancel_run)
        self.options.clicked.connect(lambda: self.on_action(self.run_id, "menu", self.options))
        self.add_clustering.clicked.connect(lambda: self.on_action(self.run_id, "cluster", None))
        self.progress_report.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        progress_scroll = self.progress_report.verticalScrollBar()
        progress_scroll.valueChanged.connect(self._progress_scroll_changed)
        progress_scroll.rangeChanged.connect(self._progress_scroll_range_changed)
        self.tabs.setVisible(False)
        self._body.hide()
        self.name.installEventFilter(self)
        self.widget.installEventFilter(self)

    @property
    def dataset(self):
        return self._dataset

    def update(self, run, dataset, records, model_label: str, info_loader) -> None:
        self._execution_note = dataset.execution_note if dataset else ""
        self._run, self._dataset = run, dataset
        self._info_loader = info_loader
        self._info_revision += 1
        self._name_text = run.label
        self.name.setToolTip(run.label)
        self._elide_name()
        self._model_label = model_label or "Imported"
        dimensions = f" · {dataset.dimensions:,} dimensions" if dataset is not None else ""
        self.header.set_text(self.summary, self._model_label + dimensions)
        item_count = dataset.item_count if dataset is not None else max(run.processed_items, run.prepared_input_count)
        self._item_count_label = f"{item_count:,} vector{'s' if item_count != 1 else ''}"
        self._storage_tooltip = ""
        if dataset is not None and dataset.size_bytes is not None:
            self._item_count_label += f" | {_human_size(dataset.size_bytes)}"
            self._storage_tooltip = (
                f"Saved embedding file: {dataset.size_bytes:,} bytes. "
                "Includes vector data and artifact metadata; excludes source media, models, and caches."
            )
        self.add_clustering.setEnabled(dataset is not None and self.coordinator.active_reduction(self.run_id) is None)
        self.add_clustering.setToolTip(
            "Run a clustering algorithm on this vector data."
            if dataset is not None else "Clustering is available after vector data is published."
        )
        self.reduction.update(run, dataset, model_label)
        self._update_status()
        self._update_progress_report()
        self._pending_records = list(records)
        if not self.tabs.isHidden() and self.tabs.currentWidget() is self.clusters_tab:
            self._update_clusters(self._pending_records)
        if not self.tabs.isHidden() and self.tabs.currentWidget() is self.info_tab:
            self._load_info()
        self._resize()

    def update_progress(self, value: int, detail: str) -> None:
        if value < 0 or (value == 0 and detail.casefold().startswith("preparing")):
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(max(0, min(99, value)))
        self.progress.setToolTip(detail)
        self._show_active_progress()
        self._update_progress_report(progress=value, detail=detail)

    def _reduction_state(self):
        return self.coordinator.reduction_state(self.run_id)

    def _pause_run(self):
        state = self._reduction_state()
        if state is None or state.state not in _ACTIVE:
            live = self.coordinator.state("embedding", self.run_id)
            paused = self._run.state == EmbeddingRunState.PAUSED and (live is None or live.state not in _ACTIVE)
            self.on_action(self.run_id, "resume" if paused else "pause", None)

    def _cancel_run(self):
        state = self._reduction_state()
        if state is not None and state.state in _ACTIVE:
            self.coordinator.cancel("reduction", state.id)
        else:
            self.on_action(self.run_id, "cancel", None)

    def update_reduction_progress(self):
        self._update_progress_report()

    def _artifact_summary(self):
        if self._dataset is None or not self._dataset.artifact_path:
            return {}
        key = (self._dataset.artifact_path, self._dataset.updated_at)
        if key != self._summary_key:
            from EmbeddingsArea.runtime.readers import load_embedding_summary
            self._saved_summary = load_embedding_summary(Path(self._dataset.artifact_path))
            self._summary_key = key
        return self._saved_summary

    def select_cluster(self, identity: str) -> None:
        # Opening details can rebuild the tree synchronously. Resolve the item
        # only after those signals return, including the first lazy population.
        self.toggle.setChecked(True)
        self.tabs.setCurrentWidget(self.clusters_tab)
        for index in range(self.cluster_tree.topLevelItemCount()):
            item = self.cluster_tree.topLevelItem(index)
            if item.data(0, QtCore.Qt.ItemDataRole.UserRole) == identity:
                self.cluster_tree.setCurrentItem(item)
                self.cluster_tree.scrollToItem(item)
                return

    def update_cluster_progress(self, identity: str, value: int, detail: str) -> None:
        widgets = self._cluster_progress.get(identity)
        if widgets is None:
            return
        bar, item = widgets
        if value < 0:
            bar.setRange(0, 0)
        else:
            bar.setRange(0, 100)
            bar.setValue(max(0, min(99, value)))
        bar.setToolTip(detail)
        item.setToolTip(0, detail)

    def _toggle_details(self, checked: bool) -> None:
        self.toggle.setProperty("iconName", "down" if checked else "next")
        text = "Hide vector data details" if checked else "Show vector data details"
        self.toggle.setToolTip(text)
        self.toggle.setAccessibleName(text)
        apply_icon_color(self.toggle)
        self.tabs.setVisible(checked)
        self._body.setVisible(checked)
        if checked and self.tabs.currentWidget() is self.info_tab:
            self._load_info()
        elif checked and self.tabs.currentWidget() is self.clusters_tab:
            self._update_clusters(self._pending_records)
        elif checked and self.tabs.currentWidget() is self.progress_tab:
            self._update_progress_report()
        self._resize()

    def _tab_changed(self, index: int) -> None:
        current = self.tabs.widget(index)
        if current is self.info_tab and not self.tabs.isHidden():
            self._load_info()
        elif current is self.clusters_tab and not self.tabs.isHidden():
            self._update_clusters(self._pending_records)
        elif current is self.progress_tab:
            self._update_progress_report()
        self._resize()

    def _load_info(self) -> None:
        if self._info_requested_revision == self._info_revision:
            return
        if self._info_tasks is None:
            self._info_tasks = BackgroundTasks(self)
            self._info_tasks.finished.connect(self._info_ready)
            self._info_tasks.failed.connect(self._info_failed)
        self._info_requested_revision = self._info_revision
        if not self._info_loaded:
            self._render_info([("Status", "Loading details…")])
        loader = self._info_loader
        self._info_tasks.submit(self._info_revision, lambda _progress: loader() if loader is not None else [])

    def _info_ready(self, revision, values) -> None:
        if revision != self._info_revision:
            return
        self._info_loaded = True
        if values != self._info_values:
            self._render_info(values)
            self._info_values = values

    def _info_failed(self, revision, message) -> None:
        if revision != self._info_revision:
            return
        self._info_requested_revision = None  # Allow retry on the next visit.
        self._info_loaded = False
        self._info_values = None
        self._render_info([("Diagnostics", message)])

    def _render_info(self, values) -> None:
        old_count = self.info.topLevelItemCount()
        technical_expanded = any(self.info.topLevelItem(index).text(0) == "Technical details"
                                 and self.info.topLevelItem(index).isExpanded() for index in range(old_count))
        for label, value in values:
            item = self._add_info_value(self.info, str(label), value)
            if label == "Technical details":
                item.setExpanded(technical_expanded)
        if not values:
            self._add_empty_row(self.info, "No run details available.")
        # Publish replacement rows before retiring old ones so accessibility
        # never observes an empty model during a populated refresh.
        self.info.model().removeRows(0, old_count)

    def _add_info_value(self, parent, label: str, value) -> QtWidgets.QTreeWidgetItem:
        if isinstance(value, Mapping):
            item = QtWidgets.QTreeWidgetItem([label, f"{len(value)} properties"])
            parent.addChild(item) if isinstance(parent, QtWidgets.QTreeWidgetItem) else parent.addTopLevelItem(item)
            for child_label, child_value in value.items():
                self._add_info_value(item, str(child_label), child_value)
            item.setExpanded(True)
            return item
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            if len(value) == 1 and not isinstance(value[0], (Mapping, list, tuple)):
                value = value[0]
            else:
                item = QtWidgets.QTreeWidgetItem([label, f"{len(value)} items"])
                parent.addChild(item) if isinstance(parent, QtWidgets.QTreeWidgetItem) else parent.addTopLevelItem(item)
                for index, child_value in enumerate(value, start=1):
                    self._add_info_value(item, f"Item {index}", child_value)
                item.setExpanded(True)
                return item
        text = self._text(value)
        item = QtWidgets.QTreeWidgetItem([label, text])
        item.setToolTip(1, text)
        parent.addChild(item) if isinstance(parent, QtWidgets.QTreeWidgetItem) else parent.addTopLevelItem(item)
        return item

    def _update_status(self) -> None:
        self.pause.setText("Pause")
        reduction = self._reduction_state()
        if reduction is not None and reduction.state in _ACTIVE:
            self._set_active_header(True)
            self.pause.setEnabled(False)
            self.pause.setToolTip("PCA reduction cannot be paused. Use Cancel to stop it.")
            self.cancel.setEnabled(reduction.state != "cancelling")
            self.status.setText("Reducing" if reduction.state == "running" else reduction.state.capitalize())
            self.status.setToolTip(reduction.detail)
            self.add_clustering.setEnabled(False)
            if self._presented_reduction is not reduction:
                self._presented_reduction = reduction
                self.tabs.setCurrentWidget(self.progress_tab)
                self.toggle.setChecked(True)
            return
        live = self.coordinator.state("embedding", self.run_id)
        if live is not None and live.state in _ACTIVE:
            self._set_active_header(True)
            live_label = {
                "queued": "Queued",
                "running": "Running",
                "cancelling": "Cancelling",
            }.get(live.state, live.state.replace("_", " ").capitalize())
            self.status.setText(live_label)
            self.status.setToolTip(live.detail or live_label)
            self._show_active_progress()
            self.pause.setEnabled(live.state in {"queued", "running"})
            self.pause.setToolTip("")
            self.cancel.setEnabled(live.state != "cancelling")
            self.update_progress(live.progress, live.detail)
            return
        state = self._run.state
        paused = state == EmbeddingRunState.PAUSED
        self._set_active_header(paused)
        if paused:
            self.options.show()
            self.pause.setText("Resume")
            self.pause.setEnabled(bool(self._run.selected_model_id))
            self.pause.setToolTip("Resume this embedding run")
            self.cancel.setEnabled(True)
            self.progress.setRange(0, 100)
            self.progress.setValue(max(0, min(100, live.progress if live is not None else self._run.progress)))
        if state == EmbeddingRunState.COMPLETED:
            label = self._item_count_label
        else:
            label = state.value.replace("_", " ").capitalize()
        self.status.setText(label)
        tooltip = (
            self._run.error_message
            or getattr(self, "_execution_note", "")
            or ("Completed with warnings" if state == EmbeddingRunState.COMPLETED and self._run.failed_items else label)
        )
        if state == EmbeddingRunState.COMPLETED and self._storage_tooltip:
            tooltip += "\n" + self._storage_tooltip
        self.status.setToolTip(tooltip)
        presentation = state.value
        if presentation != self._presented_execution_state:
            self._presented_execution_state = presentation
            if reduction is not None:
                self.tabs.setCurrentWidget(self.progress_tab)
            elif state == EmbeddingRunState.COMPLETED:
                self.tabs.setCurrentWidget(self.info_tab)
            elif state in {
                EmbeddingRunState.FAILED,
                EmbeddingRunState.CANCELLED,
                EmbeddingRunState.INTERRUPTED,
                EmbeddingRunState.PAUSED,
            }:
                self.tabs.setCurrentWidget(self.progress_tab)

    def _set_active_header(self, active: bool) -> None:
        """Swap controls without changing the two-line header geometry."""
        self._active = active
        self.tabs.setTabVisible(self.tabs.indexOf(self.clusters_tab), not active)
        for widget in (self.options,):
            widget.setVisible(not active)
        self.toggle.setVisible(True)
        self.status.setVisible(True)
        self.progress_control.setVisible(active)
        self.summary.setVisible(not active)
        self.tabs.setVisible(self.toggle.isChecked())
        self._body.setVisible(self.toggle.isChecked())

    def _show_active_progress(self) -> None:
        if self._presented_execution_state != "active":
            self._presented_execution_state = "active"
            self.tabs.setCurrentWidget(self.progress_tab)

    def eventFilter(self, watched, event):
        if watched is self.name and event.type() in {QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.FontChange}:
            self._elide_name()
        if watched is self.widget and event.type() in {QtCore.QEvent.Type.Resize, QtCore.QEvent.Type.LayoutRequest}:
            self._resize()
        if watched in (self.progress_tab, self.progress_control) and event.type() == QtCore.QEvent.Type.Show:
            self._update_progress_report()
        return super().eventFilter(watched, event)

    def _flush_activity(self):
        self._update_progress_report(detail=self._pending_activity, flush_activity=True)

    def _update_progress_report(self, *, progress: int | None = None, detail: str | None = None, flush_activity=False) -> None:
        if self._run is None or not self.widget.isVisible():
            return
        reduction = self._reduction_state()
        reducing = reduction is not None and reduction.state in _ACTIVE
        if reducing:
            self.cancel.setEnabled(reduction.state != "cancelling")
            self.progress.setToolTip(reduction.detail)
            self.progress.setRange(0, 0 if reduction.progress < 0 else 100)
            if reduction.progress >= 0:
                self.progress.setValue(reduction.progress)
        embedding = self.coordinator.state("embedding", self.run_id)
        embedding_state = embedding.state if embedding is not None else self._run.state.value
        activity = ""
        if reducing:
            activity = reduction.detail
        elif embedding_state in _ACTIVE:
            activity = detail if detail is not None else embedding.detail if embedding else ""
        self._pending_activity = activity
        if not self.progress_tab.isVisible():
            self._activity_timer.stop()
            return
        summary_error = ""
        try:
            saved_recipe = (self._artifact_summary().get("provenance") or {}).get("reduction") or {}
        except (OSError, ValueError, RuntimeError) as exc:
            saved_recipe = {}
            summary_error = f"Saved vector summary unavailable: {exc}"
        is_reduction = reduction is not None or bool(saved_recipe)
        live = reduction if is_reduction else embedding
        state = live.state if live is not None else "completed" if is_reduction else self._run.state.value
        if is_reduction:
            from EmbeddingsArea.reporting import reduction_report
            summary = reduction_report(reduction.operation if reduction else saved_recipe, state)
        else:
            summary = f"Model: {self._model_label}\nStatus: {state.replace('_', ' ').capitalize()}"
            prepared = int(getattr(self._run, "prepared_input_count", 0) or 0)
            processed = int(getattr(self._run, "processed_items", 0) or 0)
            if state not in _ACTIVE and (prepared or processed):
                summary += f"\nItems processed: {processed:,} of {prepared:,}"
        self.progress_report.setToolTip(f"Run ID: {self.run_id}")
        report = live.report if live is not None else []
        offset = live.report_offset if live is not None else 0
        if (live is not self._report_state or self._rendered_report_end < offset
                or self._rendered_report_end > offset + len(report)):
            # Starting a reduction appends a new operation to the existing history.
            if not (is_reduction and live is not self._report_state):
                self.progress_report.clear()
            self._report_state = live
            self._rendered_report_end = offset
            self._last_diagnostics = self._last_execution_note = self._last_report_summary = ""
            self._last_activity = ""
        lines = report[self._rendered_report_end - offset:]
        self._rendered_report_end = offset + len(report)
        if summary != self._last_report_summary:
            lines = [summary, "", *lines]
            self._last_report_summary = summary
        diagnostics = summary_error or ("" if is_reduction else str(getattr(self._run, "error_message", "") or ""))
        if live is not None and live.pending_error:
            diagnostics = live.pending_error
        # Keep full failure details copyable, even if a traceback exceeds the live-log cap.
        self.progress_report.setMaximumBlockCount(0 if diagnostics else 1000)
        if diagnostics and diagnostics != self._last_diagnostics:
            heading = 'Pause reason' if state == 'paused' else 'Error details'
            lines.extend(("", heading, "-" * len(heading), diagnostics))
        self._last_diagnostics = diagnostics
        note = "" if is_reduction else str(getattr(self, "_execution_note", "") or "")
        if note and note != self._last_execution_note:
            lines.extend(("", "Execution note", "--------------", note))
        self._last_execution_note = note
        if activity and activity != self._last_activity:
            if flush_activity or lines:
                if activity not in lines and (not report or report[-1] != activity):
                    lines.append(activity)
                self._last_activity = activity
                self._activity_timer.stop()
            elif not self._activity_timer.isActive():
                self._activity_timer.start()
        elif not activity:
            self._activity_timer.stop()
        if not lines:
            return
        scroll_bar = self.progress_report.verticalScrollBar()
        followed_latest_output = self._progress_follow_latest
        self._adjusting_progress_scroll = True
        try:
            self.progress_report.appendPlainText("\n".join(lines))
            if followed_latest_output:
                scroll_bar.setValue(scroll_bar.maximum())
        finally:
            self._adjusting_progress_scroll = False
        self._progress_follow_latest = followed_latest_output

    @QtCore.Slot(int)
    def _progress_scroll_changed(self, value: int) -> None:
        if not self._adjusting_progress_scroll:
            scroll_bar = self.progress_report.verticalScrollBar()
            self._progress_follow_latest = value >= scroll_bar.maximum()

    @QtCore.Slot(int, int)
    def _progress_scroll_range_changed(self, _minimum: int, maximum: int) -> None:
        if not self._progress_follow_latest or self._adjusting_progress_scroll:
            return
        self._adjusting_progress_scroll = True
        try:
            self.progress_report.verticalScrollBar().setValue(maximum)
        finally:
            self._adjusting_progress_scroll = False

    def _update_clusters(self, records) -> None:
        expanded = {
            self.cluster_tree.topLevelItem(i).data(0, QtCore.Qt.ItemDataRole.UserRole)
            for i in range(self.cluster_tree.topLevelItemCount())
            if self.cluster_tree.topLevelItem(i).isExpanded()
        }
        selected = self.cluster_tree.currentItem()
        selected_id = selected.data(0, QtCore.Qt.ItemDataRole.UserRole) if selected else None
        old_count = self.cluster_tree.topLevelItemCount()
        self._cluster_progress.clear()
        self._records = {str(record["id"]): record for record in records}
        for record in records:
            identity = str(record["id"])
            live = self.coordinator.state("clustering", identity)
            state = live.state if live is not None and live.state in _ACTIVE else str(record.get("state", "draft"))
            item = QtWidgets.QTreeWidgetItem([
                str(record.get("label") or "Clustering"),
                "",
            ])
            item.setData(0, QtCore.Qt.ItemDataRole.UserRole, identity)
            self.cluster_tree.addTopLevelItem(item)
            for label, value in self._cluster_details(record):
                QtWidgets.QTreeWidgetItem(item, [label, self._text(value)])
            item.setExpanded(identity in expanded)
            if identity == selected_id:
                self.cluster_tree.setCurrentItem(item)
            status_panel = QtWidgets.QWidget(self.cluster_tree)
            status_layout = QtWidgets.QHBoxLayout(status_panel)
            status_layout.setContentsMargins(2, 0, 6, 0)
            status_layout.setSpacing(6)
            if live is not None and live.state in _ACTIVE:
                bar = QtWidgets.QProgressBar(status_panel)
                if live.progress < 0:
                    bar.setRange(0, 0)
                else:
                    bar.setRange(0, 100)
                    bar.setValue(min(99, live.progress))
                bar.setToolTip(live.detail)
                status_layout.addWidget(bar, 1)
                cancel = QtWidgets.QPushButton("Cancel", status_panel)
                cancel.setEnabled(live.state != "cancelling")
                cancel.clicked.connect(
                    lambda _checked=False, key=identity: self.coordinator.cancel(
                        "clustering", key
                    )
                )
                status_layout.addWidget(cancel)
                self._cluster_progress[identity] = (bar, item)
            else:
                status_label = QtWidgets.QLabel(self._cluster_status(record, state), status_panel)
                status_layout.addWidget(status_label)
            button = QtWidgets.QToolButton(status_panel)
            button.setText("…")
            button.setToolTip("Clustering options")
            button.clicked.connect(
                lambda _checked=False, key=identity, anchor=button: self.on_cluster_action(
                    key, "menu", anchor
                )
            )
            status_layout.addWidget(button)
            self.cluster_tree.setItemWidget(item, 1, status_panel)
        if not records:
            self._add_empty_row(self.cluster_tree, "No clustering results yet.")
        self.cluster_tree.model().removeRows(0, old_count)

    @staticmethod
    def _add_empty_row(tree: QtWidgets.QTreeWidget, message: str) -> None:
        item = QtWidgets.QTreeWidgetItem([message, ""])
        item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsSelectable & ~QtCore.Qt.ItemFlag.ItemIsEnabled)
        tree.addTopLevelItem(item)

    @staticmethod
    def _cluster_status(record, state: str) -> str:
        if state == "completed":
            if record.get("groups_modified"):
                return "Modified"
            count = int(record.get("cluster_count", 0) or 0)
            noise = int(record.get("noise_count", 0) or 0)
            return f"Done: {count} clusters, {noise} noise"
        return state.replace("_", " ").capitalize()

    @staticmethod
    def _cluster_details(record):
        method = record.get("method_name") or record.get("method_id")
        values = [("Algorithm", method)]
        if record.get("groups_modified"):
            values.append(("Groups", "Modified in Metadata"))
        parameters = record.get("parameters") or record.get("method_settings", {}).get(record.get("method_id"), {})
        if parameters:
            values.append(("Settings", parameters))
        if record.get("preprocessing"):
            values.append(("Preprocessing", record["preprocessing"]))
        for key, label in (
            ("item_count", "Input items"), ("assigned_item_count", "Assigned items"),
            ("cluster_count", "Clusters"), ("noise_count", "Noise"),
            ("skipped_item_count", "Skipped"), ("duration_seconds", "Duration (s)"),
        ):
            if record.get(key) is not None:
                if record.get("groups_modified") and key in {"assigned_item_count", "cluster_count", "noise_count"}:
                    label = f"Original {label.lower()}"
                values.append((label, record[key]))
        diagnostics = record.get("error") or record.get("warnings")
        if diagnostics:
            values.append(("Diagnostics", diagnostics))
        return values

    @staticmethod
    def _text(value) -> str:
        if isinstance(value, dict):
            return ", ".join(f"{key}={item}" for key, item in value.items())
        if isinstance(value, (tuple, list)):
            return ", ".join(str(item) for item in value)
        return str(value if value not in (None, "") else "—")

    def _elide_name(self):
        self.name.setText(self.name.fontMetrics().elidedText(
            self._name_text, QtCore.Qt.TextElideMode.ElideRight,
            max(0, self.name.contentsRect().width() - max(0, self.name.indent())),
        ))

    def _resize(self) -> None:
        # Match Preview Assets: the tree owns width, the form owns row height.
        if self._sizing:
            return
        self._sizing = True
        try:
            self.widget.layout().activate()
            height = self.header.collapsed_height
            if not self._body.isHidden():
                height += self._body.sizeHint().height()
            changed = self.widget.minimumHeight() != height
            if changed:
                self.widget.setMinimumHeight(height)
            self.widget.resize(self.widget.width(), height)
            if changed:
                self.resized.emit()
        finally:
            self._sizing = False
