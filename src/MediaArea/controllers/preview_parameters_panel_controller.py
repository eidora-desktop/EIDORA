"""Bind basic/advanced preview tables without owning generation or persistence."""
from PySide6 import QtCore, QtWidgets

from MediaArea.domain import THUMBNAIL_RESIZE_MODES, THUMBNAIL_VIDEO_FRAME_MODES
from MediaArea.clip_options import LOOP_MODES, POSITIONS, clip_parameters, compression_description
from MediaArea.thumbnail_options import image_parameters
from MediaArea.text_options import text_parameters, text_description
from shared.qt.widgets import require_child

# key, label, object name, default, choices or numeric range, explanation
BASIC = (
    ("character_limit", "Maximum characters", "spin_text_character_limit", 400, (1, 10000, ""), "Maximum excerpt length; shorter texts appear in full."),
    ("video_frame_mode", "Frame selection", "combo_video_frame_mode", "middle", THUMBNAIL_VIDEO_FRAME_MODES, "Which still frame to extract from each video."),
    ("resize_mode", "Image sizing", "combo_thumbnail_resize_mode", "original_aspect", THUMBNAIL_RESIZE_MODES, "Preserve proportions, crop to a square, or squeeze into a square."),
    ("duration", "Duration", "spin_clip_duration", 5, (1, 30, " s"), "Requested excerpt length; shorter sources use their available duration."),
    ("position", "Position", "combo_clip_position", "middle", tuple((label, value) for value, label in POSITIONS.items()), "Where to select the excerpt within each source."),
    ("mute_video", "Mute video clip", "check_clip_audio", False, None, "Omit the audio track from generated video clips."),
    ("loop", "Loop", "combo_clip_loop", "forever", tuple((label, value) for value, label in LOOP_MODES.items()), "GIF only. Ping-Pong repeats forward and backward without duplicating endpoint frames."),
)
ADVANCED = (
    ("text_boundary", "Text boundary", "combo_text_boundary", "word", (("Prefer complete words", "word"), ("Character cut-off", "character")), "Trim partial words within the limit. Long unbroken text falls back to a character cut-off."),
    ("image_quality", "Image quality", "spin_image_quality", 85, (1, 100, ""), "Higher WebP quality usually produces larger files. Existing previews use 85."),
    ("video_quality", "Video quality", "combo_video_quality", "compact", (("Compact", "compact"), ("Balanced", "balanced"), ("High", "high")), "Higher quality preserves more detail and usually produces larger files."),
    ("video_fps", "Maximum frame rate", "spin_video_fps", 24, (1, 24, " fps"), "Never increases a lower source frame rate."),
    ("gif_fps", "Maximum frame rate", "spin_gif_fps", 12, (1, 24, " fps"), "Fewer frames usually reduce GIF size. Never increases a lower source rate."),
    ("max_edge", "Maximum edge", "combo_max_edge", 256, tuple((f"{value} px", value) for value in (128, 192, 256)), "Longest edge; preserve aspect ratio and never upscale."),
    ("audio_bitrate", "Audio bitrate", "combo_audio_bitrate", 48, tuple((f"{value} kbit/s", value) for value in (32, 48, 64, 96, 128)), "Total audio bitrate across channels. Higher values usually improve fidelity and increase size."),
    ("audio_channels", "Audio channels", "combo_audio_channels", "mono", (("Mono", "mono"), ("Stereo", "stereo")), "Stereo preserves stereo sources; mono sources remain mono."),
    ("gif_colors", "Palette", "combo_gif_colors", 128, tuple((f"{value} colors", value) for value in (64, 128, 256)), "Fewer colors can reduce GIF size at the expense of color detail."),
    ("gif_dither", "Dithering", "combo_gif_dither", "none", (("Off", "none"), ("On", "floyd_steinberg")), "Dithering smooths gradients but can increase GIF size."),
    ("random_seed", "Random seed", "spin_random_seed", 0, (0, 2147483647, ""), "Keep the seed to reproduce random selections; change it to select different excerpts or frames."),
)


class PreviewParametersPanelController(QtCore.QObject):
    changed = QtCore.Signal()
    geometry_changed = QtCore.Signal()

    def __init__(self, dialog):
        super().__init__(dialog)
        self._kind, self._media = "image", "image"
        self._editors, self._rows, self._connections = {}, {}, []
        self._basic = require_child(dialog, QtWidgets.QTableWidget, "table_basic_parameters")
        self._advanced = require_child(dialog, QtWidgets.QTableWidget, "table_advanced_parameters")
        self._toggle = require_child(dialog, QtWidgets.QToolButton, "button_advanced_settings")
        self._reset = require_child(dialog, QtWidgets.QPushButton, "button_reset_parameters")
        self._summary = require_child(dialog, QtWidgets.QLabel, "label_parameter_summary")
        for table, specs in ((self._basic, BASIC), (self._advanced, ADVANCED)):
            table.setColumnCount(2)
            table.setHorizontalHeaderLabels(["Parameter", "Value"])
            table.setRowCount(len(specs))
            table.horizontalHeader().setStretchLastSection(True)
            table.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            table.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            table.setColumnWidth(0, 210)
            for row, (key, label, name, default, choices, tip) in enumerate(specs):
                item = QtWidgets.QTableWidgetItem(label)
                item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
                item.setToolTip(tip)
                table.setItem(row, 0, item)
                if choices is None:
                    editor = QtWidgets.QCheckBox()
                    signal = editor.toggled
                elif isinstance(choices[0], int):
                    editor = QtWidgets.QSpinBox()
                    editor.setRange(choices[0], choices[1])
                    editor.setSuffix(choices[2])
                    signal = editor.valueChanged
                else:
                    editor = QtWidgets.QComboBox()
                    for text, value in choices:
                        editor.addItem(text, value)
                    signal = editor.currentIndexChanged
                editor.setObjectName(name)
                editor.setToolTip(tip)
                editor.setAccessibleName(label)
                table.setCellWidget(row, 1, editor)
                self._editors[key] = editor
                self._rows[key] = (table, row)
                self._set_value(editor, default)
                self._connect(signal, self._changed)
        self._connect(self._toggle.toggled, self._expand)
        self._connect(self._reset.clicked, self.reset)

    def _connect(self, signal, slot):
        signal.connect(slot)
        self._connections.append((signal, slot))

    def disconnect(self):
        for signal, slot in self._connections:
            signal.disconnect(slot)
        self._connections.clear()

    def configure(self, kind, media, parameters=None):
        self._kind, self._media = kind, media
        parameters = dict(parameters or {})
        if kind == "text_excerpt":
            parameters.setdefault("position", "first")
        parameters["mute_video"] = not bool(parameters.get("include_audio", True))
        for key, _label, _name, default, _choices, _tip in (*BASIC, *ADVANCED):
            self._set_value(self._editors[key], parameters.get(key, default))
        with QtCore.QSignalBlocker(self._toggle):
            self._toggle.setChecked(False)
        self._changed()

    def reset(self):
        expanded = self._toggle.isChecked()
        self.configure(self._kind, self._media)
        self._toggle.setChecked(expanded)

    @staticmethod
    def _set_value(editor, value):
        with QtCore.QSignalBlocker(editor):
            if isinstance(editor, QtWidgets.QComboBox):
                editor.setCurrentIndex(max(0, editor.findData(value)))
            elif isinstance(editor, QtWidgets.QCheckBox):
                editor.setChecked(bool(value))
            else:
                editor.setValue(int(value))

    def _values(self):
        result = {}
        for key, editor in self._editors.items():
            result[key] = (editor.currentData() if isinstance(editor, QtWidgets.QComboBox)
                           else editor.isChecked() if isinstance(editor, QtWidgets.QCheckBox) else editor.value())
        return result

    def parameters(self):
        values = self._values()
        if self._kind == "text_excerpt":
            return text_parameters(values)
        if self._kind == "image":
            result = image_parameters(values)
            result["resize_mode"] = values["resize_mode"]
            if self._media == "video":
                result["video_frame_mode"] = values["video_frame_mode"]
            return result
        values.update(preview_kind=self._kind, include_audio=not values.pop("mute_video"))
        return clip_parameters(values, self._media)

    def _changed(self, *_args):
        values = self._values()
        if self._kind == "text_excerpt":
            visible = {"character_limit", "position", "text_boundary"}
            if values["position"] == "random":
                visible.add("random_seed")
        elif self._kind == "image":
            visible = {"resize_mode", "image_quality"}
            if self._media == "video":
                visible.add("video_frame_mode")
                if values["video_frame_mode"] == "random":
                    visible.add("random_seed")
        else:
            visible = {"duration", "position"}
            if values["position"] == "random":
                visible.add("random_seed")
            if self._kind == "video_clip":
                visible.update(("mute_video", "video_quality", "video_fps", "max_edge"))
            if self._kind == "audio_clip" or (self._kind == "video_clip" and not values["mute_video"]):
                visible.update(("audio_bitrate", "audio_channels"))
            if self._kind == "animated_gif":
                visible.update(("loop", "gif_fps", "max_edge", "gif_colors", "gif_dither"))
        for key, (table, row) in self._rows.items():
            table.setRowHidden(row, key not in visible)
            self._editors[key].setVisible(key in visible)
        parameters = self.parameters()
        summary = (f"WebP · Quality {parameters['image_quality']} · Output sizes: 32, 64, 128 and 256 px"
                   if self._kind == "image" else text_description(parameters)
                   if self._kind == "text_excerpt" else compression_description(parameters))
        if parameters.get("loop") == "ping_pong":
            summary += "\nPing-Pong plays forward and backward, then repeats."
        self._summary.setText(summary)
        self._expand(self._toggle.isChecked())
        self.changed.emit()

    def _expand(self, expanded):
        self._advanced.setVisible(expanded)
        self._toggle.setArrowType(QtCore.Qt.ArrowType.DownArrow if expanded else QtCore.Qt.ArrowType.RightArrow)
        for table in (self._basic, self._advanced):
            height = table.horizontalHeader().sizeHint().height() + 2 * table.frameWidth()
            for row in range(table.rowCount()):
                if not table.isRowHidden(row):
                    editor = table.cellWidget(row, 1)
                    row_height = max(32, editor.sizeHint().height()+4)
                    table.setRowHeight(row, row_height)
                    height += row_height
            table.setFixedHeight(height)
        self.geometry_changed.emit()
