"""Outline candidate contract: bounded metadata, immutable baseline, explicit acceptance."""
import base64
from copy import deepcopy
from uuid import uuid4

import pytest

from models import db
from models.scene_v1 import Asset, SceneTaskAttempt, SceneTaskItem
from services.scene.outline import normalize_outline_draft, normalize_outline_page
from services.scene.store import checked_bytes
from workers.scene_runner import run_once
from backend.tests.integration.test_scene_v1 import create, key


def page():
    return {'title': '销售进展', 'role': 'content', 'points': ['已提供的业务事实'],
            'facts_needed': ['实际收入待提供'], 'sources': ['用户提供的第三季度台账（未核验）']}


@pytest.mark.parametrize(('field', 'value'), [
    ('title', 3), ('title', 'x' * 501), ('title', 'bad\x00text'),
    ('points', ['x'] * 21), ('facts_needed', ['x'] * 21), ('sources', ['x'] * 21),
    ('sources', [3]), ('facts_needed', ['x' * 1001]), ('sources', ['bad\ud800text']),
    ('role', 'executive-summary'), ('role', {}), ('unexpected', 'ignored?'),
])
def test_outline_schema_rejects_malformed_metadata(field, value):
    with pytest.raises(ValueError):
        normalize_outline_draft({'pages': [{**page(), field: value}]}, 1)


def test_outline_metadata_bounds_are_independent_and_legacy_shape_is_preserved():
    value = {**page(), 'points': ['要点'] * 20, 'facts_needed': ['待补'] * 20, 'sources': ['来源'] * 20}
    assert normalize_outline_draft({'pages': [value]}, 1)['pages'] == [value]
    assert normalize_outline_page({'title': '旧大纲'}) == {'title': '旧大纲', 'points': []}
    old_draft = page(); old_draft.pop('sources')
    assert normalize_outline_draft({'pages': [old_draft]}, 1)['pages'][0]['sources'] == []
    with pytest.raises(ValueError):
        normalize_outline_draft({'pages': [page()]}, 10)
    with pytest.raises(ValueError):
        normalize_outline_draft({'pages': [{'title': 'not a model draft'}]}, 1)


@pytest.fixture
def outline_task(client, app, monkeypatch, tmp_path):
    monkeypatch.setitem(app.config, 'SCENE_EDITOR_ENABLED', True)
    monkeypatch.setitem(app.config, 'ASSET_STORE_ROOT', str(tmp_path / 'assets'))
    monkeypatch.setitem(app.config, 'SCENE_RENDER_JOB_ROOT', None)
    monkeypatch.setitem(app.config, 'CREDENTIAL_ENCRYPTION_KEY', base64.urlsafe_b64encode(b'o' * 32).decode())
    monkeypatch.setitem(app.config, 'MODEL_GATEWAY_ALLOWLIST', 'https://outline.invalid')
    project, initial = create(client, app)
    root = '/api/v2/projects/' + project['project_id']
    credential = client.post('/api/v2/model-credentials', json={'label': 'Outline QA',
        'base_url': 'https://outline.invalid/v1', 'api_key': 'fake-outline-only'}, headers=key())
    assert credential.status_code == 201
    configured = client.patch(root + '/model-config', json={'base_project_version': 0,
        'model_config': {'text_model': 'fake-text'}}, headers=key())
    assert configured.status_code == 200

    def enqueue(count=1):
        response = client.post(root + '/outline-tasks', json={'base_project_version': 1,
            'brief': '用户提供的第三季度台账。未提供收入，禁止编造。', 'slide_count': count,
            'credential_id': credential.json['data']['credential_id']}, headers=key())
        return response
    return root, initial, enqueue


@pytest.mark.parametrize('count', [1, 10])
def test_outline_candidate_freezes_version_and_preserves_facts_in_confirmed_plan(
        client, app, monkeypatch, outline_task, count):
    root, initial, enqueue = outline_task
    proposed = [{**page(), 'title': f'候选第 {i + 1} 页'} for i in range(count)]
    calls = []

    def fake(*args):
        calls.append(args)
        return {'pages': deepcopy(proposed)}, 'outline-request', {'total_tokens': 10}
    monkeypatch.setattr('services.scene.generation.chat_json', fake)
    queued = enqueue(count)
    assert queued.status_code == 202
    # Change project while the task is queued. The worker must not absorb this baseline.
    manual = client.patch(root + '/outline', json={'base_project_version': 1, 'pages': [
        {'page_id': initial['page_id'], 'outline': {'title': '人工修改不能被覆盖', 'points': []}}]}, headers=key())
    assert manual.status_code == 200
    assert run_once()
    assert len(calls) == 1
    assert 'sources' in calls[0][3] and 'unverified sources' in calls[0][3]
    task = db.session.get(SceneTaskItem, queued.json['data']['task_id'])
    assert task.state == 'succeeded'
    attempt = SceneTaskAttempt.query.filter_by(task_item_id=task.id).one()
    assert attempt.provider_request_id == 'outline-request' and attempt.dispatch_state == 'resolved'
    asset = db.session.get(Asset, task.result_json['draft_asset_id'])
    assert asset.provenance_json['base_project_version'] == 1
    payload_before = checked_bytes(asset)
    draft = client.get(root + '/outline-drafts/' + asset.id)
    assert draft.status_code == 200
    assert draft.json['data']['base_project_version'] == 1
    assert draft.json['data']['pages'] == proposed
    assert client.get(root).json['data']['pages'][0]['outline_content']['title'] == '人工修改不能被覆盖'
    body = {'base_project_version': 1, 'pages': [
        {'page_id': initial['page_id'] if i == 0 else None, 'outline': value} for i, value in enumerate(proposed)]}
    stale = client.patch(root + '/outline', json=body, headers=key())
    assert stale.status_code == 409
    body['base_project_version'] = 2
    confirmed = client.patch(root + '/outline', json=body, headers=key())
    assert confirmed.status_code == 200
    assert len(confirmed.json['data']['pages']) == count
    assert confirmed.json['data']['pages'][0]['outline_content'] == proposed[0]
    plan = client.post(root + '/generation-plans', json={'base_project_version': 3}, headers=key())
    assert plan.status_code == 201, plan.json
    from models.scene_v1 import GenerationPlan
    frozen = db.session.get(GenerationPlan, plan.json['data']['plan_id'])
    assert frozen.manifest_json['pages'][0]['outline'] == proposed[0]
    assert checked_bytes(asset) == payload_before


@pytest.mark.parametrize('fallback', ['original_task', 'missing_task'])
def test_historical_draft_never_uses_current_version(client, app, monkeypatch, outline_task, fallback):
    root, _, enqueue = outline_task
    monkeypatch.setattr('services.scene.generation.chat_json', lambda *a: ({'pages': [page()]}, None, {}))
    task_id = enqueue().json['data']['task_id']
    assert run_once()
    task = db.session.get(SceneTaskItem, task_id)
    asset = db.session.get(Asset, task.result_json['draft_asset_id'])
    provenance = dict(asset.provenance_json); provenance.pop('base_project_version')
    if fallback == 'missing_task': provenance['task_item_id'] = str(uuid4())
    asset.provenance_json = provenance; db.session.commit()
    response = client.get(root + '/outline-drafts/' + asset.id)
    assert response.status_code == 200
    assert response.json['data']['base_project_version'] == (1 if fallback == 'original_task' else None)


def test_invalid_model_outline_is_failed_not_published_or_automatically_retried(client, app, monkeypatch, outline_task):
    root, _, enqueue = outline_task
    calls = []
    def fake(*args):
        calls.append(1)
        return {'pages': [{**page(), 'facts_needed': ['x'] * 21}]}, 'invalid-outline', {}
    monkeypatch.setattr('services.scene.generation.chat_json', fake)
    task_id = enqueue().json['data']['task_id']; assert run_once()
    task = db.session.get(SceneTaskItem, task_id)
    assert task.state == 'failed' and task.error_code == 'OUTLINE_INVALID'
    assert Asset.query.filter_by(kind='outline_draft').count() == 0
    assert client.get(root).json['data']['pages'][0]['outline_content']['title'] == '第一页'
    assert not run_once() and len(calls) == 1


def test_boolean_outline_count_is_rejected_before_enqueue(client, app, outline_task):
    _, _, enqueue = outline_task
    response = enqueue(True)
    assert response.status_code == 422
    assert SceneTaskItem.query.count() == 0


def test_outline_openapi_exposes_independent_metadata_bounds(client, app):
    create(client, app)
    spec = client.get('/api/v2/openapi.json').json
    outline = spec['components']['schemas']['OutlineContent']
    assert outline['additionalProperties'] is False
    for field in ('points', 'facts_needed', 'sources'):
        assert outline['properties'][field]['maxItems'] == 20
        assert outline['properties'][field]['items']['maxLength'] == 1000
    data = spec['paths']['/api/v2/projects/{project_id}/outline-drafts/{asset_id}']['get'][
        'responses']['200']['content']['application/json']['schema']['properties']['data']
    assert data['properties']['base_project_version']['type'] == ['integer', 'null']
