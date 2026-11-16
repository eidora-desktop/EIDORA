"""Generate synthetic playback fixtures offline, never inside the Qt GUI process.

Requires the pinned development PyAV/Pillow runtime. Release machines decode
these committed files; they do not need encoders or a system FFmpeg executable.
"""
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import sys

import av
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from DiscoveryArea.media_preview_service import SUPPORTED_PREVIEW_EXTENSIONS

OUTPUT = ROOT / 'src/shared/resources/media-audit'
DURATION = 3
TEXT = 'EIDORA media audit: café 日本語\nSecond line.'


def audio(path, codec, container_format, rate=48000, channels=1):
    with av.open(str(path), 'w', format=container_format) as output:
        stream = output.add_stream(codec, rate=rate)
        stream.layout = 'mono' if channels == 1 else 'stereo'
        samples = np.sin(2 * np.pi * 440 * np.arange(rate * DURATION) / rate).astype('float32') * .2
        frame = av.AudioFrame.from_ndarray(np.stack([samples] * channels), format='fltp', layout=stream.layout.name)
        frame.sample_rate = rate
        frame.pts = 0
        for packet in stream.encode(frame):
            output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)


def video(path, codec, container_format, with_audio=False):
    rate = 25 if codec == 'mpeg2video' else 10
    with av.open(str(path), 'w', format=container_format) as output:
        stream = output.add_stream(codec, rate=rate)
        stream.width, stream.height = 96, 64
        stream.pix_fmt = 'yuv420p'
        if codec == 'libx265':
            stream.options = {'x265-params': 'log-level=error:pools=1:frame-threads=1'}
        sound = output.add_stream('aac', rate=48000) if with_audio else None
        if sound is not None:
            sound.layout = 'mono'
        for index in range(rate * DURATION):
            color = ((220, 30, 30), (30, 220, 30), (30, 30, 220))[index // rate]
            with Image.new('RGB', (96, 64), color) as image:
                frame = av.VideoFrame.from_image(image)
            frame.pts, frame.time_base = index, Fraction(1, rate)
            for packet in stream.encode(frame):
                output.mux(packet)
            if sound is not None:
                start = index * 48000 // rate
                samples = np.sin(2 * np.pi * 440 * np.arange(start, start + 48000 // rate) / 48000).astype('float32') * .2
                frame = av.AudioFrame.from_ndarray(samples[None, :], format='fltp', layout='mono')
                frame.sample_rate, frame.pts = 48000, start
                for packet in sound.encode(frame):
                    output.mux(packet)
        for stream in (stream, sound):
            if stream is not None:
                for packet in stream.encode():
                    output.mux(packet)


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    cases = []

    def add(name, kind, **expected):
        path = OUTPUT / name
        cases.append(dict(id=path.stem.replace('.', '-'), file=name, kind=kind,
                          sha256=hashlib.sha256(path.read_bytes()).hexdigest(), **expected))

    images = {'jpg':'JPEG','jpeg':'JPEG','png':'PNG','bmp':'BMP','gif':'GIF','tif':'TIFF','tiff':'TIFF','webp':'WEBP'}
    for extension, format_name in images.items():
        name = f'image-{extension}.{extension}'
        with Image.new('RGB', (96, 64), (30, 220, 30)) as image:
            image.save(OUTPUT / name, format=format_name)
        add(name, 'image', width=96, height=64)
    audio_formats = {
        'wav': ('pcm_s16le','wav'), 'wave': ('pcm_s16le','wav'),
        'flac': ('flac','flac'), 'mp3': ('libmp3lame','mp3'),
        'm4a': ('aac','ipod'), 'aac': ('aac','adts'),
        'ogg': ('libvorbis','ogg'), 'oga': ('libvorbis','ogg'),
        'opus': ('libopus','ogg'),
        'aiff': ('pcm_s16be','aiff'), 'aif': ('pcm_s16be','aiff'),
    }
    for extension, (codec, container) in audio_formats.items():
        name = f'audio-{extension}.{extension}'
        audio(OUTPUT / name, codec, container)
        add(name, 'audio', codec=codec, duration_ms=3000, audio=True, sample_rate=48000, channels=1)
    for label, codec, rate, channels in (
        ('pcm24-stereo-44100','pcm_s24le',44100,2),
        ('pcm32-stereo-48000','pcm_s32le',48000,2),
        ('float32-mono-44100','pcm_f32le',44100,1),
        ('pcm16-stereo-44100','pcm_s16le',44100,2),
    ):
        name = f'audio-wav-{label}.wav'
        audio(OUTPUT / name, codec, 'wav', rate, channels)
        add(name, 'audio', codec=codec, duration_ms=3000, audio=True, sample_rate=rate, channels=channels)
    video_formats = {
        'mp4':('libx264','mp4'), 'm4v':('libx264','mp4'), 'mov':('libx264','mov'),
        'mkv':('libx264','matroska'), 'webm':('libvpx-vp9','webm'), 'avi':('mpeg4','avi'),
        'mpg':('mpeg2video','mpeg'), 'mpeg':('mpeg2video','mpeg'),
    }
    for extension, (codec, container) in video_formats.items():
        name = f'video-{extension}.{extension}'
        video(OUTPUT / name, codec, container, with_audio=extension == 'mp4')
        add(name, 'video', codec=codec, duration_ms=3000, width=96, height=64, audio=extension == 'mp4')
    for codec in ('libx265',):
        name = f'video-{codec}.mkv'
        video(OUTPUT / name, codec, 'matroska')
        add(name, 'video', codec=codec, duration_ms=3000, width=96, height=64, audio=False)
    for extension in sorted(SUPPORTED_PREVIEW_EXTENSIONS['text']):
        name = f'text-{extension[1:]}{extension}'
        record_key = ''
        content = TEXT
        if extension == '.json':
            content = json.dumps({'text': TEXT}, ensure_ascii=False)
        elif extension in {'.jsonl', '.ndjson'}:
            content = json.dumps({'id':'other', 'text':'Do not show this record'}) + '\n' + json.dumps({'id':'audit-item', 'text':TEXT}, ensure_ascii=False) + '\n'
            record_key = 'audit-item'
        (OUTPUT / name).write_text(content, encoding='utf-8')
        add(name, 'text', text=TEXT if record_key else content, record_key=record_key)
    (OUTPUT / 'manifest.json').write_text(json.dumps({'schema_version':1, 'provenance':'Synthetic EIDORA fixtures; no third-party media.', 'cases':cases}, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'{len(cases)} fixtures, {sum((OUTPUT / case["file"]).stat().st_size for case in cases):,} bytes')


if __name__ == '__main__':
    main()
