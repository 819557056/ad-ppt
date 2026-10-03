"""Private-volume byte quotas shared by API and workers.

Count files, not committed Asset rows: staged bytes and crash/rollback orphans
still consume quota. A cross-process volume lock serializes count + atomic write.
The lock is released before any database I/O and never spans provider calls.
This is an application quota, not an OS hard limit for unrelated volume writers.
"""
from contextlib import contextmanager
import errno
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import time
from uuid import UUID

from flask import current_app

from .errors import SceneError

LOCK_NAME = '.storage-quota.lock'


def _unsafe(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def private_root():
    configured = Path(current_app.config['ASSET_STORE_ROOT']).absolute()
    if configured.exists() or configured.is_symlink():
        if _unsafe(configured.lstat()):
            raise SceneError('STORAGE_LAYOUT_INVALID', 'Asset root must not be a link or reparse point', 503)
    configured.mkdir(parents=True, exist_ok=True)
    return configured.resolve()


def _owner(owner_id):
    try:
        if str(UUID(owner_id)) != owner_id:
            raise ValueError('noncanonical owner')
    except (ValueError, TypeError, AttributeError) as exc:
        raise SceneError('STORAGE_OWNER_INVALID', 'Storage owner is invalid') from exc
    return owner_id


def _limits():
    config = current_app.config
    names = ('SCENE_STORAGE_QUOTA_BYTES', 'OWNER_STORAGE_QUOTA_BYTES',
             'SCENE_STORAGE_LOCK_SECONDS', 'SCENE_STORAGE_MAX_ENTRIES')
    if any(type(config.get(name)) is not int or config[name] < 1 for name in names) or (
            type(config.get('SCENE_MIN_FREE_BYTES')) is not int or config['SCENE_MIN_FREE_BYTES'] < 0):
        raise SceneError('STORAGE_CONFIG_INVALID', 'Storage quotas and limits must be positive integers', 503)
    return tuple(config[name] for name in names)


@contextmanager
def volume_lock(root, *, timeout=None):
    """Cooperating writers/maintenance must use the same persistent lock inode."""
    if timeout is None:
        _, _, timeout, _ = _limits()
    if type(timeout) is not int or timeout < 1:
        raise ValueError('Invalid storage lock timeout')
    path = root / LOCK_NAME
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if _unsafe(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SceneError('STORAGE_LAYOUT_INVALID', 'Unsafe storage lock', 503)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o600)
    stream = os.fdopen(fd, 'r+b', buffering=0)
    acquired = False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SceneError('STORAGE_LAYOUT_INVALID', 'Unsafe storage lock', 503)
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
        else:
            import fcntl
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == 'nt':
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                if os.name == 'nt' and os.fstat(fd).st_size == 0:
                    # Windows permits locking beyond EOF; initialize only after
                    # winning the lock so first-use writers cannot race here.
                    stream.write(b'0')
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise SceneError('STORAGE_BUSY', '素材存储正忙，请稍后重试。', 503) from exc
                time.sleep(.05)
        yield
    finally:
        try:
            if acquired:
                if os.name == 'nt':
                    stream.seek(0)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            stream.close()


def _scan(root, owner_id):
    """Scan without following links; all bytes, including orphans, remain counted."""
    max_entries = current_app.config['SCENE_STORAGE_MAX_ENTRIES']
    total = owner_total = files = owner_files = entries = 0
    pending = [(root, ())]
    while pending:
        directory, relative = pending.pop()
        if len(relative) > 32:
            raise SceneError('STORAGE_LAYOUT_INVALID', 'Asset directory nesting is invalid', 503)
        with os.scandir(directory) as children:
            for child in children:
                parts = relative + (child.name,)
                is_probe = not relative and child.name.startswith('.scene-ready-')
                try:
                    # Windows DirEntry.stat reports st_nlink=0; an explicit
                    # lstat is required to validate hard-link count correctly.
                    info = os.stat(child.path, follow_symlinks=False)
                except FileNotFoundError:
                    if is_probe:
                        continue  # A readiness probe may finish while scanning.
                    raise
                if _unsafe(info):
                    raise SceneError('STORAGE_LAYOUT_INVALID', 'Links and reparse points are not supported in the asset volume', 503)
                entries += 1
                if entries > max_entries:
                    raise SceneError('STORAGE_ENTRY_LIMIT', '素材文件数量达到上限，需要管理员检查存储。', 507)
                if stat.S_ISDIR(info.st_mode):
                    pending.append((Path(child.path), parts))
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    if not relative and (child.name == LOCK_NAME or is_probe):
                        continue  # Administrative lock/probe bytes are not assets.
                    total += info.st_size
                    files += 1
                    if parts[:2] == ('owners', owner_id):
                        owner_total += info.st_size
                        owner_files += 1
                else:
                    raise SceneError('STORAGE_LAYOUT_INVALID', 'Only private regular files are supported in the asset volume', 503)
    return {'global_bytes': total, 'owner_bytes': owner_total,
            'global_files': files, 'owner_files': owner_files, 'entries': entries}


def _check(root, usage, incoming):
    global_limit, owner_limit, _, _ = _limits()
    for scope, limit in (('owner', owner_limit), ('global', global_limit)):
        used = usage[scope + '_bytes']
        if used + incoming > limit:
            raise SceneError('STORAGE_QUOTA_EXCEEDED',
                '素材存储配额不足，请联系管理员扩容或安全清理后重试；软删除项目或参考页不会释放历史素材。', 507,
                {'scope': scope, 'limit_bytes': limit, 'used_bytes': used, 'incoming_bytes': incoming})
    free = shutil.disk_usage(root).free
    if free - incoming < current_app.config['SCENE_MIN_FREE_BYTES']:
        raise SceneError('STORAGE_DISK_LOW', '素材磁盘剩余空间不足，请联系管理员处理后重试。', 503)


def storage_capacity(owner_id):
    _owner(owner_id)
    global_limit, owner_limit, _, _ = _limits()
    try:
        root = private_root()
        with volume_lock(root):
            usage = _scan(root, owner_id)
            free = shutil.disk_usage(root).free
    except OSError as exc:
        raise SceneError('STORAGE_UNAVAILABLE', 'Asset storage cannot be inspected', 503) from exc
    def summary(scope, limit):
        used = usage[scope + '_bytes']
        return {'used_bytes': used, 'limit_bytes': limit, 'available_bytes': max(0, limit - used),
                'file_count': usage[scope + '_files']}
    return {'owner': summary('owner', owner_limit), 'global': summary('global', global_limit),
            'disk_free_bytes': free, 'min_free_bytes': current_app.config['SCENE_MIN_FREE_BYTES'],
            'includes_uncommitted_files': True}


def check_storage_capacity(owner_id, incoming=1):
    """Fail early before paid work when already full. This is NOT a reservation."""
    _owner(owner_id)
    _limits()
    if type(incoming) is not int or incoming < 0:
        raise ValueError('Expected nonnegative incoming byte count')
    try:
        root = private_root()
        with volume_lock(root):
            _check(root, _scan(root, owner_id), incoming)
    except OSError as exc:
        raise SceneError('STORAGE_UNAVAILABLE', 'Asset storage cannot be inspected', 503) from exc


def write_asset_bytes(owner_id, storage_key, payload):
    _owner(owner_id)
    _limits()
    if not isinstance(storage_key, str) or PurePosixPath(storage_key).as_posix() != storage_key:
        raise SceneError('STORAGE_KEY_INVALID', 'Asset storage key is invalid')
    parts = PurePosixPath(storage_key).parts
    if (len(parts) != 7 or parts[:2] != ('owners', owner_id) or parts[2] != 'projects' or
            parts[4] != 'assets' or any(part in ('.', '..') for part in parts) or
            not re.fullmatch(r'content\.[a-z0-9]{1,12}', parts[6])):
        raise SceneError('STORAGE_KEY_INVALID', 'Asset storage key is invalid')
    try:
        if str(UUID(parts[3])) != parts[3] or str(UUID(parts[5])) != parts[5]:
            raise ValueError('noncanonical asset/project')
    except ValueError as exc:
        raise SceneError('STORAGE_KEY_INVALID', 'Asset storage key is invalid') from exc
    try:
        root = private_root()
        with volume_lock(root):
            usage = _scan(root, owner_id)
            _check(root, usage, len(payload))
            path = root.joinpath(*parts)
            # The scan rejected every existing link before creating any dirs.
            missing_dirs = sum(not parent.exists() for parent in path.parents if parent != root and parent.is_relative_to(root))
            if usage['entries'] + missing_dirs + 1 > current_app.config['SCENE_STORAGE_MAX_ENTRIES']:
                raise SceneError('STORAGE_ENTRY_LIMIT', '素材文件数量达到上限，需要管理员检查存储。', 507)
            path.parent.mkdir(parents=True, exist_ok=False, mode=0o700)
            temporary = path.with_suffix(path.suffix + '.staging')
            created = False
            try:
                with temporary.open('xb') as stream:
                    created = True
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                if os.name == 'posix':
                    fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            except OSError:
                # Remove only our own incomplete temp; a published file may
                # survive failure/DB rollback and must continue consuming quota.
                if created:
                    temporary.unlink(missing_ok=True)
                try:
                    path.parent.rmdir()
                except OSError:
                    pass
                raise
    except OSError as exc:
        raise SceneError('STORAGE_WRITE_FAILED', '素材写入失败，请检查磁盘后重试。', 503) from exc
