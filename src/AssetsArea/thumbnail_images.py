from __future__ import annotations

from pathlib import Path

from PIL import Image


def resize_thumbnail(image: Image.Image, size_px: int, resize_mode: str) -> Image.Image:
    if resize_mode == "square_cropped":
        width, height = image.size
        side = min(width, height)
        left, top = (width - side) // 2, (height - side) // 2
        with image.crop((left, top, left + side, top + side)) as cropped:
            return cropped.resize((size_px, size_px), Image.Resampling.LANCZOS)
    if resize_mode in {"square", "square_squeezed"}:
        return image.resize((size_px, size_px), Image.Resampling.LANCZOS)
    resized = image.copy()
    resized.thumbnail((size_px, size_px), Image.Resampling.LANCZOS)
    return resized


def write_thumbnail_sizes(
    image: Image.Image,
    outputs: dict[int, Path],
    resize_mode: str,
) -> None:
    for size_px, output_path in outputs.items():
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with resize_thumbnail(image, size_px, resize_mode) as resized:
            resized.save(output_path, "webp", quality=85)
