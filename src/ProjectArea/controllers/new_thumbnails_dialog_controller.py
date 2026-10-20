from __future__ import annotations

from uuid import uuid4

from PySide6 import QtCore, QtGui, QtWidgets

from ProjectArea.domain import (
    THUMBNAIL_RESIZE_MODES,
    THUMBNAIL_VIDEO_FRAME_MODES,
    ProcessInputKind,
    ProcessInputReference,
    ProcessRequest,
)
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class NewThumbnailsDialogController:
    """Collect one validated thumbnail-creation request."""

    def __init__(
        self,
        inputs: list[ProcessInputReference],
        existing_names: set[str],
        *,
        preselected_input: ProcessInputReference | None = None,
        initial_name: str = "",
        initial_parameters: dict[str, object] | None = None,
    ) -> None:
        self._inputs = list(inputs)
        self._existing_names = {name.strip().casefold() for name in existing_names}
        self._preselected = (
            preselected_input
            if isinstance(preselected_input, ProcessInputReference)
            else None
        )
        self._initial_name = initial_name
        self._parameters = dict(initial_parameters or {})
        self.dialog = load_ui("ProjectArea", "ui/new_thumbnails_dialog.ui", None)
        if not isinstance(self.dialog, QtWidgets.QDialog):
            raise TypeError("Create Thumbnails UI root must be a QDialog")
        self.dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self._input = require_child(self.dialog, QtWidgets.QComboBox, "combo_thumbnail_source")
        self._name = require_child(self.dialog, QtWidgets.QLineEdit, "edit_thumbnail_variant_name")
        self._availability = require_child(
            self.dialog, QtWidgets.QLabel, "label_thumbnail_availability"
        )
        self._resize = require_child(
            self.dialog, QtWidgets.QComboBox, "combo_thumbnail_resize_mode"
        )
        self._frame = require_child(
            self.dialog, QtWidgets.QComboBox, "combo_video_frame_mode"
        )
        self._frame_label = require_child(
            self.dialog, QtWidgets.QLabel, "label_video_frame_mode"
        )
        self._start = require_child(
            self.dialog, QtWidgets.QPushButton, "button_run_thumbnail_processing"
        )
        self._cancel = require_child(
            self.dialog, QtWidgets.QPushButton, "button_cancel_thumbnail_creation"
        )
        self._populate()
        self._connect()
        self._refresh()

    def exec(self) -> ProcessRequest | None:
        try:
            accepted = self.dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted
        finally:
            self._disconnect()
        if not accepted:
            return None
        selected = self._selected_input()
        if selected is None:
            return None
        return ProcessRequest(
            operation_id="create_thumbnails",
            input=selected,
            output_id=uuid4().hex,
            output_name=self._name.text().strip(),
            parameters={
                "resize_mode": str(self._resize.currentData() or "original_aspect"),
                "video_frame_mode": str(self._frame.currentData() or "middle"),
            },
        )

    def _populate(self) -> None:
        for group_label, kind in (
            ("Media Sources", ProcessInputKind.MEDIA_SOURCE),
            ("Thumbnails", ProcessInputKind.ARTIFACT),
        ):
            members = [candidate for candidate in self._inputs if candidate.kind == kind]
            if not members:
                continue
            self._input.addItem(group_label)
            header = self._input.model().item(self._input.count() - 1)
            if isinstance(header, QtGui.QStandardItem):
                header.setEnabled(False)
            for candidate in members:
                self._input.addItem(
                    f"  {candidate.label} ({candidate.item_count:,})",
                    candidate,
                )
        selected_index = -1
        if self._preselected is not None:
            selected_index = next(
                (
                    index
                    for index in range(self._input.count())
                    if isinstance((value := self._input.itemData(index)), ProcessInputReference)
                    and value.kind == self._preselected.kind
                    and value.id == self._preselected.id
                ),
                -1,
            )
        if selected_index < 0:
            selected_index = next(
                (
                    index
                    for index in range(self._input.count())
                    if isinstance(self._input.itemData(index), ProcessInputReference)
                ),
                -1,
            )
        self._input.setCurrentIndex(selected_index)
        for label, value in THUMBNAIL_RESIZE_MODES:
            self._resize.addItem(label, value)
        for label, value in THUMBNAIL_VIDEO_FRAME_MODES:
            self._frame.addItem(label, value)
        self._resize.setCurrentIndex(
            max(0, self._resize.findData(self._parameters.get("resize_mode", "original_aspect")))
        )
        self._frame.setCurrentIndex(
            max(0, self._frame.findData(self._parameters.get("video_frame_mode", "middle")))
        )
        if self._initial_name:
            self._name.setText(self._initial_name)
        else:
            self._set_default_name()

    def _connect(self) -> None:
        self._input.currentIndexChanged.connect(self._input_changed)
        self._name.textChanged.connect(self._refresh)
        self._resize.currentIndexChanged.connect(self._refresh)
        self._frame.currentIndexChanged.connect(self._refresh)
        self._start.clicked.connect(self.dialog.accept)
        self._cancel.clicked.connect(self.dialog.reject)

    def _disconnect(self) -> None:
        for signal, slot in (
            (self._input.currentIndexChanged, self._input_changed),
            (self._name.textChanged, self._refresh),
            (self._resize.currentIndexChanged, self._refresh),
            (self._frame.currentIndexChanged, self._refresh),
            (self._start.clicked, self.dialog.accept),
            (self._cancel.clicked, self.dialog.reject),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def _selected_input(self) -> ProcessInputReference | None:
        value = self._input.currentData()
        return value if isinstance(value, ProcessInputReference) else None

    def _input_changed(self, _index: int) -> None:
        self._set_default_name()
        self._refresh()

    def _set_default_name(self) -> None:
        selected = self._selected_input()
        if selected is None:
            return
        base = f"{selected.label} (thumbs)"
        candidate = base
        suffix = 2
        while candidate.casefold() in self._existing_names:
            candidate = f"{base} {suffix}"
            suffix += 1
        self._name.setText(candidate)

    def _refresh(self, *_args: object) -> None:
        selected = self._selected_input()
        if not self._inputs:
            self._availability.setText(
                "No eligible input is available. Link an image or video source to metadata first."
            )
        elif selected is None:
            self._availability.setText("Choose an eligible media source or thumbnail set.")
        elif selected.kind == ProcessInputKind.ARTIFACT:
            self._availability.setText(
                "The largest stored thumbnail is used for each metadata ID."
            )
        else:
            self._availability.setText("Linked media files will be used as input.")
        is_video = bool(selected and selected.media_type == "video")
        self._frame.setVisible(is_video)
        self._frame_label.setVisible(is_video)
        name = self._name.text().strip()
        self._start.setEnabled(
            selected is not None
            and bool(name)
            and name.casefold() not in self._existing_names
        )
