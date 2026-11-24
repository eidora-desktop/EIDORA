"""Latest-only generated-preview loading and bounded GIF decoding, without widgets."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from queue import Full, Queue
import threading
import time

from PySide6 import QtCore, QtGui

from DiscoveryArea.thumbnails.repository import ThumbnailVariantRepository

PLAYABLE_PREVIEW_KINDS = frozenset({"audio_clip", "video_clip", "animated_gif"})


@dataclass
class TooltipPreviewSession:
    project_root: Path
    variant_id: str
    metadata_id: int
    kind: str
    size: int
    autoplay: bool
    cancelled: threading.Event = field(default_factory=threading.Event)
    play: threading.Event = field(default_factory=threading.Event)
    # At most two messages (poster/readiness or frames), never a whole clip.
    messages: Queue = field(default_factory=lambda: Queue(maxsize=2))
    done: threading.Event = field(default_factory=threading.Event)

    def publish(self, kind: str, value) -> bool:
        while not self.cancelled.is_set():
            try:
                self.messages.put((kind, value), timeout=0.02)
                return True
            except Full:
                pass
        return False


def _preview_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Preview path is outside the project preview directory.")
    return path


def _image_reader(path: Path, size: int) -> QtGui.QImageReader:
    reader = QtGui.QImageReader(str(path))
    dimensions = reader.size()
    # Generated previews are small. Reject malformed/replaced oversized assets
    # before decoding, even on plugins that do not support scaled decoding.
    if not dimensions.isValid() or max(dimensions.width(), dimensions.height()) > 4096:
        raise ValueError("Invalid preview image dimensions.")
    reader.setScaledSize(dimensions.scaled(size, size, QtCore.Qt.AspectRatioMode.KeepAspectRatio))
    return reader


def _stream_gif(session: TooltipPreviewSession, path: Path) -> None:
    while not session.cancelled.is_set() and not session.play.wait(0.02):
        pass
    if session.cancelled.is_set():
        return
    repeats = None
    while not session.cancelled.is_set():
        reader = _image_reader(path, session.size)
        frames = 0
        while not session.cancelled.is_set() and reader.canRead():
            started = time.monotonic()
            image = reader.read()
            if image.isNull():
                raise ValueError(reader.errorString())
            if repeats is None:
                repeats = reader.loopCount()  # -1 forever; 0 means no repeats.
            frames += 1
            if not session.publish("frame", image):
                return
            delay = max(10, reader.nextImageDelay()) / 1000
            if session.cancelled.wait(max(0, delay - (time.monotonic() - started))):
                return
        if not frames:
            raise ValueError("No GIF frames could be read.")
        if repeats == 0:
            return
        if repeats is not None and repeats > 0:
            repeats -= 1


def prepare_preview_asset(session: TooltipPreviewSession):
    """Read only this Item's generated asset; never fall back to original media."""
    clip = ThumbnailVariantRepository.find_preview_clip(
        session.project_root, session.variant_id, session.metadata_id,
    )
    if session.cancelled.is_set():
        return None
    if clip is None:
        raise ValueError("No generated preview for this Item.")
    root = (session.project_root / "thumbnails").resolve()
    path = _preview_path(root, clip.relative_path)
    poster_path = _preview_path(root, clip.poster_path)
    poster = QtGui.QImage()
    if poster_path.is_file():
        poster = _image_reader(poster_path, session.size).read()
    return path, poster, clip


def load_tooltip_preview(session: TooltipPreviewSession) -> None:
    if session.kind == "text_excerpt":
        preview = ThumbnailVariantRepository.read_preview_text(session.project_root, session.variant_id, session.metadata_id)
        if session.cancelled.is_set():
            return
        if preview is None:
            raise ValueError("No saved text excerpt for this Item.")
        session.publish("text", preview)
        return
    prepared = prepare_preview_asset(session)
    if prepared is None:
        return
    path, poster, _clip = prepared
    available = path.is_file()
    if not session.publish("prepared", (path, poster, available)):
        return
    if not available:
        raise FileNotFoundError("The generated preview is missing.")
    if session.autoplay and session.kind == "animated_gif":
        _stream_gif(session, path)


class TooltipPreviewLoader:
    """One lazy worker, one replaceable pending session, bounded frame delivery."""

    def __init__(self, worker=None):
        self._worker = worker
        self._condition = threading.Condition()
        self._pending = self._active = None
        self._thread = None
        self._closed = False

    def submit(self, session: TooltipPreviewSession) -> None:
        with self._condition:
            if self._closed:
                return
            for previous in (self._active, self._pending):
                if previous is not None:
                    previous.cancelled.set()
            self._pending = session
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="eidora-tooltip", daemon=True)
                self._thread.start()
            self._condition.notify()

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending is not None)
                if self._closed:
                    return
                session, self._pending = self._pending, None
                self._active = session
            try:
                if not session.cancelled.is_set():
                    (self._worker or load_tooltip_preview)(session)
            except Exception as exc:
                session.publish("error", str(exc))
            finally:
                session.done.set()
                with self._condition:
                    self._active = None
                session = None

    def shutdown(self) -> None:
        with self._condition:
            self._closed = True
            for session in (self._active, self._pending):
                if session is not None:
                    session.cancelled.set()
            self._pending = None
            self._condition.notify()
