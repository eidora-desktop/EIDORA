"""Exercise export codecs that are easy to omit from frozen applications."""

from __future__ import annotations

from pathlib import Path


def audit_export_runtimes(output_directory: Path) -> dict[str, list[str]]:
    """Write every dependency-backed format exposed by an export dialog."""

    output_directory.mkdir(parents=True, exist_ok=True)
    results = {
        "figures": _audit_figure_exports(output_directory / "figures"),
        "canvas": _audit_canvas_exports(output_directory / "canvas"),
        "embeddings": _audit_embedding_exports(output_directory / "embeddings"),
        "animation": _audit_animation_export(output_directory / "animation"),
    }
    return results


def _audit_figure_exports(output_directory: Path) -> list[str]:
    from shared.plot_fonts import initialize_plot_fonts

    initialize_plot_fonts()
    from matplotlib.figure import Figure

    from FiguresArea.controllers.figure_settings_panel_controller import (
        FigureSettingsPanelController,
    )

    output_directory.mkdir(parents=True, exist_ok=True)
    formats = sorted(
        {
            suffix
            for _label, suffixes in FigureSettingsPanelController._matplotlib_export_options()
            for suffix in suffixes
        }
    )
    for suffix in formats:
        figure = Figure(figsize=(1, 1), dpi=24)
        figure.subplots().plot([0, 1], [0, 1])
        _require_output(figure.savefig, output_directory / f"figure.{suffix}")
    return formats


def _audit_canvas_exports(output_directory: Path) -> list[str]:
    from PySide6 import QtCore, QtGui

    from DiscoveryArea.canvas.export import (
        SUPPORTED_CANVAS_EXPORT_FORMATS,
        save_canvas_pixmap,
    )

    output_directory.mkdir(parents=True, exist_ok=True)
    pixmap = QtGui.QPixmap(QtCore.QSize(8, 8))
    pixmap.fill(QtGui.QColor("#4f46e5"))
    formats = sorted(SUPPORTED_CANVAS_EXPORT_FORMATS)
    for suffix in formats:
        _require_output(save_canvas_pixmap, pixmap, output_directory / f"canvas.{suffix}")
    return formats


def _audit_embedding_exports(output_directory: Path) -> list[str]:
    import numpy as np

    from EmbeddingsArea.exchange.formats import COMMON_EXPORT_FORMATS, write

    output_directory.mkdir(parents=True, exist_ok=True)
    matrix = np.asarray([[0.0, 1.0], [1.0, 0.0]], dtype=np.float32)
    identifiers = ["first", "second"]
    formats = sorted(COMMON_EXPORT_FORMATS)
    for suffix in formats:
        _require_output(
            write,
            output_directory / f"embeddings{suffix}",
            matrix,
            identifiers,
        )
    return formats


def _audit_animation_export(output_directory: Path) -> list[str]:
    from PIL import Image

    from FiguresArea.animation_export_service import encode_gif

    output_directory.mkdir(parents=True, exist_ok=True)
    frame_paths = []
    for index, color in enumerate(((255, 0, 0, 255), (0, 0, 255, 255))):
        path = output_directory / f"frame-{index}.png"
        Image.new("RGBA", (8, 8), color).save(path)
        frame_paths.append(path)
    destination = output_directory / "animation.gif"
    encode_gif(
        frame_paths=frame_paths,
        destination=destination,
        interval_ms=20,
        dimensions=(8, 8),
        colors=16,
        palette_mode="shared",
        dithering=False,
        repeat_forever=False,
    )
    _assert_nonempty(destination)
    return ["gif"]


def _require_output(function, *args) -> None:
    path = next((value for value in args if isinstance(value, Path)), None)
    if path is None:
        raise AssertionError("Export audit did not receive an output path.")
    function(*args)
    _assert_nonempty(path)


def _assert_nonempty(path: Path) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError(f"Export did not produce a nonempty file: {path.name}")
