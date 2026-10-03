"""Archive or restore a private data volume without following symlinks."""
import argparse
import os
import shutil
import tarfile
from pathlib import Path, PurePosixPath


def assert_empty(target):
    target = Path(target)
    if target.is_symlink() or not target.is_dir() or any(target.iterdir()):
        raise ValueError(f'Target volume must exist and be empty: {target}')


def archive_tree(source, output):
    if Path(source).is_symlink():
        raise ValueError('Source volume must not be a symlink')
    root = Path(source).resolve()
    target = Path(output).resolve()
    if not root.is_dir() or target.is_relative_to(root):
        raise ValueError('Source must be a directory and archive must be outside it')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError(f'Archive output already exists: {target}')
    # Validate the entire tree before opening the output. A rejected symlink or
    # device must not leave a plausible but incomplete backup archive behind.
    entries = sorted(root.rglob('*'))
    for path in entries:
        if path.is_symlink():
            raise ValueError(f'Symlinks are not allowed in backup volumes: {path}')
        if not path.is_file() and not path.is_dir():
            raise ValueError(f'Unsupported volume entry: {path}')
    archive = tarfile.open(target, 'x')
    try:
        with archive:
            for path in entries:
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    raise ValueError(f'Volume entry changed during backup: {path}')
                archive.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _member_path(root, member, seen):
    name = PurePosixPath(member.name)
    if (name.is_absolute() or not name.parts or any(part in ('', '.', '..') for part in name.parts)
        or not (member.isfile() or member.isdir())):
        raise ValueError(f'Unsafe archive member: {member.name}')
    normalized = name.as_posix()
    if normalized in seen:
        raise ValueError(f'Duplicate archive member: {member.name}')
    seen.add(normalized)
    path = root.joinpath(*name.parts)
    if not path.resolve().is_relative_to(root):
        raise ValueError(f'Archive member escapes target: {member.name}')
    return path


def restore_tree(archive_path, target):
    if Path(target).is_symlink():
        raise ValueError('Target volume must not be a symlink')
    root = Path(target).resolve()
    assert_empty(root)
    with tarfile.open(archive_path, 'r') as archive:
        members = archive.getmembers()
        seen = set()
        paths = [(member, _member_path(root, member, seen)) for member in members]
        for member, path in paths:
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f'Archive file cannot be read: {member.name}')
            with source, path.open('xb') as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    backup = commands.add_parser('backup')
    backup.add_argument('--source', required=True)
    backup.add_argument('--output', required=True)
    restore = commands.add_parser('restore')
    restore.add_argument('--archive', required=True)
    restore.add_argument('--target', required=True)
    check = commands.add_parser('check-empty')
    check.add_argument('--target', required=True)
    args = parser.parse_args()
    if args.command == 'backup':
        archive_tree(args.source, args.output)
    elif args.command == 'restore':
        restore_tree(args.archive, args.target)
    else:
        assert_empty(args.target)


if __name__ == '__main__':
    main()
