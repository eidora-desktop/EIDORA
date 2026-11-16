"""Exercise one real item-preview case in a source or windowed packaged worker."""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import faulthandler
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import time
import traceback


def probe(case: dict, fixtures: Path, report: dict, save, require_audio_output: bool) -> None:
    from PySide6 import QtCore, QtMultimedia, QtWidgets
    import PySide6
    from DiscoveryArea.controllers.selection_actions_dialog_controller import SelectionActionsDialogController, SelectionActionsDialogData
    from DiscoveryArea.domain import ItemMediaSource
    from DiscoveryArea.thumbnails.resolver import ThumbnailResolver
    from DiscoveryArea.thumbnails.service import DecodeRequest, ThumbnailService
    from EmbeddingsArea.domain import MediaAsset, MediaContentType, MediaLink, MediaSource, MediaSourceKind, MediaMatchStrategy
    from EmbeddingsArea.repository import MediaRepository, MediaLinkRepository
    from project.domain import Project
    from shared.qt.ui_loader import load_ui

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setQuitOnLastWindowClosed(False)
    report.update(platform=sys.platform, machine=platform.machine(), python=sys.version.split()[0],
                  pyside=PySide6.__version__, qt=QtCore.qVersion(), packaged=bool(getattr(sys, 'frozen', False)),
                  media_backend=os.environ.get('QT_MEDIA_BACKEND', 'default'),
                  decoding_devices=os.environ.get('QT_FFMPEG_DECODING_HW_DEVICE_TYPES', 'default'),
                  qt_library_paths=QtCore.QCoreApplication.libraryPaths(),
                  audio_outputs=[device.description() for device in QtMultimedia.QMediaDevices.audioOutputs()],
                  default_audio_output=QtMultimedia.QMediaDevices.defaultAudioOutput().description(),
                  audible_output_verified=False)
    if require_audio_output and case.get('audio') and not report['audio_outputs']:
        raise RuntimeError('An audio output device is required for this device-validation run.')

    class Bridge(QtCore.QObject):
        ready = QtCore.Signal(object)

    def stage(value):
        report['stage'] = value
        save()

    def pump_until(condition, description, timeout=6):
        deadline = time.monotonic() + timeout
        while True:
            app.processEvents()
            player = controller._source_video_player
            if player is not None and player.error() != QtMultimedia.QMediaPlayer.Error.NoError:
                raise RuntimeError(f'{description}: {player.error().name}: {player.errorString()}')
            if condition():
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(f'Timed out waiting for {description}')
            time.sleep(.005)

    with tempfile.TemporaryDirectory(prefix='eidora-media-probe-') as temporary:
        project = Project('Media audit', Path(temporary), 1)
        for directory in project.paths.required_directories():
            directory.mkdir(parents=True, exist_ok=True)
        media = MediaRepository(project.paths.media / 'media_index.db')
        links = MediaLinkRepository(media.database_path)
        source_path = fixtures / case['file']
        alternate = fixtures / ('image-png.png' if case['kind'] == 'image' else 'text-txt.txt' if case['kind'] == 'text' else 'audio-wav.wav')
        sources = []
        for identity, path, kind in (('primary', source_path, case['kind']), ('alternate', alternate, 'image' if case['kind'] == 'image' else 'text' if case['kind'] == 'text' else 'audio')):
            media.upsert_source(MediaSource(identity, identity.title(), path, MediaSourceKind.FILE, MediaContentType(kind)))
            media.upsert_assets([MediaAsset(identity, identity, identity, path, path.name, path.name, MediaContentType(kind), identity, path.stat().st_size, path.stat().st_mtime_ns)])
            links.replace_links_for_source(identity, 'id', MediaMatchStrategy.MANUAL, [MediaLink(identity, identity, identity, 7, 'id', '7')], '')
            sources.append(ItemMediaSource(identity, identity, identity.title(), path, kind, case.get('record_key', '') if identity == 'primary' else ''))
        thumbnails = ThumbnailService(ThumbnailResolver(project))
        bridge = Bridge()
        dialog = load_ui('DiscoveryArea', 'ui/selection_actions_dialog.ui', None)
        controller = SelectionActionsDialogController()
        controller._dialog = dialog
        controller._data = SelectionActionsDialogData(7, (('ID', '7'),), (), media_sources=tuple(sources))
        controller._request_thumbnail = lambda target, item, variant, pixels, original=False, asset='': thumbnails.request(DecodeRequest(item, pixels, variant, original, asset))
        thumbnails.subscribe_thumbnail_ready(bridge.ready.emit)
        bridge.ready.connect(controller.apply_thumbnail)
        player = None
        try:
            stage('open-preview')
            controller._populate_metadata(dialog, controller._data)
            dialog.show()
            combo = dialog.findChild(QtWidgets.QComboBox, 'combo_media_source')
            report['selector_labels'] = [combo.itemText(index) for index in range(combo.count())]
            if report['selector_labels'] != ['Primary', 'Alternate']:
                raise RuntimeError('Media source selector did not retain source labels.')
            if case['kind'] == 'image':
                pump_until(lambda: bool(controller._metadata_label.pixmap()) and not controller._metadata_label.pixmap().isNull(), 'image preview')
                picture = controller._metadata_label.pixmap().toImage()
                color = picture.pixelColor(picture.width() // 2, picture.height() // 2)
                if color.green() < max(color.red(), color.blue()) + 80 or abs(picture.width() / picture.height() - 1.5) > .03:
                    raise RuntimeError('Image pixels or aspect ratio differ from the fixture.')
                report['image'] = dict(width=picture.width(), height=picture.height(), center=color.name())
            elif case['kind'] == 'text':
                pump_until(lambda: not controller._text_preview._timer.isActive(), 'text preview')
                editor = dialog.findChild(QtWidgets.QPlainTextEdit, 'edit_source_text')
                if editor.toPlainText() != case['text'] or not editor.isReadOnly():
                    raise RuntimeError('Text preview differs from the expected document or linked record.')
                report['text_characters'] = len(editor.toPlainText())
            else:
                player = controller._source_video_player
                if player is None:
                    raise RuntimeError('The dialog did not initialize the media player.')
                controller._source_video_audio_output.setVolume(0)  # Automated runs must be silent.
                observations = dict(audio_buffers=0, audio_frames=0, video_frames=0, nonzero_samples=False)
                output = QtMultimedia.QAudioBufferOutput(player)

                def audio_buffer(buffer):
                    if buffer.isValid():
                        observations['audio_buffers'] += 1
                        observations['audio_frames'] += buffer.frameCount()
                        observations['sample_rate'] = buffer.format().sampleRate()
                        observations['channels'] = buffer.format().channelCount()
                        observations['sample_format'] = buffer.format().sampleFormat().name
                        observations['nonzero_samples'] |= any(bytes(buffer.constData()))

                def video_frame(frame):
                    if frame.isValid():
                        observations['video_frames'] += 1
                        observations['video_width'] = frame.width()
                        observations['video_height'] = frame.height()

                output.audioBufferReceived.connect(audio_buffer)
                player.setAudioBufferOutput(output)
                if case['kind'] == 'video':
                    controller._source_video_widget.videoSink().videoFrameChanged.connect(video_frame)
                stage('duration-before-play')
                pump_until(lambda: player.duration() > 0 and not controller._source_video_preview_pending, 'duration and initial preview')
                duration = player.duration()
                if abs(duration - case['duration_ms']) > 800:
                    raise RuntimeError(f'Unexpected duration: {duration} ms')
                if case['kind'] == 'audio' and player.playbackState() != QtMultimedia.QMediaPlayer.PlaybackState.StoppedState:
                    raise RuntimeError('Audio started playing before Play was pressed.')
                if controller._source_video_time_label.text().endswith(('0:00', '…')):
                    raise RuntimeError('Duration was not displayed before playback.')
                report['duration_ms'] = duration
                stage('play')
                observations['video_frames'] = 0
                controller._source_video_button.click()
                pump_until(lambda: player.position() > 150 and (not case.get('audio') or observations['audio_frames'] > 0) and (case['kind'] != 'video' or observations['video_frames'] >= 2), 'decoded samples/frames and advancing playback')
                if case.get('audio') and not observations['nonzero_samples']:
                    raise RuntimeError('The tone fixture decoded only silent samples.')
                if case['kind'] == 'audio' and (observations['channels'] != case['channels'] or observations['sample_rate'] != case['sample_rate']):
                    raise RuntimeError(f'Unexpected decoded audio format: {observations}')
                if case['kind'] == 'video' and (observations['video_width'], observations['video_height']) != (96, 64):
                    raise RuntimeError('Unexpected decoded video dimensions.')
                stage('pause-and-seek')
                controller._source_video_button.click()
                pump_until(lambda: player.playbackState() == QtMultimedia.QMediaPlayer.PlaybackState.PausedState, 'pause')
                paused = player.position()
                deadline = time.monotonic() + .12
                pump_until(lambda: time.monotonic() >= deadline, 'stable pause')
                if abs(player.position() - paused) > 80:
                    raise RuntimeError('Playback advanced while paused.')
                controller._source_video_slider.setValue(500)
                controller._handle_source_video_seek_finished()
                pump_until(lambda: abs(player.position() - duration // 2) < 350, 'seek to midpoint')
                before = player.position()
                controller._source_video_button.click()
                pump_until(lambda: player.position() > before + 100, 'resume after seek')
                report['playback'] = observations
            stage('switch-source')
            combo.setCurrentIndex(1)
            if case['kind'] in {'audio', 'video'}:
                pump_until(lambda: Path(player.source().toLocalFile()) == alternate and player.duration() > 0, 'alternate source')
                if player.playbackState() != QtMultimedia.QMediaPlayer.PlaybackState.StoppedState:
                    raise RuntimeError('Switching sources did not stop previous playback.')
            elif case['kind'] == 'text':
                pump_until(lambda: not controller._text_preview._timer.isActive(), 'alternate text')
                if editor.toPlainText() != alternate.read_text(encoding='utf-8'):
                    raise RuntimeError('Source switching retained the previous text.')
            else:
                pump_until(lambda: not controller._pending, 'alternate image')
            combo.setCurrentIndex(0)
            if player is not None:
                pump_until(lambda: player.duration() > 0 and not controller._source_video_preview_pending, 'restored source')
                if Path(player.source().toLocalFile()) != source_path:
                    raise RuntimeError('Switching back loaded the wrong media.')
            elif case['kind'] == 'text':
                pump_until(lambda: not controller._text_preview._timer.isActive(), 'restored text')
                if editor.toPlainText() != case['text']:
                    raise RuntimeError('Switching back retained the alternate text.')
            else:
                pump_until(lambda: not controller._pending, 'restored image')
            report['source_switching'] = True
            stage('close-preview')
        finally:
            controller.close()
            if player is not None and (player.playbackState() != QtMultimedia.QMediaPlayer.PlaybackState.StoppedState or not player.source().isEmpty()):
                raise RuntimeError('Closing the dialog did not stop and unload playback.')
            thumbnails.unsubscribe_thumbnail_ready(bridge.ready.emit)
            thumbnails.shutdown()
            dialog.close()
            dialog.deleteLater()
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('case_id')
    parser.add_argument('fixtures', type=Path)
    parser.add_argument('result', type=Path)
    parser.add_argument('log', type=Path)
    parser.add_argument('--require-audio-output', action='store_true')
    args = parser.parse_args(argv)
    args.result.parent.mkdir(parents=True, exist_ok=True)
    report = dict(ok=False, case_id=args.case_id, stage='imports')

    def save():
        args.result.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')

    save()
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    with args.log.open('a', encoding='utf-8', buffering=1) as log, redirect_stdout(log), redirect_stderr(log):
        faulthandler.enable(file=log)
        faulthandler.dump_traceback_later(20, file=log)
        # Native Qt logging goes to the parent-owned stderr file. A Python
        # message handler can deadlock decoder shutdown while waiting for the GIL.
        try:
            from shared.media_runtime_audit import load_cases
            case = next(case for case in load_cases(args.fixtures) if case['id'] == args.case_id)
            probe(case, args.fixtures.resolve(), report, save, args.require_audio_output)
            report.update(ok=True, stage='complete')
        except Exception as error:
            report.update(error=f'{type(error).__name__}: {error}', traceback=traceback.format_exc())
            traceback.print_exc()
        finally:
            save()
            faulthandler.cancel_dump_traceback_later()
            faulthandler.disable()
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
