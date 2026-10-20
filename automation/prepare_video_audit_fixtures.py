"""Regenerate the synthetic release-audit videos outside the GUI process.

Run manually with the pinned PyAV runtime; release builds use the committed
fixtures and need only decoder support. No third-party media is used.
"""

from pathlib import Path

import av
from PIL import Image


ENCODERS = {
    "h264": "libx264",
    "hevc": "libx265",
    "vp9": "libvpx-vp9",
    "mpeg2": "mpeg2video",
    "mjpeg": "mjpeg",
    "ffv1": "ffv1",
}


def main() -> None:
    root = Path(__file__).resolve().parents[1] / "src/shared/resources/video-audit"
    root.mkdir(parents=True, exist_ok=True)
    for label, encoder in ENCODERS.items():
        path = root / f"{label}.mkv"
        with av.open(str(path), "w", format="matroska") as container:
            stream = container.add_stream(encoder, rate=3)
            stream.width, stream.height = 96, 64
            stream.pix_fmt = "yuvj420p" if encoder == "mjpeg" else "yuv420p"
            if encoder == "libx265":
                stream.options = {"x265-params": "log-level=error:pools=1:frame-threads=1"}
            for color in ((220, 30, 30), (30, 220, 30), (30, 30, 220)):
                with Image.new("RGB", (96, 64), color) as image:
                    frame = av.VideoFrame.from_image(image)
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode():
                container.mux(packet)
        print(f"{label}: {path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
