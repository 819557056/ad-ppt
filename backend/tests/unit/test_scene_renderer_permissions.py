"""Permission ordering regression: Scene coordinator deliberately has no FOWNER.

These unit tests simulate the ownership rule only. The opt-in Docker suite runs
real Chromium/LibreOffice with the actual production capability set.
"""
import json
from pathlib import Path
from types import SimpleNamespace

from renderer import daemon


def without_fowner(monkeypatch):
    owners, events = {}, []
    original = Path.chmod
    def chmod(path, mode, **kwargs):
        if owners.get(str(path), 0) != 0:
            raise PermissionError('coordinator lacks FOWNER')
        events.append(('chmod', str(path)))
        return original(path, mode, **kwargs)
    def chown(path, uid, gid):
        events.append(('chown', str(path)))
        owners[str(path)] = uid
    monkeypatch.setattr(Path, 'chmod', chmod)
    monkeypatch.setattr(daemon.os, 'chown', chown, raising=False)
    return events


def test_job_home_permissions_are_set_before_handoff_without_fowner(monkeypatch, tmp_path):
    events = without_fowner(monkeypatch)
    home = tmp_path / 'job-home'
    home.mkdir()
    monkeypatch.setattr(daemon.tempfile, 'mkdtemp', lambda **kwargs: str(home))
    process = SimpleNamespace(pid=987654, returncode=0, wait=lambda **kwargs: 0)
    monkeypatch.setattr(daemon.subprocess, 'Popen', lambda *args, **kwargs: process)
    monkeypatch.setattr(daemon, '_kill_process_group', lambda pid: None)
    monkeypatch.setattr(daemon, '_kill_uid_processes', lambda uid: None)
    assert daemon._run_unique_uid(['synthetic-test-command'], tmp_path, 21001, 21001) == ''
    assert [name for name, path in events if path == str(home)] == ['chmod', 'chown']
    assert not home.exists()


def test_smoke_directory_permissions_are_set_before_handoff_without_fowner(monkeypatch):
    events = without_fowner(monkeypatch)
    monkeypatch.setenv('SCENE_RENDER_REQUIRE_UNIQUE_UID', 'true')
    def simulated_job(command, directory, uid, gid, **kwargs):
        assert uid == gid == 9999
        if str(command[1]).endswith('render_pdf.mjs'):
            Path(command[-1]).write_bytes(b'unit-fixture-only-' * 10)
        elif str(command[1]).endswith('render_text_layout.mjs'):
            Path(command[-1]).write_text(json.dumps({'schema_version': 1, 'engine_identity': {'font_sha256': 'test'},
                'pages': [{'smoke': {'lines': [{'start': 0, 'end': len('健康检查 Scene 123'), 'break_kind': 'end'}],
                                     'resolved_fonts': [{'family_name': 'Noto Sans CJK SC',
                                         'postscript_name': 'NotoSansCJKsc-Regular', 'is_custom_font': True}]}}]}))
        elif command[-1] == '--pdf-only':
            (directory / 'output.pdf').write_bytes(b'unit-fixture-only')
        else:
            (directory / 'page-001.png').write_bytes(b'unit-fixture-only')
    monkeypatch.setattr(daemon, '_run_unique_uid', simulated_job)
    monkeypatch.setattr(daemon, '_office_smoke_pdf_valid', lambda path: path.read_bytes() == b'unit-fixture-only')
    assert daemon.smoke_runtime() == {'chromium_ok': True, 'libreoffice_ok': True, 'pptx_pdf_ok': True,
                                      'text_layout_identity': {'font_sha256': 'test'}}
    assert [name for name, path in events] == ['chmod', 'chown']
