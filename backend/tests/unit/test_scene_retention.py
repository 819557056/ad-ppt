"""Offline, real-SQLite retention graph and crash-recovery regression."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from models import db, Page, Project, ProjectTemplateAsset
from models.scene_v1 import (ApiIdempotencyRecord, Asset, DeckSnapshot, GenerationPlan,
    PageSceneRevision, Principal, RevisionAsset, SceneCandidate, SceneExport,
    SceneMaintenanceRun, SceneTaskAttempt, SceneTaskItem, SnapshotPage, TemplateDocument)
from ops.scene_retention import maintain, _output_path
from services.scene.retention import build_plan, database_state, fingerprint, inventory, POLICY_DEFAULTS
from services.scene.validation import asset_refs, blank_scene, digest


@pytest.fixture
def store(tmp_path):
    root = tmp_path / 'assets'; root.mkdir()
    url = 'sqlite:///' + (tmp_path / 'retention.db').as_posix()
    engine = create_engine(url)
    @event.listens_for(engine, 'connect')
    def foreign_keys(connection, _record):
        connection.execute('PRAGMA foreign_keys=ON')
    db.metadata.create_all(engine)
    old = datetime.now(timezone.utc) - timedelta(days=400)
    with Session(engine, expire_on_commit=False) as session:
        owner = Principal(id=str(uuid4()), kind='local', display_name='Retention fixture')
        session.add(owner); session.flush()
        project = Project(id=str(uuid4()), owner_id=owner.id, editor_mode='scene_v1')
        session.add(project); session.flush()
        page = Page(id=str(uuid4()), project_id=project.id, order_index=0)
        session.add(page); session.commit()
        def orphan(payload=b'orphan', age=400, suffix='bin', identifier=None):
            key = f'owners/{owner.id}/projects/{project.id}/assets/{identifier or uuid4()}/content.{suffix}'
            path = root / key; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(payload)
            stamp = (datetime.now(timezone.utc) - timedelta(days=age)).timestamp()
            os.utime(path, (stamp, stamp))
            return key
        def asset(kind='generated_image', payload=b'image', age=400, **kwargs):
            identifier = str(uuid4())
            key = orphan(payload, age=age, identifier=identifier)
            row = Asset(id=identifier, owner_id=owner.id, project_id=project.id, kind=kind,
                storage_key=key, sha256=hashlib.sha256(payload).hexdigest(), byte_size=len(payload),
                mime_type='application/octet-stream', created_at=old if age == 400 else datetime.now(timezone.utc), **kwargs)
            session.add(row); session.flush()
            return row
        def revision(image=None, origin='ai_generate', parent=None):
            scene = blank_scene()
            if image:
                scene['background'] = {'kind': 'image', 'asset_id': image.id, 'fit': 'cover'}
            seq = session.query(PageSceneRevision).count() + 1
            row = PageSceneRevision(id=str(uuid4()), owner_id=owner.id, project_id=project.id,
                page_id=page.id, seq=seq, scene_json=scene, scene_hash=digest(scene), origin=origin,
                parent_revision_id=parent, created_by=owner.id, created_at=old)
            session.add(row); session.flush()
            for aid, role in asset_refs(scene):
                session.add(RevisionAsset(revision_id=row.id, asset_id=aid, project_id=project.id, role=role))
            session.flush()
            return row
        def candidate(rev, state='rejected', **kwargs):
            row = SceneCandidate(id=str(uuid4()), owner_id=owner.id, project_id=project.id,
                page_id=page.id, proposed_revision_id=rev.id, base_page_version=0,
                state=state, created_at=old, **kwargs)
            session.add(row); session.flush()
            return row
        def task(age=400, **kwargs):
            stamp = datetime.now(timezone.utc) - timedelta(days=age)
            values = dict(id=str(uuid4()), owner_id=owner.id, project_id=project.id,
                operation='generate', resource_id=page.id, input_json={}, input_hash=digest({}),
                state='succeeded', dispatch_state='acknowledged', created_at=stamp, updated_at=stamp)
            values.update(kwargs)
            row = SceneTaskItem(**values); session.add(row); session.flush()
            return row
        def attempt(task_row, age=400, **kwargs):
            stamp = datetime.now(timezone.utc) - timedelta(days=age)
            row = SceneTaskAttempt(task_item_id=task_row.id, attempt_no=1, fence_token=1,
                worker_id='test', started_at=stamp, finished_at=stamp, **kwargs)
            session.add(row); session.flush()
            return row
        def idem(payload, expired=False):
            row = ApiIdempotencyRecord(owner_id=owner.id, operation='test', idempotency_key=str(uuid4()),
                request_hash=digest({}), response_status=200, response_json=payload,
                expires_at=old if expired else datetime.now(timezone.utc) + timedelta(days=1))
            session.add(row); session.flush()
            return row
        yield SimpleNamespace(root=root, url=url, engine=engine, session=session, owner=owner,
            project=project, page=page, old=old, asset=asset, revision=revision, candidate=candidate,
            task=task, attempt=attempt, idem=idem, orphan=orphan)
    engine.dispose()


def preview(s):
    s.session.commit()
    s.session.close()
    return maintain(s.url, s.root, offline_confirmed=True)


def apply(s, plan):
    return maintain(s.url, s.root, 'apply', plan=plan, confirm_sha256=plan['plan_sha256'], offline_confirmed=True)


def test_expired_rejected_branch_and_orphans_are_reclaimed_end_to_end(store):
    s = store
    head_image = s.asset(); head = s.revision(head_image, origin='manual')
    s.page.head_revision_id = head.id
    image = s.asset(); proposed = s.revision(image, parent=head.id); c = s.candidate(proposed)
    task = s.task(result_json={'candidate_id': c.id}); attempt = s.attempt(task)
    expired = s.idem({'task_id': task.id}, expired=True)
    orphan = s.orphan(suffix='png.staging')
    unknown = s.root / 'unknown.bin'; unknown.write_bytes(b'keep unknown')
    plan = preview(s)
    assert plan['records']['assets'] == [image.id]
    assert plan['records']['scene_candidates'] == [c.id]
    assert plan['records']['page_scene_revisions'] == [proposed.id]
    assert plan['records']['scene_task_items'] == [task.id]
    assert plan['records']['scene_task_attempts'] == [attempt.id]
    assert plan['records']['api_idempotency_records'] == [expired.id]
    assert {item['path'] for item in plan['files']} == {orphan, image.storage_key}
    receipt = apply(s, plan)
    assert receipt['status'] == 'completed' and receipt['summary']['file_count'] == 2
    assert not (s.root / orphan).exists() and not (s.root / image.storage_key).exists()
    assert (s.root / head_image.storage_key).read_bytes() == b'image' and unknown.is_file()
    with Session(s.engine) as session:
        assert fingerprint(database_state(session)) == plan['after_database_sha256']
        assert session.get(PageSceneRevision, head.id)
        assert session.get(SceneCandidate, c.id) is None
    assert maintain(s.url, s.root, 'resume', run_id=receipt['run_id'], offline_confirmed=True)['status'] == 'completed'


def test_snapshot_history_plan_and_soft_deleted_template_roots(store):
    s = store
    image = s.asset(); rev = s.revision(image); c = s.candidate(rev)
    manifest = {'pages': [{'revision_id': rev.id}], 'assets': {image.id: image.sha256}}
    snapshot = DeckSnapshot(id=str(uuid4()), owner_id=s.owner.id, project_id=s.project.id,
        confirmed_by=s.owner.id, manifest_json=manifest, manifest_hash=digest(manifest))
    s.session.add(snapshot); s.session.flush()
    s.session.add(SnapshotPage(snapshot_id=snapshot.id, ordinal=1, page_id=s.page.id, revision_id=rev.id))
    planned_image = s.asset()
    historical_plan = GenerationPlan(owner_id=s.owner.id, project_id=s.project.id, base_project_version=0,
        manifest_json={'reference': planned_image.id}, manifest_hash=digest({'reference': planned_image.id}), created_by=s.owner.id)
    s.session.add(historical_plan)
    source = s.asset(); preview_asset = s.asset(kind='template_preview')
    doc = TemplateDocument(id=str(uuid4()), owner_id=s.owner.id, project_id=s.project.id,
        source_asset_id=source.id, source_type='pptx')
    s.session.add(doc); s.session.flush()
    s.session.add(ProjectTemplateAsset(project_id=s.project.id, image_path=preview_asset.storage_key,
        preview_asset_id=preview_asset.id, template_document_id=doc.id, deleted_at=s.old))
    s.project.deleted_at = s.old
    plan = preview(s)
    assert plan['records']['assets'] == [] and plan['records']['page_scene_revisions'] == []
    assert plan['records']['scene_candidates'] == [c.id]
    assert not plan['files']


def test_export_report_json_protects_indirect_visual_evidence(store):
    s = store
    evidence = s.asset(kind='visual_comparison')
    report = s.asset(kind='export_report', payload=json.dumps({'visual': {'pages': [
        {'contact_sheet_asset_id': evidence.id}]}}).encode())
    snapshot = DeckSnapshot(id=str(uuid4()), owner_id=s.owner.id, project_id=s.project.id,
        confirmed_by=s.owner.id, manifest_json={}, manifest_hash=digest({}))
    s.session.add(snapshot); s.session.flush()
    s.session.add(SceneExport(owner_id=s.owner.id, project_id=s.project.id, snapshot_id=snapshot.id,
        format='pptx', options_hash=digest({}), report_asset_id=report.id, status='failed'))
    assert not preview(s)['files']


def test_unexpired_idempotency_signed_url_and_group_protect_dependencies(store):
    s = store
    image = s.asset(); proposed = s.revision(image); candidate = s.candidate(proposed)
    group = str(uuid4())
    one = s.task(group_id=group, logical_key='one', result_json={'candidate_id': candidate.id})
    two = s.task(group_id=group, logical_key='two')
    orphan = s.orphan(); orphan_id = Path(orphan).parts[-2]
    s.idem({'group': group, 'url': f'/api/v2/assets/{orphan_id}/content?signature=test'})
    plan = preview(s)
    assert not plan['files'] and not plan['records']['scene_task_items']
    assert not plan['records']['scene_candidates']
    assert one.id != two.id


def test_task_attempt_retention_is_independent_and_unknown_groups_never_collected(store):
    s = store
    young_attempt_task = s.task(age=120); young = s.attempt(young_attempt_task, age=120)
    obsolete_task = s.task(); obsolete = s.attempt(obsolete_task)
    group = str(uuid4()); unknown = s.task(group_id=group, logical_key='unknown')
    uncertain = s.attempt(unknown, dispatch_state='may_have_been_sent')
    sibling = s.task(group_id=group, logical_key='sibling')
    plan = preview(s)
    assert plan['records']['scene_task_items'] == [obsolete_task.id]
    assert plan['records']['scene_task_attempts'] == [obsolete.id]
    assert young.id != uncertain.id and sibling.id != unknown.id


def test_pending_candidate_only_expires_after_actual_base_drift(store):
    s = store
    proposed = s.revision(); pending = s.candidate(proposed, state='pending')
    assert preview(s)['records']['scene_candidates'] == []
    s.session.add(s.page); s.page.row_version = 1; s.session.commit()
    plan = preview(s)
    assert plan['records']['scene_candidates'] == [pending.id]


def test_upload_kept_until_archive_grace_and_fresh_file_protects_old_row(store):
    s = store
    upload = s.asset(kind='upload'); fresh_file = s.asset()
    os.utime(s.root / fresh_file.storage_key, None)
    recent_orphan = s.orphan(age=0)
    assert not preview(s)['files']
    s.session.add(s.project); s.project.deleted_at = s.old
    plan = preview(s)
    assert plan['records']['assets'] == [upload.id]
    assert recent_orphan not in {item['path'] for item in plan['files']}


@pytest.mark.parametrize('state', ['queued', 'running', 'waiting_assets'])
def test_active_work_refuses_maintenance(store, state):
    store.task(state=state)
    with pytest.raises(ValueError, match='active Scene tasks'):
        preview(store)


@pytest.mark.parametrize('corruption', ['asset_bytes', 'scene_hash', 'reference_index', 'hardlink'])
def test_corrupt_and_unsafe_evidence_refuses_cleanup(store, corruption, tmp_path):
    s = store
    image = s.asset(); revision = s.revision(image)
    if corruption == 'asset_bytes':
        (s.root / image.storage_key).write_bytes(b'wrong')
    elif corruption == 'scene_hash':
        revision.scene_hash = '0' * 64
    elif corruption == 'reference_index':
        s.session.query(RevisionAsset).delete()
    else:
        os.link(s.root / image.storage_key, tmp_path / 'outside.bin')
    with pytest.raises(ValueError, match='integrity|index mismatch|Non-private'):
        preview(s)


def test_live_external_fk_protects_asset_and_is_in_fingerprint(store):
    s = store
    image = s.asset(); s.session.commit()
    s.session.execute(text('CREATE TABLE future_feature (id INTEGER PRIMARY KEY, proof TEXT REFERENCES assets(id))'))
    s.session.execute(text('INSERT INTO future_feature (proof) VALUES (:id)'), {'id': image.id})
    plan = preview(s)
    assert not plan['files'] and not plan['records']['assets']
    s.session.execute(text('DELETE FROM future_feature')); s.session.commit()
    with pytest.raises(ValueError, match='stale'):
        apply(s, plan)


@pytest.mark.parametrize('drift', ['db', 'file', 'root', 'schema', 'plan', 'confirmation', 'database_identity'])
def test_apply_rejects_stale_or_unreviewed_plan_before_any_deletion(store, drift, tmp_path):
    s = store
    image = s.asset(); plan = preview(s); original = deepcopy(plan)
    if drift == 'db':
        s.session.add(s.project); s.project.project_title = 'Changed'; s.session.commit()
    elif drift == 'file':
        s.orphan(b'new')
    elif drift == 'root':
        plan['root_inode'] += 1; plan['plan_sha256'] = fingerprint({k: v for k, v in plan.items() if k != 'plan_sha256'})
    elif drift == 'schema':
        s.session.execute(text('CREATE TABLE alembic_version (version_num TEXT)'))
        s.session.execute(text("INSERT INTO alembic_version VALUES ('new-schema')")); s.session.commit()
    elif drift == 'plan':
        plan['files'] = []
    elif drift == 'confirmation':
        with pytest.raises(ValueError, match='exact reviewed'):
            maintain(s.url, s.root, 'apply', plan=plan, confirm_sha256='wrong', offline_confirmed=True)
        return
    else:
        copy = tmp_path / 'other.db'; shutil.copyfile(tmp_path / 'retention.db', copy)
        s.url = 'sqlite:///' + copy.as_posix()
    with pytest.raises(ValueError, match='stale|identity|hash mismatch'):
        apply(s, plan)
    assert (s.root / image.storage_key).is_file()
    with Session(s.engine) as session:
        assert session.get(Asset, image.id) and session.query(SceneMaintenanceRun).count() == 0
    assert original['summary']['record_count'] == 1


def test_db_delete_failure_rolls_back_everything_without_unlink(store, monkeypatch):
    import ops.scene_retention as ops
    s = store
    image = s.asset(); plan = preview(s)
    original = ops._prune_records
    def fail(session, plan):
        original(session, plan)
        raise RuntimeError('simulated database failure')
    monkeypatch.setattr(ops, '_prune_records', fail)
    with pytest.raises(RuntimeError):
        apply(s, plan)
    with Session(s.engine) as session:
        assert session.get(Asset, image.id) and session.query(SceneMaintenanceRun).count() == 0
    assert (s.root / image.storage_key).is_file()


@pytest.mark.parametrize('drift', [None, 'preserved_file', 'database', 'selected_file'])
def test_committed_journal_recovers_partial_unlink_but_rejects_drift(store, monkeypatch, drift):
    import ops.scene_retention as ops
    s = store
    keep = s.asset(kind='upload'); one = s.asset(); two = s.asset()
    plan = preview(s); original = ops._remove_file; calls = []
    def interrupted(root, item):
        calls.append(item['path'])
        if len(calls) == 2:
            raise OSError('simulated process interruption')
        original(root, item)
    with monkeypatch.context() as patch:
        patch.setattr(ops, '_remove_file', interrupted)
        with pytest.raises(OSError):
            apply(s, plan)
    with Session(s.engine) as session:
        run = session.query(SceneMaintenanceRun).one(); run_id = run.id
        assert run.status == 'db_pruned' and session.get(Asset, one.id) is None and session.get(Asset, two.id) is None
    assert sum((s.root / item['path']).exists() for item in plan['files']) == 1
    with pytest.raises(ValueError, match='Unfinished'):
        preview(s)
    if drift == 'preserved_file':
        (s.root / keep.storage_key).write_bytes(b'changed')
    elif drift == 'database':
        s.session.add(s.project); s.project.project_title = 'changed'; s.session.commit()
    elif drift == 'selected_file':
        (s.root / calls[-1]).write_bytes(b'changed')
    if drift:
        with pytest.raises(ValueError, match='changed'):
            maintain(s.url, s.root, 'resume', run_id=run_id, offline_confirmed=True)
        assert (s.root / calls[-1]).exists()
    else:
        result = maintain(s.url, s.root, 'resume', run_id=run_id, offline_confirmed=True)
        assert result['status'] == 'completed' and (s.root / keep.storage_key).is_file()
        assert preview(s)['summary']['record_count'] == 0


def test_safety_confirmation_policy_inventory_limit_and_output_location(store):
    s = store
    with pytest.raises(ValueError, match='confirmation'):
        maintain(s.url, s.root)
    for policy in ({}, {**POLICY_DEFAULTS, 'task_days': 0}, {**POLICY_DEFAULTS, 'task_days': True}):
        with pytest.raises(ValueError, match='positive integers'):
            build_plan(s.session, s.root, policy)
    s.orphan()
    with pytest.raises(ValueError, match='entry count'):
        inventory(s.root, max_entries=1)
    with pytest.raises(ValueError, match='outside'):
        _output_path(s.root / 'plan.json', s.root)


def test_custom_deletion_trigger_fails_closed(store):
    s = store
    image = s.asset(); s.session.commit()
    s.session.execute(text('CREATE TRIGGER unsafe_delete AFTER DELETE ON assets BEGIN DELETE FROM pages; END'))
    with pytest.raises(ValueError, match='deletion triggers'):
        preview(s)
    assert (s.root / image.storage_key).exists()


def test_self_references_delete_child_first_without_disabling_foreign_keys(store):
    s = store
    source = s.asset(); derived = s.asset(source_asset_id=source.id)
    first = s.revision(source); second = s.revision(derived, parent=first.id)
    s.candidate(first); s.candidate(second)
    plan = preview(s)
    assert set(plan['records']['assets']) == {source.id, derived.id}
    assert apply(s, plan)['status'] == 'completed'
    with Session(s.engine) as session:
        assert session.query(Asset).count() == 0 and session.query(PageSceneRevision).count() == 0


def test_cli_plan_apply_receipt_and_refusal_never_mutate_unconfirmed(store, tmp_path):
    import subprocess
    import sys
    s = store
    key = s.orphan(); s.session.commit(); s.session.close()
    output = tmp_path / 'plan.json'; receipt = tmp_path / 'receipt.json'
    backend = Path(__file__).resolve().parents[2]
    base = [sys.executable, '-m', 'ops.scene_retention', '--database-url', s.url, '--asset-root', str(s.root)]
    env = dict(os.environ); env.pop('SCENE_MAINTENANCE_CONFIRMED', None)
    def run(*args):
        return subprocess.run([*base, *args], cwd=backend, env=env, capture_output=True, text=True,
            timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    denied = run('plan', '--output', str(output))
    assert denied.returncode == 1 and not output.exists() and 'confirmation' in denied.stderr
    planned = run('--offline-confirmed', 'plan', '--output', str(output))
    assert planned.returncode == 0, planned.stderr
    original_plan = output.read_bytes()
    overwrite = run('--offline-confirmed', 'plan', '--output', str(output))
    assert overwrite.returncode == 1 and output.read_bytes() == original_plan
    plan = json.loads(original_plan)
    applied = run('--offline-confirmed', 'apply', '--plan', str(output), '--confirm-sha256',
                  plan['plan_sha256'], '--receipt', str(receipt))
    assert applied.returncode == 0, applied.stderr
    result = json.loads(receipt.read_text(encoding='utf-8'))
    assert result['status'] == 'completed' and not (s.root / key).exists()
    assert 'encrypted_secret' not in output.read_text(encoding='utf-8')
    replay = run('--offline-confirmed', 'resume', '--run-id', result['run_id'], '--receipt', str(tmp_path / 'replay.json'))
    assert replay.returncode == 0, replay.stderr
    assert json.loads((tmp_path / 'replay.json').read_text(encoding='utf-8')) == result


def test_incomplete_journal_and_invalid_retention_config_visible_in_readiness(client, app, monkeypatch):
    from models import db
    monkeypatch.setitem(app.config, 'SCENE_EDITOR_ENABLED', True)
    with app.app_context():
        run = SceneMaintenanceRun(plan_sha256='0' * 64, plan_json={}, after_database_sha256='0' * 64)
        db.session.add(run); db.session.commit()
    checks = client.get('/api/v2/readiness').json['data']['checks']
    assert checks['maintenance'] is False and checks['retention_config'] is True
    monkeypatch.setitem(app.config, 'SCENE_RETENTION_TASK_DAYS', 0)
    assert client.get('/api/v2/readiness').json['data']['checks']['retention_config'] is False


def test_unknown_columns_in_existing_table_are_not_silently_ignored(store):
    store.asset(); store.session.commit()
    store.session.execute(text('ALTER TABLE projects ADD COLUMN future_asset_ref TEXT'))
    with pytest.raises(ValueError, match='columns differ'):
        preview(store)


def test_real_migration_schema_and_downgrade_journal_guard(tmp_path):
    import subprocess
    import sys
    database = tmp_path / 'migrated.db'; root = tmp_path / 'volume'; root.mkdir()
    url = 'sqlite:///' + database.as_posix()
    env = dict(os.environ, DATABASE_URL=url)
    env.pop('BANANA_SKIP_AUTO_MIGRATE', None)
    backend = Path(__file__).resolve().parents[2]
    def migrate(*args):
        return subprocess.run([sys.executable, '-m', 'alembic', *args], cwd=backend, env=env,
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    upgraded = migrate('upgrade', 'head')
    assert upgraded.returncode == 0, upgraded.stderr
    plan = maintain(url, root, offline_confirmed=True)
    result = maintain(url, root, 'apply', plan=plan, confirm_sha256=plan['plan_sha256'], offline_confirmed=True)
    assert result['status'] == 'completed'
    refused = migrate('downgrade', 'e4d650a02879')
    assert refused.returncode != 0 and 'preserve recovery evidence' in refused.stderr
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            assert session.get(SceneMaintenanceRun, result['run_id']).status == 'completed'
    finally:
        engine.dispose()


@pytest.mark.parametrize('model', [GenerationPlan, DeckSnapshot])
def test_corrupt_immutable_manifest_refuses_cleanup(store, model):
    s = store
    image = s.asset()
    values = dict(owner_id=s.owner.id, project_id=s.project.id,
                  manifest_json={'asset': image.id}, manifest_hash='0' * 64)
    values.update(dict(created_by=s.owner.id, base_project_version=0) if model == GenerationPlan
                  else dict(confirmed_by=s.owner.id))
    s.session.add(model(**values))
    with pytest.raises(ValueError, match='manifest hash mismatch'):
        preview(s)
    assert (s.root / image.storage_key).exists()
