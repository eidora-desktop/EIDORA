from __future__ import annotations

from collections.abc import Callable, Iterator
import math
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from typing import Any, Literal

from PIL import Image

from FiguresArea.animation_service import AnimationSequenceService
from FiguresArea.domain import (
    AnimationExportQuality,
    AnimationExportResult,
    AnimationExportSettings,
    AnimationFrame,
    AnimationRepeatStyle,
)
from shared.internal_workers import internal_worker_arguments


QUALITY_COLORS: dict[AnimationExportQuality, int] = {
    "highest": 256,
    "high": 192,
    "medium": 128,
    "low": 64,
}
PaletteMode = Literal["per_frame", "shared"]


class AnimationExportWorkspace:
    """Temporary rendered frames and encoded output for one atomic GIF export."""

    def __init__(
        self,
        *,
        settings: AnimationExportSettings,
        frames: list[AnimationFrame],
        repeat_style: AnimationRepeatStyle,
    ) -> None:
        self.settings = settings
        self.frames = list(frames)
        self.playback_frames = AnimationSequenceService.export_frames(frames, repeat_style)
        self._temporary = TemporaryDirectory(
            prefix=".eidora-animation-export-", dir=settings.destination.parent
        )
        self.root = Path(self._temporary.name)
        self.rendered_directory = self.root / "rendered"
        self.sequence_directory = self.root / "sequence"
        self.rendered_directory.mkdir()
        self.sequence_directory.mkdir()
        self.encoded_path = self.root / "encoded.gif"
        self._rendered_paths: dict[str, Path] = {}

    def render_path(self, index: int) -> Path:
        return self.rendered_directory / f"source-{index:06d}.png"

    def register_rendered(self, frame: AnimationFrame, path: Path) -> None:
        self._rendered_paths[frame.frame_id] = Path(path)

    def materialize_playback_sequence(self) -> None:
        for index, frame in enumerate(self.playback_frames):
            source = self._rendered_paths.get(frame.frame_id)
            if source is None or not source.is_file():
                raise ValueError(f"Animation frame '{frame.label}' was not rendered.")
            destination = self.sequence_directory / f"frame-{index:06d}.png"
            try:
                os.link(source, destination)
            except OSError:
                shutil.copyfile(source, destination)

    def cleanup(self) -> None:
        self._temporary.cleanup()


class AnimationExportService:
    """Prepare isolated GIF encoding and commit verified output atomically."""

    @staticmethod
    def quality_colors(quality: AnimationExportQuality) -> int:
        try:
            return QUALITY_COLORS[quality]
        except KeyError as error:
            raise ValueError(f"Unsupported GIF quality: {quality}") from error

    @staticmethod
    def palette_mode(quality: AnimationExportQuality) -> PaletteMode:
        """Use maximum color fidelity only for the intentionally largest preset."""

        return "per_frame" if quality == "highest" else "shared"

    @staticmethod
    def output_dimensions(settings: AnimationExportSettings) -> tuple[int, int]:
        return max(2, int(settings.width)), max(2, int(settings.height))

    @staticmethod
    def validate_request(
        frames: list[AnimationFrame], settings: AnimationExportSettings
    ) -> None:
        if not frames:
            raise ValueError("Add at least one frame before exporting an animation.")
        if settings.quality not in QUALITY_COLORS:
            raise ValueError("Choose a valid GIF quality.")
        if Path(settings.destination).suffix.lower() != ".gif":
            raise ValueError("The output filename must end in .gif.")
        if not Path(settings.destination).parent.is_dir():
            raise ValueError("Choose an existing output folder.")
        if not 2 <= int(settings.width) <= 16384:
            raise ValueError("GIF width must be between 2 and 16,384 pixels.")
        if not 2 <= int(settings.height) <= 16384:
            raise ValueError("GIF height must be between 2 and 16,384 pixels.")

    def create_workspace(
        self,
        *,
        frames: list[AnimationFrame],
        settings: AnimationExportSettings,
        repeat_style: AnimationRepeatStyle,
    ) -> AnimationExportWorkspace:
        self.validate_request(frames, settings)
        return AnimationExportWorkspace(
            settings=settings, frames=frames, repeat_style=repeat_style
        )

    @staticmethod
    def worker_program() -> str:
        return sys.executable

    def worker_arguments(
        self,
        *,
        workspace: AnimationExportWorkspace,
        interval_ms: int,
        repeat_style: AnimationRepeatStyle,
    ) -> list[str]:
        settings = workspace.settings
        return internal_worker_arguments(
            "gif",
            str(workspace.sequence_directory),
            str(workspace.encoded_path),
            str(len(workspace.playback_frames)),
            str(max(1, int(interval_ms))),
            str(settings.width),
            str(settings.height),
            str(self.quality_colors(settings.quality)),
            self.palette_mode(settings.quality),
            "1" if settings.gif_dithering else "0",
            "1" if repeat_style != "play_once" else "0",
        )

    @staticmethod
    def finalize(
        *,
        workspace: AnimationExportWorkspace,
        expected_dimensions: tuple[int, int],
    ) -> AnimationExportResult:
        encoded = workspace.encoded_path
        if not encoded.is_file() or encoded.stat().st_size <= 0:
            raise RuntimeError("The GIF encoder did not produce an output file.")
        with Image.open(encoded) as image:
            image.seek(max(0, len(workspace.playback_frames) - 1))
            image.load()
            if image.size != expected_dimensions:
                raise RuntimeError("The exported GIF has unexpected dimensions.")
        destination = workspace.settings.destination
        os.replace(encoded, destination)
        return AnimationExportResult(
            destination=destination, bytes_written=destination.stat().st_size
        )


def encode_gif(
    *,
    frame_paths: list[Path],
    destination: Path,
    interval_ms: int,
    dimensions: tuple[int, int],
    colors: int,
    palette_mode: PaletteMode,
    dithering: bool,
    repeat_forever: bool,
    progress_callback: Callable[[int], None] | None = None,
) -> None:
    """Encode frames with one shared adaptive palette and bounded frame loading."""

    if not frame_paths:
        raise ValueError("The animation contains no exportable frames.")
    if palette_mode not in {"per_frame", "shared"}:
        raise ValueError(f"Unsupported GIF palette mode: {palette_mode}")
    palette = (
        _shared_palette(frame_paths, colors=colors)
        if palette_mode == "shared"
        else None
    )
    dither = Image.Dither.FLOYDSTEINBERG if dithering else Image.Dither.NONE
    first = _quantized_frame(
        frame_paths[0],
        dimensions=dimensions,
        colors=colors,
        palette=palette,
        dither=dither,
    )
    if progress_callback is not None:
        progress_callback(1)
    save_options: dict[str, Any] = {
        "format": "GIF",
        "save_all": True,
        "append_images": _remaining_quantized_frames(
            frame_paths[1:],
            dimensions=dimensions,
            colors=colors,
            palette=palette,
            dither=dither,
            progress_callback=progress_callback,
        ),
        "duration": max(1, int(interval_ms)),
        "disposal": 2,
        "optimize": palette_mode == "shared",
    }
    if repeat_forever:
        save_options["loop"] = 0
    try:
        first.save(destination, **save_options)
    finally:
        first.close()
        if palette is not None:
            palette.close()


def _shared_palette(frame_paths: list[Path], *, colors: int) -> Image.Image:
    tile_size = 64
    columns = max(1, math.ceil(math.sqrt(len(frame_paths))))
    rows = math.ceil(len(frame_paths) / columns)
    samples = Image.new("RGB", (columns * tile_size, rows * tile_size), "white")
    try:
        for index, path in enumerate(frame_paths):
            with Image.open(path) as source:
                sample = _rgb_image(source)
            sample.thumbnail((tile_size, tile_size), Image.Resampling.LANCZOS)
            x = (index % columns) * tile_size + (tile_size - sample.width) // 2
            y = (index // columns) * tile_size + (tile_size - sample.height) // 2
            samples.paste(sample, (x, y))
            sample.close()
        return samples.quantize(
            colors=min(max(int(colors), 2), 256), method=Image.Quantize.MEDIANCUT
        )
    finally:
        samples.close()


def _remaining_quantized_frames(
    frame_paths: list[Path],
    *,
    dimensions: tuple[int, int],
    colors: int,
    palette: Image.Image | None,
    dither: Image.Dither,
    progress_callback: Callable[[int], None] | None,
) -> Iterator[Image.Image]:
    for index, path in enumerate(frame_paths, start=2):
        image = _quantized_frame(
            path,
            dimensions=dimensions,
            colors=colors,
            palette=palette,
            dither=dither,
        )
        if progress_callback is not None:
            progress_callback(index)
        try:
            yield image
        finally:
            image.close()


def _quantized_frame(
    path: Path,
    *,
    dimensions: tuple[int, int],
    colors: int,
    palette: Image.Image | None,
    dither: Image.Dither,
) -> Image.Image:
    with Image.open(path) as source:
        image = _rgb_image(source)
    if image.size != dimensions:
        resized = image.resize(dimensions, Image.Resampling.LANCZOS)
        image.close()
        image = resized
    try:
        if palette is not None:
            return image.quantize(palette=palette, dither=dither)
        return image.quantize(
            colors=min(max(int(colors), 2), 256),
            method=Image.Quantize.MAXCOVERAGE,
            dither=dither,
        )
    finally:
        image.close()


def _rgb_image(source: Image.Image) -> Image.Image:
    if source.mode in {"RGBA", "LA"} or "transparency" in source.info:
        rgba = source.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        rgba.close()
        return background
    return source.convert("RGB")


class GifAnimationExportService:
    """Programmatic synchronous GIF export retained for non-dialog callers."""

    def export(
        self,
        *,
        frames: list[AnimationFrame],
        destination: Path,
        interval_ms: int,
        repeat_style: AnimationRepeatStyle,
        render_frame: Callable[[AnimationFrame, Path], None],
    ) -> None:
        if not frames:
            raise ValueError("Add at least one snapshot before exporting an animation.")
        playback_frames = AnimationSequenceService.export_frames(frames, repeat_style)
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(
            prefix=".eidora-animation-export-", dir=target.parent
        ) as temporary_root:
            temporary_directory = Path(temporary_root)
            rendered_paths: dict[str, Path] = {}
            for index, frame in enumerate(frames):
                frame_path = temporary_directory / f"frame-{index:06d}.png"
                render_frame(frame, frame_path)
                rendered_paths[frame.frame_id] = frame_path
            ordered_paths = [rendered_paths[frame.frame_id] for frame in playback_frames]
            with Image.open(ordered_paths[0]) as first:
                dimensions = first.size
            temporary_gif = temporary_directory / "animation.gif"
            encode_gif(
                frame_paths=ordered_paths,
                destination=temporary_gif,
                interval_ms=interval_ms,
                dimensions=dimensions,
                colors=QUALITY_COLORS["highest"],
                palette_mode="per_frame",
                dithering=True,
                repeat_forever=repeat_style != "play_once",
            )
            os.replace(temporary_gif, target)
