"""Queue limits cover every ingress and pre-reserve worker DAG expansion."""
import base64
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import threading
from uuid import uuid4

import pytest
from PIL import Image

from models import db, Project
from models.scene_v1 import Asset, ApiIdempotencyRecord, Principal, SceneExport, SceneTaskItem
from services.scene.queue_capacity import admit_tasks, task_units
from services.scene.versioning import LOCAL_OWNER_ID, SceneError
from workers.scene_runner import run_once


def key():
    return {'Idempotency-Key': str(uuid4())}


@pytest.fixture
def setup_queue(client, app, tmp_path, monkeypatch):
    monkeypatch.setitem(app.config, 'SCENE_EDITOR_ENABLED', True)
    monkeypatch.setitem(app.config, 'ASSET_STORE_ROOT', str(tmp_path / 'assets'))
    monkeypatch.setitem(app.config, 'CREDENTIAL_ENCRYPTION_KEY', base64.urlsafe_b64encode(b'q' * 32).decode())
    monkeypatch.setitem(app.config, 'MODEL_GATEWAY_ALLOWLIST', 'https://model.example')
    monkeypatch.setitem(app.config, 'SCENE_QUEUE_CAPACITY_UNITS', 1024)
    monkeypatch.setitem(app.config, 'OWNER_QUEUE_CAPACITY_UNITS', 256)
    project = client.post('/api/v2/projects', json={'title': 'Queue limits',
        'model_config': {'text_model': 'fake-text', 'image_model': 'fake-image'},
        'pages': [{'title': 'One'}, {'title': 'Two'}]}, headers=key())
    assert project.status_code == 201, project.json
    credential = client.post('/api/v2/model-credentials', json={'label': 'Mine',
        'base_url': 'https://model.example/v1', 'api_key': 'test-api-key'}, headers=key())
    assert credential.status_code == 201
    return project.json['data'], credential.json['data']['credential_id']


def seeded(project, operation='generate_outline', state='queued', *, result=None, owner=LOCAL_OWNER_ID):
    item = SceneTaskItem(id=str(uuid4()), owner_id=owner, project_id=project['project_id'],
        resource_id=project['project_id'], operation=operation, state=state,
        input_json={}, input_hash='0' * 64, result_json=result or {})
    db.session.add(item)
    db.session.commit()
    return item.id


def outline_body(project, credential):
    return {'base_project_version': project['project_version'], 'brief': 'Offline test',
            'slide_count': 2, 'credential_id': credential}


def test_active_units_include_future_children_but_not_terminal_history(client, app, setup_queue):
    project, _ = setup_queue
    with app.app_context():
        seeded(project, 'generate_scene')
        seeded(project, 'export', 'running')
        seeded(project, 'ai_edit', 'waiting_assets', result={'draft_asset_id': str(uuid4())})
        seeded(project, 'generate_asset')
        for terminal in ('failed', 'succeeded', 'cancelled', 'outcome_unknown'):
            seeded(project, 'generate_scene', terminal)
        assert task_units('ai_edit') == 7 and task_units('ai_edit', {'draft_asset_id': 'draft'}) == 1
    response = client.get('/api/v2/queue-capacity')
    assert response.status_code == 200
    assert response.json['data']['owner'] == {'active_items': 4, 'reserved_units': 10,
        'limit_units': 256, 'available_units': 246}
    assert response.json['data']['global']['reserved_units'] == 10
    assert response.json['data']['max_generated_assets_per_page'] == 6


@pytest.mark.parametrize('ingress', ['outline', 'template_upload', 'analysis', 'generation', 'edit', 'export', 'check'])
def test_every_task_ingress_rejects_full_queue_without_partial_writes(client, app, setup_queue, monkeypatch, ingress):
    project, credential = setup_queue
    root = '/api/v2/projects/' + project['project_id']
    if ingress == 'analysis':
        image = BytesIO(); Image.new('RGB', (32, 18)).save(image, format='PNG'); image.seek(0)
        imported = client.post(root + '/template-documents', data={'file': (image, 'reference.png')}, headers=key())
        assert imported.status_code == 202 and run_once()
        document_id = imported.json['data']['document_id']
    if ingress == 'generation':
        plan = client.post(root + '/generation-plans', json={'base_project_version': project['project_version']}, headers=key())
        assert plan.status_code == 201
    if ingress == 'export':
        snapshot = client.post(root + '/snapshots', json={'project_version': project['project_version'],
            'pages': [{'page_id': p['page_id'], 'revision_id': p['revision_id'], 'page_version': p['page_version']} for p in project['pages']]}, headers=key())
        assert snapshot.status_code == 201
    with app.app_context():
        seeded(project)
        before = (SceneTaskItem.query.count(), Asset.query.count(), SceneExport.query.count(), ApiIdempotencyRecord.query.count())
    monkeypatch.setitem(app.config, 'OWNER_QUEUE_CAPACITY_UNITS', 1)
    if ingress == 'outline':
        response = client.post(root + '/outline-tasks', json=outline_body(project, credential), headers=key())
    elif ingress == 'template_upload':
        image = BytesIO(); Image.new('RGB', (32, 18)).save(image, format='PNG'); image.seek(0)
        response = client.post(root + '/template-documents', data={'file': (image, 'reference.png')}, headers=key())
    elif ingress == 'analysis':
        response = client.post(root + '/template-documents/' + document_id + '/analyze', json={
            'credential_id': credential, 'selected_page_indexes': [1]}, headers=key())
    elif ingress == 'generation':
        response = client.post(root + '/generation-tasks', json={'plan_id': plan.json['data']['plan_id'], 'credential_id': credential,
            'targets': [{'page_id': p['page_id'], 'base_revision_id': p['revision_id'], 'base_page_version': p['page_version']} for p in project['pages']]}, headers=key())
    elif ingress == 'edit':
        page = project['pages'][0]
        response = client.post(root + '/pages/' + page['page_id'] + '/ai-edits', json={
            'base_revision_id': page['revision_id'], 'base_page_version': page['page_version'],
            'credential_id': credential, 'instruction': 'Simplify'}, headers=key())
    elif ingress == 'export':
        response = client.post(root + '/exports', json={'snapshot_id': snapshot.json['data']['snapshot_id'], 'format': 'pptx'}, headers=key())
    else:
        response = client.post('/api/v2/model-credentials/' + credential + '/check', json={'project_id': project['project_id']}, headers=key())
    assert response.status_code == 429, response.json
    assert response.json['error']['code'] == 'QUEUE_CAPACITY_EXCEEDED'
    assert response.json['error']['details']['scope'] == 'owner'
    with app.app_context():
        assert (SceneTaskItem.query.count(), Asset.query.count(), SceneExport.query.count(), ApiIdempotencyRecord.query.count()) == before
    if ingress == 'template_upload':
        from pathlib import Path
        assert not list(Path(app.config['ASSET_STORE_ROOT']).rglob('content.*'))


def test_generation_batch_is_atomic_and_idempotent_replay_ignores_full_capacity(client, app, setup_queue, monkeypatch):
    project, credential = setup_queue
    root = '/api/v2/projects/' + project['project_id']
    plan = client.post(root + '/generation-plans', json={'base_project_version': project['project_version']}, headers=key()).json['data']
    payload = {'plan_id': plan['plan_id'], 'credential_id': credential,
        'targets': [{'page_id': p['page_id'], 'base_revision_id': p['revision_id'], 'base_page_version': p['page_version']} for p in project['pages']]}
    monkeypatch.setitem(app.config, 'OWNER_QUEUE_CAPACITY_UNITS', 13)
    headers = key()
    denied = client.post(root + '/generation-tasks', json=payload, headers=headers)
    assert denied.status_code == 429 and denied.json['error']['details']['requested_units'] == 14
    with app.app_context():
        assert SceneTaskItem.query.count() == 0
    monkeypatch.setitem(app.config, 'OWNER_QUEUE_CAPACITY_UNITS', 14)
    accepted = client.post(root + '/generation-tasks', json=payload, headers=headers)
    assert accepted.status_code == 202
    replay = client.post(root + '/generation-tasks', json=payload, headers=headers)
    assert replay.status_code == 202 and replay.json['data'] == accepted.json['data']
    assert client.get('/api/v2/queue-capacity').json['data']['owner']['reserved_units'] == 14
    assert client.post(root + '/generation-tasks', json=payload, headers=key()).status_code == 429


def test_retry_is_admitted_once_and_cancel_releases_capacity(client, app, setup_queue, monkeypatch):
    project, _ = setup_queue
    with app.app_context():
        blocker = seeded(project)
        failed = seeded(project, state='failed')
    monkeypatch.setitem(app.config, 'OWNER_QUEUE_CAPACITY_UNITS', 1)
    url = '/api/v2/tasks/' + failed + '/retry'
    headers = key()
    denied = client.post(url, json={}, headers=headers)
    assert denied.status_code == 429
    with app.app_context():
        assert db.session.get(SceneTaskItem, failed).state == 'failed'
    assert client.post('/api/v2/tasks/' + blocker + '/cancel', json={}, headers=key()).status_code == 200
    accepted = client.post(url, json={}, headers=headers)
    assert accepted.status_code == 200
    assert client.post(url, json={}, headers=headers).json['data'] == accepted.json['data']
    assert client.post(url, json={}, headers=key()).status_code == 409
    assert client.get('/api/v2/queue-capacity').json['data']['owner']['reserved_units'] == 1


def test_global_capacity_counts_other_owners_but_owner_limit_does_not(client, app, setup_queue, monkeypatch):
    project, credential = setup_queue
    other_owner, other_project = str(uuid4()), str(uuid4())
    with app.app_context():
        db.session.add(Principal(id=other_owner, kind='sub2api_user', display_name='Other'))
        db.session.add(Project(id=other_project, owner_id=other_owner, editor_mode='scene_v1'))
        db.session.commit()
        seeded({'project_id': other_project}, owner=other_owner)
    monkeypatch.setitem(app.config, 'SCENE_QUEUE_CAPACITY_UNITS', 1)
    response = client.post('/api/v2/projects/' + project['project_id'] + '/outline-tasks', json=outline_body(project, credential), headers=key())
    assert response.status_code == 429 and response.json['error']['details']['scope'] == 'global'
    usage = client.get('/api/v2/queue-capacity').json['data']
    assert usage['owner']['reserved_units'] == 0 and usage['global']['reserved_units'] == 1


@pytest.mark.parametrize('child_count', [0, 6, 7])
def test_paid_draft_expansion_respects_reserved_budget(client, app, setup_queue, monkeypatch, child_count):
    project, credential = setup_queue
    root = '/api/v2/projects/' + project['project_id']
    page = project['pages'][0]
    plan = client.post(root + '/generation-plans', json={'base_project_version': project['project_version']}, headers=key()).json['data']
    monkeypatch.setitem(app.config, 'OWNER_QUEUE_CAPACITY_UNITS', 7)
    accepted = client.post(root + '/generation-tasks', json={'plan_id': plan['plan_id'], 'credential_id': credential,
        'targets': [{'page_id': page['page_id'], 'base_revision_id': page['revision_id'], 'base_page_version': page['page_version']}]}, headers=key())
    assert accepted.status_code == 202
    monkeypatch.setattr('services.scene.generation._model_call', lambda *_args, **_kwargs: {
        'background': {'kind': 'solid', 'color': '#FFFFFF'}, 'elements': [],
        'image_requests': [{'prompt': 'Decoration without text', 'role': 'decoration',
            'frame': {'x': index * 40, 'y': 0, 'w': 30, 'h': 30, 'rotation_deg': 0}} for index in range(child_count)]})
    assert run_once()
    usage = client.get('/api/v2/queue-capacity').json['data']['owner']
    parent_id = accepted.json['data']['tasks'][0]['task_id']
    with app.app_context():
        parent = db.session.get(SceneTaskItem, parent_id)
        if child_count > 6:
            assert parent.state == 'failed' and parent.error_code == 'MODEL_SCENE_INVALID'
            assert SceneTaskItem.query.filter_by(operation='generate_asset').count() == 0
            assert usage['reserved_units'] == 0
            return
        assert parent.state == 'waiting_assets' and parent.result_json['draft_asset_id']
        assert SceneTaskItem.query.filter_by(operation='generate_asset', state='queued').count() == child_count
    assert usage['active_items'] == usage['reserved_units'] == 1 + child_count
    response = client.post(root + '/outline-tasks', json=outline_body(project, credential), headers=key())
    assert response.status_code == (429 if child_count == 6 else 202)
    if child_count == 6:
        with app.app_context():
            child = SceneTaskItem.query.filter_by(operation='generate_asset').first()
            child.state = 'running'
            db.session.commit()
        assert client.post('/api/v2/tasks/' + parent_id + '/cancel', json={}, headers=key()).status_code == 200
        # Cancellation is only a request for running work: its unit stays used.
        remaining = client.get('/api/v2/queue-capacity').json['data']['owner']
        assert remaining['active_items'] == remaining['reserved_units'] == 1


def test_concurrent_sqlite_admissions_cannot_overfill(client, app, setup_queue, monkeypatch):
    project, credential = setup_queue
    monkeypatch.setitem(app.config, 'OWNER_QUEUE_CAPACITY_UNITS', 1)
    import services.scene.queue_capacity as capacity
    original = capacity._lock_admissions
    barrier = threading.Barrier(2)
    def start_together():
        barrier.wait(timeout=10)
        original()
    monkeypatch.setattr(capacity, '_lock_admissions', start_together)
    def submit():
        with app.test_client() as concurrent:
            result = concurrent.post('/api/v2/projects/' + project['project_id'] + '/outline-tasks',
                json=outline_body(project, credential), headers=key())
            return result.status_code, result.json
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: submit(), range(2)))
    assert sorted(code for code, _ in responses) == [202, 429], responses
    with app.app_context():
        assert SceneTaskItem.query.count() == 1


@pytest.mark.parametrize('invalid', [0, -1, True, '10'])
def test_invalid_capacity_fails_closed(client, app, setup_queue, monkeypatch, invalid):
    project, _ = setup_queue
    monkeypatch.setitem(app.config, 'OWNER_QUEUE_CAPACITY_UNITS', invalid)
    assert client.get('/api/v2/readiness').json['data']['checks']['queue_config'] is False
    with app.app_context():
        with pytest.raises(SceneError) as caught:
            admit_tasks(LOCAL_OWNER_ID, 'export')
        assert caught.value.code == 'QUEUE_CONFIG_INVALID'
        assert SceneTaskItem.query.count() == 0
