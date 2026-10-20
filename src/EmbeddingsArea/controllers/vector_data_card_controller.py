"""Small binder for one embedding run card."""
from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping, Sequence

from PySide6 import QtCore, QtGui, QtWidgets

from EmbeddingsArea.domain import EmbeddingRunState
from shared.qt.icons import apply_icon_color
from shared.qt.item_views import configure_panel_item_view
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


_ACTIVE = {"queued", "running", "cancelling"}


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
        self._run = None
        self._dataset = None
        self._records: dict[str, dict] = {}
        self._pending_records: list[dict] = []
        self._cluster_progress: dict[str, tuple[QtWidgets.QProgressBar, QtWidgets.QTreeWidgetItem]] = {}
        self._info_loaded = False
        self._active = False
        self._presented_execution_state = ""
        self._model_label = "Imported"
        self._progress_follow_latest = True
        self._adjusting_progress_scroll = False
        self._info_loader: Callable[[], list[tuple[str, object]]] | None = None

        self.toggle = require_child(self.widget, QtWidgets.QToolButton, "button_toggle_details")
        self.tabs = require_child(self.widget, QtWidgets.QTabWidget, "tabs_run_details")
        self.name = require_child(self.widget, QtWidgets.QLabel, "label_run_name")
        self.status = require_child(self.widget, QtWidgets.QLabel, "label_status")
        self.progress = require_child(self.widget, QtWidgets.QProgressBar, "progress_run")
        self.progress_control = require_child(self.widget, QtWidgets.QWidget, "progress_control")
        self.pause = require_child(self.widget, QtWidgets.QPushButton, "button_pause_run")
        self.cancel = require_child(self.widget, QtWidgets.QPushButton, "button_cancel_run")
        self.options = require_child(self.widget, QtWidgets.QToolButton, "button_run_options")
        self.info = require_child(self.widget, QtWidgets.QTreeWidget, "tree_run_info")
        self.progress_report = require_child(self.widget, QtWidgets.QTextBrowser, "tb_progress_report")
        self.progress_tab = require_child(self.widget, QtWidgets.QWidget, "tab_progress")
        self.info_tab = require_child(self.widget, QtWidgets.QWidget, "tab_run_info")
        self.clusters_tab = require_child(self.widget, QtWidgets.QWidget, "tab_run_clusters")
        self.cluster_tree = require_child(self.widget, QtWidgets.QTreeWidget, "tree_clustering_runs")
        self.add_clustering = require_child(self.widget, QtWidgets.QPushButton, "button_add_clustering")

        configure_panel_item_view(self.info)
        configure_panel_item_view(self.cluster_tree)
        self.info.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.info.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.cluster_tree.header().setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.cluster_tree.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.toggle.toggled.connect(self._toggle_details)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.pause.clicked.connect(lambda: self.on_action(self.run_id, "pause", None))
        self.cancel.clicked.connect(lambda: self.on_action(self.run_id, "cancel", None))
        self.options.clicked.connect(lambda: self.on_action(self.run_id, "menu", self.options))
        self.add_clustering.clicked.connect(lambda: self.on_action(self.run_id, "cluster", None))
        self.progress_report.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        progress_scroll = self.progress_report.verticalScrollBar()
        progress_scroll.valueChanged.connect(self._progress_scroll_changed)
        progress_scroll.rangeChanged.connect(self._progress_scroll_range_changed)
        self.tabs.setVisible(False)

    def update(self, run, dataset, records, model_label: str, info_loader) -> None:
        self._execution_note = dataset.execution_note if dataset else ""
        self._run, self._dataset = run, dataset
        self._info_loader = info_loader
        self.name.setText(run.label)
        self._model_label = model_label or "Imported"
        item_count = dataset.item_count if dataset is not None else max(run.processed_items, run.prepared_input_count)
        self._item_count_label = f"{item_count:,} item{'s' if item_count != 1 else ''}"
        self.add_clustering.setEnabled(dataset is not None)
        self.add_clustering.setToolTip(
            "Run a clustering algorithm on this vector data."
            if dataset is not None else "Clustering is available after vector data is published."
        )
        self._update_status()
        self._update_progress_report()
        self._pending_records = list(records)
        if not self.tabs.isHidden() and self.tabs.currentWidget() is self.clusters_tab:
            self._update_clusters(self._pending_records)
        if self._info_loaded:
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
        apply_icon_color(self.toggle)
        self.tabs.setVisible(checked)
        if checked and self.tabs.currentWidget() is self.info_tab:
            self._load_info()
        elif checked and self.tabs.currentWidget() is self.clusters_tab:
            self._update_clusters(self._pending_records)
        self._resize()

    def _tab_changed(self, index: int) -> None:
        current = self.tabs.widget(index)
        if current is self.info_tab and not self.tabs.isHidden():
            self._load_info()
        elif current is self.clusters_tab and not self.tabs.isHidden():
            self._update_clusters(self._pending_records)
        self._resize()

    def _load_info(self) -> None:
        self._info_loaded = True
        try:
            values = self._info_loader() if self._info_loader is not None else []
        except Exception as exc:  # noqa: BLE001
            values = [("Diagnostics", str(exc))]
        old_count = self.info.topLevelItemCount()
        for label, value in values:
            self._add_info_value(self.info, str(label), value)
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
            self.cancel.setEnabled(live.state != "cancelling")
            self.update_progress(live.progress, live.detail)
            return
        self._set_active_header(False)
        state = self._run.state
        if state == EmbeddingRunState.COMPLETED:
            label = self._item_count_label
        else:
            label = state.value.replace("_", " ").capitalize()
        self.status.setText(label)
        self.status.setToolTip(
            self._run.error_message
            or getattr(self, "_execution_note", "")
            or ("Completed with warnings" if state == EmbeddingRunState.COMPLETED and self._run.failed_items else label)
        )
        presentation = state.value
        if presentation != self._presented_execution_state:
            self._presented_execution_state = presentation
            if state == EmbeddingRunState.COMPLETED:
                self.tabs.setCurrentWidget(self.info_tab)
            elif state in {
                EmbeddingRunState.FAILED,
                EmbeddingRunState.CANCELLED,
                EmbeddingRunState.INTERRUPTED,
                EmbeddingRunState.PAUSED,
            }:
                self.toggle.setChecked(True)
                self.tabs.setCurrentWidget(self.progress_tab)

    def _set_active_header(self, active: bool) -> None:
        """Swap the one-line card header between live controls and result metadata."""
        self._active = active
        self.tabs.setTabVisible(self.tabs.indexOf(self.clusters_tab), not active)
        for widget in (self.options,):
            widget.setVisible(not active)
        self.toggle.setVisible(True)
        self.status.setVisible(True)
        self.progress_control.setVisible(active)
        self.tabs.setVisible(self.toggle.isChecked())

    def _show_active_progress(self) -> None:
        if self._presented_execution_state != "active":
            self._presented_execution_state = "active"
            self.tabs.setCurrentWidget(self.progress_tab)
            self.toggle.setChecked(True)
        self.tabs.setVisible(self.toggle.isChecked())
        self._resize()

    def _update_progress_report(self, *, progress: int | None = None, detail: str | None = None) -> None:
        if self._run is None:
            return
        live = self.coordinator.state("embedding", self.run_id)
        state = live.state if live is not None else self._run.state.value
        if progress is None:
            progress = live.progress if live is not None else int(self._run.progress)
        if detail is None:
            detail = live.detail if live is not None else ""
        lines = [
            "Embedding execution report",
            "==========================",
            f"Run: {self._run.label}",
            f"Run ID: {self.run_id}",
            f"Model: {self._model_label}",
            f"Status: {state.replace('_', ' ').capitalize()}",
            f"Progress: {'Working' if progress < 0 else f'{progress}%'}",
        ]
        prepared = int(getattr(self._run, "prepared_input_count", 0) or 0)
        processed = int(getattr(self._run, "processed_items", 0) or 0)
        if prepared or processed:
            lines.append(f"Items processed: {processed:,} of {prepared:,}")
        if getattr(self._run, "updated_at", ""):
            lines.append(f"Last saved: {self._run.updated_at}")
        lines.extend(("", "Activity", "--------"))
        report = list(live.report) if live is not None else []
        if detail and detail not in report:
            report.append(detail)
        lines.extend(report or ["No live activity messages are available for this run."])
        diagnostics = str(getattr(self._run, "error_message", "") or "")
        if live is not None and live.pending_error:
            diagnostics = live.pending_error
        if diagnostics:
            lines.extend(("", "Error details", "-------------", diagnostics))
        note = str(getattr(self, "_execution_note", "") or "")
        if note:
            lines.extend(("", "Execution note", "--------------", note))
        scroll_bar = self.progress_report.verticalScrollBar()
        previous_value = scroll_bar.value()
        followed_latest_output = self._progress_follow_latest
        cursor = self.progress_report.textCursor()
        anchor, position = cursor.anchor(), cursor.position()
        had_selection = cursor.hasSelection()

        self._adjusting_progress_scroll = True
        try:
            self.progress_report.setPlainText("\n".join(lines))

            if had_selection:
                document_length = max(0, self.progress_report.document().characterCount() - 1)
                cursor.setPosition(min(anchor, document_length))
                cursor.setPosition(
                    min(position, document_length),
                    QtGui.QTextCursor.MoveMode.KeepAnchor,
                )
                self.progress_report.setTextCursor(cursor)
            if followed_latest_output:
                if not had_selection:
                    cursor = self.progress_report.textCursor()
                    cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
                    self.progress_report.setTextCursor(cursor)
                self.progress_report.ensureCursorVisible()
                scroll_bar.setValue(scroll_bar.maximum())
            else:
                scroll_bar.setValue(min(previous_value, scroll_bar.maximum()))
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
            count = int(record.get("cluster_count", 0) or 0)
            noise = int(record.get("noise_count", 0) or 0)
            return f"Done · {count} clusters · {noise} noise"
        return state.replace("_", " ").capitalize()

    @staticmethod
    def _cluster_details(record):
        method = record.get("method_name") or record.get("method_id")
        values = [("Algorithm", method)]
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

    def _resize(self) -> None:
        # QListWidget owns the card width. Calling adjustSize() here applies the
        # current tab's intrinsic width and visibly narrows the card when the
        # Clusters page has a smaller size hint. Only the row height belongs to
        # the card binder.
        self.widget.updateGeometry()
        self.resized.emit()
