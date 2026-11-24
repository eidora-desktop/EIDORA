"""One explicit playback owner for pins, independent of persisted annotations."""
from functools import partial
from queue import Empty
import threading

from PySide6 import QtCore, QtGui

from DiscoveryArea.pin_preview_service import (
    PinPreviewSession, load_pin_preview, load_pin_posters, convert_video_frame,
)
from DiscoveryArea.tooltip_preview_service import TooltipPreviewLoader
from shared.qt.background_tasks import BackgroundTasks


class PinPlaybackController(QtCore.QObject):
    playing_changed = QtCore.Signal(bool)
    posters_ready = QtCore.Signal(object)

    def __init__(self, canvas):
        super().__init__(canvas)
        self._canvas = canvas
        self._key = None
        self._entries = {}
        self._positions = {}
        self._durations = {}
        self._poster_key = None
        self._poster_revision = 0
        self._poster_cancel = threading.Event()
        self._posters = BackgroundTasks(self)
        self._posters.finished.connect(self._posters_finished)
        self._loader = TooltipPreviewLoader(worker=load_pin_preview)
        self._converter = BackgroundTasks(self)
        self._converter.finished.connect(self._converted_frame)
        self._converter.failed.connect(self._conversion_failed)
        self._revision = 0
        self._active = None
        self._session = None
        self._player = self._audio = self._sink = None
        self._state = "stopped"
        self._position = self._duration = 0
        self._want_play = self._started = self._preview_pending = False
        self._reserved = False
        self._gif_pending = False
        self._poll = QtCore.QTimer(self)
        self._poll.setInterval(15)
        self._poll.timeout.connect(self._collect)
        self._progress = QtCore.QTimer(self)
        self._progress.setInterval(40)
        self._progress.timeout.connect(self._update_position)
        canvas.pin_media_requested.connect(self._requested)
        canvas.pin_media_visibility_changed.connect(self.refresh_visible)
        loader = self._loader
        self.destroyed.connect(lambda: loader.shutdown())

    @property
    def playing(self):
        return self._reserved

    def configure(self, project_root, variant, kind, loop, size, entries):
        key = (project_root, variant, kind, bool(loop), int(size))
        if key != self._key:
            self.reset()
            self._key = key
        self._entries = {str(entry["annotation_id"]): int(entry["metadata_id"]) for entry in entries}
        self._positions = {key: value for key, value in self._positions.items() if key in self._entries}
        self._durations = {key: value for key, value in self._durations.items() if key in self._entries}
        self._canvas.configure_pin_media(kind)
        self.refresh_visible(self._canvas.visible_pin_media_ids())

    def refresh_visible(self, visible):
        if self._key is None:
            return
        visible = set(visible) & self._entries.keys()
        if self._active is not None and self._active not in visible:
            self._release_active()
        for key in visible:
            if key != self._active:
                position, duration = self._positions.get(key, 0), self._durations.get(key, 0)
                self._canvas.update_pin_media(key, state="paused" if position else "stopped", position=position, duration=duration)
        request_key = (self._key, tuple(sorted((key, self._entries[key]) for key in visible)))
        if request_key == self._poster_key:
            return
        self._poster_key = request_key
        self._poster_revision += 1
        self._poster_cancel.set()
        self._poster_cancel = threading.Event()
        root, variant, _kind, _loop, size = self._key
        entries = [(key, self._entries[key]) for key in sorted(visible)]
        job = partial(load_pin_posters, root, variant, entries, size, self._poster_cancel)
        self._posters.submit(self._poster_revision, lambda _progress: job())

    def _posters_finished(self, revision, result):
        if revision != self._poster_revision or not result:
            return
        self._durations.update({key: value[3] for key, value in result.items()})
        self.posters_ready.emit(result)
        self.refresh_visible(self._canvas.visible_pin_media_ids())

    def _requested(self, annotation_id, action, fraction):
        if self._key is None or annotation_id not in self._entries:
            return
        if annotation_id not in self._canvas.visible_pin_media_ids():
            return
        if self._active != annotation_id or self._state == "error":
            self._release_active()
            self._active = annotation_id
            self._position = self._positions.get(annotation_id, 0)
            self._duration = self._durations.get(annotation_id, 0)
            if action == "seek":
                self._position = round(fraction * self._duration)
            elif self._duration and self._position >= self._duration:
                self._position = 0
            self._want_play = action == "toggle"
            root, variant, kind, _loop, size = self._key
            self._session = PinPreviewSession(root, variant, self._entries[annotation_id], kind, size, False,
                                              position_ms=self._position)
            self._gif_pending = kind == "animated_gif"
            if self._want_play:
                self._session.play.set()
            self._state = "loading"
            self._publish()
            self._loader.submit(self._session)
            self._poll.start()
            return
        if action == "seek":
            self._position = round(max(0.0, min(1.0, fraction)) * self._duration)
            if self._state == "finished":
                self._state = "paused"
            if self._session.kind == "animated_gif":
                self._session.seek(self._position)
                self._gif_pending = True
                self._poll.start()
            elif self._player is not None:
                self._revision += 1
                self._player.setPosition(self._position)
                if self._session.kind == "video_clip" and not self._want_play:
                    self._preview_video_frame()
            self._publish()
        elif self._state == "playing":
            self.pause()
        else:
            self._want_play = True
            if self._state == "finished":
                self._position = 0
                if self._session.kind == "animated_gif":
                    self._session.seek(0)
                elif self._player is not None:
                    self._player.setPosition(0)
            self._state = "playing"
            if self._session.kind == "animated_gif":
                self._session.play.set()
                self._poll.start()
            elif self._player is not None:
                self._player.play()
            self._publish()

    def _collect(self):
        session = self._session
        if session is None:
            return
        for _ in range(2):
            try:
                kind, value = session.messages.get_nowait()
            except Empty:
                break
            if kind == "prepared":
                path, poster, self._duration = value
                self._durations[self._active] = self._duration
                if session.kind == "animated_gif":
                    self._state = "playing" if self._want_play else "paused"
                else:
                    self._start_player(session, path)
                self._publish(image=poster)
            elif kind in {"frame", "ended"}:
                if value[0] != session.epoch:
                    continue
                if kind == "frame":
                    _, image, self._position = value
                    self._gif_pending = False
                    self._publish(image=image)
                else:
                    _, self._position = value
                    self._duration = self._position
                    self._state = "finished"
                    self._want_play = False
                    self._publish()
            elif kind == "error":
                self._failed(session, value)
                return
        if (session.done.is_set() or (self._state in {"paused", "finished"} and not self._gif_pending)) and session.messages.empty():
            self._poll.stop()

    def _start_player(self, session, path):
        try:
            from PySide6 import QtMultimedia
            player = self._player = QtMultimedia.QMediaPlayer(self)
            self._audio = QtMultimedia.QAudioOutput(self)
            self._audio.setVolume(.8)
            player.setAudioOutput(self._audio)
            player.setLoops(QtMultimedia.QMediaPlayer.Loops.Infinite if self._key[3] else 1)
            player.errorOccurred.connect(lambda _code, message: self._failed(session, message))
            player.mediaStatusChanged.connect(lambda status: self._media_status(session, status))
            player.playbackStateChanged.connect(lambda state: self._playback_state(session, state))
            if session.kind == "video_clip":
                self._sink = QtMultimedia.QVideoSink(self)
                player.setVideoSink(self._sink)
                self._sink.videoFrameChanged.connect(lambda frame: self._video_frame(session, frame))
            player.setSource(QtCore.QUrl.fromLocalFile(str(path)))
        except (ImportError, RuntimeError) as exc:
            self._failed(session, str(exc))

    def _media_status(self, session, status):
        from PySide6.QtMultimedia import QMediaPlayer
        if session is not self._session or self._player is None:
            return
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._position = self._duration
            self._state = "finished"
            self._want_play = False
            self._progress.stop()
            self._publish()
        elif status in {QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia} and not self._started:
            self._started = True
            self._duration = self._player.duration() or self._duration
            self._player.setPosition(self._position)
            if self._want_play:
                self._player.play()
            elif session.kind == "video_clip":
                self._preview_video_frame()
            else:
                self._state = "paused"
                self._publish()

    def _preview_video_frame(self):
        # A stopped video needs decoding to display the seek target. Mute until
        # the sink supplies one frame, then pause immediately in _video_frame.
        self._preview_pending = True
        self._audio.setMuted(True)
        self._player.play()

    def _playback_state(self, session, state):
        from PySide6.QtMultimedia import QMediaPlayer
        if session is not self._session:
            return
        if state == QMediaPlayer.PlaybackState.PlayingState and not self._preview_pending:
            self._state = "playing"
            self._progress.start()
        else:
            self._progress.stop()
        self._publish()

    def _video_frame(self, session, frame):
        if session is not self._session or not frame.isValid():
            return  # Keep the final image when Qt clears its output.
        from PySide6.QtMultimedia import QVideoFrame
        task = partial(convert_video_frame, QVideoFrame(frame), session.size, session.cancelled)
        self._converter.submit(self._revision, lambda _progress: task())
        if self._preview_pending:
            self._preview_pending = False
            self._player.pause()
            self._audio.setMuted(False)
            self._state = "paused"
            self._publish()

    def _converted_frame(self, revision, image):
        if revision == self._revision and self._session is not None and image is not None:
            self._publish(image=image)

    def _conversion_failed(self, revision, message):
        if revision == self._revision and self._session is not None:
            self._failed(self._session, message)

    def _update_position(self):
        if self._player is not None and self._state == "playing":
            self._position = self._player.position()
            self._duration = self._player.duration() or self._duration
            self._publish()

    def _publish(self, image=None):
        reserved = self._state == "playing" or (self._state == "loading" and self._want_play)
        if reserved != self._reserved:
            self._reserved = reserved
            self.playing_changed.emit(reserved)
        if self._active is not None:
            self._positions[self._active] = self._position
            self._canvas.update_pin_media(self._active, state=self._state, position=self._position,
                                          duration=self._duration, image=image)

    def pause(self):
        if self._session is None:
            return
        self._want_play = False
        self._session.play.clear()
        if self._player is not None:
            self._player.pause()
            self._position = self._player.position()
        self._state = "paused"
        self._progress.stop()
        self._publish()

    def _failed(self, session, _message):
        if session is not self._session:
            return
        key = self._active
        self._release_active()
        self._active = key
        self._state = "error"
        self._publish(image=QtGui.QImage())

    def _release_active(self):
        session, self._session = self._session, None
        if session is not None:
            session.cancelled.set()
        self._revision += 1
        self._poll.stop()
        self._progress.stop()
        player, audio, sink = self._player, self._audio, self._sink
        self._player = self._audio = self._sink = None
        if player is not None:
            self._position = self._duration if self._state == "finished" else player.position()
            audio.setMuted(True)
            player.stop()
            player.setSource(QtCore.QUrl())
            player.setAudioOutput(None)
            player.setVideoSink(None)
            player.deleteLater()
        for obj in (audio, sink):
            if obj is not None:
                obj.deleteLater()
        self._state = "paused"
        self._want_play = self._started = self._preview_pending = False
        self._publish(image=QtGui.QImage())
        self._active = None

    def reset(self):
        self._release_active()
        self._key = self._poster_key = None
        self._entries.clear()
        self._positions.clear()
        self._durations.clear()
        self._poster_revision += 1
        self._poster_cancel.set()

    def suspend(self):
        self._release_active()

    def shutdown(self):
        self.reset()
        self._loader.shutdown()
        self._posters.shutdown()
        self._converter.shutdown()
