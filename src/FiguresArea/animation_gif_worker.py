from __future__ import annotations

from pathlib import Path
import sys

from FiguresArea.animation_export_service import encode_gif


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 10:
        print(
            "usage: animation_gif_worker sequence_dir destination frame_count "
            "interval_ms width height colors palette_mode dithering repeat_forever",
            file=sys.stderr,
        )
        return 2
    try:
        sequence_directory = Path(args[0])
        frame_count = int(args[2])
        encode_gif(
            frame_paths=[
                sequence_directory / f"frame-{index:06d}.png"
                for index in range(frame_count)
            ],
            destination=Path(args[1]),
            interval_ms=int(args[3]),
            dimensions=(int(args[4]), int(args[5])),
            colors=int(args[6]),
            palette_mode=args[7],
            dithering=bool(int(args[8])),
            repeat_forever=bool(int(args[9])),
            progress_callback=lambda frame: print(f"frame={frame}", flush=True),
        )
        return 0
    except Exception as error:  # noqa: BLE001 - report worker failures to the UI
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
