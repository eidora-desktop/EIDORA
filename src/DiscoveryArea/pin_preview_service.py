"""Generated pin assets and controllable GIF decoding, with bounded frame delivery."""
from dataclasses import dataclass, field
import threading
import time

from PySide6 import QtCore, QtGui

from DiscoveryArea.tooltip_preview_service import (
    TooltipPreviewSession, prepare_preview_asset, _image_reader,
)


@dataclass
class PinPreviewSession(TooltipPreviewSession):
    position_ms: int = 0
    epoch: int = 0
    _seek: tuple | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def seek(self, position_ms):
        with self._lock:
            self.epoch += 1
            self._seek = (self.epoch, max(0, int(position_ms)))
        return self.epoch

    def take_seek(self):
        with self._lock:
            value, self._seek = self._seek, None
            return value


def load_pin_preview(session):
    prepared = prepare_preview_asset(session)
    if prepared is None:
        return
    path, poster, clip = prepared
    if not path.is_file():
        raise FileNotFoundError("The generated preview is missing.")
    duration = max(1, round(clip.duration_seconds * 1000))
    if not session.publish("prepared", (path, poster, duration)):
        return
    if session.kind != "animated_gif":
        return
    reader = None
    position = 0
    target = session.position_ms
    epoch = session.epoch
    repeats = None
    while not session.cancelled.is_set():
        seek = session.take_seek()
        if seek is not None:
            epoch, target = seek
            reader = None
            repeats = None
        if reader is None:
            reader = _image_reader(path, session.size)
            position = 0
        # A seek reads forward outside the GUI thread, retaining just one frame.
        if not session.play.is_set() and target is None:
            session.cancelled.wait(.02)
            continue
        if not reader.canRead():
            if target is not None:
                target = None
            if repeats == 0:
                session.play.clear()
                session.publish("ended", (epoch, position))
                # Stay responsive to an explicit replay/seek after finishing.
                while not session.cancelled.is_set() and session._seek is None:
                    session.cancelled.wait(.02)
                continue
            if repeats is not None and repeats > 0:
                repeats -= 1
            reader = _image_reader(path, session.size)
            position = 0
        started = time.monotonic()
        image = reader.read()
        if image.isNull():
            raise ValueError(reader.errorString())
        if repeats is None:
            repeats = reader.loopCount()
        delay = max(10, reader.nextImageDelay())
        next_position = position + delay
        if target is not None and next_position < target and reader.canRead():
            position = next_position
            continue
        target = None
        if not session.publish("frame", (epoch, image, position)):
            return
        position = next_position
        # Pause freezes time; cancellation and seeking interrupt the wait.
        remaining = max(0, delay / 1000 - (time.monotonic() - started))
        while remaining > 0 and not session.cancelled.is_set():
            if session._seek is not None:
                break
            tick = time.monotonic()
            session.cancelled.wait(min(.02, remaining))
            if session.play.is_set():
                remaining -= time.monotonic() - tick


def load_pin_posters(project_root, variant, entries, size, cancelled):
    output = {}
    for annotation_id, metadata_id in entries:
        if cancelled.is_set():
            return None
        session = TooltipPreviewSession(project_root, variant, metadata_id, "image", size, False, cancelled=cancelled)
        try:
            prepared = prepare_preview_asset(session)
            if prepared is None:
                return None
            _path, image, clip = prepared
            if image.isNull():
                continue
            image = image.convertToFormat(QtGui.QImage.Format.Format_RGBA8888)
            output[annotation_id] = (bytes(image.constBits()), image.width(), image.height(), round(clip.duration_seconds*1000))
        except (OSError, ValueError):
            continue
    return output


def convert_video_frame(frame, size, cancelled):
    if cancelled.is_set():
        return None
    image = frame.toImage()
    if image.isNull() or cancelled.is_set():
        return None
    return image.scaled(size, size, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                        QtCore.Qt.TransformationMode.SmoothTransformation)
