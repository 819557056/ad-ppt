"""No-network renderer daemon. Has only the job volume, fonts and render tools."""
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

import fitz

try:
    from .observability import job_event
except ImportError:  # Executed as /app/renderer/daemon.py in the container.
    from observability import job_event

ROOT = Path(os.environ.get('SCENE_RENDER_JOB_ROOT', '/jobs')).resolve()
HERE = Path(__file__).resolve().parent
FONT = HERE.parent / 'backend' / 'fonts' / 'NotoSansSC-Regular.ttf'
STATUS = ROOT / '.renderer_status.json'


class RendererIsolationFailure(RuntimeError):
    """Stop the daemon rather than expose another job after failed cleanup."""


def _job_identity(directory, manifest):
    if os.environ.get('SCENE_RENDER_REQUIRE_UNIQUE_UID') != 'true':
        return None
    uid, gid = manifest.get('run_uid'), manifest.get('run_gid')
    if (os.name != 'posix' or os.geteuid() != 0 or type(uid) is not int or
        type(gid) is not int or uid < 10_000 or uid != gid or uid >= 2_147_483_647):
        raise RendererIsolationFailure('per-job unprivileged identity is required')
    info = directory.stat()
    if info.st_uid != uid or info.st_gid != gid or info.st_mode & 0o077:
        raise RendererIsolationFailure('job directory ownership or mode is unsafe')
    return uid, gid


def _kill_process_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise RendererIsolationFailure('cannot terminate renderer process group') from exc


def _live_uid_pids(uid):
    pids = []
    try:
        entries = list(Path('/proc').iterdir())
    except OSError as exc:
        raise RendererIsolationFailure('cannot inspect renderer processes') from exc
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            status = (entry / 'status').read_text(encoding='utf-8')
            real_uid = next(int(line.split()[1]) for line in status.splitlines()
                            if line.startswith('Uid:'))
            if real_uid == uid:
                pids.append(int(entry.name))
        except FileNotFoundError:
            continue
        except (OSError, StopIteration, ValueError) as exc:
            raise RendererIsolationFailure('cannot inspect renderer process identity') from exc
    return pids


def _kill_uid_processes(uid):
    # A converter can detach from its process group. The monotonic UID is
    # exclusive to this job, so any remaining process with it is disposable.
    for _ in range(10):
        # This daemon is PID 1 in the renderer container; reap reparented
        # children so defunct processes do not look like live escapees.
        while True:
            try:
                if os.waitpid(-1, os.WNOHANG)[0] == 0:
                    break
            except ChildProcessError:
                break
            except OSError as exc:
                raise RendererIsolationFailure('cannot reap renderer process') from exc
        pids = _live_uid_pids(uid)
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as exc:
                raise RendererIsolationFailure('cannot terminate renderer process') from exc
        time.sleep(.1)
    if _live_uid_pids(uid):
        raise RendererIsolationFailure('job processes survived SIGKILL')


def _run_unique_uid(command, directory, uid, gid, *, timeout=None):
    home = Path(tempfile.mkdtemp(prefix='scene-render-home-'))
    # Configure permissions while we still own it: production drops FOWNER.
    home.chmod(0o700)
    os.chown(home, uid, gid)
    env = {**os.environ, 'HOME': str(home), 'TMPDIR': str(home),
           'XDG_CACHE_HOME': str(home / 'cache'), 'XDG_CONFIG_HOME': str(home / 'config')}
    process = None
    try:
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(command, stdout=output, stderr=subprocess.DEVNULL,
                cwd=directory, env=env, user=uid, group=gid, extra_groups=(),
                start_new_session=True)
            try:
                process.wait(timeout=timeout or int(os.environ.get('RENDER_TIMEOUT_SECONDS', '180')))
            except subprocess.TimeoutExpired as exc:
                _kill_process_group(process.pid)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired as stuck:
                    raise RendererIsolationFailure('converter survived SIGKILL') from stuck
                raise RuntimeError('renderer process timed out') from exc
            if process.returncode != 0:
                raise RuntimeError('renderer process failed')
            output.seek(0, os.SEEK_END)
            output.seek(max(0, output.tell() - 4096))
            return output.read().decode('utf-8', 'replace')
    finally:
        try:
            if process is not None:
                _kill_process_group(process.pid)
                _kill_uid_processes(uid)
        finally:
            try:
                shutil.rmtree(home)
            except OSError as exc:
                raise RendererIsolationFailure('job home cleanup failed') from exc


def _office_smoke_pdf_valid(path):
    with fitz.open(path) as document:
        return len(document) == 1 and ''.join(chr(character[0])
            for span in document[0].get_texttrace() for character in span['chars']) == 'Scene readiness 中文 123'


def smoke_runtime():
    """Exercise the bundled browser/font and LibreOffice without network input."""
    checks = {'chromium_ok': False, 'libreoffice_ok': False, 'pptx_pdf_ok': False}
    with tempfile.TemporaryDirectory(prefix='scene-render-smoke-') as temporary:
        directory = Path(temporary)
        smoke_identity = 9999 if os.environ.get('SCENE_RENDER_REQUIRE_UNIQUE_UID') == 'true' else None
        if smoke_identity is not None:
            directory.chmod(0o700)
            os.chown(directory, smoke_identity, smoke_identity)
        html = directory / 'smoke.html'
        html.write_text('<!doctype html><meta charset="utf-8"><style>'
            f'@font-face{{font-family:NotoScene;src:url("{FONT.as_uri()}")}}'
            '@page{size:160pt 90pt;margin:0}'
            'body{font-family:NotoScene;margin:0;font-size:12pt;line-height:1.2}'
            '[data-scene-id]{width:160pt;height:60pt}'
            '[data-scene-paragraph]{min-height:1lh}</style>'
            '<section class="slide"><div data-scene-id="smoke"><div data-scene-content>'
            '<div data-scene-paragraph>健康检查 Scene 123</div></div></div></section>', encoding='utf-8')
        pdf = directory / 'smoke.pdf'
        try:
            command = ['node', str(HERE / 'render_pdf.mjs'), str(html), str(pdf)]
            if smoke_identity is None:
                passed = subprocess.run(command, capture_output=True, timeout=45).returncode == 0
            else:
                _run_unique_uid(command, directory, smoke_identity, smoke_identity, timeout=45)
                passed = True
            checks['chromium_ok'] = passed and pdf.is_file() and pdf.stat().st_size > 100
        except RendererIsolationFailure:
            raise
        except (OSError, subprocess.TimeoutExpired, RuntimeError):
            pass
        if checks['chromium_ok']:
            layout = directory / 'layout.json'
            try:
                command = ['node', str(HERE / 'render_text_layout.mjs'), str(html), str(layout)]
                if smoke_identity is None:
                    passed = subprocess.run(command, capture_output=True, timeout=45).returncode == 0
                else:
                    _run_unique_uid(command, directory, smoke_identity, smoke_identity, timeout=45)
                    passed = True
                measured = json.loads(layout.read_text(encoding='utf-8')) if passed else None
                smoke = measured['pages'][0]['smoke'] if measured else {}
                lines = smoke.get('lines', [])
                checks['chromium_ok'] = bool(measured and measured.get('schema_version') == 1 and
                    len(lines) == 1 and lines[0].get('break_kind') == 'end' and
                    lines[0].get('start') == 0 and lines[0].get('end') == len('健康检查 Scene 123') and
                    smoke.get('resolved_fonts') and all(font.get('is_custom_font') is True and
                        font.get('family_name') == 'Noto Sans CJK SC' and
                        font.get('postscript_name') == 'NotoSansCJKsc-Regular'
                        for font in smoke['resolved_fonts']) and
                    measured.get('engine_identity', {}).get('font_sha256'))
                if checks['chromium_ok']:
                    checks['text_layout_identity'] = measured['engine_identity']
            except RendererIsolationFailure:
                raise
            except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError, KeyError, IndexError, TypeError):
                checks['chromium_ok'] = False
        try:
            command = [os.environ.get('SCENE_RENDER_PYTHON', 'python3'),
                       str(HERE / 'convert_template.py'), str(HERE / 'smoke.pptx'), str(directory)]
            if smoke_identity is None:
                passed = subprocess.run(command, capture_output=True, timeout=60).returncode == 0
            else:
                _run_unique_uid(command, directory, smoke_identity, smoke_identity, timeout=60)
                passed = True
            checks['libreoffice_ok'] = passed and (directory / 'page-001.png').is_file()
            if checks['libreoffice_ok']:
                command.append('--pdf-only')
                if smoke_identity is None:
                    passed = subprocess.run(command, capture_output=True, timeout=60).returncode == 0
                else:
                    _run_unique_uid(command, directory, smoke_identity, smoke_identity, timeout=60)
                    passed = True
                if passed and (directory / 'output.pdf').is_file():
                    checks['pptx_pdf_ok'] = _office_smoke_pdf_valid(directory / 'output.pdf')

        except RendererIsolationFailure:
            raise
        except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError):
            pass
    return checks


def publish_status(checks):
    temporary = ROOT / f'.renderer_status.{uuid4()}.tmp'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump({'updated_at': time.time(), **checks,
            'isolation_mode': 'unique_uid' if os.environ.get('SCENE_RENDER_REQUIRE_UNIQUE_UID') == 'true'
                              else 'fixed_uid'}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(STATUS)


def _result_metadata(kind, stdout):
    lines = stdout.strip().splitlines()
    version_line = (next((line for line in lines if not line.startswith('SCENE_RENDER_WARNINGS=')), None)
                    if kind in ('pptx', 'pptx_pdf') else (lines[-1] if lines else None))
    metadata = {'engine_version': version_line[:120] if version_line else None}
    if kind in ('pptx', 'pptx_pdf'):
        allowed = {'FONT_FAMILY_UNAVAILABLE', 'FONT_AVAILABILITY_UNVERIFIED'}
        warnings = set()
        for line in lines:
            if line.startswith('SCENE_RENDER_WARNINGS='):
                warnings.update(code for code in line.partition('=')[2].split(',') if code in allowed)
        metadata['warnings'] = sorted(warnings)
    return metadata


def run_job(directory):
    started = time.monotonic()
    manifest = {}
    try:
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        result = _run_job(directory, manifest)
    except Exception as exc:
        job_event(manifest, started, outcome='failed', error_code=(
            'RENDER_ISOLATION_FAILED' if isinstance(exc, RendererIsolationFailure) else 'RENDER_FAILED'))
        raise
    job_event(manifest, started, outcome='succeeded')
    return result


def _run_job(directory, manifest):
    identity = _job_identity(directory, manifest)
    kind = manifest.get('kind')
    if kind == 'pdf':
        command = ['node', str(HERE / 'render_pdf.mjs'), str(directory / 'input.html'), str(directory / 'output.pdf')]
    elif kind == 'text_layout':
        command = ['node', str(HERE / 'render_text_layout.mjs'),
                   str(directory / 'input.html'), str(directory / 'output.json')]
    elif kind == 'html_preview':
        command = ['node', str(HERE / 'render_preview.mjs'), str(directory / 'input.html'), str(directory)]
    elif kind in ('pptx', 'pptx_pdf'):
        command = [os.environ.get('SCENE_RENDER_PYTHON', 'python3'), str(HERE / 'convert_template.py'),
                   str(directory / 'input.pptx'), str(directory)]
        if kind == 'pptx_pdf':
            command.append('--pdf-only')
    else:
        raise ValueError('unsupported job kind')
    if identity is None:
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=int(os.environ.get('RENDER_TIMEOUT_SECONDS', '180')))
        if result.returncode != 0:
            raise RuntimeError('renderer process failed')
        stdout = result.stdout
    else:
        stdout = _run_unique_uid(command, directory, *identity)
    output = {'status': 'succeeded', 'kind': kind, **_result_metadata(kind, stdout)}
    if kind in ('pptx', 'html_preview'):
        output['page_count'] = len(list(directory.glob('page-*.png')))
    return output


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    if os.environ.get('SCENE_RENDER_REQUIRE_UNIQUE_UID') == 'true':
        if os.name != 'posix' or os.geteuid() != 0:
            raise RendererIsolationFailure('renderer coordinator must run as root')
        os.chown(ROOT, 0, 0)
        ROOT.chmod(0o711)
    unavailable = {'chromium_ok': False, 'libreoffice_ok': False, 'pptx_pdf_ok': False}
    publish_status(unavailable)
    runtime = {'checks': smoke_runtime()}
    publish_status(runtime['checks'])
    status_lock = threading.Lock()
    def heartbeat():
        while True:
            time.sleep(2)
            with status_lock:
                publish_status(runtime['checks'])
    threading.Thread(target=heartbeat, name='renderer-heartbeat', daemon=True).start()
    next_probe = time.monotonic() + 300
    while True:
        if time.monotonic() >= next_probe:
            with status_lock:
                runtime['checks'] = unavailable
                publish_status(unavailable)
            checks = smoke_runtime()
            with status_lock:
                runtime['checks'] = checks
                publish_status(checks)
            next_probe = time.monotonic() + 300
        found = False
        for directory in ROOT.iterdir():
            if not directory.is_dir() or not directory.resolve().is_relative_to(ROOT):
                continue
            ready = directory / 'ready'
            running = directory / 'running'
            try:
                ready.rename(running)
            except OSError:
                continue
            found = True
            fatal = False
            try:
                result = run_job(directory)
            except Exception as exc:
                result = {'status': 'failed', 'error_code': type(exc).__name__}
                fatal = isinstance(exc, RendererIsolationFailure)
            if os.environ.get('SCENE_RENDER_REQUIRE_UNIQUE_UID') == 'true':
                os.chown(directory, 0, 0)
                directory.chmod(0o700)
            temp = directory / f'.result-{uuid4()}.tmp'
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(result, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(directory / 'result.json')
            if fatal:
                with status_lock:
                    runtime['checks'] = unavailable
                    publish_status(unavailable)
                raise SystemExit('Renderer isolation failed; refusing further jobs')
        if not found:
            time.sleep(.5)


if __name__ == '__main__':
    main()
