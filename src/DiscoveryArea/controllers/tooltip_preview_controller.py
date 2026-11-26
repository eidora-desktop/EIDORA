"""Playback owned by the visible tooltip, independent of the canvas renderer."""
from pathlib import Path
from queue import Empty

from PySide6 import QtCore, QtGui, QtWidgets

from DiscoveryArea.tooltip_preview_service import TooltipPreviewLoader, TooltipPreviewSession


class _AudioPlayhead(QtWidgets.QWidget):
    """One runtime marker over the unchanged, Designer-owned waveform label."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.fillRect(event.rect(), QtGui.QColor("#ff3b30"))


class TooltipPreviewController(QtCore.QObject):
    geometry_changed = QtCore.Signal()
    HOVER_DELAY_MS = 200

    def __init__(self, popup):
        super().__init__(popup)
        self._popup = popup
        self._loader = TooltipPreviewLoader()
        self._session = None
        self._key = None
        self._prepared = None
        self._player = self._audio = self._video = None
        self._last_video_frame = None
        self._video_finished = False
        self._loop = False
        self._playhead = None
        self._audio_finished = False
        self._playhead_timer = QtCore.QTimer(self)
        self._playhead_timer.setInterval(40)  # At most 25 updates/second, only while playing.
        self._playhead_timer.timeout.connect(self._update_audio_playhead)
        self._delay = QtCore.QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.setInterval(self.HOVER_DELAY_MS)
        self._delay.timeout.connect(self._start_playback)
        self._poll_timer = QtCore.QTimer(self)
        self._poll_timer.setInterval(15)
        self._poll_timer.timeout.connect(self._poll)
        popup.installEventFilter(self)
        self._window = popup.parentWidget().window()
        self._window.installEventFilter(self)
        loader = self._loader
        self.destroyed.connect(lambda: loader.shutdown())

    def show_preview(self, project_root, variant_id, metadata_id, kind, size, *, loop, autoplay):
        key = ((Path(project_root), variant_id, metadata_id, kind) if kind == "text_excerpt"
               else (Path(project_root), variant_id, metadata_id, kind, size, loop, autoplay))
        if key == self._key:
            return  # Moving/resizing unrelated UI must not restart a clip.
        self.stop()
        self._key = key
        self._loop = loop
        if kind == "text_excerpt":
            self._popup.set_text_preview_message("Loading text excerpt…")
        else:
            self._popup.set_thumbnail_size(size)
            self._popup.set_placeholder()
        self._session = TooltipPreviewSession(Path(project_root), variant_id, metadata_id, kind, size, autoplay)
        self._loader.submit(self._session)
        self._poll_timer.start()
        if autoplay and kind != "text_excerpt":
            self._delay.start()

    def _poll(self):
        session = self._session
        if session is None:
            return
        if not self._popup.isVisible():
            self.stop()
            return
        for _ in range(2):
            try:
                kind, value = session.messages.get_nowait()
            except Empty:
                break
            if kind == "text":
                self._popup.set_text_preview(value)
                self.geometry_changed.emit()
            elif kind == "prepared":
                path, poster, available = value
                if not poster.isNull():
                    self._popup.set_thumbnail_image(poster)
                    self.geometry_changed.emit()
                if available:
                    self._prepared = path
                    if session.autoplay and not self._delay.isActive():
                        self._start_playback()
            elif kind == "frame":
                self._popup.set_thumbnail_image(value)
            elif kind == "error":
                self._failed(session, value)
                return
        if session.done.is_set() and session.messages.empty():
            self._poll_timer.stop()

    def _start_playback(self):
        session = self._session
        if session is None or not session.autoplay or self._prepared is None:
            return
        if not self._popup.isVisible():
            self.stop()
            return
        if session.kind == "animated_gif":
            session.play.set()
            return
        if self._player is not None:
            return
        try:
            # Keep multimedia plugins out of startup and static-image hovering.
            from PySide6 import QtMultimedia
            self._player = QtMultimedia.QMediaPlayer(self)
            self._audio = QtMultimedia.QAudioOutput(self)
            self._player.setAudioOutput(self._audio)
            self._player.setLoops(QtMultimedia.QMediaPlayer.Loops.Infinite if self._loop else 1)
            self._player.errorOccurred.connect(lambda _code, message: self._failed(session, message))
            if session.kind == "video_clip":
                from PySide6.QtMultimediaWidgets import QVideoWidget
                # One runtime video surface inside the Designer-owned preview.
                self._video = QVideoWidget(self._popup.preview_label)
                self._video.setGeometry(self._popup.preview_label.rect())
                self._video.hide()
                self._video.setAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                self._player.setVideoOutput(self._video)
                self._video.videoSink().videoFrameChanged.connect(
                    lambda frame: self._show_video_frame(session, frame))
                self._player.mediaStatusChanged.connect(
                    lambda status: self._handle_video_status(session, status))
            elif session.kind == "audio_clip":
                self._playhead = _AudioPlayhead(self._popup.preview_label)
                self._player.playbackStateChanged.connect(
                    lambda state: self._handle_audio_state(session, state))
                self._player.mediaStatusChanged.connect(
                    lambda status: self._handle_audio_status(session, status))
            player = self._player
            player.setSource(QtCore.QUrl.fromLocalFile(str(self._prepared)))
            if self._session is session and self._player is player:
                player.play()
        except (ImportError, RuntimeError) as exc:
            self._failed(session, str(exc))

    def _handle_audio_state(self, session, state):
        from PySide6.QtMultimedia import QMediaPlayer
        if self._session is not session or self._playhead is None:
            return
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._audio_finished = False
            self._playhead_timer.start()
        else:
            self._playhead_timer.stop()
        self._update_audio_playhead()

    def _handle_audio_status(self, session, status):
        from PySide6.QtMultimedia import QMediaPlayer
        if (self._session is not session or self._playhead is None or self._loop
                or status != QMediaPlayer.MediaStatus.EndOfMedia):
            return
        self._audio_finished = True
        self._playhead_timer.stop()
        self._update_audio_playhead()

    def _update_audio_playhead(self):
        if self._playhead is None or self._player is None:
            return
        label = self._popup.preview_label
        duration = self._player.duration()
        if duration <= 0 or label.isHidden() or label.pixmap().isNull():
            self._playhead.hide()
            return
        progress = 1.0 if self._audio_finished else max(0.0, min(1.0, self._player.position() / duration))
        width = min(2, label.width())
        x = round(progress * (label.width() - width))
        self._playhead.setGeometry(x, 0, width, label.height())
        self._playhead.show()

    def _show_video_frame(self, session, frame):
        if self._session is not session or self._video is None:
            return
        if frame.isValid():
            if not self._loop:
                from PySide6.QtMultimedia import QVideoFrame
                # Shared frame reference: no pixel conversion or frame history.
                self._last_video_frame = QVideoFrame(frame)
            self._video.show()
        elif self._video_finished and self._last_video_frame is not None:
            self._video.videoSink().setVideoFrame(self._last_video_frame)

    def _handle_video_status(self, session, status):
        from PySide6.QtMultimedia import QMediaPlayer
        if (self._session is not session or self._video is None or self._loop
                or status != QMediaPlayer.MediaStatus.EndOfMedia):
            return
        self._video_finished = True
        # Qt may clear the sink before or after reporting EndOfMedia.
        if self._last_video_frame is not None:
            self._video.videoSink().setVideoFrame(self._last_video_frame)

    def _failed(self, session, message):
        if self._session is not session:
            return
        self._session = None
        session.cancelled.set()
        self._delay.stop()
        self._poll_timer.stop()
        self._release_player()
        if session.kind == "text_excerpt":
            self._popup.set_text_preview_message(message)
        else:
            self._popup.show_preview_error(message)
        self.geometry_changed.emit()

    def _release_player(self):
        player, audio, video = self._player, self._audio, self._video
        self._player = self._audio = self._video = None
        self._playhead_timer.stop()
        if self._playhead is not None:
            self._playhead.hide()
            self._playhead.deleteLater()
            self._playhead = None
        self._audio_finished = False
        self._last_video_frame = None
        self._video_finished = False
        if audio is not None:
            audio.setMuted(True)
        if player is not None:
            player.errorOccurred.disconnect()
            player.stop()
            player.setSource(QtCore.QUrl())
            player.setVideoOutput(None)
            player.setAudioOutput(None)
            player.deleteLater()
        if audio is not None:
            audio.deleteLater()
        if video is not None:
            video.hide()
            video.deleteLater()

    def stop(self):
        session, self._session = self._session, None
        if session is not None:
            session.cancelled.set()
        self._key = self._prepared = None
        self._delay.stop()
        self._poll_timer.stop()
        self._release_player()

    def eventFilter(self, obj, event):
        if ((obj is self._popup and event.type() == QtCore.QEvent.Type.Hide)
                or (obj is self._window and event.type() in (
                    QtCore.QEvent.Type.WindowDeactivate, QtCore.QEvent.Type.Hide))):
            self.stop()
            if obj is self._window:
                self._popup.hide()
        return False

    def shutdown(self):
        self.stop()
        self._loader.shutdown()
        self._window.removeEventFilter(self)
