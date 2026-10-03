"""No live calls: verify the opt-in runner against in-memory HTTP/Flask and fake models."""
import base64
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from backend.tests.live_scene_contract import (
    BUDGET_ACK, CONFIRM, MAX_RESUME_SECONDS, ContractError, ContractRun, LiveConfig, reference_png, safe_url)


def settings(tmp_path):
    return {'SCENE_LIVE_CONFIRM': CONFIRM, 'SCENE_LIVE_BUDGET_ACK': BUDGET_ACK,
            'SCENE_LIVE_ORIGIN': 'http://127.0.0.1:5011', 'SCENE_LIVE_ACCESS_CODE': 'private-access-test',
            'SCENE_LIVE_GATEWAY': 'https://model.example/v1', 'SCENE_LIVE_CREDENTIAL_ID': str(uuid4()),
            'SCENE_LIVE_TEXT_MODEL': 'fixture-text', 'SCENE_LIVE_IMAGE_MODEL': 'fixture-image',
            'SCENE_LIVE_RUN_DIR': str(tmp_path / 'contract')}


def test_default_requires_no_config_file_secret_or_network(tmp_path):
    class Guard(dict):
        def get(self, key, default=None):
            assert key == 'SCENE_LIVE_CONFIRM'
            return default
    assert LiveConfig.from_environment(Guard()) is None
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(('name', 'value'), [
    ('SCENE_LIVE_CONFIRM', 'yes'), ('SCENE_LIVE_BUDGET_ACK', ''),
    ('SCENE_LIVE_ACCESS_CODE', ''), ('SCENE_LIVE_ACCESS_CODE', 'bad\nheader'),
    ('SCENE_LIVE_ACCESS_CODE', '非ASCII'), ('SCENE_LIVE_TEXT_MODEL', 'x' * 151), ('SCENE_LIVE_CREDENTIAL_ID', 'wrong'),
    ('SCENE_LIVE_TIMEOUT_SECONDS', 'NaN'), ('SCENE_LIVE_TIMEOUT_SECONDS', '29'),
    ('SCENE_LIVE_TIMEOUT_SECONDS', '1801'), ('SCENE_LIVE_RUN_DIR', 'relative/path')])
def test_invalid_configuration_fails_before_network_or_directory(tmp_path, name, value):
    env = settings(tmp_path);env[name] = value
    with pytest.raises(ContractError):
        LiveConfig.from_environment(env)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('url', [
    'http://example.com', 'https://user:password@example.com', 'https://example.com/?access_code=secret',
    'https://example.com/#fragment', 'https://example.com/api', 'https://example.com:wrong',
    'https://example.com\\evil', 'https://example.com\n'])
def test_api_endpoint_rejects_cleartext_nonloopback_embedded_secrets_and_paths(url):
    with pytest.raises(ContractError):
        safe_url(url)


def test_valid_origins_and_gateway_do_not_guess_configuration():
    assert safe_url('http://[::1]:5000/') == 'http://[::1]:5000'
    assert safe_url('https://example.com/') == 'https://example.com'
    assert safe_url('https://example.com/proxy/v1/', gateway=True) == 'https://example.com/proxy/v1'
    with pytest.raises(ContractError):
        safe_url('http://127.0.0.1/v1', gateway=True)


def no_network(_request):
    pytest.fail('Network must not be used')


def test_locked_corrupt_and_expired_journals_never_start_new_work(tmp_path):
    config = LiveConfig.from_environment(settings(tmp_path))
    runner = ContractRun(config, transport=httpx.MockTransport(no_network))
    with runner.journal_lock():
        with pytest.raises(ContractError, match='locked'):
            ContractRun(config, transport=httpx.MockTransport(no_network)).run()
        assert (config.run_dir / '.running').exists()
    assert not (config.run_dir / '.running').exists()
    path = config.run_dir / 'contract.json'
    original = path.read_text()
    path.write_text('{broken')
    with pytest.raises(ContractError, match='Invalid existing journal'):
        ContractRun(config, transport=httpx.MockTransport(no_network)).run()
    assert path.read_text() == '{broken'
    record = json.loads(original);record['created_at'] -= MAX_RESUME_SECONDS + 1
    path.write_text(json.dumps(record))
    with pytest.raises(ContractError, match='expired'):
        ContractRun(config, transport=httpx.MockTransport(no_network)).run()


def test_configuration_change_cannot_replay_old_journal(tmp_path):
    env = settings(tmp_path);config = LiveConfig.from_environment(env)
    with ContractRun(config).journal_lock():
        pass
    env['SCENE_LIVE_IMAGE_MODEL'] = 'another-model'
    with pytest.raises(ContractError, match='Journal/configuration changed'):
        ContractRun(LiveConfig.from_environment(env), transport=httpx.MockTransport(no_network)).run()
    assert config.access_code not in repr(config)


def test_nonempty_directory_and_symlink_are_not_repurposed(tmp_path):
    config = LiveConfig.from_environment(settings(tmp_path));config.run_dir.mkdir()
    (config.run_dir / 'existing-evidence.txt').write_text('keep')
    with pytest.raises(ContractError, match='not empty'):
        ContractRun(config, transport=httpx.MockTransport(no_network)).run()
    assert (config.run_dir / 'existing-evidence.txt').read_text() == 'keep'
    link = tmp_path / 'linked'
    try:
        link.symlink_to(config.run_dir, target_is_directory=True)
    except OSError:
        return  # Windows may lack symlink privilege; nonempty refusal still ran.
    env = settings(tmp_path);env['SCENE_LIVE_RUN_DIR'] = str(link)
    with pytest.raises(ContractError, match='symlink'):
        ContractRun(LiveConfig.from_environment(env), transport=httpx.MockTransport(no_network)).run()


@pytest.mark.parametrize('status', [302, 401, 429, 500])
def test_errors_and_redirects_never_leak_response_or_follow_location(tmp_path, status):
    config = LiveConfig.from_environment(settings(tmp_path));requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, text='Authorization secret reflected here',
                              headers={'Location': 'https://unrelated.invalid'})
    with pytest.raises(ContractError) as exc:
        ContractRun(config, transport=httpx.MockTransport(handler)).run()
    assert 'reflected' not in str(exc.value) and len(requests) == 1
    assert 'reflected' not in (config.run_dir / 'contract.json').read_text()
    assert requests[0].headers['X-Access-Code'] == config.access_code


def test_bounded_response_and_malformed_envelope(tmp_path):
    config = LiveConfig.from_environment(settings(tmp_path))
    with pytest.raises(ContractError, match='limit'):
        ContractRun(config, transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b'x' * (2 * 1024 * 1024 + 1)))).run()
    with pytest.raises(ContractError, match='envelope'):
        ContractRun(config, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))).run()


@pytest.fixture
def api_gateway(client, app, tmp_path, monkeypatch):
    """Real controller, DB and worker with ONLY model calls/readiness replaced."""
    from workers.scene_runner import run_once
    from services.scene.credentials import create_credential
    from services.scene.versioning import ensure_owner, LOCAL_OWNER_ID
    from models import db
    access = 'private-access-test'
    monkeypatch.setenv('ACCESS_CODE', access)
    for name, value in {'SCENE_EDITOR_ENABLED': True, 'MODEL_GATEWAY_ALLOWLIST': 'https://model.example',
                        'ASSET_STORE_ROOT': str(tmp_path / 'assets'),
                        'CREDENTIAL_ENCRYPTION_KEY': base64.urlsafe_b64encode(b'l' * 32).decode()}.items():
        monkeypatch.setitem(app.config, name, value)
    monkeypatch.setattr('controllers.scene_controller.scene_readiness', lambda: {'ready': True, 'checks': {'fixture': True}})
    ensure_owner()
    credential = create_credential(LOCAL_OWNER_ID, 'Offline contract fixture', 'https://model.example/v1', 'private-provider-test-key')
    db.session.commit()
    env = settings(tmp_path);env['SCENE_LIVE_CREDENTIAL_ID'] = credential.id
    counts = {'text': 0, 'image': 0, 'vision': 0, 'listing': 0, 'posts': 0}
    behavior = {'lose_image_submission': False, 'unknown_text': False}
    def text(_base, _key, _model, system, prompt):
        assert _key == 'private-provider-test-key'
        if isinstance(prompt, list):
            counts['vision'] += 1
            assert prompt[1]['image_url']['url'].startswith('data:image/jpeg;base64,')
            return {'schema_version': 1, 'role': 'content',
                    'palette': {'background': '#FFFFFF', 'text': '#17324D', 'accent': '#E9B949'},
                    'font_suggestions': [], 'layout_hints': [], 'decorative_hints': [],
                    'content_density': 'low', 'warnings': []}, 'offline-vision', {'total_tokens': 9}
        counts['text'] += 1
        if behavior['unknown_text']:
            from services.scene.provider import ProviderOutcomeUnknown
            raise ProviderOutcomeUnknown()
        return {'ok': True}, 'offline-text', {'total_tokens': 3}
    def picture(*args):
        counts['image'] += 1
        return reference_png(), 'offline-image', {'images': 1}
    def listing(*args):
        counts['listing'] += 1
        return ['fixture-text', 'fixture-image']
    monkeypatch.setattr('services.scene.generation.chat_json', text)
    monkeypatch.setattr('services.scene.generation.generate_image', picture)
    monkeypatch.setattr('workers.scene_runner.list_models', listing)
    def handler(request):
        path = request.url.raw_path.decode()
        if request.method == 'GET' and path.startswith('/api/v2/tasks/'):
            run_once()
        if request.method in ('POST', 'PATCH'):
            counts['posts'] += 1
        response = client.open(path, method=request.method, headers=dict(request.headers), data=request.content)
        if behavior['lose_image_submission'] and path.endswith('/check') and request.method == 'POST' and json.loads(request.content).get('kind') == 'image_generation':
            behavior['lose_image_submission'] = False
            assert response.status_code == 202
            raise httpx.ReadError('Sensitive raw transport error must not reach output')
        return httpx.Response(response.status_code, content=response.data, headers=dict(response.headers))
    return LiveConfig.from_environment(env), httpx.MockTransport(handler), counts, behavior


def test_real_api_worker_contract_and_completed_resume_do_not_add_paid_jobs(api_gateway):
    from models.scene_v1 import SceneTaskAttempt, SceneTaskItem
    from models import Project
    config, transport, counts, _ = api_gateway
    path = ContractRun(config, transport=transport).run()
    record = json.loads(path.read_text())
    assert record['status'] == 'passed' and record['paid_operations_submitted'] == 3
    assert counts == {'text': 1, 'image': 1, 'vision': 1, 'listing': 1, 'posts': 7}
    assert SceneTaskItem.query.count() == 5  # list, text, image, local import, vision
    assert SceneTaskAttempt.query.count() == 5
    assert Project.query.count() == 1
    assert sum(bool(row.usage_json) for row in SceneTaskAttempt.query.all()) == 3
    saved_counts = dict(counts)
    completed_at = record['completed_at']
    ContractRun(config, transport=transport).run()
    assert counts == saved_counts and SceneTaskItem.query.count() == 5
    assert json.loads(path.read_text())['completed_at'] == completed_at
    assert 'private-access-test' not in path.read_text() and 'private-provider-test-key' not in path.read_text()
    assert 'sig=' not in path.read_text() and 'Authorization' not in path.read_text()


def test_lost_submission_response_resumes_same_task_not_new_paid_attempt(api_gateway):
    from models.scene_v1 import SceneTaskItem
    config, transport, counts, behavior = api_gateway
    behavior['lose_image_submission'] = True
    with pytest.raises(ContractError, match='interrupted') as exc:
        ContractRun(config, transport=transport).run()
    assert 'Sensitive' not in str(exc.value)
    original = SceneTaskItem.query.filter_by(operation='check_credential').all()
    assert len(original) == 3 and counts['image'] == 0
    image_id = next(item.id for item in original if item.input_json['kind'] == 'image_generation')
    before = json.loads((config.run_dir / 'contract.json').read_text())
    assert before['steps']['image_generation']['response'] is None
    ContractRun(config, transport=transport).run()
    after = json.loads((config.run_dir / 'contract.json').read_text())
    assert after['steps']['image_generation']['response']['task_id'] == image_id
    assert after['steps']['image_generation']['idempotency_key'] == before['steps']['image_generation']['idempotency_key']
    assert counts['image'] == counts['vision'] == counts['text'] == 1
    assert SceneTaskItem.query.count() == 5


def test_unknown_paid_result_stops_and_never_retries_or_advances(api_gateway):
    from models.scene_v1 import SceneTaskItem
    config, transport, counts, behavior = api_gateway
    behavior['unknown_text'] = True
    for _ in range(2):
        with pytest.raises(ContractError, match='needs attention'):
            ContractRun(config, transport=transport).run()
    assert counts['text'] == 1 and counts['image'] == counts['vision'] == 0
    assert SceneTaskItem.query.count() == 2
    assert SceneTaskItem.query.filter_by(state='outcome_unknown').count() == 1


def test_timeout_keeps_task_and_resume_only_polls(api_gateway, monkeypatch):
    config, transport, counts, _ = api_gateway
    ticks = iter([0, 0, 9999])
    monkeypatch.setattr('workers.scene_runner.run_once', lambda: False)
    # Isolated wait test rather than modifying production worker timeouts.
    run = ContractRun(config, monotonic=lambda: next(ticks), sleep=lambda _: None)
    task_id = str(uuid4())
    run.request = lambda *args, **kwargs: {'task_id': task_id, 'operation': 'check_credential',
        'state': 'running', 'attempt_count': 1, 'possible_charge': True}
    with run.journal_lock():
        with pytest.raises(ContractError, match='still pending'):
            run.wait_task(task_id, 'check_credential')
        assert run.journal['tasks'][task_id]['state'] == 'running'
    assert counts['text'] == counts['image'] == 0


def test_response_allowlist_strips_unrelated_nested_data():
    task_id = str(uuid4())
    response = {'tasks': [{'task_id': task_id, 'url': '?sig=secret', 'private': 'do not persist'}],
                'access_code': 'reflected'}
    assert ContractRun.clean_response(response, ('tasks',)) == {'tasks': [{'task_id': task_id}]}


def test_journal_is_durable_before_any_mutating_request(api_gateway, monkeypatch):
    config, transport, counts, _ = api_gateway
    runner = ContractRun(config, transport=transport)
    original = runner.save
    def fail_pre_submit():
        if runner.journal['steps']:
            raise OSError('disk full / private detail')
        return original()
    monkeypatch.setattr(runner, 'save', fail_pre_submit)
    with pytest.raises(ContractError, match='Evidence I/O failed') as exc:
        runner.run()
    assert 'private detail' not in str(exc.value)
    assert counts['posts'] == counts['text'] == counts['image'] == 0
    assert json.loads((config.run_dir / 'contract.json').read_text())['steps'] == {}


def test_journal_response_write_failure_keeps_idempotency_for_explicit_resume(api_gateway, monkeypatch):
    from models.scene_v1 import SceneTaskItem
    config, transport, counts, _ = api_gateway
    runner = ContractRun(config, transport=transport)
    original = runner.save
    def fail_response_save():
        entry = runner.journal['steps'].get('image_generation', {})
        if entry.get('response'):
            raise OSError('simulated disk failure after HTTP 202')
        return original()
    monkeypatch.setattr(runner, 'save', fail_response_save)
    with pytest.raises(ContractError, match='Evidence I/O failed'):
        runner.run()
    entry = json.loads((config.run_dir / 'contract.json').read_text())['steps']['image_generation']
    assert entry['response'] is None and counts['image'] == 0
    ContractRun(config, transport=transport).run()
    assert counts['image'] == 1 and SceneTaskItem.query.count() == 5


def test_mutated_resume_request_cannot_emit_another_post(api_gateway):
    config, transport, counts, _ = api_gateway
    path = ContractRun(config, transport=transport).run()
    record = json.loads(path.read_text());record['steps']['image_generation']['request_hash'] = '0' * 64
    path.write_text(json.dumps(record))
    before = dict(counts)
    with pytest.raises(ContractError, match='Resume request differs'):
        ContractRun(config, transport=transport).run()
    assert counts == before


def test_preflight_rejects_wrong_gateway_without_creating_project(api_gateway):
    from dataclasses import replace
    config, transport, counts, _ = api_gateway
    config = replace(config, gateway='https://wrong.example/v1')
    with pytest.raises(ContractError, match='gateway/status differs'):
        ContractRun(config, transport=transport).run()
    assert counts['posts'] == 0


def test_changed_project_model_does_not_dispatch_reference_probe(api_gateway):
    from models import db, Project
    config, transport, counts, behavior = api_gateway
    behavior['lose_image_submission'] = True
    with pytest.raises(ContractError, match='interrupted'):
        ContractRun(config, transport=transport).run()
    project = Project.query.one()
    project.model_config_json = {'text_model': 'another-model', 'image_model': config.image_model}
    db.session.commit()
    with pytest.raises(ContractError, match='project models changed'):
        ContractRun(config, transport=transport).run()
    assert counts['text'] == counts['image'] == 1 and counts['vision'] == 0


def test_modified_reference_is_not_mistaken_for_original_verified_model_result(api_gateway):
    from models import db, ProjectTemplateAsset
    config, transport, counts, _ = api_gateway
    ContractRun(config, transport=transport).run()
    reference = ProjectTemplateAsset.query.one()
    profile = reference.get_analysis();profile['palette']['accent'] = '#FFFFFF'
    reference.set_analysis(profile);db.session.commit()
    original = dict(counts)
    with pytest.raises(ContractError, match='profile changed'):
        ContractRun(config, transport=transport).run()
    assert counts == original
