"""Independent Scene worker. Run: python -m workers.scene_runner [--once]."""
import argparse
import json
import os
import random
import socket
import subprocess
import threading
import time
from datetime import timedelta, timezone
from importlib.metadata import version
from io import BytesIO

from sqlalchemy import and_, or_, update
from flask import current_app
from PIL import Image

from app import create_app
from models import db, Project
from models.scene_v1 import (Asset, DeckSnapshot, ModelCredential, PageSceneRevision, Principal, SceneExport,
                             SceneTaskAttempt, SceneTaskItem, SceneWorkerHeartbeat, now)
from services.scene.generation import _fence, _model_call, generate_asset, generate_candidate, generate_outline_draft
from services.scene.credentials import decrypt_credential
from services.scene.provider import list_models
from services.scene.provider import ProviderOutcomeUnknown
from services.scene.versioning import SceneError
from services.templates.scene_importer import import_document
from services.templates.style_analysis import analyze_template
from services.exports.scene_pdf import measure_text_layout, render_pdf, verify_pdf
from services.exports.scene_pptx import render_pptx, verify_pptx
from services.exports.quality_gate import validate_snapshot_content
from services.exports.visual_compare import compare_export
from services.exports.rendered_text import TargetRenderError
from services.scene.store import checked_bytes, put_bytes
from services.scene.validation import asset_refs, digest

from services.scene import telemetry
WORKER_ID = f'{socket.gethostname()}:{os.getpid()}'
MODEL_OPERATIONS = ('generate_scene', 'generate_asset', 'ai_edit', 'analyze_template',
                    'generate_outline', 'check_credential')


def _is_paid_phase(item):
    if item.operation == 'check_credential':
        return (item.input_json or {}).get('kind') in ('text_generation', 'image_generation')
    return item.operation in MODEL_OPERATIONS and not (
        item.operation in ('generate_scene', 'ai_edit') and
        (item.result_json or {}).get('draft_asset_id'))


def _finish_expired_attempt(item, state):
    attempt = SceneTaskAttempt.query.filter_by(task_item_id=item.id,
        attempt_no=item.attempt_count).first()
    if attempt and attempt.finished_at is None:
        attempt.finished_at = now()
        attempt.error_code = 'MODEL_OUTCOME_UNKNOWN' if state == 'outcome_unknown' else 'LEASE_EXPIRED'


def _recover_expired(current):
    expired = SceneTaskItem.query.filter(SceneTaskItem.state == 'running',
        SceneTaskItem.lease_expires_at < current).with_for_update(skip_locked=True).all()
    for item in expired:
        if item.cancel_requested_at:
            state = 'cancelled'
        elif _is_paid_phase(item) and item.dispatch_state not in ('not_sent', 'rejected', 'resolved'):
            state = 'outcome_unknown'
        elif item.attempt_count >= item.max_attempts:
            state = 'failed'
        else:
            state = 'queued'
        _finish_expired_attempt(item, state)
        item.state = state
        item.error_code = ('MODEL_OUTCOME_UNKNOWN' if state == 'outcome_unknown' else
                           'LEASE_EXPIRED' if state == 'failed' else None)
        item.dispatch_state = 'not_sent' if state == 'queued' else item.dispatch_state
        if state == 'queued':
            item.queued_at = current
        item.lease_owner = None
        item.lease_expires_at = None
    if expired:
        db.session.commit()
        for item in expired:
            if item.state == 'queued':
                telemetry.retry_committed(item, 'lease_recovery')


def renew_lease(item_id, fence, worker_id, lease_seconds):
    """A stale worker cannot renew a different attempt or a cancelled item."""
    current = now()
    result = db.session.execute(update(SceneTaskItem).where(
        SceneTaskItem.id == item_id,
        SceneTaskItem.state == 'running',
        SceneTaskItem.lease_owner == worker_id,
        SceneTaskItem.fence_token == fence,
        SceneTaskItem.cancel_requested_at.is_(None),
        SceneTaskItem.lease_expires_at > current,
    ).values(lease_expires_at=current + timedelta(seconds=lease_seconds))
     .execution_options(synchronize_session=False))
    db.session.commit()
    return result.rowcount == 1


def _heartbeat(app, item_id, fence, stop, trace_context=None):
    interval = app.config['SCENE_HEARTBEAT_SECONDS']
    with app.app_context():
        while not stop.wait(interval):
            try:
                if not renew_lease(item_id, fence, WORKER_ID,
                                   app.config['SCENE_LEASE_SECONDS']):
                    telemetry.emit('scene.lease_lost', context=trace_context, stage='heartbeat', outcome='lease_lost', error_code='TASK_FENCE_LOST')
                    break
            except Exception:
                db.session.rollback()
                telemetry.emit('scene.heartbeat_failed', context=trace_context, stage='heartbeat', outcome='failed', error_code='DATABASE_ERROR')


def _local_retryable(item, exc):
    if (item.operation in MODEL_OPERATIONS and item.attempt_count < item.max_attempts and
        isinstance(exc, SceneError) and exc.code == 'MODEL_RATE_LIMITED' and
        item.dispatch_state == 'rejected'):
        return True
    if item.operation not in ('export', 'import_template', 'generate_scene', 'ai_edit') or item.attempt_count >= item.max_attempts:
        return False
    if item.operation in ('generate_scene', 'ai_edit') and not (item.result_json or {}).get('draft_asset_id'):
        return False
    return (isinstance(exc, (OSError, TimeoutError, subprocess.TimeoutExpired)) or
            isinstance(exc, SceneError) and exc.code in ('RENDER_TIMEOUT', 'RENDER_FAILED'))


def claim():
    current = now()
    _recover_expired(current)
    waiting = SceneTaskItem.query.filter(SceneTaskItem.state == 'waiting_assets',
        SceneTaskItem.operation.in_(['generate_scene', 'ai_edit'])).with_for_update(skip_locked=True).all()
    for parent in waiting:
        if parent.cancel_requested_at:
            parent.state = 'cancelled'
            continue
        children = SceneTaskItem.query.filter_by(group_id=parent.group_id, page_id=parent.page_id,
            operation='generate_asset').all()
        if len(children) != (parent.result_json or {}).get('asset_count'):
            parent.error_code = 'ASSET_TASK_MISSING'
        elif all(child.state == 'succeeded' for child in children):
            parent.state = 'queued'
            parent.queued_at = now()
            parent.error_code = None
        elif any(child.state in ('failed', 'outcome_unknown', 'cancelled') for child in children):
            parent.error_code = 'ASSET_STEP_NEEDS_ATTENTION'
        else:
            parent.error_code = None
    db.session.commit()
    query = SceneTaskItem.query.filter(
        SceneTaskItem.state == 'queued',
        or_(SceneTaskItem.next_run_at.is_(None), SceneTaskItem.next_run_at <= current),
    ).order_by(SceneTaskItem.created_at, SceneTaskItem.id).with_for_update(skip_locked=True)
    item = None
    cursor = None
    while item is None:
        batch = query
        if cursor is not None:
            batch = batch.filter(or_(SceneTaskItem.created_at > cursor[0],
                and_(SceneTaskItem.created_at == cursor[0], SceneTaskItem.id > cursor[1])))
        candidates = batch.limit(50).all()
        if not candidates:
            break
        # A busy owner's first 50 queued model calls must not starve later
        # owners or local export work. Keyset paging also survives rows being
        # cancelled/failed during this scan.
        cursor = (candidates[-1].created_at, candidates[-1].id)
        for candidate in candidates:
            project = db.session.get(Project, candidate.project_id)
            if project is None or project.deleted_at is not None or project.owner_id != candidate.owner_id:
                candidate.state = 'cancelled'
                continue
            if candidate.operation == 'generate_asset':
                parent = db.session.get(SceneTaskItem, (candidate.input_json or {}).get('parent_task_id'))
                if (parent is None or parent.owner_id != candidate.owner_id or
                        parent.project_id != candidate.project_id or
                        parent.state != 'waiting_assets' or parent.cancel_requested_at):
                    candidate.state = 'cancelled'
                    candidate.error_code = 'PARENT_UNAVAILABLE'
                    continue
            if candidate.attempt_count >= candidate.max_attempts:
                candidate.state = 'failed'
                candidate.error_code = 'ATTEMPTS_EXHAUSTED'
                continue
            if candidate.cancel_requested_at:
                candidate.state = 'cancelled'
                continue
            if _is_paid_phase(candidate):
                # Serialize owner-level allocation across PostgreSQL workers.
                principal = db.session.get(Principal, candidate.owner_id, with_for_update=True)
                if principal is None or principal.status != 'active':
                    candidate.state = 'cancelled'
                    continue
                running = SceneTaskItem.query.filter(
                    SceneTaskItem.owner_id == candidate.owner_id,
                    SceneTaskItem.operation.in_(MODEL_OPERATIONS),
                    SceneTaskItem.state == 'running', SceneTaskItem.lease_expires_at > current).count()
                if running >= current_app.config['OWNER_MODEL_CONCURRENCY']:
                    continue
            item = candidate
            break
    if item is None:
        db.session.commit()
        return None
    current = now()
    item.state = 'running'
    item.lease_owner = WORKER_ID
    item.lease_expires_at = current + timedelta(seconds=current_app.config['SCENE_LEASE_SECONDS'])
    item.fence_token += 1
    item.attempt_count += 1
    wait = None
    if item.queued_at is not None:
        ready_times = [stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
                       for stamp in (item.queued_at, item.next_run_at) if stamp is not None]
        wait = max(0, (current - max(ready_times)).total_seconds())
    db.session.add(SceneTaskAttempt(task_item_id=item.id, attempt_no=item.attempt_count,
        request_id=item.retry_request_id or item.request_id, queue_wait_seconds=wait,
        fence_token=item.fence_token, worker_id=WORKER_ID))
    db.session.commit()
    return item.id, item.fence_token


def _inputs(item):
    export = db.session.get(SceneExport, item.resource_id)
    snapshot = db.session.get(DeckSnapshot, export.snapshot_id)
    if snapshot.owner_id != item.owner_id or snapshot.project_id != item.project_id:
        raise ValueError('snapshot ownership mismatch')
    if digest(snapshot.manifest_json) != snapshot.manifest_hash:
        raise ValueError('snapshot manifest hash mismatch')
    font = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'fonts', 'NotoSansSC-Regular.ttf')
    import hashlib
    try:
        with open(font, 'rb') as stream:
            actual_font_hash = hashlib.sha256(stream.read()).hexdigest()
    except OSError as exc:
        raise SceneError('FONT_UNAVAILABLE', 'Fixed Scene font is missing', 503) from exc
    if actual_font_hash != snapshot.manifest_json['font_sha256']:
        raise SceneError('FONT_MISMATCH', 'Snapshot font hash mismatch', 503)
    revisions, assets = {}, {}
    for page in snapshot.manifest_json['pages']:
        revision = db.session.get(PageSceneRevision, page['revision_id'])
        if revision is None or revision.page_id != page['page_id'] or revision.scene_hash != page['scene_hash'] or digest(revision.scene_json) != revision.scene_hash:
            raise SceneError('SCENE_HASH_MISMATCH', 'Snapshot Scene revision is missing or corrupt', 503,
                             {'revision_id': page['revision_id']})
        revisions[revision.id] = revision
        for asset_id, _ in asset_refs(revision.scene_json):
            asset = db.session.get(Asset, asset_id)
            if asset is None or asset.owner_id != item.owner_id or asset.project_id != item.project_id or asset.state != 'ready':
                raise SceneError('ASSET_UNAVAILABLE', 'Snapshot asset unavailable', 503, {'asset_id': asset_id})
            if snapshot.manifest_json['assets'].get(asset_id) != asset.sha256:
                raise SceneError('ASSET_UNAVAILABLE', 'Snapshot asset hash mismatch', 503, {'asset_id': asset_id})
            try:
                checked_bytes(asset)
            except (OSError, ValueError) as exc:
                raise SceneError('ASSET_UNAVAILABLE', 'Snapshot asset missing or corrupt', 503,
                                 {'asset_id': asset_id}) from exc
            assets[asset_id] = asset
    return export, snapshot, revisions, assets


def perform(item_id, fence):
    item = db.session.get(SceneTaskItem, item_id)
    if item.operation in ('generate_scene', 'ai_edit'):
        generate_candidate(item_id, fence, WORKER_ID)
        return
    if item.operation == 'generate_asset':
        generate_asset(item_id, fence, WORKER_ID)
        return
    if item.operation == 'generate_outline':
        generate_outline_draft(item_id, fence, WORKER_ID)
        return
    if item.operation == 'check_credential':
        item = _fence(item_id, fence, WORKER_ID)
        credential = ModelCredential.query.filter_by(id=item.resource_id,
            owner_id=item.owner_id).first()
        if credential is None or credential.status == 'revoked':
            raise SceneError('CREDENTIAL_REVOKED', 'Credential unavailable')
        kind = (item.input_json or {}).get('kind')
        attempt = SceneTaskAttempt.query.filter_by(task_item_id=item.id,
            attempt_no=item.attempt_count).one()
        if kind == 'model_listing':
            base_url, api_key = credential.base_url, decrypt_credential(credential)
            db.session.commit()  # Never hold the task row lock across network I/O.
            with telemetry.stage('model_list'):
                models = list_models(base_url, api_key)
        elif kind in ('text_generation', 'image_generation'):
            model_id = item.input_json['model_id']
            if kind == 'text_generation':
                probe = _model_call(item, attempt, 'credential_text',
                    'Return exactly {"ok":true}.', credential, model_id,
                    system_override='This is a minimal capability check. Return ONLY JSON {"ok":true}.')
                if probe != {'ok': True}:
                    raise SceneError('MODEL_CAPABILITY_INVALID', 'Text model did not return the expected JSON')
            else:
                probe = _model_call(item, attempt, 'image',
                    'A plain yellow circle on a white background; no text.', credential, model_id)
                try:
                    with Image.open(BytesIO(probe)) as image:
                        image.verify()
                        if image.width < 64 or image.height < 64:
                            raise ValueError('image too small')
                except (OSError, ValueError) as exc:
                    raise SceneError('IMAGE_RESPONSE_INVALID', 'Image capability probe returned invalid image') from exc
        else:
            raise SceneError('TASK_INPUT_INVALID', 'Unsupported credential check kind')
        db.session.expire_all()
        item = _fence(item_id, fence, WORKER_ID)
        credential = db.session.get(ModelCredential, item.resource_id)
        if credential.status == 'revoked':
            raise SceneError('CREDENTIAL_REVOKED', 'Credential revoked')
        credential.status = 'connected'
        capabilities = dict(credential.capabilities_json or {})
        if kind == 'model_listing':
            capabilities.update({'model_list_observed': True, 'models': models,
                                 'model_list_checked_at': now().isoformat()})
            capabilities.setdefault('text_generation_verified', False)
            capabilities.setdefault('image_generation_verified', False)
        else:
            capabilities.update({f'{kind}_verified': True,
                                 f'{kind}_model_id': model_id,
                                 f'{kind}_checked_at': now().isoformat()})
        credential.capabilities_json = capabilities
        item.state = 'succeeded'
        item.result_json = ({'model_count': len(models), 'credential_id': credential.id,
            'note': 'Model listing does not prove text or image capability'} if kind == 'model_listing'
            else {'credential_id': credential.id, 'kind': kind, 'model_id': model_id,
                  'verified': True})
        item.lease_owner = None
        item.lease_expires_at = None
        attempt = SceneTaskAttempt.query.filter_by(task_item_id=item.id, attempt_no=item.attempt_count).one()
        attempt.dispatch_state = 'resolved'
        attempt.finished_at = now()
        db.session.commit()
        return
    if item.operation == 'analyze_template':
        analyze_template(item_id, fence, WORKER_ID)
        return
    if item.operation == 'import_template':
        result = import_document(item.resource_id)
        db.session.expire_all()
        item = db.session.get(SceneTaskItem, item_id)
        if item.state != 'running' or item.lease_owner != WORKER_ID or item.fence_token != fence or item.cancel_requested_at or not SceneTaskItem.query.filter(
            SceneTaskItem.id == item.id, SceneTaskItem.lease_expires_at > now()).first():
            db.session.rollback()
            return
        item.state = 'succeeded'
        item.result_json = result
        item.lease_owner = None
        item.lease_expires_at = None
        attempt = SceneTaskAttempt.query.filter_by(task_item_id=item.id, attempt_no=item.attempt_count).one()
        attempt.dispatch_state = 'resolved'
        attempt.finished_at = now()
        db.session.commit()
        return
    if item.operation != 'export':
        raise ValueError('unsupported task operation')
    export, snapshot, revisions, assets = _inputs(item)
    telemetry.bind(snapshot_id=snapshot.id)
    validate_snapshot_content(snapshot, revisions)
    text_layout = measure_text_layout(snapshot, revisions, assets)
    if export.format == 'pptx':
        payload = render_pptx(snapshot, revisions, assets, text_layout)
        report = verify_pptx(payload, snapshot, revisions, text_layout, assets)
        mime, suffix = 'application/vnd.openxmlformats-officedocument.presentationml.presentation', 'pptx'
    elif export.format == 'pdf':
        payload = render_pdf(snapshot, revisions, assets, text_layout)
        report = verify_pdf(payload, snapshot, revisions)
        mime, suffix = 'application/pdf', 'pdf'
    else:
        raise ValueError('unsupported export format')
    db.session.expire_all()
    item = db.session.get(SceneTaskItem, item_id)
    export = db.session.get(SceneExport, item.resource_id)
    if item.state != 'running' or item.lease_owner != WORKER_ID or item.fence_token != fence or item.cancel_requested_at or not SceneTaskItem.query.filter(
        SceneTaskItem.id == item.id, SceneTaskItem.lease_expires_at > now()).first():
        telemetry.emit('scene.lease_lost', outcome='lease_lost', error_code='TASK_FENCE_LOST')
        return
    try:
        visual, sheets = compare_export(payload, export.format, snapshot, revisions, assets, text_layout)
    except TargetRenderError:
        raise  # Content loss, reflow and clipping are not waivable visual differences.
    except Exception as exc:
        telemetry.emit('scene.visual_unavailable', stage='visual_compare', outcome='failed', error_code=telemetry.code_of(exc))
        visual, sheets = {'status': 'not_run', 'reason': type(exc).__name__, 'pages': []}, []
    db.session.expire_all()
    item = SceneTaskItem.query.filter_by(id=item_id).with_for_update().one()
    if item.state != 'running' or item.lease_owner != WORKER_ID or item.fence_token != fence or item.cancel_requested_at or not SceneTaskItem.query.filter(
        SceneTaskItem.id == item.id, SceneTaskItem.lease_expires_at > now()).first():
        db.session.rollback()
        telemetry.emit('scene.lease_lost', outcome='lease_lost', error_code='TASK_FENCE_LOST')
        return
    export = db.session.get(SceneExport, item.resource_id)
    visual_asset_ids = []
    for index, sheet in enumerate(sheets):
        proof = put_bytes(item.owner_id, item.project_id, 'visual_comparison', sheet,
                          'image/png', 'png', provenance={'snapshot_id': snapshot.id,
                          'export_id': export.id, 'page_index': index})
        visual_asset_ids.append(proof.id)
    for page_report, asset_id in zip(visual['pages'], visual_asset_ids):
        page_report['contact_sheet_asset_id'] = asset_id
    from services.exports.report import font_notices
    quality_report = {'status': 'needs_review', 'structural_and_text': 'passed',
                      'visual_comparison': visual['status'], 'visual': visual,
                      'waivable_warning_codes': ['VISUAL_DIFF_UNCALIBRATED']
                          if visual['status'] == 'needs_review' else [],
                      'format': export.format,
                      'snapshot_id': snapshot.id, 'snapshot_hash': snapshot.manifest_hash,
                      'engine': 'python-pptx' if export.format == 'pptx' else 'chromium',
                      'engine_versions': {'python-pptx': version('python-pptx'),
                                          'PyMuPDF': version('PyMuPDF'),
                                          'pypdfium2': version('pypdfium2'),
                                          'Pillow': version('Pillow'),
                                          'text_layout': text_layout['engine'] if text_layout else 'not_run'},
                      'checks': report,
                      'text_layout': text_layout,
                      'revision_hashes': {p['page_id']: p['scene_hash'] for p in snapshot.manifest_json['pages']},
                      'asset_hashes': snapshot.manifest_json['assets'],
                      'font_manifest_id': snapshot.manifest_json['font_manifest_id'],
                      'font_family': 'Noto Sans CJK SC',
                      'font_warnings': font_notices(export.format, visual),
                      'font_sha256': snapshot.manifest_json['font_sha256']}
    report_payload = json.dumps(quality_report, ensure_ascii=False,
                                separators=(',', ':'), sort_keys=True).encode('utf-8')
    report_asset = put_bytes(item.owner_id, item.project_id, 'export_report', report_payload,
                             'application/json', 'json',
                             provenance={'snapshot_id': snapshot.id, 'export_id': export.id})
    asset = put_bytes(item.owner_id, item.project_id, 'export', payload, mime, suffix,
                      provenance={'snapshot_id': snapshot.id, 'snapshot_hash': snapshot.manifest_hash,
                                  'report_asset_id': report_asset.id})
    export.file_asset_id = asset.id
    export.report_asset_id = report_asset.id
    export.status = 'needs_review'
    export.completed_at = now()
    item.state = 'succeeded'
    item.result_json = {'export_id': export.id, 'asset_id': asset.id,
                        'report_asset_id': report_asset.id, 'report': report}
    item.lease_owner = None
    item.lease_expires_at = None
    attempt = SceneTaskAttempt.query.filter_by(task_item_id=item.id, attempt_no=item.attempt_count).first()
    attempt.dispatch_state = 'resolved'
    attempt.finished_at = now()
    db.session.commit()


def run_once():
    with telemetry.scope() as trace:
        try:
            return _run_once()
        finally:
            try:
                engine = db.engine
                db.session.rollback()
                telemetry.flush_metrics(engine, trace)
            except Exception:
                telemetry.emit('scene.metrics_unavailable', error_code='METRICS_UNAVAILABLE', outcome='failed')


def _run_once():
    claimed = claim()
    if claimed is None:
        return False
    item_id, fence = claimed
    selected = db.session.get(SceneTaskItem, item_id)
    trace = telemetry.TRACE.get()
    trace.operation = selected.operation
    telemetry.bind(**telemetry.task_context(selected))
    context = dict(trace.ids)
    app = current_app._get_current_object()
    stop = threading.Event()
    heart = threading.Thread(target=_heartbeat, args=(app, item_id, fence, stop, context), daemon=True)
    heart.start()
    try:
        with telemetry.stage('attempt'):
            perform(item_id, fence)
    except Exception as exc:
        db.session.rollback()
        item = SceneTaskItem.query.filter_by(id=item_id).with_for_update().first()
        lease_valid = bool(item and SceneTaskItem.query.filter(
            SceneTaskItem.id == item_id, SceneTaskItem.lease_expires_at > now()).first())
        if (item and item.state == 'running' and item.lease_owner == WORKER_ID and
            item.fence_token == fence and (item.cancel_requested_at or lease_valid)):
            retry = _local_retryable(item, exc) and item.cancel_requested_at is None
            item.state = ('cancelled' if item.cancel_requested_at else 'queued' if retry else
                          'outcome_unknown' if isinstance(exc, ProviderOutcomeUnknown) else 'failed')
            item.error_code = telemetry.code_of(exc)
            if retry:
                delay = max(min(300, 5 * 2 ** (item.attempt_count - 1)) + random.uniform(0, 1),
                            (exc.details or {}).get('retry_after_seconds', 0) if isinstance(exc, SceneError) else 0)
                item.queued_at = now()
                item.next_run_at = now() + timedelta(seconds=delay)
            item.lease_owner = None
            item.lease_expires_at = None
            export = db.session.get(SceneExport, item.resource_id)
            if export:
                export.status = 'queued' if retry else 'failed'
                export.error_code = None if retry else item.error_code
            if item.operation == 'import_template' and not retry:
                from models.scene_v1 import TemplateDocument
                document = db.session.get(TemplateDocument, item.resource_id)
                if document:
                    document.status = 'failed'
                    document.error_code = item.error_code
            if item.operation == 'analyze_template':
                from models import ProjectTemplateAsset
                from models.scene_v1 import TemplateDocument
                template = ProjectTemplateAsset.query.filter_by(id=item.resource_id,
                    project_id=item.project_id, deleted_at=None).populate_existing().with_for_update().first()
                if template and template.analysis_revision == item.input_json.get('base_analysis_revision'):
                    template.analysis_status = 'failed'
                    document = TemplateDocument.query.filter_by(id=template.template_document_id).populate_existing().with_for_update().first()
                    if document:
                        document.status = 'partial_failed'
            attempt = SceneTaskAttempt.query.filter_by(task_item_id=item.id, attempt_no=item.attempt_count).first()
            if attempt:
                attempt.error_code = item.error_code
                attempt.finished_at = now()
            db.session.commit()
            if retry:
                telemetry.retry_committed(item, 'retry_automatic')
    finally:
        stop.set()
        heart.join(timeout=app.config['SCENE_HEARTBEAT_SECONDS'] + 1)
        db.session.expire_all()
        finished = db.session.get(SceneTaskItem, item_id)
        outcome = (finished.state if finished and finished.fence_token == fence and finished.state != 'running'
                   else 'lease_lost')
        telemetry.emit('scene.attempt_finished', stage='attempt', outcome=outcome,
                       attempt_no=finished.attempt_count if finished else None)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    app = create_app()
    with app.app_context():
        while True:
            heartbeat = db.session.get(SceneWorkerHeartbeat, WORKER_ID)
            if heartbeat is None:
                heartbeat = SceneWorkerHeartbeat(worker_id=WORKER_ID, heartbeat_at=now())
                db.session.add(heartbeat)
            else:
                heartbeat.heartbeat_at = now()
            db.session.commit()
            did_work = run_once()
            if args.once:
                break
            if not did_work:
                time.sleep(2)


if __name__ == '__main__':
    main()
