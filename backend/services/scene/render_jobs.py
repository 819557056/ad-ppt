"""File-manifest transport to an isolated, no-network renderer container."""
from services.scene.telemetry import stage, TRACE, IDS, valid_id

import json
import os
import shutil
import stat
import time
from pathlib import Path
from uuid import uuid4

from flask import current_app

from .versioning import SceneError


def _acquire_render_lock(root, timeout):
    """Only one private render job may be visible on the shared volume."""
    lock = open(root / '.render.lock', 'a+b')
    if os.name == 'nt':
        import msvcrt
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
    else:
        import fcntl
    deadline = time.monotonic() + timeout
    while True:
        try:
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock
        except (OSError, BlockingIOError) as exc:
            if time.monotonic() >= deadline:
                lock.close()
                raise SceneError('RENDERER_BUSY', 'Renderer job slot is busy', 503) from exc
            time.sleep(.25)


def _release_render_lock(lock):
    try:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    finally:
        lock.close()


def _clear_completed_orphans(root):
    for directory in root.iterdir():
        if not directory.is_dir() or not directory.resolve().is_relative_to(root):
            continue
        if not (directory / 'result.json').is_file():
            raise SceneError('RENDERER_ORPHANED_JOB',
                             'An unfinished renderer job needs operator recovery', 503)
        shutil.rmtree(directory)


def _read_render_output(job, filename, byte_limit):
    """Never follow a converter-created link into the privileged worker view."""
    path = job / filename
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0)
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > byte_limit:
                raise SceneError('RENDER_OUTPUT_INVALID', 'Renderer output is not a bounded regular file', 503)
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                payload = stream.read(byte_limit + 1)
            if len(payload) > byte_limit:
                raise SceneError('RENDER_TOO_LARGE', 'Renderer output exceeds limit', 503)
            return payload
        finally:
            os.close(fd)
    except OSError as exc:
        raise SceneError('RENDER_OUTPUT_INVALID', 'Renderer output is unavailable or unsafe', 503) from exc


def _allocate_unique_identity(root, base_uid):
    """Monotonic UID allocation under the cross-process render lock.

    Never recycle a UID while the job volume exists: a detached converter from
    an earlier job must not gain access to any later job directory.
    """
    if os.name != 'posix' or os.geteuid() != 0:
        raise SceneError('RENDERER_CONFIG_INVALID', 'Unique renderer UIDs require a root worker on Linux', 503)
    if base_uid < 10_000 or base_uid >= 2_147_483_647:
        raise SceneError('RENDERER_CONFIG_INVALID', 'Renderer UID base is invalid', 503)
    counter = root / '.render-uid-log'
    fd = os.open(counter, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or
            info.st_mode & 0o077 or info.st_size % 8):
            raise SceneError('RENDERER_CONFIG_INVALID', 'Renderer UID counter is unsafe', 503)
        if info.st_size:
            os.lseek(fd, -8, os.SEEK_END)
            last_uid = int.from_bytes(os.read(fd, 8), 'big')
            uid = last_uid + 1
        else:
            uid = base_uid
        if uid < base_uid or uid >= 2_147_483_647:
            raise SceneError('RENDERER_UID_EXHAUSTED', 'Renderer UID range exhausted or corrupted', 503)
        if os.write(fd, uid.to_bytes(8, 'big')) != 8:
            raise OSError('Short renderer UID log write')
        os.fsync(fd)
        return uid, uid
    except (OSError, ValueError) as exc:
        raise SceneError('RENDERER_CONFIG_INVALID', 'Renderer UID counter is unreadable', 503) from exc
    finally:
        os.close(fd)


def run_render_job(kind, payload, *, expected_count=None):
    with stage('render_' + kind):
        return _coordinate_render_job(kind, payload, expected_count=expected_count)


def _coordinate_render_job(kind, payload, *, expected_count=None):
    configured = current_app.config.get('SCENE_RENDER_JOB_ROOT')
    if not configured:
        raise SceneError('RENDERER_UNAVAILABLE', 'Isolated renderer job root not configured', 503)
    root = Path(configured).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with stage('render_queue'):
        lock = _acquire_render_lock(root, current_app.config['RENDER_TIMEOUT_SECONDS'])
    try:
        _clear_completed_orphans(root)
        return _run_render_job_unlocked(kind, payload, expected_count=expected_count)
    finally:
        _release_render_lock(lock)


def _run_render_job_unlocked(kind, payload, *, expected_count=None):
    root = Path(current_app.config.get('SCENE_RENDER_JOB_ROOT') or '').resolve()
    if not current_app.config.get('SCENE_RENDER_JOB_ROOT'):
        raise SceneError('RENDERER_UNAVAILABLE', 'Isolated renderer job root not configured', 503)
    root.mkdir(parents=True, exist_ok=True)
    job = (root / str(uuid4())).resolve()
    if not job.is_relative_to(root) or job == root:
        raise SceneError('RENDERER_PATH_INVALID', 'Invalid renderer job path', 503)
    renderer_uid = current_app.config.get('SCENE_RENDER_UID', -1)
    renderer_gid = current_app.config.get('SCENE_RENDER_GID', -1)
    unique_uid = current_app.config.get('SCENE_RENDER_UNIQUE_UID', False)
    if unique_uid:
        if renderer_uid >= 0 or renderer_gid >= 0:
            raise SceneError('RENDERER_CONFIG_INVALID', 'Fixed and unique renderer identities conflict', 503)
        renderer_uid, renderer_gid = _allocate_unique_identity(
            root, current_app.config.get('SCENE_RENDER_UID_BASE', 10_000))
    elif (renderer_uid < 0) != (renderer_gid < 0):
        raise SceneError('RENDERER_CONFIG_INVALID', 'Renderer UID and GID must be configured together', 503)
    job.mkdir(mode=0o700)
    def share(path):
        # Official worker runs as root and hands each private job to the
        # unprivileged renderer. A 0700 root-owned directory is unreadable to it.
        if renderer_uid >= 0:
            os.chown(path, renderer_uid, renderer_gid)
        path.chmod(0o600 if path.is_file() else 0o700)
    try:
        share(job)
        filename = 'input.html' if kind in ('pdf', 'html_preview', 'text_layout') else 'input.pptx'
        source = job / filename
        source.write_bytes(payload)
        share(source)
        manifest = job / 'manifest.json'
        manifest.write_text(json.dumps({'kind': kind, 'expected_count': expected_count,
                                        'trace': {key: valid_id((TRACE.get().ids if TRACE.get() else {}).get(key)) for key in IDS},
                                        'run_uid': renderer_uid if unique_uid else None,
                                        'run_gid': renderer_gid if unique_uid else None}), encoding='utf-8')
        share(manifest)
        ready = job / 'ready'
        ready.write_text('', encoding='utf-8')
        share(ready)
        deadline = time.monotonic() + current_app.config['RENDER_TIMEOUT_SECONDS']
        result_file = job / 'result.json'
        while time.monotonic() < deadline:
            if result_file.is_file():
                result = json.loads(_read_render_output(job, 'result.json', 64 * 1024))
                if result.get('status') != 'succeeded':
                    raise SceneError('RENDER_FAILED', 'Isolated renderer failed')
                if kind in ('pdf', 'pptx_pdf'):
                    output = _read_render_output(job, 'output.pdf', 100 * 1024 * 1024)
                    return output, result
                if kind == 'text_layout':
                    return _read_render_output(job, 'output.json', 8 * 1024 * 1024), result
                count = result.get('page_count')
                if count != expected_count:
                    raise SceneError('RENDER_INCOMPLETE', 'Template page count mismatch')
                files, remaining = [], 500 * 1024 * 1024
                for index in range(1, count + 1):
                    payload = _read_render_output(job, f'page-{index:03d}.png', remaining)
                    files.append(payload)
                    remaining -= len(payload)
                return files, result
            time.sleep(.5)
        raise SceneError('RENDER_TIMEOUT', 'Isolated renderer timed out')
    finally:
        # The resolved path has been verified inside the dedicated job root.
        if job.is_relative_to(root) and job != root:
            shutil.rmtree(job, ignore_errors=True)
