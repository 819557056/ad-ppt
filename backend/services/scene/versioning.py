"""Transactional Scene revision, candidate and snapshot operations."""
from datetime import timedelta
import hashlib
from pathlib import Path
from uuid import uuid4

from flask import current_app
from sqlalchemy import text, update

from models import db, Page, Project
from models.scene_v1 import (ApiIdempotencyRecord, Asset, DeckSnapshot, GenerationPlan,
                             PageSceneRevision, Principal, RevisionAsset, SceneCandidate,
                             SnapshotPage, new_id, now)
from .commands import apply_commands, assert_locked_preserved
from .errors import SceneError
from .validation import asset_refs, blank_scene, digest, normalize_scene

LOCAL_OWNER_ID = '00000000-0000-4000-8000-000000000001'
FONT_DIR = Path(__file__).resolve().parents[2] / 'fonts'


def ensure_owner():
    owner = db.session.get(Principal, LOCAL_OWNER_ID)
    if owner is None:
        owner = Principal(id=LOCAL_OWNER_ID, kind='local_owner', display_name='Local owner')
        db.session.add(owner)
        db.session.flush()
    if owner.status != 'active':
        raise SceneError('OWNER_DISABLED', 'Owner is disabled', 403)
    return owner.id


def owned_project(project_id, owner_id):
    project = Project.query.filter_by(id=project_id, owner_id=owner_id, editor_mode='scene_v1', deleted_at=None).first()
    if project is None:
        raise SceneError('NOT_FOUND', 'Project not found', 404)
    return project


def owned_page(project, page_id):
    page = Page.query.filter_by(id=page_id, project_id=project.id, deleted_at=None).first()
    if page is None:
        raise SceneError('NOT_FOUND', 'Page not found', 404)
    return page


def current_scene(page):
    revision = db.session.get(PageSceneRevision, page.head_revision_id) if page.head_revision_id else None
    if revision is None or revision.page_id != page.id:
        raise SceneError('SCENE_MISSING', 'Page has no accepted scene', 422)
    return revision


def validate_assets(scene, owner_id, project_id):
    for asset_id, _ in asset_refs(scene):
        asset = Asset.query.filter_by(id=asset_id, owner_id=owner_id, project_id=project_id, state='ready').first()
        if asset is None:
            raise SceneError('ASSET_NOT_READY', f'Asset {asset_id} is unavailable', 422)


def insert_revision(page, project, owner_id, scene, origin, parent_id=None, plan_id=None):
    try:
        clean = normalize_scene(scene, float(project.canvas_width_pt), float(project.canvas_height_pt))
    except ValueError as exc:
        raise SceneError('SCENE_INVALID', str(exc)) from exc
    validate_assets(clean, owner_id, project.id)
    revision = PageSceneRevision(id=new_id(), owner_id=owner_id, project_id=project.id,
                                 page_id=page.id, seq=page.next_scene_seq,
                                 parent_revision_id=parent_id, scene_json=clean,
                                 scene_hash=digest(clean), origin=origin,
                                 generation_plan_id=plan_id, created_by=owner_id,
                                 validation_json={})
    page.next_scene_seq += 1
    db.session.add(revision)
    for asset_id, role in set(asset_refs(clean)):
        db.session.add(RevisionAsset(revision_id=revision.id, asset_id=asset_id,
                                     project_id=project.id, role=role))
    db.session.flush()
    return revision


def scene_response(page, revision):
    from .store import signed_url
    ids = {asset_id for asset_id, _ in asset_refs(revision.scene_json)}
    urls = {asset.id: signed_url(asset) for asset in Asset.query.filter(
        Asset.id.in_(ids), Asset.owner_id == revision.owner_id,
        Asset.project_id == revision.project_id, Asset.state == 'ready').all()}
    return {'page_id': page.id, 'page_version': page.row_version,
            'revision_id': revision.id, 'scene_hash': revision.scene_hash,
            'scene': revision.scene_json, 'asset_urls': urls}


def save_commands(project, page, owner_id, payload):
    base = current_scene(page)
    if payload.get('base_revision_id') != base.id or payload.get('base_page_version') != page.row_version:
        raise SceneError('SCENE_VERSION_CONFLICT', 'Page changed; compare and retry', 409,
                         {'current_page_version': page.row_version, 'current_revision_id': base.id})
    new_scene = apply_commands(base.scene_json, payload.get('commands'))
    clean = normalize_scene(new_scene, float(project.canvas_width_pt), float(project.canvas_height_pt))
    validate_assets(clean, owner_id, project.id)
    # Compare-and-swap protects SQLite development too, where FOR UPDATE is ignored.
    old_version = page.row_version
    result = db.session.execute(update(Page).where(Page.id == page.id, Page.row_version == old_version,
                                                   Page.head_revision_id == base.id)
                                .values(row_version=old_version + 1))
    if result.rowcount != 1:
        raise SceneError('SCENE_VERSION_CONFLICT', 'Page changed; compare and retry', 409)
    revision = insert_revision(page, project, owner_id, clean, 'manual', base.id)
    page.head_revision_id = revision.id
    db.session.flush()
    return scene_response(page, revision)


def restore_revision(project, page, owner_id, payload):
    base = current_scene(page)
    if payload.get('base_revision_id') != base.id or payload.get('base_page_version') != page.row_version:
        raise SceneError('SCENE_VERSION_CONFLICT', 'Page changed', 409)
    target = PageSceneRevision.query.filter_by(id=payload.get('target_revision_id'), page_id=page.id).first()
    if target is None:
        raise SceneError('NOT_FOUND', 'Revision not found', 404)
    assert_locked_preserved(base.scene_json, target.scene_json)
    old_version = page.row_version
    result = db.session.execute(update(Page).where(Page.id == page.id,
        Page.row_version == old_version, Page.head_revision_id == base.id)
        .values(row_version=old_version + 1))
    if result.rowcount != 1:
        raise SceneError('SCENE_VERSION_CONFLICT', 'Page changed; compare and retry', 409)
    revision = insert_revision(page, project, owner_id, target.scene_json, 'restore', base.id)
    page.head_revision_id = revision.id
    db.session.flush()
    return scene_response(page, revision)


def accept_candidates(project, owner_id, ids):
    if (not isinstance(ids, list) or not ids or len(ids) > 30 or
            any(not isinstance(candidate_id, str) for candidate_id in ids) or
            len(ids) != len(set(ids))):
        raise SceneError('CANDIDATES_INVALID', 'Select distinct candidates')
    project = Project.query.filter_by(id=project.id, owner_id=owner_id,
        editor_mode='scene_v1', deleted_at=None).populate_existing().with_for_update().first()
    if project is None:
        raise SceneError('NOT_FOUND', 'Project not found', 404)
    candidates = SceneCandidate.query.filter(SceneCandidate.id.in_(ids),
                    SceneCandidate.project_id == project.id, SceneCandidate.owner_id == owner_id).order_by(
                        SceneCandidate.page_id, SceneCandidate.id).with_for_update().all()
    if len(candidates) != len(ids):
        raise SceneError('NOT_FOUND', 'Candidate not found', 404)
    result = []
    for candidate in candidates:
        # Serialize acceptance with manual save's Page CAS, using stable page order
        # for a multi-page batch. READ COMMITTED refreshes the row after a wait.
        page = Page.query.filter_by(id=candidate.page_id, project_id=project.id,
                                    deleted_at=None).with_for_update().first()
        if page is None:
            raise SceneError('NOT_FOUND', 'Page not found', 404)
        revision = db.session.get(PageSceneRevision, candidate.proposed_revision_id)
        if revision is None:
            raise SceneError('CANDIDATE_INVALID', 'Candidate revision is missing')
        if candidate.state == 'accepted' and page.head_revision_id == revision.id:
            result.append(scene_response(page, revision))
            continue
        if candidate.state != 'pending' or page.head_revision_id != candidate.base_revision_id or page.row_version != candidate.base_page_version:
            candidate.state = 'stale'
            raise SceneError('CANDIDATE_STALE', 'Candidate base changed', 409, {'page_id': page.id})
        if revision.page_id != page.id:
            raise SceneError('CANDIDATE_INVALID', 'Candidate revision belongs to another page')
        if page.head_revision_id:
            assert_locked_preserved(current_scene(page).scene_json, revision.scene_json)
        page.head_revision_id = revision.id
        page.row_version += 1
        candidate.state = 'accepted'
        candidate.accepted_at = now()
        result.append(scene_response(page, revision))
    db.session.flush()
    return result


def make_snapshot(project, owner_id, payload, *, persist=True, collect_blockers=False):
    if persist and collect_blockers:
        raise ValueError('Collecting snapshot blockers must not persist a snapshot')
    selected = payload.get('pages')
    maximum = current_app.config['MAX_GENERATED_PAGES']
    if not isinstance(selected, list) or not selected or len(selected) > maximum:
        raise SceneError('SNAPSHOT_INVALID', f'Select 1..{maximum} pages')
    if payload.get('pending_candidate_policy') not in (None, 'export_accepted'):
        raise SceneError('SNAPSHOT_INVALID', 'Unsupported pending candidate policy')
    if any(not isinstance(item, dict) or set(item) != {'page_id', 'page_version', 'revision_id'} or
           not isinstance(item['page_id'], str) or not isinstance(item['revision_id'], str) or
           type(item['page_version']) is not int for item in selected):
        raise SceneError('SNAPSHOT_INVALID', 'Each page must contain page_id, page_version and revision_id')
    # Freeze one consistent project/page state before validating any head. A
    # concurrent edit cannot interleave between two selected page reads.
    project_query = Project.query.filter_by(id=project.id, owner_id=owner_id,
        editor_mode='scene_v1', deleted_at=None).populate_existing()
    project = (project_query.with_for_update() if persist else project_query).first()
    if project is None:
        raise SceneError('NOT_FOUND', 'Project not found', 404)
    page_ids = [item['page_id'] for item in selected]
    pages_query = Page.query.filter(Page.id.in_(page_ids), Page.project_id == project.id,
        Page.deleted_at.is_(None)).order_by(Page.id).populate_existing()
    locked_pages = (pages_query.with_for_update() if persist else pages_query).all()
    by_id = {page.id: page for page in locked_pages}
    if payload.get('project_version') != project.row_version:
        raise SceneError('PROJECT_VERSION_CONFLICT', 'Project plan changed', 409)
    if len(by_id) != len(set(page_ids)):
        raise SceneError('NOT_FOUND', 'Selected page not found', 404)
    try:
        import json
        font_manifest = json.loads((FONT_DIR / 'manifest.json').read_text(encoding='utf-8'))
        font = (FONT_DIR / font_manifest['font_file']).resolve()
        if (font_manifest['font_manifest_id'] != project.font_manifest_id or
                not font.is_relative_to(FONT_DIR.resolve())):
            raise ValueError('font manifest mismatch')
        font_sha256 = hashlib.sha256(font.read_bytes()).hexdigest()
        if font_sha256 != font_manifest['sha256']:
            raise ValueError('font hash mismatch')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SceneError('FONT_UNAVAILABLE', 'Fixed Scene font is missing or corrupt', 503) from exc
    from .store import checked_bytes
    from services.exports.quality_gate import validate_text_fit
    manifest_pages = []
    manifest_assets = {}
    seen = set()
    blockers = []

    def check_page(item):
        page = by_id[item['page_id']]
        if page.id in seen or page.row_version != item.get('page_version') or page.head_revision_id != item.get('revision_id'):
            raise SceneError('SNAPSHOT_VERSION_CONFLICT', 'Page changed or duplicated', 409,
                             {'page_id': page.id})
        seen.add(page.id)
        rev = current_scene(page)
        if digest(rev.scene_json) != rev.scene_hash:
            raise SceneError('SCENE_HASH_MISMATCH', 'Stored Scene revision is corrupt', 503,
                             {'revision_id': rev.id})
        try:
            validate_text_fit(rev.scene_json)
        except SceneError as exc:
            raise SceneError(exc.code, exc.message, exc.status,
                             {**exc.details, 'page_id': page.id}) from exc
        except ValueError as exc:
            raise SceneError('TEXT_OVERFLOW', str(exc), 422, {'page_id': page.id}) from exc
        pending = SceneCandidate.query.filter_by(owner_id=owner_id, project_id=project.id,
            page_id=page.id, state='pending', base_revision_id=rev.id,
            base_page_version=page.row_version).first()
        if pending is not None:
            if (rev.seq == 1 and not rev.scene_json['elements'] and
                rev.scene_json['background']['kind'] == 'solid'):
                raise SceneError('FIRST_GENERATION_UNACCEPTED',
                                 'Accept or reject the first generation candidate before export', 422)
            if payload.get('pending_candidate_policy') != 'export_accepted':
                raise SceneError('PENDING_CANDIDATE_CHOICE_REQUIRED',
                                 'Explicitly choose to export the currently accepted revision', 422)
        validate_assets(rev.scene_json, owner_id, project.id)
        for asset_id, _ in asset_refs(rev.scene_json):
            asset = db.session.get(Asset, asset_id)
            if asset_id not in manifest_assets:
                try:
                    checked_bytes(asset)
                except (OSError, ValueError) as exc:
                    raise SceneError('ASSET_UNAVAILABLE', 'Referenced asset is missing or corrupt', 503,
                                     {'asset_id': asset_id}) from exc
            manifest_assets[asset_id] = asset.sha256
        manifest_pages.append({'page_id': page.id, 'revision_id': rev.id,
                               'scene_hash': rev.scene_hash, 'page_version': page.row_version})

    for item in selected:
        if not collect_blockers:
            check_page(item)
            continue
        try:
            check_page(item)
        except SceneError as exc:
            blockers.append({'code': exc.code, 'message': exc.message,
                             'page_id': exc.details.get('page_id', item['page_id']),
                             'details': exc.details})
    if collect_blockers:
        return {'ready': not blockers, 'blockers': blockers, 'page_count': len(selected)}
    manifest = {'project_id': project.id, 'title': project.project_title,
                'canvas': {'width_pt': float(project.canvas_width_pt), 'height_pt': float(project.canvas_height_pt)},
                'font_manifest_id': project.font_manifest_id,
                'font_sha256': font_sha256,
                'assets': manifest_assets, 'pages': manifest_pages}
    if not persist:
        return manifest
    snapshot = DeckSnapshot(id=new_id(), owner_id=owner_id, project_id=project.id,
                            confirmed_by=owner_id, manifest_json=manifest, manifest_hash=digest(manifest))
    db.session.add(snapshot)
    for ordinal, item in enumerate(manifest_pages, 1):
        db.session.add(SnapshotPage(snapshot_id=snapshot.id, ordinal=ordinal,
                                    page_id=item['page_id'], revision_id=item['revision_id']))
    db.session.flush()
    return snapshot


def idempotent(owner_id, operation, key, request_hash):
    if not key or len(key) > 200:
        raise SceneError('IDEMPOTENCY_KEY_REQUIRED', 'Idempotency-Key required', 422)
    # Serialize identical keys before checking the record. Otherwise two
    # concurrent POSTs can both observe absence and the loser gets a database
    # uniqueness error instead of the first request's committed response.
    if db.session.get_bind().dialect.name == 'postgresql':
        scope = f'{owner_id}\0{operation}\0{key}'.encode('utf-8')
        lock_key = int.from_bytes(hashlib.sha256(scope).digest()[:8], 'big', signed=True)
        db.session.execute(text('SELECT pg_advisory_xact_lock(:lock_key)'), {'lock_key': lock_key})
    record = ApiIdempotencyRecord.query.filter_by(owner_id=owner_id, operation=operation, idempotency_key=key).first()
    if record and record.request_hash != request_hash:
        raise SceneError('IDEMPOTENCY_CONFLICT', 'Key reused for different input', 409)
    return record


def remember(owner_id, operation, key, request_hash, status, response):
    db.session.add(ApiIdempotencyRecord(id=new_id(), owner_id=owner_id,
                   operation=operation, idempotency_key=key, request_hash=request_hash,
                   response_status=status, response_json=response, expires_at=now() + timedelta(days=7)))
