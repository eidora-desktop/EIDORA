"""Encode one bounded excerpt in an isolated process; never import from startup/UI."""
from __future__ import annotations

from fractions import Fraction
import json
import math
from pathlib import Path
import sys
import tempfile

from MediaArea.clip_options import clip_parameters, excerpt_start, VIDEO_QUALITY_CRF
from MediaArea.video_thumbnail_worker import _duration_seconds, _select_stream, _display_image


class ClipSourceError(ValueError):
    pass


class ClipMissingError(ClipSourceError):
    pass


def _video_frames(path, start, length, fps, scratch, max_edge=256):
    import av
    import numpy as np

    with av.open(str(path)) as container:
        stream = _select_stream(container)
        origin = float((stream.start_time or 0) * stream.time_base)
        container.seek(int((origin + start) / stream.time_base), stream=stream, backward=True)
        count, shape = 0, None
        next_time = start
        previous, previous_time = None, start
        with scratch.open("wb") as output:
            def write_frame(frame):
                nonlocal count, shape, next_time
                with _display_image(frame, stream) as image:
                    image.thumbnail((max_edge, max_edge))
                    if min(image.size) < 2:
                        raise ClipSourceError("Video dimensions are too small to encode.")
                    width, height = image.width // 2 * 2, image.height // 2 * 2
                    frame_shape = (height, width, 3)
                    if shape is not None and frame_shape != shape:
                        raise ClipSourceError("Video dimensions change within the excerpt.")
                    shape = frame_shape
                    with image.resize((width, height)) as resized:
                        output.write(np.asarray(resized, dtype=np.uint8).tobytes())
                count += 1
                next_time = start + count / fps
            # Sample the frame actually displayed at each target timestamp. Holding
            # one decoded frame preserves timing for variable-rate sources.
            for frame in container.decode(stream):
                if frame.pts is None:
                    continue
                timestamp = float(frame.pts * frame.time_base) - origin
                if previous is not None:
                    while next_time + 1e-6 < min(timestamp, start + length):
                        write_frame(previous)
                if timestamp >= start + length:
                    break
                previous, previous_time = frame, timestamp
            if previous is not None:
                while next_time + 1e-6 < min(start + length, previous_time + 1/fps):
                    write_frame(previous)
        if not count:
            raise ClipSourceError("No video frames found in the requested excerpt.")
    return np.memmap(scratch, dtype=np.uint8, mode="r", shape=(count, *shape))


def _audio_samples(path, start, length, timeline_origin=None, *, allow_silence=False, channels="mono"):
    import av
    import numpy as np

    rate = 48000
    end = 0
    with av.open(str(path)) as container:
        streams = list(container.streams.audio)
        if not streams:
            raise ClipSourceError("The source has no audio stream.")
        stream = streams[0]
        layout = "stereo" if channels == "stereo" and len(stream.layout.channels) > 1 else "mono"
        samples = np.zeros((2 if layout == "stereo" else 1, math.ceil(length * rate)), dtype=np.float32)
        origin = float((stream.start_time or 0) * stream.time_base) if timeline_origin is None else timeline_origin
        container.seek(int((origin + start) / stream.time_base), stream=stream, backward=True)
        resampler = av.AudioResampler(format="fltp", layout=layout, rate=rate)
        def collect(frame):
            nonlocal end
            if frame.pts is None:
                raise ClipSourceError("Audio timestamps are unavailable.")
            offset = round((float(frame.pts * frame.time_base) - origin - start) * rate)
            data = frame.to_ndarray()
            left, right = max(0, offset), min(samples.shape[1], offset + data.shape[1])
            if right > left:
                samples[:, left:right] = data[:, left-offset:right-offset]
                end = max(end, right)
        for frame in container.decode(stream):
            if frame.pts is not None and float(frame.pts * frame.time_base) - origin >= start + length:
                break
            for converted in resampler.resample(frame):
                collect(converted)
        for converted in resampler.resample(None):
            collect(converted)
    if not end:
        if allow_silence:
            return samples
        raise ClipSourceError("No audio samples found in the requested excerpt.")
    return samples[:, :end]


def _waveform(samples, path):
    import numpy as np
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (256, 96), "#202830")
    draw = ImageDraw.Draw(image)
    for x, part in enumerate(np.array_split(np.max(np.abs(samples), axis=0), 256)):
        peak = min(1.0, float(np.max(np.abs(part)))) if part.size else 0
        half = max(1, round(peak * 43))
        draw.line((x, 48-half, x, 48+half), fill="#b4d9ed")
    image.save(path, "WEBP", quality=75)
    image.close()


def _encode_video(path, frames, indices, fps, samples, parameters):
    import av

    with av.open(str(path), "w", options={"movflags": "+faststart"}) as output:
        stream = output.add_stream("libx264", rate=Fraction(str(fps)).limit_denominator(1001))
        stream.width, stream.height = frames.shape[2], frames.shape[1]
        stream.pix_fmt = "yuv420p"
        stream.options = {"crf": str(VIDEO_QUALITY_CRF[parameters["video_quality"]]), "preset": "medium", "threads": "1"}
        audio = _audio_stream(output, samples, parameters["audio_bitrate"]) if samples is not None else None
        for index in indices:
            frame = av.VideoFrame.from_ndarray(frames[index], format="rgb24")
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
        if audio is not None:
            _encode_audio(output, audio, samples)


def _audio_stream(output, samples, bitrate):
    stream = output.add_stream("aac", rate=48000)
    stream.layout = "stereo" if samples.shape[0] == 2 else "mono"
    stream.bit_rate = bitrate * 1000
    return stream


def _encode_audio(output, stream, samples):
    import av
    import numpy as np

    for offset in range(0, samples.shape[1], 1024):
        frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(samples[:, offset:offset+1024]), format="fltp", layout=stream.layout.name)
        frame.sample_rate = 48000
        frame.pts = offset
        frame.time_base = Fraction(1, 48000)
        for packet in stream.encode(frame):
            output.mux(packet)
    for packet in stream.encode():
        output.mux(packet)


def _encode_gif(path, frames, indices, fps, parameters):
    from PIL import Image
    import numpy as np

    # One palette sampled across the excerpt; bounded to twenty 64px images.
    sample_indices = np.linspace(0, len(frames)-1, min(20, len(frames)), dtype=int)
    palette_source = Image.new("RGB", (64 * len(sample_indices), 64))
    for x, index in enumerate(sample_indices):
        with Image.fromarray(frames[index]) as image:
            image.thumbnail((64, 64))
            palette_source.paste(image, (x * 64, 0))
    palette = palette_source.quantize(colors=parameters["gif_colors"])
    palette_source.close()
    dither = Image.Dither.FLOYDSTEINBERG if parameters["gif_dither"] == "floyd_steinberg" else Image.Dither.NONE
    def images():
        for index in indices:
            with Image.fromarray(frames[index]) as image:
                yield image.quantize(palette=palette, dither=dither)
    sequence = images()
    first = next(sequence)
    # GIF delays are centiseconds; distribute rounding instead of changing duration.
    durations = [10 * (round((i+1)*100/fps)-round(i*100/fps)) for i in range(len(indices))]
    loop = {} if parameters["loop"] == "once" else {"loop": 0}
    try:
        first.save(path, save_all=True, append_images=sequence, duration=durations,
                   optimize=True, disposal=1, **loop)
    finally:
        first.close()
        palette.close()


def process(payload):
    import av
    from PIL import Image

    parameters = clip_parameters(payload["parameters"], payload["media_type"])
    source = Path(payload["source"])
    output, poster = Path(payload["output"]), Path(payload["poster"])
    if not source.is_file():
        raise ClipMissingError("The source file is missing.")
    try:
        container = av.open(str(source))
    except (OSError, ValueError) as exc:
        raise ClipSourceError(str(exc)) from exc
    with container:
        stream = _select_stream(container) if payload["media_type"] == "video" else next(iter(container.streams.audio), None)
        if stream is None:
            raise ClipSourceError("No usable media stream.")
        duration = _duration_seconds(container, stream)
        if duration is None or not math.isfinite(duration) or duration <= 0:
            raise ClipSourceError("The source duration is unavailable.")
        source_fps = float(stream.average_rate or 24) if payload["media_type"] == "video" else 0
        has_audio = bool(container.streams.audio)
        origin = float((stream.start_time or 0) * stream.time_base)
    seed = str(payload["seed"])
    if parameters["random_seed"]:
        seed += f":{parameters['random_seed']}"
    start = excerpt_start(duration, parameters["duration"], parameters["position"], seed)
    length = min(duration-start, parameters["duration"])
    output.parent.mkdir(parents=True, exist_ok=True)
    kind = parameters["preview_kind"]
    samples = None
    if kind == "audio_clip" or (kind == "video_clip" and parameters["include_audio"] and has_audio):
        try:
            samples = _audio_samples(source, start, length, origin, allow_silence=kind == "video_clip", channels=parameters["audio_channels"])
        except av.error.FFmpegError as exc:
            raise ClipSourceError(str(exc)) from exc
    width = height = 0
    fps = 0.0
    if kind == "audio_clip":
        with av.open(str(output), "w") as container:
            _encode_audio(container, _audio_stream(container, samples, parameters["audio_bitrate"]), samples)
        _waveform(samples, poster)
        length = samples.shape[1] / 48000
    else:
        fps = min(source_fps, parameters["gif_fps"] if kind == "animated_gif" else parameters["video_fps"])
        if not math.isfinite(fps) or fps <= 0:
            raise ClipSourceError("The source frame rate is invalid.")
        with tempfile.TemporaryDirectory(dir=output.parent) as scratch:
            try:
                frames = _video_frames(source, start, length, fps, Path(scratch)/"frames.rgb", parameters["max_edge"])
            except av.error.FFmpegError as exc:
                raise ClipSourceError(str(exc)) from exc
            try:
                height, width = frames.shape[1:3]
                indices = list(range(len(frames)))
                if kind == "animated_gif" and parameters["loop"] == "ping_pong":
                    indices += list(range(len(frames)-2, 0, -1))
                with Image.fromarray(frames[len(frames)//2]) as image:
                    image.save(poster, "WEBP", quality=75)
                if kind == "animated_gif":
                    _encode_gif(output, frames, indices, fps, parameters)
                else:
                    _encode_video(output, frames, indices, fps, samples, parameters)
                length = len(indices)/fps
            finally:
                frames._mmap.close()
    return dict(ok=True, start_seconds=start, duration_seconds=length, width=width, height=height, fps=fps,
                size_bytes=output.stat().st_size)


def main(arguments=None):
    args = list(sys.argv[1:] if arguments is None else arguments)
    if len(args) != 2:
        return 2
    try:
        result = process(json.loads(args[0]))
    except Exception as exc:
        kind = "missing" if isinstance(exc, ClipMissingError) else "source" if isinstance(exc, ClipSourceError) else "storage" if isinstance(exc, OSError) else "worker"
        result = dict(ok=False, kind=kind, error=f"{type(exc).__name__}: {exc}")
    Path(args[1]).write_text(json.dumps(result), encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
