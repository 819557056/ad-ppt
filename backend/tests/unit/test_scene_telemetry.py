"""Real API/worker/DB tracing, with only paid-provider responses mocked."""
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from models import db
from models.scene_v1 import SceneTaskItem, SceneTaskAttempt, SceneMetric, now
from services.scene import telemetry as t
from services.scene.provider import ProviderOutcomeUnknown
from services.scene.errors import SceneError
from services.scene.store import put_bytes, signed_url
from ops.scene_metrics import render_metrics
from workers.scene_runner import run_once
from backend.tests.integration.test_scene_v1 import create, key, text_element, _queued_generation


@pytest.fixture
def records(app, monkeypatch, tmp_path):
    captured = []
    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(json.loads(record.getMessage()))
    monkeypatch.setattr(t.LOG, 'handlers', [Capture()])
    monkeypatch.setitem(app.config, 'SCENE_EDITOR_ENABLED', True)
    monkeypatch.setitem(app.config, 'ASSET_STORE_ROOT', str(tmp_path / 'assets'))
    monkeypatch.setitem(app.config, 'SCENE_RENDER_JOB_ROOT', None)
    monkeypatch.delenv('ACCESS_CODE', raising=False)
    yield captured
    assert t.TRACE.get() is None


def metric(name, **labels):
    return sum(row.value for row in SceneMetric.query.filter_by(metric=name, **labels))


def test_request_body_header_log_share_one_server_generated_id(client, app, records):
    caller_id = str(uuid4())
    response = client.post('/api/v2/projects', json={'title': 'PRIVATE-TITLE'},
        headers={**key(), 'X-Request-ID': caller_id, 'Authorization': 'Bearer PRIVATE-KEY'})
    assert response.status_code == 201
    identifier = response.json['request_id']
    assert str(UUID(identifier)) == identifier and identifier != caller_id
    assert response.headers['X-Request-ID'] == identifier
    event = [r for r in records if r['event'] == 'scene.request'][-1]
    assert event['request_id'] == identifier and event['project_id'] == response.json['data']['project_id']
    assert not any(value in json.dumps(records) for value in ('PRIVATE-TITLE', 'PRIVATE-KEY', caller_id))
    assert metric('scene_http_requests_total', outcome='success') == 1


@pytest.mark.parametrize('mode,status', [('auth', 401), ('input', 422), ('internal', 500)])
def test_errors_are_correlated_without_exception_header_or_url_secrets(client, app, records, monkeypatch, mode, status):
    if mode == 'auth':
        monkeypatch.setenv('ACCESS_CODE', 'PRIVATE-ACCESS')
        response = client.get('/api/v2/projects?token=PRIVATE-QUERY')
    elif mode == 'input':
        response = client.post('/api/v2/projects?secret=PRIVATE-QUERY', json={'title': 'PRIVATE-TITLE'})
    else:
        def explode():
            raise RuntimeError('PRIVATE-KEY https://user:PRIVATE-PASSWORD@secret.example?q=PRIVATE-QUERY E:\\PRIVATE-PATH')
        monkeypatch.setitem(app.view_functions, 'scene_v2.list_projects', explode)
        response = client.get('/api/v2/projects?token=PRIVATE-QUERY')
    assert response.status_code == status
    assert response.json['request_id'] == response.headers['X-Request-ID']
    assert records[-1]['request_id'] == response.json['request_id']
    assert 'PRIVATE-' not in json.dumps(records)
    assert t.TRACE.get() is None


def test_signed_json_download_is_not_parsed_or_logged(client, app, records):
    project, _ = create(client, app)
    row = put_bytes(__import__('services.scene.versioning', fromlist=['LOCAL_OWNER_ID']).LOCAL_OWNER_ID,
        project['project_id'], 'outline_draft', b'{"secret":"PRIVATE-ASSET"}', 'application/json', 'json')
    db.session.commit()
    url = signed_url(row)
    response = client.get(url)
    assert response.status_code == 200 and response.data == b'{"secret":"PRIVATE-ASSET"}'
    assert 'X-Request-ID' in response.headers
    assert 'PRIVATE-ASSET' not in json.dumps(records) and 'sig=' not in json.dumps(records)


@pytest.mark.parametrize('uri', ['/api/v2/assets/id/content?sig=PRIVATE',
    '/api%2fv2/assets/id/content?sig=PRIVATE', 'https://example.test/api/v2/assets/id/content?sig=PRIVATE'])
@pytest.mark.parametrize('colored', [False, True])
def test_werkzeug_filter_blocks_raw_scene_access_lines(uri, colored):
    request_line = 'GET %s HTTP/1.1'
    if colored:
        request_line = '\x1b[31m\x1b[1m' + request_line + '\x1b[0m'
    record = logging.LogRecord('werkzeug', 20, '', 1, 'host - - "' + request_line + '" 404', (uri,), None)
    assert not t.SceneAccessFilter().filter(record)
    record.args = ('/health',)
    assert t.SceneAccessFilter().filter(record)


def mock_draft(monkeypatch, *, images=False):
    image = BytesIO(); Image.new('RGB', (64, 64), '#557799').save(image, format='PNG')
    def chat(*_args, **_kwargs):
        return {'background': {'kind': 'solid', 'color': '#FFFFFF'},
            'elements': [text_element('Private generated content')],
            'image_requests': [{'role': 'background', 'prompt': 'PRIVATE-PROMPT'}] if images else []}, 'PRIVATE-UPSTREAM-ID', {}
    monkeypatch.setattr('services.scene.generation.chat_json', chat)
    monkeypatch.setattr('services.scene.generation.generate_image', lambda *_args: (image.getvalue(), 'PRIVATE-UPSTREAM-ID', {}))
    return image.getvalue()


def test_persisted_trace_follows_actual_generation_dag_and_queue_wait(client, app, records, monkeypatch):
    project, page = create(client, app)
    parent_id = _queued_generation(client, app, project, page)
    enqueue = next(r for r in records if r['event'] == 'scene.task_enqueued' and r['item_id'] == parent_id)
    response_event = next(r for r in records if r.get('endpoint') == 'generate_scenes')
    original = enqueue['request_id']
    assert original == response_event['request_id']
    parent = db.session.get(SceneTaskItem, parent_id)
    assert parent.request_id == original
    parent.queued_at = now() - timedelta(seconds=10)
    db.session.commit()
    mock_draft(monkeypatch, images=True)
    assert run_once() and run_once() and run_once()
    parent = db.session.get(SceneTaskItem, parent_id)
    child = SceneTaskItem.query.filter_by(operation='generate_asset').one()
    assert parent.state == child.state == 'succeeded'
    assert child.request_id == original
    attempts = SceneTaskAttempt.query.order_by(SceneTaskAttempt.started_at).all()
    assert len(attempts) == 3 and {a.request_id for a in attempts} == {original}
    assert attempts[0].queue_wait_seconds >= 9
    worker_events = [r for r in records if r['operation'] != 'http' and r['item_id']]
    assert all(r['request_id'] == original for r in worker_events)
    assert {r.get('stage') for r in records} >= {'model_text', 'model_image', 'candidate_finalize', 'candidate_publish'}
    assert any(r['event'] == 'scene.attempt_started' and r['item_id'] == child.id for r in records)
    assert not any(s in json.dumps(records) for s in ('sandbox-test-key', 'PRIVATE-PROMPT', 'PRIVATE-UPSTREAM-ID', 'Private generated content'))
    assert metric('scene_task_transitions_total', outcome='succeeded') == 2
    assert metric('scene_queue_wait_seconds_count') == 3
    assert metric('scene_stage_duration_seconds_count', stage='model_text', outcome='success') == 1
    assert metric('scene_stage_duration_seconds_count', stage='asset_write', outcome='success') > 0
    assert metric('scene_retries_total') == 0  # Local DAG finalization is not a retry.


def test_unknown_manual_retry_creates_new_attempt_trace_not_new_original(client, app, records, monkeypatch):
    project, page = create(client, app)
    item_id = _queued_generation(client, app, project, page)
    original = db.session.get(SceneTaskItem, item_id).request_id
    def unknown(*_args):
        raise ProviderOutcomeUnknown()
    monkeypatch.setattr('services.scene.generation.chat_json', unknown)
    assert run_once()
    assert db.session.get(SceneTaskItem, item_id).state == 'outcome_unknown'
    assert metric('scene_task_transitions_total', outcome='outcome_unknown', source='model_unknown') == 1
    assert not run_once()  # Never automatically replay an uncertain paid request.
    headers = key()
    retried = client.post(f'/api/v2/tasks/{item_id}/retry', json={'acknowledge_possible_charge': True}, headers=headers)
    assert retried.status_code == 200
    retry_id = retried.json['request_id']
    replay = client.post(f'/api/v2/tasks/{item_id}/retry', json={'acknowledge_possible_charge': True}, headers=headers)
    assert replay.status_code == 200 and replay.json['request_id'] != retry_id
    assert metric('scene_retries_total', stage='retry_manual') == 1
    mock_draft(monkeypatch)
    assert run_once() and run_once()
    item = db.session.get(SceneTaskItem, item_id)
    assert item.request_id == original and item.retry_request_id == retry_id
    attempts = SceneTaskAttempt.query.order_by(SceneTaskAttempt.attempt_no).all()
    assert attempts[0].request_id == original and {a.request_id for a in attempts[1:]} == {retry_id}
    assert all(r['origin_request_id'] == original for r in records if r['event'] == 'scene.attempt_started')


def test_explicit_rate_limit_is_counted_separately_from_unknown(client, app, records, monkeypatch):
    project, page = create(client, app)
    item_id = _queued_generation(client, app, project, page)
    def limited(*_args):
        raise SceneError('MODEL_RATE_LIMITED', 'PRIVATE-ERROR', 429, {'retry_after_seconds': 2})
    monkeypatch.setattr('services.scene.generation.chat_json', limited)
    assert run_once()
    item = db.session.get(SceneTaskItem, item_id)
    assert item.state == 'queued' and item.dispatch_state == 'rejected'
    assert metric('scene_retries_total', stage='retry_automatic') == 1
    assert metric('scene_stage_duration_seconds_count', stage='model_text', source='model_rate_limit', outcome='failed') == 1
    assert metric('scene_task_transitions_total', outcome='outcome_unknown') == 0
    assert 'PRIVATE-ERROR' not in json.dumps(records)


def test_rollback_never_emits_committed_task_transition(client, app, records):
    project, page = create(client, app)
    item_id = _queued_generation(client, app, project, page)
    with t.scope(operation='worker'):
        item = db.session.get(SceneTaskItem, item_id)
        item.state = 'succeeded'
        db.session.flush()
        db.session.rollback()
    assert not any(r['event'] == 'scene.task_transition' and r['outcome'] == 'succeeded' for r in records)
    assert db.session.get(SceneTaskItem, item_id).state == 'queued'


def test_instrumentation_failure_cannot_undo_paid_result(client, app, records, monkeypatch):
    project, page = create(client, app)
    item_id = _queued_generation(client, app, project, page)
    mock_draft(monkeypatch)
    class FailedEngine:
        def connect(self):
            raise RuntimeError('PRIVATE-CONNECTION-STRING')
    original = t.flush_metrics
    monkeypatch.setattr(t, 'flush_metrics', lambda _engine, trace: original(FailedEngine(), trace))
    assert run_once() and run_once()
    assert db.session.get(SceneTaskItem, item_id).state == 'succeeded'
    assert any(r['event'] == 'scene.metrics_unavailable' for r in records)
    assert 'PRIVATE-CONNECTION-STRING' not in json.dumps(records)


def test_metrics_export_contains_histograms_counts_and_actual_disk_not_ids(client, app, records, tmp_path):
    project, _ = create(client, app)
    root = Path(app.config['ASSET_STORE_ROOT']); root.mkdir(parents=True, exist_ok=True)
    (root / 'orphan.bin').write_bytes(b'x' * 17)
    output = render_metrics(db.engine, root)
    assert '# TYPE scene_stage_duration_seconds histogram' in output
    assert 'le="+Inf"' in output and 'le="0.01"' in output
    assert 'scene_asset_storage_bytes 17' in output
    assert 'scene_disk_free_bytes' in output and 'scene_metrics_scrape_success 1' in output
    assert project['project_id'] not in output and str(root) not in output
    assert 'request_id' not in output and 'owner_id' not in output


def test_metric_labels_are_bounded_and_zero_buckets_are_present():
    with t.scope() as trace:
        t.add_metric('scene_http_requests_total', stage='PRIVATE-STAGE')
        t.add_metric('PRIVATE-METRIC', 1)
        t.observe('scene_stage_duration_seconds', 5, stage='attempt')
        assert len(trace.metrics) == len(t.BUCKETS) + 2
        assert trace.metrics[('scene_stage_duration_seconds_bucket', 'worker', 'attempt', 'success', 'none', '0.01')] == 0
        assert not any('PRIVATE' in str(key) for key in trace.metrics)


def test_context_isolated_between_scopes_and_does_not_reuse_previous_request():
    with t.scope(request_id=str(uuid4())) as outer:
        with t.scope() as inner:
            assert t.request_id() != outer.ids['request_id']
        assert t.request_id() == outer.ids['request_id']
    assert t.TRACE.get() is None


def test_metrics_are_atomic_across_real_processes(tmp_path):
    engine = create_engine('sqlite:///' + (tmp_path / 'metrics.db').as_posix())
    SceneMetric.__table__.create(engine)
    code = """from sqlalchemy import create_engine
from services.scene import telemetry as t
import sys, json
engine=create_engine(sys.argv[1])
dropped=[]
t.emit=lambda event, **kwargs: dropped.append(event) if event == 'scene.metrics_unavailable' else None
for _ in range(20):
    with t.scope() as trace:
        t.add_metric('scene_retries_total',stage='retry_automatic')
        t.flush_metrics(engine,trace)
engine.dispose()
print(json.dumps({'committed':20-len(dropped),'dropped':len(dropped)}))
"""
    processes = [subprocess.Popen([sys.executable, '-c', code, str(engine.url)],
        cwd=Path(__file__).resolve().parents[2], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    reports = []
    try:
        for process in processes:
            out, err = process.communicate(timeout=40)
            assert process.returncode == 0, (out, err)
            reports.append(json.loads(out))
        # Operational metrics deliberately drop a batch on the 250 ms lock
        # timeout. Assert atomicity of successful writes, not an exactly-once
        # promise the production contract explicitly does not make.
        assert all(report['committed'] > 0 and report['committed'] + report['dropped'] == 20
                   for report in reports)
        with engine.connect() as connection:
            assert connection.execute(select(SceneMetric.value)).scalar_one() == sum(
                report['committed'] for report in reports)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        engine.dispose()


def test_sqlite_metric_contention_drops_one_batch_and_restores_timeout(tmp_path, monkeypatch):
    from sqlalchemy import text
    import time
    engine = create_engine('sqlite:///' + (tmp_path / 'locked-metrics.db').as_posix())
    SceneMetric.__table__.create(engine)
    unavailable = []
    monkeypatch.setattr(t, 'emit', lambda event, **kwargs: unavailable.append(event))
    try:
        with engine.connect() as held:
            with engine.connect() as probe:
                original_timeout = probe.execute(text('PRAGMA busy_timeout')).scalar_one()
            held.exec_driver_sql('BEGIN IMMEDIATE')
            started = time.monotonic()
            with t.scope() as trace:
                t.add_metric('scene_retries_total', stage='retry_automatic')
                t.flush_metrics(engine, trace)
                assert not trace.metrics
            assert time.monotonic() - started < 3
            assert unavailable == ['scene.metrics_unavailable']
            with engine.connect() as probe:
                assert probe.execute(text('PRAGMA busy_timeout')).scalar_one() == original_timeout
            held.rollback()
        with engine.connect() as connection:
            assert connection.execute(select(SceneMetric.value)).first() is None
    finally:
        engine.dispose()


def test_renderer_structured_event_keeps_only_canonical_trace_fields(capsys):
    from renderer.observability import job_event
    import time
    request_id = str(uuid4())
    job_event({'kind': 'pdf', 'trace': {'request_id': request_id, 'project_id': 'PRIVATE-PATH',
        'api_key': 'PRIVATE-KEY'}, 'filename': 'PRIVATE-FILE'}, time.monotonic(), outcome='failed', error_code='PRIVATE-ERROR')
    payload = json.loads(capsys.readouterr().out)
    assert payload['request_id'] == request_id and payload['project_id'] is None
    assert payload['error_code'] == 'RENDER_FAILED' and 'PRIVATE-' not in json.dumps(payload)


def test_render_failure_records_one_counter_and_safe_stage(client, app, records):
    from services.scene.render_jobs import run_render_job
    with t.scope(operation='export') as trace:
        with pytest.raises(SceneError, match=''):
            run_render_job('pdf', b'PRIVATE-PAYLOAD')
        assert sum(v for k,v in trace.metrics.items() if k[0] == 'scene_render_failures_total') == 1
    assert records[-1]['stage'] == 'render_pdf' and records[-1]['failure_source'] == 'render'
    assert 'PRIVATE-' not in json.dumps(records)


def test_export_stages_carry_the_frozen_snapshot_identity(client, app, records, monkeypatch):
    project, page = create(client, app)
    frozen = client.post(f'/api/v2/projects/{project["project_id"]}/snapshots', json={
        'project_version': project['project_version'], 'pages': [{'page_id': page['page_id'],
        'page_version': page['page_version'], 'revision_id': page['revision_id']}]}, headers=key())
    assert frozen.status_code == 201, frozen.json
    snapshot_id = frozen.json['data']['snapshot_id']
    queued = client.post(f'/api/v2/projects/{project["project_id"]}/exports', json={
        'snapshot_id': snapshot_id, 'format': 'pptx'}, headers=key())
    assert queued.status_code == 202
    monkeypatch.delenv('SCENE_CHROMIUM_PATH', raising=False)
    from backend.tests.scene_layout_fixtures import single_line_layout
    monkeypatch.setattr('workers.scene_runner.measure_text_layout',
        lambda snapshot, revisions, assets: single_line_layout(snapshot, revisions))
    monkeypatch.setattr('workers.scene_runner.compare_export', lambda *args: ({'status': 'not_run', 'pages': []}, []))
    assert run_once()
    events = [r for r in records if r.get('stage') in ('snapshot_validate', 'pptx_compile', 'pptx_verify')]
    assert len(events) >= 3
    assert all(r['snapshot_id'] == snapshot_id and r['request_id'] == queued.json['request_id'] for r in events)
    assert all(r['project_id'] == project['project_id'] for r in events)


def test_version_conflict_counter_does_not_count_idempotency_conflict(client, app, records):
    project, _ = create(client, app)
    rejected = client.patch(f'/api/v2/projects/{project["project_id"]}/model-config', json={
        'base_project_version': -1, 'model_config': {'text_model': 'example'}}, headers=key())
    assert rejected.status_code == 409
    assert metric('scene_version_conflicts_total') == 1
    headers = key()
    assert client.post('/api/v2/projects', json={'title': 'One'}, headers=headers).status_code == 201
    assert client.post('/api/v2/projects', json={'title': 'Two'}, headers=headers).status_code == 409
    assert metric('scene_version_conflicts_total') == 1


def test_scene_proxy_log_format_excludes_query_headers_and_referrers():
    root = Path(__file__).resolve().parents[3]
    text = (root / 'frontend/nginx.scene.conf').read_text(encoding='utf-8')
    log_format = text.split('log_format', 1)[1].split(';', 1)[0]
    assert '$uri' in log_format and '$upstream_http_x_request_id' in log_format
    assert '$request_uri' not in log_format and '$http_' not in log_format and '"$request"' not in log_format
    assert 'error_log /dev/null;' in text
    assert 'nginx.scene.conf' in (root / 'frontend/Dockerfile.scene').read_text()


@pytest.mark.parametrize('phase', ['engine', 'query', 'write', 'replace'])
def test_metrics_cli_failures_are_redacted_and_keep_last_valid_file(monkeypatch, tmp_path, capsys, phase):
    import ops.scene_metrics as cli
    target = tmp_path / 'metrics.prom'
    target.write_bytes(b'previous valid snapshot\n')
    monkeypatch.setenv('DATABASE_URL', 'PRIVATE-CONNECTION-STRING')
    monkeypatch.setenv('ASSET_STORE_ROOT', str(tmp_path))
    monkeypatch.setattr(sys, 'argv', ['scene_metrics', '--output', str(target)])
    def fail(*_args, **_kwargs):
        raise RuntimeError('PRIVATE-KEY https://PRIVATE-USER:PRIVATE-PASSWORD@PRIVATE-HOST')
    class Engine:
        def dispose(self):
            fail()  # Even broken cleanup must not expose a second traceback.
    monkeypatch.setattr(cli, 'create_engine', fail if phase == 'engine' else lambda *_a, **_k: Engine())
    monkeypatch.setattr(cli, 'render_metrics', fail if phase == 'query' else lambda *_a, **_k: 'new snapshot\n')
    if phase == 'write':
        monkeypatch.setattr(cli.os, 'fsync', fail)
    if phase == 'replace':
        monkeypatch.setattr(cli.os, 'replace', fail)
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == '' and 'PRIVATE-' not in captured.err
    assert 'no valid snapshot published' in captured.err
    assert target.read_bytes() == b'previous valid snapshot\n'
    assert not list(tmp_path.glob('.scene-metrics-*'))


def test_metrics_cli_malformed_database_url_has_no_traceback(tmp_path):
    env = dict(os.environ, DATABASE_URL='private-driver://PRIVATE-USER:PRIVATE-KEY@PRIVATE-HOST/db',
               ASSET_STORE_ROOT=str(tmp_path))
    result = subprocess.run([sys.executable, '-m', 'ops.scene_metrics'], env=env,
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=30)
    assert result.returncode == 1 and not result.stdout
    assert result.stderr == 'Scene metrics scrape failed; no valid snapshot published.\n'



def test_broken_commit_observer_cannot_roll_back_task_admission(client, app, records, monkeypatch):
    project, page = create(client, app)
    def fail(*_args, **_kwargs):
        raise RuntimeError('PRIVATE-COMMIT-CONTEXT')
    monkeypatch.setattr(t, 'task_context', fail)
    identifier = _queued_generation(client, app, project, page)
    assert db.session.get(SceneTaskItem, identifier).state == 'queued'
    assert any(row['event'] == 'scene.metrics_unavailable' for row in records)
    assert 'PRIVATE-' not in json.dumps(records)



def test_broken_json_log_sink_never_prints_a_fallback_traceback(monkeypatch, capsys):
    class BrokenStream:
        def write(self, _value):
            raise RuntimeError('PRIVATE-LOG-SINK-PATH')
        def flush(self):
            pass
    monkeypatch.setattr(t.LOG, 'handlers', [t.SceneJsonHandler(BrokenStream())])
    monkeypatch.setattr(t.LOG, 'level', logging.INFO)
    monkeypatch.setattr(logging, 'raiseExceptions', True)
    with t.scope():
        t.emit('scene.stage', stage='model_text', outcome='success')
    output = capsys.readouterr()
    assert output.out == output.err == ''
