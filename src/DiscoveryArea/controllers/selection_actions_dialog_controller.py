from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import sys

from PySide6 import QtCore, QtGui, QtWidgets

from DiscoveryArea.nearest_neighbors import NeighborSearchRequest, NeighborSearchResult
from DiscoveryArea.thumbnails.service import DecodedThumbnail, ThumbnailDecodeFailure
from shared.qt.icons import apply_icon_color
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


@dataclass(frozen=True)
class SelectionActionsDialogData:
    metadata_id: int
    metadata_rows: tuple[tuple[str, str], ...]
    thumbnail_options: tuple[tuple[str, str], ...]
    pin_scope_mode: str = ""
    neighbor_source_label: str = ""
    neighbor_unavailable_reason: str = ""
    neighbor_candidate_ids: tuple[int, ...] = ()
    neighbor_visible_ids: tuple[int, ...] = ()
    neighbor_embedding_options: tuple[
        tuple[str, str, int], ...
    ] = ()
    neighbor_default_embedding_id: str = ""
    neighbor_thumbnail_variant_id: str = "none"
    source_file_path: str = ""
    source_content_type: str = ""
    source_file_unavailable_reason: str = ""


@dataclass(frozen=True)
class SelectionActionsResult:
    action: str


ThumbnailRequester = Callable[[str, int, str, int, bool], None]
NeighborRequester = Callable[[NeighborSearchRequest], int]
NeighborCanceller = Callable[[], None]
NeighborGroupSaver = Callable[[str, tuple[int, ...]], str]
NeighborHighlighter = Callable[[int | None], None]
DialogShownCallback = Callable[[], None]
PinScopeSetter = Callable[[str], bool]


class _NeighborCard(QtWidgets.QToolButton):
    THUMBNAIL_SIZE = 112
    HEIGHT = 128

    def __init__(
        self,
        metadata_id: int,
        tooltip: str,
        score_text: str,
        parent: QtWidgets.QWidget,
    ) -> None:
        super().__init__(parent)
        self.metadata_id = int(metadata_id)
        self.score_text = str(score_text)
        self.setObjectName(f"button_neighbor_{self.metadata_id}")
        self.setToolTip(str(tooltip))
        self.setAutoRaise(False)
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.setMinimumWidth(1)
        self.setFixedHeight(self.HEIGHT)
        self.setIconSize(QtCore.QSize(self.THUMBNAIL_SIZE, self.THUMBNAIL_SIZE))
        self.set_thumbnail(None)

    def set_thumbnail(self, payload: DecodedThumbnail | None) -> None:
        canvas = QtGui.QPixmap(self.THUMBNAIL_SIZE, self.THUMBNAIL_SIZE)
        canvas.fill(self.palette().color(QtGui.QPalette.ColorRole.AlternateBase))
        if payload is not None:
            image = QtGui.QImage(
                payload.rgba,
                payload.width,
                payload.height,
                payload.width * 4,
                QtGui.QImage.Format.Format_RGBA8888,
            ).copy()
            pixmap = QtGui.QPixmap.fromImage(image).scaled(
                self.THUMBNAIL_SIZE,
                self.THUMBNAIL_SIZE,
                QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            )
            painter = QtGui.QPainter(canvas)
            painter.drawPixmap(
                (canvas.width() - pixmap.width()) // 2,
                (canvas.height() - pixmap.height()) // 2,
                pixmap,
            )
            painter.end()
        self.setIcon(QtGui.QIcon(canvas))

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        font = painter.font()
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1.0))
        painter.setFont(font)
        metrics = QtGui.QFontMetrics(font)
        badge_width = metrics.horizontalAdvance(self.score_text) + 10
        badge_height = metrics.height() + 6
        thumbnail_left = (self.width() - self.THUMBNAIL_SIZE) // 2
        thumbnail_top = (self.height() - self.THUMBNAIL_SIZE) // 2
        badge = QtCore.QRect(
            thumbnail_left + self.THUMBNAIL_SIZE - badge_width - 3,
            thumbnail_top + self.THUMBNAIL_SIZE - badge_height - 3,
            badge_width,
            badge_height,
        )
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(0, 0, 0, 180))
        painter.drawRoundedRect(badge, 4, 4)
        painter.setPen(QtGui.QColor("white"))
        painter.drawText(badge, QtCore.Qt.AlignmentFlag.AlignCenter, self.score_text)
        if self.isChecked():
            pen = QtGui.QPen(
                self.palette().color(QtGui.QPalette.ColorRole.Highlight),
                4,
            )
            painter.setPen(pen)
            painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
            painter.drawRect(self.rect().adjusted(2, 2, -3, -3))
        painter.end()


class SelectionActionsDialogController:
    DETAIL_THUMBNAIL_SIZE = 768
    _NEIGHBOR_GRID_COLUMNS = 3
    _NEIGHBOR_INITIAL_COUNT = 12
    _NEIGHBOR_BATCH_COUNT = 9

    def __init__(self) -> None:
        self._data: SelectionActionsDialogData | None = None
        self._request_thumbnail: ThumbnailRequester | None = None
        self._request_neighbors: NeighborRequester | None = None
        self._cancel_neighbors: NeighborCanceller | None = None
        self._save_neighbor_group: NeighborGroupSaver | None = None
        self._highlight_neighbor: NeighborHighlighter | None = None
        self._pending: dict[str, tuple[int, str, int]] = {}
        self._metadata_label: QtWidgets.QLabel | None = None
        self._source_video_status_label: QtWidgets.QLabel | None = None
        self._source_file_button: QtWidgets.QPushButton | None = None
        self._source_file_revealer: Callable[[Path], bool] | None = None
        self._source_video_button: QtWidgets.QToolButton | None = None
        self._source_video_slider: QtWidgets.QSlider | None = None
        self._source_video_time_label: QtWidgets.QLabel | None = None
        self._source_media_controls: QtWidgets.QWidget | None = None
        self._metadata_media_stack: QtWidgets.QStackedWidget | None = None
        self._source_video_player: QtCore.QObject | None = None
        self._source_video_audio_output: QtCore.QObject | None = None
        self._source_video_widget: QtWidgets.QWidget | None = None
        self._source_video_loaded_path: Path | None = None
        self._source_video_seeking = False
        self._source_video_preview_pending = False
        self._dialog: QtWidgets.QDialog | None = None
        self._neighbor_query_id: int | None = None
        self._neighbor_request_revision: int | None = None
        self._neighbor_search_started = False
        self._neighbor_results: NeighborSearchResult | None = None
        self._neighbor_cards: dict[str, _NeighborCard] = {}
        self._neighbor_grid_spacer: QtWidgets.QSpacerItem | None = None
        self._neighbor_default_group_label = ""
        self._neighbor_item_count_by_embedding: dict[str, int] = {}
        self._rendered_neighbor_count = 0

    def exec(
        self,
        data: SelectionActionsDialogData,
        global_pos: QtCore.QPoint | None = None,
        request_thumbnail: ThumbnailRequester | None = None,
        request_neighbors: NeighborRequester | None = None,
        cancel_neighbors: NeighborCanceller | None = None,
        save_neighbor_group: NeighborGroupSaver | None = None,
        highlight_neighbor: NeighborHighlighter | None = None,
        dialog_shown: DialogShownCallback | None = None,
        set_pin_scope: PinScopeSetter | None = None,
    ) -> SelectionActionsResult | None:
        dialog = load_ui("DiscoveryArea", "ui/selection_actions_dialog.ui", None)
        if not isinstance(dialog, QtWidgets.QDialog):
            return None
        dialog.setWindowTitle(f"Selected item: {int(data.metadata_id)}")
        self._data, self._request_thumbnail = data, request_thumbnail
        self._request_neighbors = request_neighbors
        self._cancel_neighbors = cancel_neighbors
        self._save_neighbor_group = save_neighbor_group
        self._highlight_neighbor = highlight_neighbor
        self._dialog = dialog
        self._neighbor_query_id = int(data.metadata_id)
        dialog.setWindowFlag(QtCore.Qt.WindowType.Tool, True)
        dialog.setWindowModality(QtCore.Qt.WindowModality.NonModal)
        stack = require_child(dialog, QtWidgets.QStackedWidget, "stack_selection_actions")
        group = QtWidgets.QButtonGroup(dialog)
        group.setExclusive(True)
        for index, name in enumerate(("button_metadata_page", "button_nearest_neighbors_page")):
            button = require_child(dialog, QtWidgets.QAbstractButton, name)
            group.addButton(button, index)
            button.clicked.connect(lambda checked=False, page=index: stack.setCurrentIndex(page))
        group.button(0).setChecked(True)
        stack.setCurrentIndex(0)
        self._populate_metadata(dialog, data)
        self._populate_nearest_neighbors(dialog, data)
        stack.currentChanged.connect(self._handle_page_changed)
        if self._highlight_neighbor is not None:
            self._highlight_neighbor(None)
        chosen: list[SelectionActionsResult] = []
        self._configure_pin_menu(dialog, data, set_pin_scope)
        open_metadata_button = require_child(
            dialog,
            QtWidgets.QPushButton,
            "button_open_metadata",
        )

        def open_metadata() -> None:
            chosen.append(SelectionActionsResult("metadata"))
            dialog.accept()

        open_metadata_button.clicked.connect(open_metadata)
        for names in (
            ("button_cancel_metadata",),
            ("button_cancel_neighbors", "button_cancel_nn"),
        ):
            button_box = None
            for name in names:
                button_box = dialog.findChild(QtWidgets.QDialogButtonBox, name)
                if button_box is not None:
                    break
            if button_box is None:
                raise RuntimeError(
                    f"{dialog.objectName()} is missing {names[0]!r}"
                )
            button_box.rejected.connect(dialog.reject)
        self._position_default(dialog, global_pos)
        event_loop = QtCore.QEventLoop()
        dialog.finished.connect(event_loop.quit)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        if dialog_shown is not None:
            dialog_shown()
        if dialog.isVisible():
            event_loop.exec()
        accepted = dialog.result() == QtWidgets.QDialog.DialogCode.Accepted
        self.close()
        return chosen[0] if accepted and chosen else None

    def _configure_pin_menu(
        self,
        dialog: QtWidgets.QDialog,
        data: SelectionActionsDialogData,
        set_pin_scope: PinScopeSetter | None,
    ) -> None:
        button = require_child(
            dialog,
            QtWidgets.QToolButton,
            "button_pin_menu",
        )
        current_scope = (
            "projection"
            if str(data.pin_scope_mode).strip().lower() == "projection"
            else "project"
            if str(data.pin_scope_mode).strip().lower() == "project"
            else ""
        )
        menu = QtWidgets.QMenu(button)
        collection_wide = menu.addAction("Create Pin - Collection Wide")
        projection_exclusive = menu.addAction(
            "Create Pin - This Projection Only"
        )
        menu.addSeparator()
        remove_pin = menu.addAction("Remove Pin")
        collection_wide.setCheckable(True)
        projection_exclusive.setCheckable(True)

        def show_scope(scope: str) -> None:
            collection_wide.setChecked(scope == "project")
            projection_exclusive.setChecked(scope == "projection")
            remove_pin.setEnabled(bool(scope))
            button.setToolTip(
                "Change pin scope" if scope else "Create pin"
            )

        show_scope(current_scope)
        button.setEnabled(set_pin_scope is not None)
        button.setMenu(menu)
        button.setPopupMode(
            QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup
        )
        button.setStyleSheet(
            "QToolButton::menu-indicator { image: none; width: 0px; height: 0px; }"
        )

        def change_scope(scope: str) -> None:
            nonlocal current_scope
            if scope == current_scope:
                show_scope(current_scope)
                return
            if set_pin_scope is None or not set_pin_scope(scope):
                show_scope(current_scope)
                return
            current_scope = scope
            show_scope(current_scope)

        collection_wide.triggered.connect(lambda: change_scope("project"))
        projection_exclusive.triggered.connect(
            lambda: change_scope("projection")
        )
        remove_pin.triggered.connect(lambda: change_scope(""))

    def close(self) -> None:
        self._dispose_source_video_player()
        self._pending.clear()
        self._data = None
        self._request_thumbnail = None
        self._request_neighbors = None
        if self._cancel_neighbors is not None:
            self._cancel_neighbors()
        self._cancel_neighbors = None
        self._save_neighbor_group = None
        if self._highlight_neighbor is not None:
            self._highlight_neighbor(None)
        self._highlight_neighbor = None
        self._metadata_label = None
        self._source_video_status_label = None
        self._source_file_button = None
        self._source_file_revealer = None
        self._source_video_button = None
        self._source_video_slider = None
        self._source_video_time_label = None
        self._source_media_controls = None
        self._metadata_media_stack = None
        self._dialog = None
        self._neighbor_query_id = None
        self._neighbor_request_revision = None
        self._neighbor_search_started = False
        self._neighbor_results = None
        self._neighbor_cards.clear()
        self._neighbor_grid_spacer = None
        self._neighbor_default_group_label = ""
        self._neighbor_item_count_by_embedding.clear()
        self._rendered_neighbor_count = 0

    def apply_thumbnail(self, payload: DecodedThumbnail | ThumbnailDecodeFailure) -> bool:
        handled = False
        key = (int(payload.metadata_id), str(payload.variant_id or "none"), int(payload.target_px))
        for target, pending in tuple(self._pending.items()):
            if pending != key:
                continue
            handled = True
            self._pending.pop(target, None)
            if isinstance(payload, ThumbnailDecodeFailure):
                if target == "metadata_source" and self._metadata_label is not None:
                    self._metadata_label.setPixmap(QtGui.QPixmap())
                    self._metadata_label.setText("Source preview unavailable")
                elif target in self._neighbor_cards:
                    self._neighbor_cards[target].set_thumbnail(None)
            elif target == "metadata_source":
                self._show_metadata_thumbnail(payload)
            elif target in self._neighbor_cards:
                self._neighbor_cards[target].set_thumbnail(payload)
        return handled

    def apply_neighbor_result(self, revision: int, payload: NeighborSearchResult) -> bool:
        if self._neighbor_request_revision != int(revision):
            return False
        self._neighbor_request_revision = None
        self._neighbor_results = payload
        self._show_neighbor_results(payload)
        return True

    def apply_neighbor_failure(self, revision: int, message: str) -> bool:
        if self._neighbor_request_revision != int(revision):
            return False
        self._neighbor_request_revision = None
        detail = str(message).partition(": ")[2] or str(message)
        self._show_neighbor_message(detail)
        self._set_neighbor_controls_enabled(True)
        self._set_neighbor_save_enabled(False)
        return True

    def _populate_nearest_neighbors(
        self,
        dialog: QtWidgets.QDialog,
        data: SelectionActionsDialogData,
    ) -> None:
        embeddings = require_child(dialog, QtWidgets.QComboBox, "combo_embeddings")
        embeddings.clear()
        options = data.neighbor_embedding_options
        if not options and data.neighbor_candidate_ids:
            fallback_id = data.neighbor_default_embedding_id or "active"
            options = ((
                fallback_id,
                data.neighbor_source_label or "Current projection embedding",
                len(data.neighbor_candidate_ids),
            ),)
        self._neighbor_item_count_by_embedding = {
            str(run_id): max(0, int(item_count))
            for run_id, _label, item_count in options
        }
        for run_id, label, _item_count in options:
            embeddings.addItem(str(label), str(run_id))
        selected_embedding = embeddings.findData(data.neighbor_default_embedding_id)
        embeddings.setCurrentIndex(max(0, selected_embedding))

        metric = require_child(dialog, QtWidgets.QComboBox, "combo_neighbor_metric")
        metric.clear()
        metric.addItem("Cosine Similarity", "cosine")
        metric.addItem("Euclidean Distance", "euclidean")
        thumbnail = require_child(
            dialog,
            QtWidgets.QComboBox,
            "combo_neighbor_thumbnail_source",
        )
        thumbnail.clear()
        thumbnail.addItem("None", "none")
        for variant_id, label in data.thumbnail_options:
            if str(variant_id) != "none":
                thumbnail.addItem(str(label), str(variant_id))
        selected = thumbnail.findData(data.neighbor_thumbnail_variant_id)
        if selected < 0 and thumbnail.count() > 1:
            selected = 1
        thumbnail.setCurrentIndex(max(0, selected))

        embeddings.currentIndexChanged.connect(self._mark_neighbor_search_dirty)
        metric.currentIndexChanged.connect(self._configure_neighbor_radius_for_metric)
        metric.currentIndexChanged.connect(self._mark_neighbor_search_dirty)
        require_child(
            dialog,
            QtWidgets.QCheckBox,
            "checkbox_neighbors_visible_only",
        ).toggled.connect(self._mark_neighbor_search_dirty)
        thumbnail.currentIndexChanged.connect(self._refresh_neighbor_thumbnails)
        scroll = require_child(
            dialog,
            QtWidgets.QScrollArea,
            "scroll_neighbor_results",
        )
        scroll.verticalScrollBar().valueChanged.connect(self._load_neighbor_scroll_margin)
        scroll.verticalScrollBar().rangeChanged.connect(
            self._schedule_neighbor_grid_fill
        )
        save_button = require_child(dialog, QtWidgets.QPushButton, "button_save_nn_group")
        save_button.setEnabled(False)
        save_button.clicked.connect(self._save_current_neighbor_group)
        top_k_radio = require_child(
            dialog,
            QtWidgets.QRadioButton,
            "radio_topk",
        )
        radius_radio = require_child(
            dialog,
            QtWidgets.QRadioButton,
            "radio_radius",
        )
        top_k_spin = require_child(
            dialog,
            QtWidgets.QSpinBox,
            "spin_save_topk",
        )
        radius_spin = require_child(
            dialog,
            QtWidgets.QDoubleSpinBox,
            "spin_save_radius",
        )
        top_k_radio.toggled.connect(self._update_neighbor_save_controls)
        radius_radio.toggled.connect(self._update_neighbor_save_controls)
        top_k_spin.valueChanged.connect(self._update_neighbor_group_default)
        top_k_spin.valueChanged.connect(self._reset_neighbor_save_button)
        radius_spin.valueChanged.connect(self._update_neighbor_group_default)
        radius_spin.valueChanged.connect(self._reset_neighbor_save_button)
        group_edit = require_child(dialog, QtWidgets.QLineEdit, "line_nn_group")
        group_edit.textChanged.connect(self._reset_neighbor_save_button)
        self._configure_neighbor_radius_for_metric()
        self._update_neighbor_save_controls()

        unavailable = str(data.neighbor_unavailable_reason or "").strip()
        if not unavailable and (
            not self._neighbor_item_count_by_embedding
            or self._request_neighbors is None
        ):
            unavailable = "No completed embeddings are available."
        if unavailable:
            page_button = require_child(
                dialog,
                QtWidgets.QAbstractButton,
                "button_nearest_neighbors_page",
            )
            page_button.setEnabled(False)
            page_button.setToolTip(unavailable)

    @QtCore.Slot(int)
    def _handle_page_changed(self, index: int) -> None:
        if int(index) != 0:
            self._pause_source_video()
        if int(index) == 1 and not self._neighbor_search_started:
            self._start_neighbor_search()
        elif int(index) != 1:
            for card in self._neighbor_cards.values():
                card.setAutoExclusive(False)
                card.setChecked(False)
                card.setAutoExclusive(True)
            if self._highlight_neighbor is not None:
                self._highlight_neighbor(None)

    @QtCore.Slot()
    def _start_neighbor_search(self) -> None:
        if self._dialog is None or self._data is None or self._request_neighbors is None:
            return
        if (
            self._data.neighbor_unavailable_reason
            or not self._neighbor_item_count_by_embedding
        ):
            return
        self._neighbor_search_started = True
        self._clear_neighbor_grid()
        self._reset_neighbor_save_button()
        self._set_neighbor_controls_enabled(False)
        self._set_neighbor_save_enabled(False)
        embeddings = require_child(
            self._dialog,
            QtWidgets.QComboBox,
            "combo_embeddings",
        )
        embedding_run_id = str(embeddings.currentData() or "")
        self._show_neighbor_message(
            f"Searching {embeddings.currentText() or 'the selected embedding'}…"
        )
        query_id = int(self._neighbor_query_id or self._data.metadata_id)
        visible_only = require_child(
            self._dialog,
            QtWidgets.QCheckBox,
            "checkbox_neighbors_visible_only",
        ).isChecked()
        if visible_only:
            candidate_ids = self._data.neighbor_visible_ids
            if query_id not in candidate_ids:
                candidate_ids = (query_id, *candidate_ids)
            include_all_candidates = False
            count = max(1, len(candidate_ids))
        else:
            candidate_ids = ()
            include_all_candidates = True
            count = max(
                1,
                self._neighbor_item_count_by_embedding.get(embedding_run_id, 0),
            )
        request = NeighborSearchRequest(
            query_metadata_id=query_id,
            candidate_metadata_ids=tuple(int(value) for value in candidate_ids),
            count=count,
            metric=str(
                require_child(
                    self._dialog,
                    QtWidgets.QComboBox,
                    "combo_neighbor_metric",
                ).currentData()
                or "cosine"
            ),
            embedding_run_id=embedding_run_id,
            include_all_candidates=include_all_candidates,
        )
        self._neighbor_request_revision = int(self._request_neighbors(request))

    def _mark_neighbor_search_dirty(self, *_args: object) -> None:
        if not self._neighbor_search_started or self._data is None:
            return
        if self._cancel_neighbors is not None:
            self._cancel_neighbors()
        self._neighbor_request_revision = None
        self._neighbor_results = None
        self._clear_neighbor_grid()
        self._start_neighbor_search()

    def _show_neighbor_results(self, result: NeighborSearchResult) -> None:
        if self._dialog is None:
            return
        self._clear_neighbor_grid()
        self._neighbor_query_id = int(result.query_metadata_id)
        if result.matches:
            self._append_neighbor_cards(self._NEIGHBOR_INITIAL_COUNT)
            self._schedule_neighbor_grid_fill()
        else:
            self._show_neighbor_message("No eligible neighbors found.")
        self._set_neighbor_controls_enabled(True)
        self._set_neighbor_save_enabled(bool(result.matches))
        spin = require_child(self._dialog, QtWidgets.QSpinBox, "spin_save_topk")
        available = max(1, len(result.matches))
        with QtCore.QSignalBlocker(spin):
            spin.setMaximum(min(1000, available))
            spin.setValue(min(spin.value(), available))
        self._update_neighbor_group_default()

    def _append_neighbor_cards(self, count: int) -> None:
        if self._dialog is None or self._neighbor_results is None:
            return
        grid = require_child(
            self._dialog,
            QtWidgets.QGridLayout,
            "layout_neighbor_results_grid",
        )
        metric_label = (
            "Cosine similarity"
            if self._neighbor_results.metric == "cosine"
            else "Euclidean distance"
        )
        start = self._rendered_neighbor_count
        stop = min(start + max(0, int(count)), len(self._neighbor_results.matches))
        if self._neighbor_grid_spacer is not None:
            grid.removeItem(self._neighbor_grid_spacer)
            self._neighbor_grid_spacer = None
        for index in range(start, stop):
            rank = index + 1
            match = self._neighbor_results.matches[index]
            tooltip = (
                f"Rank {rank}\nMetadata ID: {match.metadata_id}\n"
                f"{metric_label}: {match.score:.6g}"
            )
            card = _NeighborCard(
                match.metadata_id,
                tooltip,
                f"{match.score:.4g}",
                self._dialog,
            )
            card.clicked.connect(
                lambda _checked=False, metadata_id=match.metadata_id: QtCore.QTimer.singleShot(
                    0,
                    lambda target=metadata_id: self._activate_canvas_item(target),
                )
            )
            target = f"neighbor:{rank}:{match.metadata_id}"
            self._neighbor_cards[target] = card
            grid.addWidget(
                card,
                (rank - 1) // self._NEIGHBOR_GRID_COLUMNS,
                (rank - 1) % self._NEIGHBOR_GRID_COLUMNS,
            )
            self._request_neighbor_card_thumbnail(target, card)
        self._rendered_neighbor_count = stop
        self._neighbor_grid_spacer = QtWidgets.QSpacerItem(
            0,
            0,
            QtWidgets.QSizePolicy.Policy.Minimum,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        grid.addItem(
            self._neighbor_grid_spacer,
            (stop + self._NEIGHBOR_GRID_COLUMNS - 1) // self._NEIGHBOR_GRID_COLUMNS,
            0,
            1,
            self._NEIGHBOR_GRID_COLUMNS,
        )

    def _schedule_neighbor_grid_fill(self, *_args: object) -> None:
        QtCore.QTimer.singleShot(0, self._ensure_neighbor_grid_scrollable)

    def _ensure_neighbor_grid_scrollable(self) -> None:
        if self._dialog is None or self._neighbor_results is None:
            return
        total = len(self._neighbor_results.matches)
        if self._rendered_neighbor_count >= total:
            return
        scroll = require_child(
            self._dialog,
            QtWidgets.QScrollArea,
            "scroll_neighbor_results",
        )
        rows_to_overflow = max(
            1,
            scroll.viewport().height() // _NeighborCard.HEIGHT + 1,
        )
        target_count = min(
            total,
            max(
                self._NEIGHBOR_INITIAL_COUNT,
                rows_to_overflow * self._NEIGHBOR_GRID_COLUMNS,
            ),
        )
        if target_count > self._rendered_neighbor_count:
            self._append_neighbor_cards(target_count - self._rendered_neighbor_count)

    def _load_neighbor_scroll_margin(self, value: int) -> None:
        if self._dialog is None or self._neighbor_results is None:
            return
        scrollbar = require_child(
            self._dialog,
            QtWidgets.QScrollArea,
            "scroll_neighbor_results",
        ).verticalScrollBar()
        margin = 3 * (_NeighborCard.THUMBNAIL_SIZE + 16)
        if int(value) >= max(0, scrollbar.maximum() - margin):
            self._append_neighbor_cards(self._NEIGHBOR_BATCH_COUNT)

    def _activate_canvas_item(self, metadata_id: int) -> None:
        if self._highlight_neighbor is not None:
            self._highlight_neighbor(int(metadata_id))

    def _refresh_neighbor_thumbnails(self, *_args: object) -> None:
        if self._dialog is None or self._neighbor_results is None:
            return
        for target in tuple(self._neighbor_cards):
            self._pending.pop(target, None)
        variant = str(
            require_child(
                self._dialog,
                QtWidgets.QComboBox,
                "combo_neighbor_thumbnail_source",
            ).currentData()
            or "none"
        )
        for target, card in self._neighbor_cards.items():
            card.set_thumbnail(None)
            self._request_neighbor_card_thumbnail(target, card, variant=variant)

    def _request_neighbor_card_thumbnail(
        self,
        target: str,
        card: _NeighborCard,
        *,
        variant: str | None = None,
    ) -> None:
        if self._dialog is None:
            return
        selected_variant = str(
            variant
            or require_child(
                self._dialog,
                QtWidgets.QComboBox,
                "combo_neighbor_thumbnail_source",
            ).currentData()
            or "none"
        )
        if selected_variant == "none" or self._request_thumbnail is None:
            return
        request = (card.metadata_id, selected_variant, _NeighborCard.THUMBNAIL_SIZE)
        self._pending[target] = request
        self._request_thumbnail(target, *request, False)

    def _clear_neighbor_grid(self) -> None:
        if self._dialog is None:
            self._neighbor_cards.clear()
            return
        for target in tuple(self._neighbor_cards):
            self._pending.pop(target, None)
        grid = require_child(
            self._dialog,
            QtWidgets.QGridLayout,
            "layout_neighbor_results_grid",
        )
        while grid.count():
            item = grid.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._neighbor_cards.clear()
        self._neighbor_grid_spacer = None
        self._rendered_neighbor_count = 0

    def _show_neighbor_message(self, text: str) -> None:
        if self._dialog is None:
            return
        self._clear_neighbor_grid()
        label = QtWidgets.QLabel(str(text), self._dialog)
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        require_child(
            self._dialog,
            QtWidgets.QGridLayout,
            "layout_neighbor_results_grid",
        ).addWidget(label, 0, 0, 1, 3)

    def _set_neighbor_controls_enabled(self, enabled: bool) -> None:
        if self._dialog is None:
            return
        unavailable = bool(self._data and self._data.neighbor_unavailable_reason)
        controls = require_child(
            self._dialog,
            QtWidgets.QWidget,
            "panel_neighbor_controls",
        )
        controls.setEnabled(bool(enabled) and not unavailable)

    def _set_neighbor_save_enabled(self, enabled: bool) -> None:
        if self._dialog is None:
            return
        require_child(
            self._dialog,
            QtWidgets.QPushButton,
            "button_save_nn_group",
        ).setEnabled(bool(enabled) and self._save_neighbor_group is not None)

    def _configure_neighbor_radius_for_metric(self, *_args: object) -> None:
        if self._dialog is None:
            return
        metric = str(
            require_child(
                self._dialog,
                QtWidgets.QComboBox,
                "combo_neighbor_metric",
            ).currentData()
            or "cosine"
        )
        radius = require_child(
            self._dialog,
            QtWidgets.QDoubleSpinBox,
            "spin_save_radius",
        )
        if metric == "cosine":
            radius.setRange(-1.0, 1.0)
        else:
            radius.setRange(0.0, 99.0)

    def _update_neighbor_save_controls(self, *_args: object) -> None:
        if self._dialog is None:
            return
        top_k_checked = require_child(
            self._dialog,
            QtWidgets.QRadioButton,
            "radio_topk",
        ).isChecked()
        radius_checked = require_child(
            self._dialog,
            QtWidgets.QRadioButton,
            "radio_radius",
        ).isChecked()
        require_child(
            self._dialog,
            QtWidgets.QSpinBox,
            "spin_save_topk",
        ).setEnabled(top_k_checked)
        require_child(
            self._dialog,
            QtWidgets.QDoubleSpinBox,
            "spin_save_radius",
        ).setEnabled(radius_checked)
        self._update_neighbor_group_default()
        self._reset_neighbor_save_button()

    def _update_neighbor_group_default(self, *_args: object) -> None:
        if self._dialog is None:
            return
        label_edit = require_child(
            self._dialog,
            QtWidgets.QLineEdit,
            "line_nn_group",
        )
        new_default = self._default_neighbor_group_label()
        current_label = label_edit.text().strip()
        if not current_label or current_label == self._neighbor_default_group_label:
            with QtCore.QSignalBlocker(label_edit):
                label_edit.setText(new_default)
        self._neighbor_default_group_label = new_default

    def _default_neighbor_group_label(self) -> str:
        if self._dialog is None:
            return ""
        metadata_id = int(
            self._neighbor_query_id
            or (self._data.metadata_id if self._data is not None else 0)
        )
        top_k_checked = require_child(
            self._dialog,
            QtWidgets.QRadioButton,
            "radio_topk",
        ).isChecked()
        if top_k_checked:
            parameter = f"Top{require_child(self._dialog, QtWidgets.QSpinBox, 'spin_save_topk').value()}"
        else:
            radius = require_child(
                self._dialog,
                QtWidgets.QDoubleSpinBox,
                "spin_save_radius",
            )
            value = f"{radius.value():.{radius.decimals()}f}".rstrip("0").rstrip(".")
            parameter = f"Radius{value}"
        return f"{parameter}_NN_{metadata_id}"

    def _reset_neighbor_save_button(self, *_args: object) -> None:
        if self._dialog is None:
            return
        button = require_child(
            self._dialog,
            QtWidgets.QPushButton,
            "button_save_nn_group",
        )
        button.setText("Save as group")
        button.setToolTip("")

    def _save_current_neighbor_group(self) -> None:
        if (
            self._dialog is None
            or self._neighbor_results is None
            or self._save_neighbor_group is None
        ):
            return
        label_edit = require_child(self._dialog, QtWidgets.QLineEdit, "line_nn_group")
        label = label_edit.text().strip()
        if not label:
            label_edit.setFocus()
            label_edit.setToolTip("Enter a group label.")
            return
        member_ids = self._neighbor_member_ids_for_save()
        button = require_child(
            self._dialog,
            QtWidgets.QPushButton,
            "button_save_nn_group",
        )
        try:
            message = self._save_neighbor_group(label, member_ids)
        except Exception as exc:
            button.setText("Save failed")
            button.setToolTip(str(exc))
            return
        button.setText("Group saved")
        button.setToolTip(str(message))

    def _neighbor_member_ids_for_save(self) -> tuple[int, ...]:
        if self._dialog is None or self._neighbor_results is None:
            return ()
        if require_child(
            self._dialog,
            QtWidgets.QRadioButton,
            "radio_topk",
        ).isChecked():
            top_k = require_child(
                self._dialog,
                QtWidgets.QSpinBox,
                "spin_save_topk",
            ).value()
            matches = self._neighbor_results.matches[:top_k]
        else:
            radius = require_child(
                self._dialog,
                QtWidgets.QDoubleSpinBox,
                "spin_save_radius",
            ).value()
            if self._neighbor_results.metric == "cosine":
                matches = tuple(
                    match
                    for match in self._neighbor_results.matches
                    if match.score >= radius
                )
            else:
                matches = tuple(
                    match
                    for match in self._neighbor_results.matches
                    if match.score <= radius
                )
        return tuple(match.metadata_id for match in matches)

    def _populate_metadata(self, dialog: QtWidgets.QDialog, data: SelectionActionsDialogData) -> None:
        self._metadata_label = require_child(dialog, QtWidgets.QLabel, "label_metadata_thumbnail")
        self._metadata_media_stack = require_child(
            dialog,
            QtWidgets.QStackedWidget,
            "stack_metadata_media",
        )
        self._metadata_media_stack.setCurrentIndex(0)
        self._source_video_button = require_child(
            dialog,
            QtWidgets.QToolButton,
            "button_toggle_source_media",
        )
        self._source_video_slider = require_child(
            dialog,
            QtWidgets.QSlider,
            "slider_source_media_position",
        )
        self._source_video_time_label = require_child(
            dialog,
            QtWidgets.QLabel,
            "label_source_media_time",
        )
        self._source_media_controls = require_child(
            dialog,
            QtWidgets.QWidget,
            "panel_source_playback_controls",
        )
        self._source_video_status_label = require_child(
            dialog,
            QtWidgets.QLabel,
            "label_source_media_status",
        )
        self._source_file_button = require_child(
            dialog,
            QtWidgets.QPushButton,
            "button_show_file",
        )
        content_type = str(data.source_content_type).strip().lower()
        has_playback = content_type in {"audio", "video"}
        has_source_file = bool(
            data.source_file_path
            or data.source_file_unavailable_reason
        )
        source_file_available = bool(data.source_file_path) and not bool(
            data.source_file_unavailable_reason
        )
        self._source_file_button.setVisible(has_source_file)
        self._source_file_button.setEnabled(source_file_available)
        self._source_file_button.clicked.connect(self._reveal_source_file)
        self._source_media_controls.setVisible(has_playback)
        source_available = has_playback and source_file_available
        self._source_video_button.setEnabled(source_available)
        self._source_video_button.clicked.connect(self._toggle_source_video)
        self._source_video_slider.setEnabled(False)
        self._source_video_slider.setRange(0, 1000)
        self._source_video_slider.setValue(0)
        self._source_video_slider.sliderPressed.connect(
            self._handle_source_video_seek_started
        )
        self._source_video_slider.sliderMoved.connect(
            self._handle_source_video_seek_preview
        )
        self._source_video_slider.sliderReleased.connect(
            self._handle_source_video_seek_finished
        )
        self._source_video_time_label.setText("0:00 / 0:00")
        source_status = data.source_file_unavailable_reason
        self._source_video_status_label.setText(source_status)
        self._source_video_status_label.setVisible(
            bool(source_status)
        )
        metadata_tree = dialog.findChild(
            QtWidgets.QTreeWidget,
            "tree_metadata_excerpt",
        )
        if metadata_tree is None:
            metadata_tree = dialog.findChild(
                QtWidgets.QTreeWidget,
                "_treemetadata_excerpt",
            )
        if metadata_tree is None:
            raise RuntimeError(
                f"{dialog.objectName()} is missing 'tree_metadata_excerpt'"
            )
        metadata_tree.setObjectName("tree_metadata_excerpt")
        metadata_tree.clear()
        metadata_tree.setColumnCount(2)
        metadata_tree.setHeaderLabels(["Field", "Value"])
        metadata_tree.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
        metadata_tree.header().setSectionResizeMode(
            0,
            QtWidgets.QHeaderView.ResizeMode.ResizeToContents,
        )
        metadata_tree.header().setSectionResizeMode(
            1,
            QtWidgets.QHeaderView.ResizeMode.Stretch,
        )
        for field, value in data.metadata_rows:
            field_text = str(field)
            value_text = str(value)
            item = QtWidgets.QTreeWidgetItem([field_text, value_text])
            item.setToolTip(0, field_text)
            item.setToolTip(1, value_text)
            metadata_tree.addTopLevelItem(item)
        QtCore.QTimer.singleShot(0, self._request_initial_source_preview)

    def _request_initial_source_preview(self) -> None:
        if self._dialog is None or self._data is None:
            return
        content_type = str(self._data.source_content_type).strip().lower()
        if content_type == "video" and self._data.source_file_path:
            self._prepare_source_video_preview()
            return
        if content_type == "audio" and self._data.source_file_path:
            if self._metadata_label is not None:
                self._metadata_label.setText(
                    f"Audio source\n{Path(self._data.source_file_path).name}"
                )
            return
        self._request_original_source_preview()

    def _prepare_source_video_preview(self) -> None:
        if self._data is None or not self._data.source_file_path:
            return
        path = Path(self._data.source_file_path)
        if not path.is_file():
            self._show_source_video_error(
                "The linked source file is no longer available."
            )
            return
        if not self._ensure_source_video_player():
            return
        assert self._source_video_player is not None
        if self._source_video_loaded_path != path:
            self._source_video_player.setSource(
                QtCore.QUrl.fromLocalFile(str(path))
            )
            self._source_video_loaded_path = path
        if self._metadata_media_stack is not None:
            self._metadata_media_stack.setCurrentIndex(1)
        self._source_video_preview_pending = True
        if self._source_video_audio_output is not None:
            self._source_video_audio_output.setMuted(True)
        self._source_video_player.play()

    def _request_original_source_preview(self) -> None:
        self._pending.pop("metadata_source", None)
        if self._metadata_label is not None:
            self._metadata_label.setPixmap(QtGui.QPixmap())
            self._metadata_label.setText("Loading source preview…")
        if (
            self._data is None
            or not self._data.source_file_path
            or self._request_thumbnail is None
        ):
            if self._metadata_label is not None:
                self._metadata_label.setText("No linked source preview")
            return
        request = (
            self._data.metadata_id,
            "none",
            self.DETAIL_THUMBNAIL_SIZE,
        )
        self._pending["metadata_source"] = request
        self._request_thumbnail("metadata_source", *request, True)

    def _show_metadata_thumbnail(self, payload: DecodedThumbnail) -> None:
        if self._metadata_label is None:
            return
        image = QtGui.QImage(payload.rgba, payload.width, payload.height, payload.width * 4, QtGui.QImage.Format.Format_RGBA8888).copy()
        available = self._metadata_label.size().boundedTo(QtCore.QSize(payload.target_px, payload.target_px))
        pixmap = QtGui.QPixmap.fromImage(image).scaled(
            available,
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
        self._metadata_label.setText("")
        self._metadata_label.setPixmap(pixmap)

    def _toggle_source_video(self) -> None:
        if self._source_video_player is not None:
            state = self._source_video_player.playbackState()
            if (
                getattr(state, "name", "") == "PlayingState"
                and not self._source_video_preview_pending
            ):
                self._pause_source_video()
                return
        self._play_source_video()

    def _play_source_video(self) -> None:
        if self._data is None or not self._data.source_file_path:
            return
        path = Path(self._data.source_file_path)
        if not path.is_file():
            self._show_source_video_error(
                "The linked source file is no longer available."
            )
            return
        if not self._ensure_source_video_player():
            return
        assert self._source_video_player is not None
        self._source_video_preview_pending = False
        if self._source_video_audio_output is not None:
            self._source_video_audio_output.setMuted(False)
        if self._source_video_loaded_path != path:
            self._source_video_player.setSource(
                QtCore.QUrl.fromLocalFile(str(path))
            )
            self._source_video_loaded_path = path
        elif getattr(
            self._source_video_player.mediaStatus(),
            "name",
            "",
        ) == "EndOfMedia":
            self._source_video_player.setPosition(0)
        if (
            self._metadata_media_stack is not None
            and str(self._data.source_content_type).strip().lower() == "video"
        ):
            self._metadata_media_stack.setCurrentIndex(1)
        if self._source_video_status_label is not None:
            self._source_video_status_label.clear()
            self._source_video_status_label.setVisible(False)
        self._source_video_player.play()

    def _ensure_source_video_player(self) -> bool:
        if self._source_video_player is not None:
            return True
        if self._dialog is None:
            return False
        try:
            from PySide6 import QtMultimedia, QtMultimediaWidgets
        except ImportError:
            self._show_source_video_error(
                "Video playback is unavailable in this EIDORA installation."
            )
            return False
        host = require_child(
            self._dialog,
            QtWidgets.QWidget,
            "panel_source_video_player",
        )
        layout = require_child(
            self._dialog,
            QtWidgets.QVBoxLayout,
            "layout_source_video_player",
        )
        video_widget = QtMultimediaWidgets.QVideoWidget(host)
        video_widget.setAspectRatioMode(
            QtCore.Qt.AspectRatioMode.KeepAspectRatio
        )
        layout.addWidget(video_widget)
        audio_output = QtMultimedia.QAudioOutput(self._dialog)
        audio_output.setVolume(0.8)
        player = QtMultimedia.QMediaPlayer(self._dialog)
        player.setAudioOutput(audio_output)
        player.setVideoOutput(video_widget)
        player.positionChanged.connect(self._handle_source_video_position)
        player.durationChanged.connect(self._handle_source_video_duration)
        player.playbackStateChanged.connect(
            self._handle_source_video_playback_state
        )
        player.mediaStatusChanged.connect(self._handle_source_video_media_status)
        player.errorOccurred.connect(self._handle_source_video_player_error)
        video_widget.videoSink().videoFrameChanged.connect(
            self._handle_source_video_frame
        )
        self._source_video_player = player
        self._source_video_audio_output = audio_output
        self._source_video_widget = video_widget
        return True

    def _pause_source_video(self) -> None:
        if self._source_video_player is None:
            return
        try:
            self._source_video_player.pause()
        except RuntimeError:
            pass

    def _handle_source_video_playback_state(self, state: object) -> None:
        playing = (
            getattr(state, "name", "") == "PlayingState"
            and not self._source_video_preview_pending
        )
        if self._source_video_button is None:
            return
        label = "Pause media" if playing else "Play media"
        self._source_video_button.setText("")
        self._source_video_button.setToolTip(label)
        self._source_video_button.setAccessibleName(label)
        self._source_video_button.setProperty(
            "iconName",
            "animation_pause" if playing else "animation_play",
        )
        apply_icon_color(self._source_video_button)

    def _handle_source_video_frame(self, frame: object) -> None:
        if not self._source_video_preview_pending:
            return
        is_valid = getattr(frame, "isValid", None)
        if callable(is_valid) and not is_valid():
            return
        self._source_video_preview_pending = False
        self._pause_source_video()
        if self._source_video_audio_output is not None:
            self._source_video_audio_output.setMuted(False)
        if self._source_video_player is not None:
            self._handle_source_video_playback_state(
                self._source_video_player.playbackState()
            )

    def _handle_source_video_media_status(self, status: object) -> None:
        if getattr(status, "name", "") == "EndOfMedia":
            self._handle_source_video_playback_state(status)

    def _handle_source_video_player_error(
        self,
        _error: object,
        message: str,
    ) -> None:
        detail = str(message).strip() or "The source video could not be played."
        self._show_source_video_error(detail)

    def _handle_source_video_position(self, position_ms: int) -> None:
        if self._source_video_player is None:
            return
        duration = max(0, int(self._source_video_player.duration()))
        position = max(0, int(position_ms))
        if (
            self._source_video_slider is not None
            and not self._source_video_seeking
        ):
            value = int(round(position * 1000 / duration)) if duration else 0
            self._source_video_slider.setValue(value)
        self._update_source_video_time(position, duration)

    def _handle_source_video_duration(self, duration_ms: int) -> None:
        duration = max(0, int(duration_ms))
        if self._source_video_slider is not None:
            self._source_video_slider.setEnabled(duration > 0)
        position = (
            max(0, int(self._source_video_player.position()))
            if self._source_video_player is not None
            else 0
        )
        self._update_source_video_time(position, duration)

    def _handle_source_video_seek_started(self) -> None:
        self._source_video_seeking = True

    def _handle_source_video_seek_preview(self, value: int) -> None:
        if self._source_video_player is None:
            return
        duration = max(0, int(self._source_video_player.duration()))
        position = int(round(duration * max(0, min(1000, int(value))) / 1000))
        self._update_source_video_time(position, duration)

    def _handle_source_video_seek_finished(self) -> None:
        if self._source_video_player is not None and self._source_video_slider is not None:
            duration = max(0, int(self._source_video_player.duration()))
            self._source_video_player.setPosition(
                int(round(duration * self._source_video_slider.value() / 1000))
            )
        self._source_video_seeking = False

    def _update_source_video_time(self, position_ms: int, duration_ms: int) -> None:
        if self._source_video_time_label is not None:
            self._source_video_time_label.setText(
                f"{self._format_media_time(position_ms)} / "
                f"{self._format_media_time(duration_ms)}"
            )

    @staticmethod
    def _format_media_time(milliseconds: int) -> str:
        total_seconds = max(0, int(milliseconds)) // 1000
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return (
            f"{hours}:{minutes:02d}:{seconds:02d}"
            if hours
            else f"{minutes}:{seconds:02d}"
        )

    def _dispose_source_video_player(self) -> None:
        player = self._source_video_player
        self._source_video_player = None
        self._source_video_audio_output = None
        self._source_video_widget = None
        self._source_video_loaded_path = None
        self._source_video_seeking = False
        self._source_video_preview_pending = False
        if player is None:
            return
        try:
            player.stop()
            player.setVideoOutput(None)
            player.setAudioOutput(None)
            player.setSource(QtCore.QUrl())
        except RuntimeError:
            pass

    def _show_source_video_error(self, message: str) -> None:
        self._source_video_preview_pending = False
        if self._source_video_audio_output is not None:
            self._source_video_audio_output.setMuted(False)
        if self._source_video_status_label is not None:
            self._source_video_status_label.setText(str(message))
            self._source_video_status_label.setVisible(True)
        if self._dialog is not None:
            button = self._dialog.findChild(
                QtWidgets.QToolButton,
                "button_toggle_source_media",
            )
            if button is not None:
                button.setEnabled(False)
        if self._source_video_slider is not None:
            self._source_video_slider.setEnabled(False)
        if self._metadata_media_stack is not None:
            self._metadata_media_stack.setCurrentIndex(0)

    def _reveal_source_file(self) -> None:
        if self._data is None:
            return
        source = self._data.source_file_path
        if not source:
            return
        path = Path(source)
        if not path.is_file():
            self._show_source_file_error(
                "The linked source file is no longer available."
            )
            return
        try:
            revealed = (
                self._source_file_revealer(path)
                if self._source_file_revealer is not None
                else self._reveal_path_in_file_browser(path)
            )
        except (OSError, RuntimeError):
            revealed = False
        if not revealed:
            self._show_source_file_error(
                "The source file could not be shown in the system file browser."
            )

    def _show_source_file_error(self, message: str) -> None:
        if self._source_video_status_label is not None:
            self._source_video_status_label.setText(str(message))
            self._source_video_status_label.setVisible(True)
        if self._source_file_button is not None:
            self._source_file_button.setEnabled(False)

    @staticmethod
    def _reveal_path_in_file_browser(path: Path) -> bool:
        if sys.platform == "darwin":
            started = QtCore.QProcess.startDetached(
                "/usr/bin/open",
                ["-R", str(path)],
            )
        elif sys.platform == "win32":
            started = QtCore.QProcess.startDetached(
                "explorer.exe",
                ["/select,", QtCore.QDir.toNativeSeparators(str(path))],
            )
        else:
            return bool(
                QtGui.QDesktopServices.openUrl(
                    QtCore.QUrl.fromLocalFile(str(path.parent))
                )
            )
        return bool(started[0] if isinstance(started, tuple) else started)

    @staticmethod
    def _position_default(dialog: QtWidgets.QDialog, global_pos: QtCore.QPoint | None) -> None:
        dialog.adjustSize()
        size = dialog.sizeHint().expandedTo(dialog.minimumSizeHint())
        screen = (
            QtGui.QGuiApplication.screenAt(global_pos)
            if global_pos is not None
            else None
        ) or QtGui.QGuiApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        # Keep this substantial dialog in a predictable upper-left position;
        # use the click only to choose the monitor on multi-screen setups.
        anchor = QtCore.QPoint(available.left() + 32, available.top() + 32)
        dialog.move(
            min(
                max(anchor.x(), available.left()),
                max(available.left(), available.right() - size.width() + 1),
            ),
            min(
                max(anchor.y(), available.top()),
                max(available.top(), available.bottom() - size.height() + 1),
            ),
        )
