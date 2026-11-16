"""Release gate for every advertised item-preview format, using isolated Qt workers."""
from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

from shared.internal_workers import internal_worker_command, read_worker_result

FIXTURES = Path(__file__).resolve().parent / 'resources' / 'media-audit'
CASE_TIMEOUT_SECONDS = 30
AUDIT_TIMEOUT_SECONDS = 600


def load_cases(root: Path = FIXTURES) -> list[dict]:
    from DiscoveryArea.media_preview_service import SUPPORTED_PREVIEW_EXTENSIONS
    expected = SUPPORTED_PREVIEW_EXTENSIONS
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema_version') != 1 or not manifest.get('cases'):
        raise ValueError('Missing or unsupported media audit manifest.')
    seen = {kind: set() for kind in expected}
    identities = set()
    for case in manifest['cases']:
        name, kind, identity = case['file'], case['kind'], case['id']
        if Path(name).name != name or kind not in expected or not identity.replace('-', '').isalnum() or identity in identities:
            raise ValueError(f'Invalid or duplicate media audit case: {identity}')
        identities.add(identity)
        path = root / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != case['sha256']:
            raise ValueError(f'Missing or changed media audit fixture: {name}')
        seen[kind].add(path.suffix.lower())
    if seen != expected:
        raise ValueError(f'Media audit coverage differs from supported extensions: expected {expected}, found {seen}')
    return manifest['cases']


def audit_media_runtime(
    report_path: Path, *, checkpoint: Callable[[str], None] | None = None,
    fixtures: Path = FIXTURES, require_audio_output: bool = False,
) -> dict:
    """Default decoding for all formats, plus CPU decoding for every video fixture.

    Each child runs the installed executable, writes a result file, and has a
    native crash log. A missing audio endpoint is recorded separately from decode
    success; audible-output QA remains a manual release requirement.
    """
    cases = load_cases(fixtures)
    started = time.monotonic()
    report = dict(schema_version=1, ok=False, platform=sys.platform, machine=platform.machine(),
                  fixture_count=len(cases), audible_output_verified=False, cases=[])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    directory = report_path.with_suffix('')
    directory.mkdir(parents=True, exist_ok=True)
    planned = [(case, 'default') for case in cases] + [(case, 'software') for case in cases if case['kind'] == 'video']
    for case, mode in planned:
        identity = f'{case["id"]}-{mode}'
        if checkpoint is not None:
            checkpoint(f'media-playback:{identity}')
        reply = directory / f'{identity}.json'
        log = directory / f'{identity}.log'
        reply.unlink(missing_ok=True)
        result = dict(case_id=case['id'], file=case['file'], kind=case['kind'], mode=mode, ok=False,
                      log=str(log), result=str(reply))
        remaining = AUDIT_TIMEOUT_SECONDS - (time.monotonic() - started)
        if remaining <= 0:
            result['error'] = 'Media audit exceeded its overall time limit.'
            report['cases'].append(result)
            break
        environment = dict(os.environ)
        environment['QT_MEDIA_BACKEND'] = 'ffmpeg'
        environment['QT_FFMPEG_DEBUG'] = '1'
        environment['QT_FORCE_STDERR_LOGGING'] = '1'
        environment['QT_LOGGING_RULES'] = 'qt.multimedia.ffmpeg.*=true'
        environment.pop('QT_FFMPEG_DECODING_HW_DEVICE_TYPES', None)
        environment.pop('QT_FFMPEG_HW_ALLOW_PROFILE_MISMATCH', None)
        if mode == 'software':
            environment['QT_FFMPEG_DECODING_HW_DEVICE_TYPES'] = ','
        command = internal_worker_command('media-playback-audit', case['id'], fixtures, reply, log)
        if require_audio_output:
            command.append('--require-audio-output')
        try:
            log.write_text('', encoding='utf-8')
            with log.open('a', encoding='utf-8') as output:
                completed = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT,
                                           env=environment, timeout=min(CASE_TIMEOUT_SECONDS, remaining))
            result['exit_code'] = completed.returncode
            payload = read_worker_result(reply)
            if payload.get('case_id') != case['id']:
                raise ValueError('Worker reply does not match the requested fixture.')
            result['probe'] = payload
            result['ok'] = completed.returncode == 0 and payload['ok'] is True
            if not result['ok']:
                result['error'] = payload.get('error') or f'Worker exited with {completed.returncode} (0x{completed.returncode & 0xffffffff:08x}).'
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            result['error'] = f'{type(error).__name__}: {error}'
        report['cases'].append(result)
        report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    report['expected_case_count'] = len(planned)
    report['ok'] = len(report['cases']) == len(planned) and all(result['ok'] for result in report['cases'])
    report['elapsed_seconds'] = round(time.monotonic() - started, 3)
    report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def require_media_audit(report: dict, *, require_packaged: bool = False) -> None:
    media = report.get('media_playback', {})
    fixtures = load_cases()
    expected = {(case['id'], 'default') for case in fixtures}
    expected.update((case['id'], 'software') for case in fixtures if case['kind'] == 'video')
    results = media.get('cases', [])
    actual = {(case.get('case_id'), case.get('mode')) for case in results}
    if (media.get('ok') is not True or media.get('schema_version') != 1
            or media.get('fixture_count') != len(fixtures)
            or media.get('expected_case_count') != len(expected)
            or len(results) != len(expected) or actual != expected
            or any(case.get('ok') is not True for case in results)
            or (require_packaged and any(case.get('probe', {}).get('packaged') is not True for case in results))):
        raise ValueError('A complete successful packaged media-playback audit is required.')
