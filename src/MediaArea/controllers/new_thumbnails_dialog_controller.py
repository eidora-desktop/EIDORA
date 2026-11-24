from __future__ import annotations

from uuid import uuid4

from PySide6 import QtCore, QtGui, QtWidgets

from MediaArea.domain import (
    ProcessInputKind,
    ProcessInputReference,
    ProcessRequest,
)
from MediaArea.preview_names import default_preview_name
from MediaArea.clip_options import PREVIEW_KINDS
from MediaArea.controllers.preview_parameters_panel_controller import PreviewParametersPanelController
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
        fixed_input: bool = False,
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
        if fixed_input:
            self._inputs = [candidate for candidate in self._inputs if candidate == self._preselected]
        self._initial_name = initial_name
        self._parameters = dict(initial_parameters or {})
        self.dialog = load_ui("MediaArea", "ui/new_thumbnails_dialog.ui", None)
        if not isinstance(self.dialog, QtWidgets.QDialog):
            raise TypeError("Create Preview Assets UI root must be a QDialog")
        self.dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self._input = require_child(self.dialog, QtWidgets.QComboBox, "combo_thumbnail_source")
        self._name = require_child(self.dialog, QtWidgets.QLineEdit, "edit_thumbnail_variant_name")
        self._availability = require_child(
            self.dialog, QtWidgets.QLabel, "label_thumbnail_availability"
        )
        self._error = require_child(self.dialog, QtWidgets.QLabel, "label_error")
        self._start = require_child(
            self.dialog, QtWidgets.QPushButton, "button_run_thumbnail_processing"
        )
        self._cancel = require_child(
            self.dialog, QtWidgets.QPushButton, "button_cancel_thumbnail_creation"
        )
        fixed_source = require_child(self.dialog, QtWidgets.QLabel, "label_fixed_thumbnail_source")
        fixed_source.setVisible(fixed_input)
        self._input.setVisible(not fixed_input)
        require_child(self.dialog, QtWidgets.QWidget, "panel_thumbnail_input").setVisible(not fixed_input)
        require_child(self.dialog, QtWidgets.QLabel, "label_preview_title").setVisible(not fixed_input)
        if fixed_input and self._preselected is not None:
            fixed_source.setText(self._preselected.label)
            fixed_source.setToolTip(self._preselected.label)
        self._kind = require_child(self.dialog, QtWidgets.QComboBox, "combo_preview_kind")
        self._parameter_panel = PreviewParametersPanelController(self.dialog)
        self._populate()
        self._connect()
        self._refresh()
        self._fit_dialog_height()

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
            parameters=self._request_parameters(),
        )

    def _populate(self) -> None:
        for group_label, kind in (
            ("Media Sources", ProcessInputKind.MEDIA_SOURCE),
            ("Preview Assets", ProcessInputKind.ARTIFACT),
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
        self._populate_processing()
        if self._initial_name:
            self._name.setText(self._initial_name)
        else:
            self._set_default_name()

    def _connect(self) -> None:
        self._connections = [
            (self._kind.currentIndexChanged, self._kind_changed),
            (self._input.currentIndexChanged, self._input_changed),
            (self._name.textChanged, self._refresh),
            (self._parameter_panel.changed, self._refresh),
            (self._parameter_panel.geometry_changed, self._fit_dialog_height),
            (self._start.clicked, self.dialog.accept),
            (self._cancel.clicked, self.dialog.reject),
        ]
        for signal, slot in self._connections:
            signal.connect(slot)

    def _disconnect(self) -> None:
        self._parameter_panel.disconnect()
        for signal, slot in self._connections:
            signal.disconnect(slot)
        self._connections.clear()

    def _selected_input(self) -> ProcessInputReference | None:
        value = self._input.currentData()
        return value if isinstance(value, ProcessInputReference) else None

    def _input_changed(self, _index: int) -> None:
        self._parameters = {}
        self._populate_processing()
        self._set_default_name()
        self._refresh()
        self._fit_dialog_height()

    def _set_default_name(self) -> None:
        selected = self._selected_input()
        if selected is None:
            return
        self._name.setText(default_preview_name(
            selected.label, selected.media_type, preview_kind=str(self._kind.currentData()), existing_names=self._existing_names,
        ))

    def _fit_dialog_height(self) -> None:
        self.dialog.ensurePolished()
        form = require_child(self.dialog, QtWidgets.QWidget, "panel_preview_form")
        footer = require_child(self.dialog, QtWidgets.QWidget, "panel_footer")
        for layout in reversed(form.findChildren(QtWidgets.QLayout)):
            layout.invalidate()
            layout.activate()
        footer.layout().activate()
        form_height = form.layout().totalHeightForWidth(self.dialog.width())
        height = (form_height if form_height >= 0 else form.sizeHint().height()) + footer.sizeHint().height()
        screen = self.dialog.screen()
        if screen is not None:
            height = min(height, max(300, screen.availableGeometry().height() - 80))
        self.dialog.resize(self.dialog.width(), height)

    def _refresh(self, *_args: object) -> None:
        selected = self._selected_input()
        if not self._inputs:
            self._availability.setText(
                "No eligible input is available. Link an image, video, audio, or text source to Items first."
            )
        elif selected is None:
            self._availability.setText("Choose an eligible media source or existing Preview Assets.")
        elif selected.kind == ProcessInputKind.ARTIFACT:
            self._availability.setText(
                "The largest stored Preview Asset is used for each Item."
            )
        else:
            description = PREVIEW_KINDS.get(self._kind.currentData()) or {
                "image": "Image thumbnails", "video": "Video frames",
                "audio": "Audio clips", "text": "Text excerpts"}[selected.media_type]
            self._availability.setText(f"{description} · {selected.item_count:,} linked files")
        name = self._name.text().strip()
        error = ""
        if selected is not None:
            if not name:
                error = "Enter an output name."
            elif name.casefold() in self._existing_names:
                error = "That name is already used by Preview Assets or a pending job."
        self._error.setText(error)
        self._error.setVisible(bool(error))
        self._start.setEnabled(selected is not None and not error)

    def _populate_processing(self):
        selected = self._selected_input()
        media = selected.media_type if selected else "image"
        choices = [("Image thumbnails", "image")]
        if media == "video":
            choices = [("Still frames", "image"), ("Video clips", "video_clip"), ("Animated GIFs", "animated_gif")]
        elif media == "audio":
            choices = [("Audio clips", "audio_clip")]
        elif media == "text":
            choices = [("Text excerpts", "text_excerpt")]
        with QtCore.QSignalBlocker(self._kind):
            self._kind.clear()
            for label, kind in choices:
                self._kind.addItem(label, kind)
            self._kind.setCurrentIndex(max(0, self._kind.findData(self._parameters.get("preview_kind", choices[0][1]))))
        self._parameter_panel.configure(self._kind.currentData(), media, self._parameters)

    def _kind_changed(self, _index):
        selected = self._selected_input()
        self._parameter_panel.configure(self._kind.currentData(), selected.media_type if selected else "image")
        self._set_default_name()
        self._refresh()
        self._fit_dialog_height()

    def _request_parameters(self):
        return self._parameter_panel.parameters()
