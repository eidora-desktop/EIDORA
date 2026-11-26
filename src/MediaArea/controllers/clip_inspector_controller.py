"""Inspect one generated clip at a time; no decoder is retained after closing."""
from PySide6 import QtCore, QtGui, QtWidgets

from MediaArea.clip_inspection_service import ClipInspectionService
from shared.qt.ui_loader import load_ui
from shared.qt.widgets import require_child


class ClipInspectorController:
    def __init__(self, repository, variant_id, label, parameters):
        self._service = ClipInspectionService(repository, variant_id)
        self._parameters = parameters
        self._kind = parameters["preview_kind"]
        self.dialog = load_ui("MediaArea", "ui/clip_inspector.ui", None)
        self.dialog.setWindowTitle(f"Inspect {label}")
        self.dialog.setWindowModality(QtCore.Qt.WindowModality.ApplicationModal)
        self._connections = []
        self._player = self._audio = self._movie = None
        self._clip = None
        self._stack = require_child(self.dialog, QtWidgets.QStackedWidget, "stack_preview")
        self._poster = require_child(self.dialog, QtWidgets.QLabel, "label_poster")
        self._status = require_child(self.dialog, QtWidgets.QLabel, "label_status")
        self._play = require_child(self.dialog, QtWidgets.QPushButton, "button_play")
        self._mute = require_child(self.dialog, QtWidgets.QCheckBox, "check_mute")
        self._slider = require_child(self.dialog, QtWidgets.QSlider, "slider_position")
        self._previous = require_child(self.dialog, QtWidgets.QPushButton, "button_previous")
        self._next = require_child(self.dialog, QtWidgets.QPushButton, "button_next")
        self._connect(self._play.clicked, self._toggle_play)
        self._connect(self._previous.clicked, lambda: self._navigate(True))
        self._connect(self._next.clicked, lambda: self._navigate(False))
        self._connect(require_child(self.dialog, QtWidgets.QPushButton, "button_close").clicked, self.dialog.accept)
        self._connect(self.dialog.finished, self._stop)
        self._mute.setVisible(self._kind != "animated_gif")
        self._slider.setVisible(self._kind != "animated_gif")
        self._load(self._service.adjacent())

    def _connect(self, signal, callback):
        signal.connect(callback)
        self._connections.append((signal, callback))

    def exec(self):
        try:
            self.dialog.exec()
        finally:
            self._stop()
            for signal, callback in self._connections:
                signal.disconnect(callback)
            self._connections.clear()

    def _ensure_player(self):
        if self._player is not None:
            return
        from PySide6 import QtMultimedia
        self._player = QtMultimedia.QMediaPlayer(self.dialog)
        self._audio = QtMultimedia.QAudioOutput(self.dialog)
        self._audio.setMuted(self._mute.isChecked())
        self._player.setAudioOutput(self._audio)
        self._connect(self._mute.toggled, self._audio.setMuted)
        self._connect(self._player.durationChanged, lambda duration: self._slider.setRange(0, duration))
        self._connect(self._player.positionChanged, self._update_position)
        self._connect(self._slider.sliderMoved, self._player.setPosition)
        self._connect(self._player.playbackStateChanged, lambda state: self._play.setText(
            "Pause" if state == QtMultimedia.QMediaPlayer.PlaybackState.PlayingState else "Play"))
        self._connect(self._player.errorOccurred, lambda _code, message: self._status.setText(f"Playback unavailable: {message}"))
        if self._kind == "video_clip":
            from PySide6.QtMultimediaWidgets import QVideoWidget
            video = QVideoWidget(self.dialog)
            require_child(self.dialog, QtWidgets.QVBoxLayout, "layout_video").addWidget(video)
            self._player.setVideoOutput(video)

    def _update_position(self, position):
        if not self._slider.isSliderDown():
            self._slider.setValue(position)

    def _stop(self, *_args):
        if self._player is not None:
            self._player.stop()
            self._player.setSource(QtCore.QUrl())
        if self._movie is not None:
            self._movie.stop()
            self._poster.setMovie(None)
            self._movie.deleteLater()
            self._movie = None
        self._play.setText("Play")

    def _navigate(self, previous):
        if self._clip:
            self._load(self._service.adjacent(self._clip.metadata_id, previous=previous))

    def _load(self, clip):
        self._stop()
        self._clip = clip
        self._stack.setCurrentIndex(0)
        self._play.setEnabled(False)
        if clip is None:
            self._status.setText("No generated clips are available.")
            return
        self._previous.setEnabled(self._service.adjacent(clip.metadata_id, previous=True) is not None)
        self._next.setEnabled(self._service.adjacent(clip.metadata_id) is not None)
        require_child(self.dialog, QtWidgets.QLabel, "label_item").setText(
            f"Item {clip.metadata_id} · {clip.duration_seconds:.2f} s · {clip.size_bytes / 1024:.1f} KiB")
        try:
            self._path, poster = self._service.paths(clip)
            if not self._path.is_file():
                raise FileNotFoundError("The generated preview file is missing.")
            self._poster.setPixmap(QtGui.QPixmap(str(poster)))
            self._status.setText(f"Source excerpt starts at {clip.start_seconds:.2f} s.")
            self._play.setEnabled(True)
            if self._kind != "animated_gif":
                self._ensure_player()
                self._slider.setRange(0, round(clip.duration_seconds * 1000))
                self._player.setSource(QtCore.QUrl.fromLocalFile(str(self._path)))
        except (OSError, ValueError, ImportError) as exc:
            self._status.setText(str(exc))
            self._play.setEnabled(False)

    def _toggle_play(self):
        if self._kind == "animated_gif":
            if self._movie is None:
                self._movie = QtGui.QMovie(str(self._path), parent=self.dialog)
                # CacheNone keeps inspection bounded; the file carries GIF loop semantics.
                self._poster.setMovie(self._movie)
                self._movie.finished.connect(lambda: self._play.setText("Play"))
            if self._movie.state() == QtGui.QMovie.MovieState.Running:
                self._movie.setPaused(True)
                self._play.setText("Play")
            else:
                self._movie.start()
                self._movie.setPaused(False)
                self._play.setText("Pause")
        elif self._player is not None:
            from PySide6 import QtMultimedia
            if self._player.playbackState() == QtMultimedia.QMediaPlayer.PlaybackState.PlayingState:
                self._player.pause()
            else:
                if self._kind == "video_clip":
                    self._stack.setCurrentIndex(1)
                self._player.play()
