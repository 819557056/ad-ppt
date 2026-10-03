"""Physical-file quota regression: rollback, races and unsafe paths."""
import base64
from io import BytesIO
import os
from types import SimpleNamespace
from uuid import uuid4

from PIL import Image
import pytest

from models import db
from models.scene_v1 import Asset
from services.scene.errors import SceneError
from services.scene.storage_quota import (LOCK_NAME, _unsafe, check_storage_capacity,
    storage_capacity, write_asset_bytes)
from services.scene.store import path_for, put_bytes
from services.scene.versioning import LOCAL_OWNER_ID
from workers.scene_runner import run_once


def headers():
    return {'Idempotency-Key': str(uuid4())}


def storage_key(owner=LOCAL_OWNER_ID, project=None):
    return f'owners/{owner}/projects/{project or uuid4()}/assets/{uuid4()}/content.bin'


@pytest.fixture
def storage(client, app, tmp_path, monkeypatch):
    monkeypatch.setitem(app.config, 'SCENE_EDITOR_ENABLED', True)
    monkeypatch.setitem(app.config, 'ASSET_STORE_ROOT', str(tmp_path / 'store'))
    monkeypatch.setitem(app.config, 'OWNER_STORAGE_QUOTA_BYTES', 1024 * 1024)
    monkeypatch.setitem(app.config, 'SCENE_STORAGE_QUOTA_BYTES', 2 * 1024 * 1024)
    monkeypatch.setitem(app.config, 'SCENE_STORAGE_LOCK_SECONDS', 2)
    monkeypatch.setitem(app.config, 'SCENE_STORAGE_MAX_ENTRIES', 1000)
    monkeypatch.setitem(app.config, 'SCENE_MIN_FREE_BYTES', 0)
    project = client.post('/api/v2/projects', json={'title': 'Storage',
        'model_config': {'text_model': 'fake-text'}}, headers=headers())
    assert project.status_code == 201
    return project.json['data'], tmp_path / 'store'


def test_rollback_orphans_remain_counted_and_cannot_bypass_quota(client, app, storage, monkeypatch):
    project, root = storage
    monkeypatch.setitem(app.config, 'OWNER_STORAGE_QUOTA_BYTES', 100)
    with app.app_context():
        asset = put_bytes(LOCAL_OWNER_ID, project['project_id'], 'upload', b'x' * 80, 'application/octet-stream', 'bin')
        saved_path = path_for(asset)
        db.session.rollback()
        assert Asset.query.count() == 0 and saved_path.read_bytes() == b'x' * 80
        with pytest.raises(SceneError) as exc:
            put_bytes(LOCAL_OWNER_ID, project['project_id'], 'upload', b'y' * 21, 'application/octet-stream', 'bin')
        assert exc.value.code == 'STORAGE_QUOTA_EXCEEDED' and exc.value.status == 507
    response = client.get('/api/v2/storage-capacity')
    assert response.status_code == 200
    assert response.json['data']['owner'] == {'used_bytes': 80, 'limit_bytes': 100, 'available_bytes': 20, 'file_count': 1}
    assert response.json['data']['includes_uncommitted_files'] is True
    assert len(list(root.rglob('content.*'))) == 1


def test_staging_bytes_and_other_owners_count_without_database_state(app, storage, monkeypatch):
    _, root = storage
    another = str(uuid4())
    monkeypatch.setitem(app.config, 'SCENE_STORAGE_QUOTA_BYTES', 100)
    with app.app_context():
        write_asset_bytes(another, storage_key(another), b'a' * 70)
        leftover = root / 'owners' / LOCAL_OWNER_ID / 'interrupted.staging'
        leftover.parent.mkdir(parents=True)
        leftover.write_bytes(b'partial' * 3)
        observed = storage_capacity(LOCAL_OWNER_ID)
        assert observed['owner']['used_bytes'] == 21 and observed['global']['used_bytes'] == 91
        with pytest.raises(SceneError) as exc:
            write_asset_bytes(LOCAL_OWNER_ID, storage_key(), b'x' * 10)
        assert exc.value.details['scope'] == 'global'
        assert observed['owner']['available_bytes'] > 10


def test_upload_has_no_partial_asset_and_same_key_can_retry_after_increase(client, app, storage, monkeypatch):
    project, root = storage
    stream = BytesIO(); Image.new('RGB', (40, 20), '#24415c').save(stream, format='PNG')
    payload = stream.getvalue()
    monkeypatch.setitem(app.config, 'OWNER_STORAGE_QUOTA_BYTES', 1)
    url = '/api/v2/projects/' + project['project_id'] + '/assets'
    key = headers()
    def upload():
        return client.post(url, data={'file': (BytesIO(payload), 'test.png')}, headers=key)
    denied = upload()
    assert denied.status_code == 507 and denied.json['error']['code'] == 'STORAGE_QUOTA_EXCEEDED'
    with app.app_context():
        assert Asset.query.count() == 0
    assert not list(root.rglob('content.*'))
    monkeypatch.setitem(app.config, 'OWNER_STORAGE_QUOTA_BYTES', len(payload))
    accepted = upload()
    assert accepted.status_code == 201, accepted.json
    replay = upload()
    assert replay.status_code == 201 and replay.json['data']['asset_id'] == accepted.json['data']['asset_id']
    assert len(list(root.rglob('content.*'))) == 1
    archived = client.delete('/api/v2/projects/' + project['project_id'], json={}, headers=headers())
    assert archived.status_code == 200
    assert client.get('/api/v2/storage-capacity').json['data']['owner']['used_bytes'] == len(payload)
    assert client.get(accepted.json['data']['url']).status_code == 200


def test_disk_floor_and_failed_rename_leave_no_partial_files(app, storage, monkeypatch):
    _, root = storage
    with app.app_context():
        monkeypatch.setitem(app.config, 'SCENE_MIN_FREE_BYTES', 64)
        with monkeypatch.context() as disk:
            disk.setattr('services.scene.storage_quota.shutil.disk_usage', lambda _root: SimpleNamespace(free=70))
            with pytest.raises(SceneError) as exc:
                write_asset_bytes(LOCAL_OWNER_ID, storage_key(), b'x' * 7)
            assert exc.value.code == 'STORAGE_DISK_LOW'
        assert not list(root.rglob('content.*'))
        with monkeypatch.context() as rename:
            def failed(*_args):
                raise OSError('simulated rename failure')
            rename.setattr('services.scene.storage_quota.os.replace', failed)
            with pytest.raises(SceneError) as exc:
                write_asset_bytes(LOCAL_OWNER_ID, storage_key(), b'partial')
            assert exc.value.code == 'STORAGE_WRITE_FAILED'
        assert not list(root.rglob('*.staging'))
        assert storage_capacity(LOCAL_OWNER_ID)['owner']['used_bytes'] == 0


def test_volume_entry_limit_prevents_unbounded_small_files(app, storage, monkeypatch):
    _, root = storage
    with app.app_context():
        monkeypatch.setitem(app.config, 'SCENE_STORAGE_MAX_ENTRIES', 2)
        with pytest.raises(SceneError) as exc:
            write_asset_bytes(LOCAL_OWNER_ID, storage_key(), b'x')
        assert exc.value.code == 'STORAGE_ENTRY_LIMIT'
        assert list(root.iterdir()) == [root / LOCK_NAME]


@pytest.mark.parametrize('bad', ['../outside.bin', 'owners/x/projects/y/assets/z/content.bin', None, '/absolute/content.bin'])
def test_invalid_storage_keys_never_escape_volume(app, storage, bad):
    with app.app_context():
        with pytest.raises(SceneError) as exc:
            write_asset_bytes(LOCAL_OWNER_ID, bad, b'x')
        assert exc.value.code == 'STORAGE_KEY_INVALID'


def test_hardlinks_and_reparse_metadata_fail_closed(app, storage, tmp_path):
    _, root = storage
    root.mkdir()
    outside = tmp_path / 'outside.bin'; outside.write_bytes(b'private')
    os.link(outside, root / 'linked.bin')
    with app.app_context():
        with pytest.raises(SceneError) as exc:
            write_asset_bytes(LOCAL_OWNER_ID, storage_key(), b'x')
        assert exc.value.code == 'STORAGE_LAYOUT_INVALID'
    assert outside.read_bytes() == b'private'
    assert _unsafe(SimpleNamespace(st_mode=0, st_file_attributes=0x400))


@pytest.mark.parametrize('name,value', [('OWNER_STORAGE_QUOTA_BYTES', 0), ('SCENE_STORAGE_QUOTA_BYTES', -1),
    ('SCENE_STORAGE_LOCK_SECONDS', True), ('SCENE_STORAGE_MAX_ENTRIES', '100'), ('SCENE_MIN_FREE_BYTES', -1)])
def test_invalid_storage_configuration_fails_closed(client, app, storage, monkeypatch, name, value):
    monkeypatch.setitem(app.config, name, value)
    assert client.get('/api/v2/readiness').json['data']['checks']['storage_config'] is False
    with app.app_context():
        with pytest.raises(SceneError) as exc:
            check_storage_capacity(LOCAL_OWNER_ID)
        assert exc.value.code == 'STORAGE_CONFIG_INVALID'


@pytest.mark.parametrize('already_full', [True, False])
def test_paid_work_checks_before_dispatch_and_marks_late_failure_chargeable(client, app, storage, monkeypatch, already_full):
    project, _ = storage
    monkeypatch.setitem(app.config, 'CREDENTIAL_ENCRYPTION_KEY', base64.urlsafe_b64encode(b's' * 32).decode())
    monkeypatch.setitem(app.config, 'MODEL_GATEWAY_ALLOWLIST', 'https://model.example')
    credential = client.post('/api/v2/model-credentials', json={'label': 'Storage test',
        'base_url': 'https://model.example/v1', 'api_key': 'sandbox-test-key'}, headers=headers()).json['data']
    calls = []
    def fake_chat(*_args, **_kwargs):
        calls.append(True)
        app.config['OWNER_STORAGE_QUOTA_BYTES'] = 1
        return ({'pages': [{'role': 'cover', 'title': 'Test', 'points': [], 'facts_needed': []}]}, 'req-test', {'tokens': 3})
    monkeypatch.setattr('services.scene.generation.chat_json', fake_chat)
    if already_full:
        with app.app_context():
            write_asset_bytes(LOCAL_OWNER_ID, storage_key(), b'x')
        monkeypatch.setitem(app.config, 'OWNER_STORAGE_QUOTA_BYTES', 1)
    task = client.post('/api/v2/projects/' + project['project_id'] + '/outline-tasks', json={
        'base_project_version': project['project_version'], 'credential_id': credential['credential_id'],
        'brief': 'Offline', 'slide_count': 1}, headers=headers())
    assert task.status_code == 202
    assert run_once()
    result = client.get('/api/v2/tasks/' + task.json['data']['task_id']).json['data']
    assert result['state'] == 'failed' and result['error_code'] == 'STORAGE_QUOTA_EXCEEDED'
    assert result['possible_charge'] is not already_full
    assert bool(calls) is not already_full
    assert run_once() is False  # No automatic paid retry after storage failure.


def test_two_os_processes_share_the_volume_quota(app, storage, tmp_path):
    from contextlib import ExitStack
    import json
    from pathlib import Path
    import subprocess
    import sys
    import time
    _, root = storage
    backend = Path(__file__).resolve().parents[2]
    gate = tmp_path / 'start'
    script = r"""
import json, sys, time
from pathlib import Path
from flask import Flask
from services.scene.storage_quota import write_asset_bytes
from services.scene.errors import SceneError
import services.scene.storage_quota as quota
root, gate, ready, key = sys.argv[1:]
app = Flask(__name__)
app.config.update(ASSET_STORE_ROOT=root, OWNER_STORAGE_QUOTA_BYTES=100, SCENE_STORAGE_QUOTA_BYTES=100,
    SCENE_STORAGE_LOCK_SECONDS=5, SCENE_STORAGE_MAX_ENTRIES=1000, SCENE_MIN_FREE_BYTES=0)
original = quota._scan
def slow_scan(*args):
    time.sleep(.2)
    return original(*args)
quota._scan = slow_scan
Path(ready).write_text('ready')
deadline = time.monotonic() + 10
while not Path(gate).exists():
    if time.monotonic() > deadline: raise RuntimeError('test gate timed out')
    time.sleep(.02)
with app.app_context():
    try:
        write_asset_bytes('00000000-0000-4000-8000-000000000001', key, b'x' * 80)
        print(json.dumps({'result': 'stored'}))
    except SceneError as exc:
        print(json.dumps({'result': exc.code}))
"""
    ready = [tmp_path / ('ready-' + str(index)) for index in range(2)]
    with ExitStack() as stack:
        processes = [stack.enter_context(subprocess.Popen([sys.executable, '-c', script, str(root), str(gate),
            str(ready[index]), storage_key()], cwd=backend, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))) for index in range(2)]
        deadline = time.monotonic() + 10
        while not all(path.exists() for path in ready):
            assert all(process.poll() is None for process in processes), 'Child process failed before gate'
            assert time.monotonic() < deadline, 'Children did not reach start gate'
            time.sleep(.02)
        gate.write_text('start')
        outputs = [process.communicate(timeout=15) for process in processes]
        assert all(process.returncode == 0 for process in processes), outputs
        results = sorted(json.loads(out)['result'] for out, _ in outputs)
        assert results == ['STORAGE_QUOTA_EXCEEDED', 'stored'], outputs
    with app.app_context():
        assert storage_capacity(LOCAL_OWNER_ID)['owner']['used_bytes'] == 80
