"""Rejected volume entries must not create misleading backup artifacts."""
import tarfile

import pytest

from ops.scene_volume_archive import archive_tree


def test_rejected_symlink_does_not_leave_partial_archive(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'first.txt').write_text('valid', encoding='utf-8')
    try:
        (source / 'linked.txt').symlink_to(source / 'first.txt')
    except OSError as exc:
        pytest.skip(f'Host cannot create symlinks: {exc}')
    target = tmp_path / 'backup.tar'
    with pytest.raises(ValueError, match='Symlinks are not allowed'):
        archive_tree(source, target)
    assert not target.exists()


def test_archive_write_failure_removes_partial_output(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'first.txt').write_text('valid', encoding='utf-8')
    (source / 'second.txt').write_text('also valid', encoding='utf-8')
    target = tmp_path / 'backup.tar'
    original_add = tarfile.TarFile.add
    calls = 0

    def interrupted_add(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('simulated disk failure')
        return original_add(self, *args, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, 'add', interrupted_add)
    with pytest.raises(OSError, match='simulated disk failure'):
        archive_tree(source, target)
    assert not target.exists()
