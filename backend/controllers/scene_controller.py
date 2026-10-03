"""Owner-scoped v2 endpoints. Legacy image routes never mutate Scene projects."""
import hashlib
import json
import math
import os
import re
from copy import deepcopy

from werkzeug.exceptions import HTTPException

from flask import Blueprint, current_app, jsonify, request, send_file
from pathlib import Path
from sqlalchemy import or_, and_

from models import db, Page, Project, ProjectTemplateAsset
from models.scene_v1 import (Asset, DeckSnapshot, GenerationPlan, PageSceneRevision,
                             SceneCandidate, SceneExport, SceneTaskItem, SnapshotPage, ModelCredential, TemplateDocument,
                             new_id, now)
from services.scene.credentials import create_credential, credential_request_fingerprint, credential_summary
from services.scene.openapi import build_openapi
from services.scene.readiness import scene_readiness
from services.scene.queue_capacity import admit_tasks, queue_capacity
from services.scene.storage_quota import storage_capacity
from services.templates.scene_importer import inspect_pptx, inspect_reference_image
from services.templates.style_profile import normalize_style_profile
from services.scene.store import put_bytes
from services.scene.store import checked_bytes, path_for, put_image, signed_url, verify_url
from services.scene.validation import asset_refs, blank_scene, digest
from services.scene.versioning import (SceneError, accept_candidates, current_scene,
    ensure_owner, idempotent, insert_revision, make_snapshot, owned_page,
    owned_project, remember, restore_revision, save_commands, scene_response)

from services.scene import telemetry

scene_bp = Blueprint('scene_v2', __name__, url_prefix='/api/v2')


def ok(data, status=200):
    if isinstance(data, dict):
        telemetry.bind(**{key: value for key, value in data.items() if key in telemetry.IDS and key != 'request_id'})
        if data.get('task_group_id'):
            telemetry.bind(task_id=data['task_group_id'])
    return jsonify({'data': data, 'request_id': telemetry.request_id()}), status


def paged_rows(query, model, order_field, *, ascending=False):
    try:
        limit = int(request.args.get('limit', '20'))
    except ValueError as exc:
        raise SceneError('PAGE_LIMIT_INVALID', 'Limit must be an integer') from exc
    if not 1 <= limit <= 100:
        raise SceneError('PAGE_LIMIT_INVALID', 'Limit must be 1..100')
    cursor = request.args.get('cursor')
    if cursor:
        previous = query.filter(model.id == cursor).first()
        if previous is None:
            raise SceneError('CURSOR_INVALID', 'List cursor unavailable', 422)
        value = getattr(previous, order_field.key)
        query = query.filter(or_(order_field > value, and_(order_field == value, model.id > previous.id))
            if ascending else or_(order_field < value, and_(order_field == value, model.id < previous.id)))
    direction = (order_field.asc(), model.id.asc()) if ascending else (order_field.desc(), model.id.desc())
    rows = query.order_by(*direction).limit(limit + 1).all()
    return rows[:limit], rows[limit - 1].id if len(rows) > limit else None


def possible_charge_on_retry(task):
    if task.operation in ('generate_scene', 'ai_edit') and (task.result_json or {}).get('draft_asset_id'):
        return False  # The remaining phase resolves an already-paid immutable draft.
    if task.operation == 'check_credential':
        return ((task.input_json or {}).get('kind') in ('text_generation', 'image_generation') and
                task.dispatch_state not in ('not_sent', 'rejected'))
    return (task.operation in ('generate_scene', 'generate_asset', 'ai_edit',
                               'analyze_template', 'generate_outline') and
            task.dispatch_state not in ('not_sent', 'rejected'))


@scene_bp.errorhandler(SceneError)
def scene_error(exc):
    telemetry.set_error(exc.code)
    db.session.rollback()
    return jsonify({'error': {'code': exc.code, 'message': exc.message,
                              'stage': (request.endpoint or 'scene_v2').rsplit('.', 1)[-1],
                              'retryable': False, 'details': exc.details},
                    'request_id': telemetry.request_id()}), exc.status


@scene_bp.errorhandler(ValueError)
def value_error(exc):
    telemetry.set_error('INPUT_INVALID')
    db.session.rollback()
    return jsonify({'error': {'code': 'INPUT_INVALID', 'message': 'Invalid Scene input',
                              'stage': (request.endpoint or 'scene_v2').rsplit('.', 1)[-1],
                              'retryable': False, 'details': {}},
                    'request_id': telemetry.request_id()}), 422


@scene_bp.errorhandler(Exception)
def unexpected_scene_error(exc):
    db.session.rollback()
    status = exc.code if isinstance(exc, HTTPException) else 500
    code = 'HTTP_ERROR' if isinstance(exc, HTTPException) else telemetry.code_of(exc)
    telemetry.set_error(code)
    return jsonify({'error': {'code': code, 'message': 'Scene request failed',
        'stage': (request.endpoint or 'scene_v2').rsplit('.', 1)[-1],
        'retryable': False, 'details': {}}, 'request_id': telemetry.request_id()}), status


@scene_bp.before_request
def scene_access():
    if (request.path.startswith('/api/v2/assets/') and request.path.endswith('/content')) or request.path == '/api/v2/fonts/noto-sans-sc':
        return
    if not current_app.config.get('SCENE_EDITOR_ENABLED'):
        raise SceneError('SCENE_DISABLED', 'Scene editor is disabled', 404)
    if not current_app.config.get('TESTING') and (
        not __import__('os').getenv('ACCESS_CODE') or
        current_app.secret_key == 'your-secret-key-change-this'
    ):
        raise SceneError('SCENE_AUTH_NOT_CONFIGURED', 'Configure ACCESS_CODE and SECRET_KEY', 503)
    origin = request.headers.get('Origin')
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and origin:
        allowed = current_app.config.get('CORS_ORIGINS', [])
        if allowed != '*' and origin not in allowed and origin != request.host_url.rstrip('/'):
            raise SceneError('ORIGIN_FORBIDDEN', 'Untrusted origin', 403)


def owner_id():
    return ensure_owner()


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise SceneError('INPUT_INVALID', 'JSON object required')
    return data


def unique_request(operation, payload):
    owner = owner_id()
    key = request.headers.get('Idempotency-Key')
    request_hash = digest(payload)
    existing = idempotent(owner, operation, key, request_hash)
    return owner, key, request_hash, existing


def task_project(project_id, owner):
    # Every admission locks its project before the queue mutex. NO KEY UPDATE
    # still serializes planning/archive writes but permits worker FK inserts.
    project = Project.query.filter_by(id=project_id, owner_id=owner,
        editor_mode='scene_v1', deleted_at=None).populate_existing().with_for_update(key_share=True).first()
    if project is None:
        raise SceneError('NOT_FOUND', 'Project not found', 404)
    return project


def refreshed_scene_data(data, owner, project_id):
    """Replay immutable revision metadata while renewing five-minute asset URLs."""
    response = dict(data)
    ids = {asset_id for asset_id, _ in asset_refs(response['scene'])}
    assets = Asset.query.filter(Asset.id.in_(ids), Asset.owner_id == owner,
                                Asset.project_id == project_id, Asset.state == 'ready').all()
    response['asset_urls'] = {asset.id: signed_url(asset) for asset in assets}
    return response


def replay_scene_response(record, owner, project_id):
    return ok(refreshed_scene_data(record.response_json['data'], owner, project_id),
              record.response_status)


def project_data(project, include_pages=False):
    data = {'project_id': project.id, 'title': project.project_title,
            'prompt': project.idea_prompt, 'editor_mode': project.editor_mode,
            'project_version': project.row_version,
            'canvas': {'width_pt': float(project.canvas_width_pt),
                       'height_pt': float(project.canvas_height_pt)},
            'font_manifest_id': project.font_manifest_id,
            'active_plan_id': project.active_plan_id,
            'model_config': project.model_config_json or {}}
    if include_pages:
        data['pages'] = [page_data(p) for p in Page.query.filter_by(project_id=project.id, deleted_at=None)
                         .order_by(Page.order_index).all()]
    return data


def page_data(page):
    return {'page_id': page.id, 'order_index': page.order_index,
            'outline_content': page.get_outline_content(),
            'page_version': page.row_version, 'revision_id': page.head_revision_id,
            'template_asset_id': page.template_asset_id,
            'template_style_text': page.template_style_text}


@scene_bp.get('/readiness')
def readiness():
    result = scene_readiness()
    return ok(result, 200 if result['ready'] else 503)


@scene_bp.get('/storage-capacity')
def get_storage_capacity():
    return ok(storage_capacity(owner_id()))


@scene_bp.get('/queue-capacity')
def get_queue_capacity():
    return ok(queue_capacity(owner_id()))


@scene_bp.get('/openapi.json')
def scene_openapi():
    return jsonify(build_openapi())


def valid_outline(value):
    from services.scene.outline import normalize_outline_page
    try:
        return normalize_outline_page(value)
    except ValueError as exc:
        raise SceneError('OUTLINE_INVALID', 'Outline fields or limits invalid') from exc


def valid_model_config(value):
    if not isinstance(value, dict) or set(value) - {'text_model', 'image_model'} or any(
        not isinstance(model, str) or not 1 <= len(model) <= 150 for model in value.values()):
        raise SceneError('MODEL_CONFIG_INVALID', 'Configure supported text/image model names')
    return value


@scene_bp.post('/projects')
def create_project():
    data = body()
    owner, key, request_hash, existing = unique_request('create_scene_project', data)
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    raw_title = data.get('title')
    title = raw_title.strip() if isinstance(raw_title, str) else ''
    pages = data.get('pages', [{'title': '新页面', 'points': []}])
    canvas = data.get('canvas', {'width_pt': 960, 'height_pt': 540})
    prompt = data.get('prompt', '')
    if (not title or len(title) > 255 or not isinstance(prompt, str) or len(prompt) > 10000 or
        not isinstance(pages, list) or not 1 <= len(pages) <= current_app.config['MAX_GENERATED_PAGES']):
        raise SceneError('PROJECT_INVALID', 'Title and 1..30 pages required')
    if not isinstance(canvas, dict) or set(canvas) != {'width_pt', 'height_pt'}:
        raise SceneError('CANVAS_INVALID', 'Canvas width and height required')
    try:
        width, height = float(canvas['width_pt']), float(canvas['height_pt'])
    except (TypeError, ValueError):
        raise SceneError('CANVAS_INVALID', 'Canvas width and height must be numbers') from None
    if not (math.isfinite(width) and math.isfinite(height) and 300 <= width <= 2880 and 200 <= height <= 2880):
        raise SceneError('CANVAS_INVALID', 'Canvas out of range')
    model_config = valid_model_config(data.get('model_config') or {})
    project = Project(id=new_id(), owner_id=owner, editor_mode='scene_v1',
                      project_title=title, idea_prompt=prompt,
                      canvas_width_pt=width, canvas_height_pt=height,
                      font_manifest_id='fonts-v1', model_config_json=model_config,
                      status='DRAFT')
    db.session.add(project)
    db.session.flush()
    for index, outline in enumerate(pages):
        page = Page(id=new_id(), project_id=project.id, order_index=index, status='DRAFT')
        page.set_outline_content(valid_outline(outline))
        db.session.add(page)
        db.session.flush()
        revision = insert_revision(page, project, owner, blank_scene(width, height), 'manual')
        page.head_revision_id = revision.id
        page.row_version = 1
    response = project_data(project, include_pages=True)
    remember(owner, 'create_scene_project', key, request_hash, 201, {'data': response})
    db.session.commit()
    return ok(response, 201)


@scene_bp.get('/projects')
def list_projects():
    owner = owner_id()
    try:
        limit = int(request.args.get('limit', '20'))
    except ValueError as exc:
        raise SceneError('PAGE_LIMIT_INVALID', 'Limit must be an integer') from exc
    if not 1 <= limit <= 100:
        raise SceneError('PAGE_LIMIT_INVALID', 'Limit must be 1..100')
    query = Project.query.filter_by(owner_id=owner, editor_mode='scene_v1', deleted_at=None)
    cursor = request.args.get('cursor')
    if cursor:
        previous = query.filter_by(id=cursor).first()
        if previous is None:
            raise SceneError('CURSOR_INVALID', 'Project cursor unavailable', 422)
        query = query.filter(or_(Project.updated_at < previous.updated_at,
            and_(Project.updated_at == previous.updated_at, Project.id < previous.id)))
    rows = query.order_by(Project.updated_at.desc(), Project.id.desc()).limit(limit + 1).all()
    next_cursor = rows[limit - 1].id if len(rows) > limit else None
    return ok({'items': [project_data(row) for row in rows[:limit]],
               'next_cursor': next_cursor})


@scene_bp.get('/projects/<project_id>')
def get_project(project_id):
    return ok(project_data(owned_project(project_id, owner_id()), include_pages=True))


@scene_bp.delete('/projects/<project_id>')
def archive_project(project_id):
    owner = owner_id()
    operation = f'archive_project:{project_id}'
    _, key, request_hash, existing = unique_request(operation, {})
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    project = Project.query.filter_by(id=project_id, owner_id=owner,
        editor_mode='scene_v1', deleted_at=None).populate_existing().with_for_update().first()
    if project is None:
        raise SceneError('NOT_FOUND', 'Project not found', 404)
    from models.scene_v1 import now
    project.deleted_at = now()
    for task in SceneTaskItem.query.filter_by(project_id=project.id, owner_id=owner).filter(
        SceneTaskItem.state.in_(['queued', 'running', 'waiting_assets'])):
        task.cancel_requested_at = now()
        if task.state in ('queued', 'waiting_assets'):
            task.state = 'cancelled'
    response = {'project_id': project.id, 'archived': True}
    remember(owner, operation, key, request_hash, 200, {'data': response})
    db.session.commit()
    return ok(response)


@scene_bp.post('/projects/<project_id>/pages')
def add_page(project_id):
    owner = owner_id()
    project = owned_project(project_id, owner)
    data = body()
    operation = f'add_page:{project_id}'
    _, key, request_hash, existing = unique_request(operation, data)
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    project = Project.query.filter_by(id=project_id, owner_id=owner,
        editor_mode='scene_v1', deleted_at=None).populate_existing().with_for_update().first()
    if project is None:
        raise SceneError('NOT_FOUND', 'Project not found', 404)
    if data.get('base_project_version') != project.row_version:
        raise SceneError('PROJECT_VERSION_CONFLICT', 'Project changed', 409)
    pages = Page.query.filter_by(project_id=project.id, deleted_at=None).order_by(
        Page.order_index).populate_existing().with_for_update().all()
    count = len(pages)
    if count >= current_app.config['MAX_GENERATED_PAGES']:
        raise SceneError('PAGE_LIMIT', 'Page limit reached')
    insert_at = data.get('insert_at', count)
    if type(insert_at) is not int or not 0 <= insert_at <= count:
        raise SceneError('PAGE_POSITION_INVALID', 'Insert position must be within the deck')
    source_id = data.get('copy_from_page_id')
    source = next((item for item in pages if item.id == source_id), None) if source_id is not None else None
    if source_id is not None and source is None:
        raise SceneError('NOT_FOUND', 'Source page not found', 404)
    if source is not None:
        source_revision = current_scene(source)
        if (data.get('copy_from_page_version') != source.row_version or
                data.get('copy_from_revision_id') != source_revision.id):
            raise SceneError('PAGE_VERSION_CONFLICT', 'Source page changed', 409)
        scene = deepcopy(source_revision.scene_json)
        for element in scene['elements']:
            element['id'] = new_id()
        outline = data.get('outline', source.get_outline_content() or {'title': '', 'points': []})
    else:
        if 'copy_from_page_version' in data or 'copy_from_revision_id' in data:
            raise SceneError('INPUT_INVALID', 'Source page ID required for copying')
        scene = blank_scene(float(project.canvas_width_pt), float(project.canvas_height_pt))
        outline = data.get('outline') or {'title': '', 'points': []}
    for sibling in pages[insert_at:]:
        sibling.order_index += 1
        sibling.row_version += 1
    page = Page(id=new_id(), project_id=project.id, order_index=insert_at, status='DRAFT')
    page.set_outline_content(valid_outline(outline))
    if source is not None:
        page.template_asset_id = source.template_asset_id
        page.template_style_text = source.template_style_text
        page.template_selection_source = source.template_selection_source
        page.template_match_reason = source.template_match_reason
        page.template_match_confidence = source.template_match_confidence
    db.session.add(page)
    db.session.flush()
    rev = insert_revision(page, project, owner, scene, 'copy' if source is not None else 'manual')
    if source is not None:
        rev.validation_json = {'copied_from_page_id': source.id,
                               'copied_from_revision_id': source_revision.id}
    page.head_revision_id = rev.id
    page.row_version = 1
    project.row_version += 1
    response = page_data(page)
    remember(owner, operation, key, request_hash, 201, {'data': response})
    db.session.commit()
    return ok(response, 201)


@scene_bp.delete('/projects/<project_id>/pages/<page_id>')
def delete_scene_page(project_id, page_id):
    owner = owner_id()
    owned_project(project_id, owner)
    data = body()
    operation = f'delete_page:{page_id}'
    _, key, request_hash, existing = unique_request(operation, data)
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    project = Project.query.filter_by(id=project_id, owner_id=owner,
        editor_mode='scene_v1', deleted_at=None).populate_existing().with_for_update().first()
    if project is None:
        raise SceneError('NOT_FOUND', 'Project not found', 404)
    pages = Page.query.filter_by(project_id=project.id, deleted_at=None).order_by(
        Page.id).populate_existing().with_for_update().all()
    page = next((item for item in pages if item.id == page_id), None)
    if page is None:
        raise SceneError('NOT_FOUND', 'Page not found', 404)
    if data.get('base_project_version') != project.row_version or data.get('base_page_version') != page.row_version:
        raise SceneError('PAGE_VERSION_CONFLICT', 'Project or page changed', 409)
    pages.sort(key=lambda item: (item.order_index, item.id))
    if len(pages) <= 1:
        raise SceneError('PAGE_LIMIT', 'Keep at least one page')
    from models.scene_v1 import now
    page.deleted_at = now()
    for task in SceneTaskItem.query.filter_by(page_id=page.id, project_id=project.id, owner_id=owner).filter(
        SceneTaskItem.state.in_(['queued', 'running', 'waiting_assets'])):
        task.cancel_requested_at = now()
        if task.state in ('queued', 'waiting_assets'):
            task.state = 'cancelled'
    for index, sibling in enumerate(p for p in pages if p.id != page.id):
        sibling.order_index = index
        sibling.row_version += 1
    project.row_version += 1
    response = project_data(project, True)
    remember(owner, operation, key, request_hash, 200, {'data': response})
    db.session.commit()
    return ok(response)


@scene_bp.patch('/projects/<project_id>/model-config')
def set_model_config(project_id):
    owner = owner_id()
    owned_project(project_id, owner)
    data = body()
    operation = f'model_config:{project_id}'
    _, key, request_hash, existing = unique_request(operation, data)
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    project = Project.query.filter_by(id=project_id, owner_id=owner,
        editor_mode='scene_v1', deleted_at=None).populate_existing().with_for_update().first()
    if project is None:
        raise SceneError('NOT_FOUND', 'Project not found', 404)
    if data.get('base_project_version') != project.row_version:
        raise SceneError('PROJECT_VERSION_CONFLICT', 'Project changed', 409)
    config = valid_model_config(data.get('model_config'))
    project.model_config_json = config
    project.row_version += 1
    response = project_data(project, True)
    remember(owner, operation, key, request_hash, 200, {'data': response})
    db.session.commit()
    return ok(response)


@scene_bp.patch('/projects/<project_id>/outline')
def save_outline(project_id):
    owner = owner_id()
    owned_project(project_id, owner)
    data = body()
    operation = f'save_outline:{project_id}'
    _, key, request_hash, existing = unique_request(operation, data)
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    # Serialize planning updates before locking affected pages. Scene writes use
    # Page CAS, so an edit racing this outline change cannot lose row_version.
    project = Project.query.filter_by(id=project_id, owner_id=owner,
        editor_mode='scene_v1', deleted_at=None).populate_existing().with_for_update().first()
    if project is None:
        raise SceneError('NOT_FOUND', 'Project not found', 404)
    if data.get('base_project_version') != project.row_version:
        raise SceneError('PROJECT_VERSION_CONFLICT', 'Project changed', 409)
    provided = data.get('pages')
    pages = Page.query.filter_by(project_id=project.id, deleted_at=None).order_by(
        Page.id).populate_existing().with_for_update().all()
    if not isinstance(provided, list) or not 1 <= len(provided) <= current_app.config['MAX_GENERATED_PAGES'] or not all(isinstance(x, dict) for x in provided):
        raise SceneError('OUTLINE_INVALID', 'Provide 1..30 outline pages')
    if any(item.get('page_id') is not None and
           not isinstance(item.get('page_id'), str) for item in provided):
        raise SceneError('OUTLINE_INVALID', 'Invalid page ID')
    ids = [item['page_id'] for item in provided if item.get('page_id')]
    by_id = {p.id: p for p in pages}
    if len(ids) != len(set(ids)) or not set(ids) <= set(by_id):
        raise SceneError('OUTLINE_INVALID', 'Duplicate or foreign page ID')
    from models.scene_v1 import now
    for index, item in enumerate(provided):
        outline = item.get('outline')
        outline = valid_outline(outline)
        page = by_id.get(item.get('page_id'))
        if page is None:
            page = Page(id=new_id(), project_id=project.id, order_index=index, status='DRAFT')
            db.session.add(page)
            db.session.flush()
            revision = insert_revision(page, project, owner, blank_scene(float(project.canvas_width_pt), float(project.canvas_height_pt)), 'manual')
            page.head_revision_id = revision.id
            page.row_version = 1
        page.order_index = index
        page.set_outline_content(outline)
        if item.get('page_id'):
            page.row_version += 1
    for page in pages:
        if page.id not in ids:
            page.deleted_at = now()
    project.row_version += 1
    response = project_data(project, True)
    remember(owner, operation, key, request_hash, 200, {'data': response})
    db.session.commit()
    return ok(response)


@scene_bp.post('/projects/<project_id>/outline-tasks')
def generate_outline_task(project_id):
    owner = owner_id()
    project = owned_project(project_id, owner)
    data = body()
    _, key, request_hash, existing = unique_request(f'outline:{project_id}', data)
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    project = task_project(project_id, owner)
    if data.get('base_project_version') != project.row_version:
        raise SceneError('PROJECT_VERSION_CONFLICT', 'Project changed', 409)
    count, brief = data.get('slide_count'), data.get('brief')
    if type(count) is not int or not 1 <= count <= current_app.config['MAX_GENERATED_PAGES'] or not isinstance(brief, str) or not 1 <= len(brief) <= 10000:
        raise SceneError('OUTLINE_INPUT_INVALID', 'Brief and page count required')
    credential = ModelCredential.query.filter_by(id=data.get('credential_id'), owner_id=owner).first()
    if not credential or credential.status == 'revoked':
        raise SceneError('CREDENTIAL_NOT_FOUND', 'Select your model API Key')
    model = (project.model_config_json or {}).get('text_model')
    if not model:
        raise SceneError('MODEL_NOT_CONFIGURED', 'Set a text model first')
    admit_tasks(owner, 'generate_outline')
    frozen = {'brief': brief, 'slide_count': count, 'base_project_version': project.row_version,
              'text_model': model}
    task = SceneTaskItem(id=new_id(), owner_id=owner, project_id=project.id,
        credential_id=credential.id, group_id=new_id(), logical_key='outline',
        operation='generate_outline', resource_id=project.id,
        input_json=frozen, input_hash=digest(frozen))
    db.session.add(task)
    response = {'task_id': task.id}
    remember(owner, f'outline:{project_id}', key, request_hash, 202, {'data': response})
    db.session.commit()
    return ok(response, 202)


@scene_bp.get('/projects/<project_id>/outline-drafts/<asset_id>')
def get_outline_draft(project_id, asset_id):
    owner = owner_id()
    owned_project(project_id, owner)
    asset = Asset.query.filter_by(id=asset_id, owner_id=owner, project_id=project_id,
                                  kind='outline_draft', state='ready').first()
    if asset is None:
        raise SceneError('NOT_FOUND', 'Outline draft not found', 404)
    provenance = asset.provenance_json or {}
    version = provenance.get('base_project_version')
    if type(version) is not int or version < 0:
        # Compatibility for earlier drafts: recover their *original* task
        # version, never substitute today's project version for missing proof.
        task = SceneTaskItem.query.filter_by(id=provenance.get('task_item_id'),
            owner_id=owner, project_id=project_id, operation='generate_outline').first()
        version = (task.input_json or {}).get('base_project_version') if task else None
        if type(version) is not int or version < 0:
            version = None
    return ok({**json.loads(checked_bytes(asset)), 'base_project_version': version})


@scene_bp.get('/projects/<project_id>/pages/<page_id>/scene')
def get_scene(project_id, page_id):
    project = owned_project(project_id, owner_id())
    page = owned_page(project, page_id)
    revision_id = request.args.get('revision_id')
    rev = PageSceneRevision.query.filter_by(id=revision_id, page_id=page.id).first() if revision_id else current_scene(page)
    if rev is None:
        raise SceneError('NOT_FOUND', 'Revision not found', 404)
    return ok(scene_response(page, rev))


@scene_bp.patch('/projects/<project_id>/pages/<page_id>/scene')
def patch_scene(project_id, page_id):
    owner = owner_id()
    owned_page(owned_project(project_id, owner), page_id)
    data = body()
    _, key, request_hash, existing = unique_request(f'save_scene:{page_id}', data)
    if existing:
        return replay_scene_response(existing, owner, project_id)
    project = Project.query.filter_by(id=project_id, owner_id=owner,
        editor_mode='scene_v1', deleted_at=None).populate_existing().with_for_update().first()
    if project is None:
        raise SceneError('NOT_FOUND', 'Project not found', 404)
    page = Page.query.filter_by(id=page_id, project_id=project.id,
        deleted_at=None).populate_existing().with_for_update().first()
    if page is None:
        raise SceneError('NOT_FOUND', 'Page not found', 404)
    response = save_commands(project, page, owner, data)
    remember(owner, f'save_scene:{page_id}', key, request_hash, 200, {'data': response})
    db.session.commit()
    return ok(response)


@scene_bp.get('/projects/<project_id>/pages/<page_id>/revisions')
def list_revisions(project_id, page_id):
    project = owned_project(project_id, owner_id())
    page = owned_page(project, page_id)
    rows, next_cursor = paged_rows(PageSceneRevision.query.filter_by(page_id=page.id,
        project_id=project.id, owner_id=project.owner_id), PageSceneRevision, PageSceneRevision.seq)
    return ok({'items': [{'revision_id': r.id, 'seq': r.seq, 'origin': r.origin,
                'scene_hash': r.scene_hash, 'created_at': r.created_at.isoformat()} for r in rows],
               'next_cursor': next_cursor})


@scene_bp.post('/projects/<project_id>/pages/<page_id>/restore')
def restore_scene(project_id, page_id):
    owner = owner_id()
    owned_page(owned_project(project_id, owner), page_id)
    data = body()
    operation = f'restore_scene:{page_id}'
    _, key, request_hash, existing = unique_request(operation, data)
    if existing:
        return replay_scene_response(existing, owner, project_id)
    project = Project.query.filter_by(id=project_id, owner_id=owner,
        editor_mode='scene_v1', deleted_at=None).populate_existing().with_for_update().first()
    if project is None:
        raise SceneError('NOT_FOUND', 'Project not found', 404)
    page = Page.query.filter_by(id=page_id, project_id=project.id,
        deleted_at=None).populate_existing().with_for_update().first()
    if page is None:
        raise SceneError('NOT_FOUND', 'Page not found', 404)
    response = restore_revision(project, page, owner, data)
    remember(owner, operation, key, request_hash, 200, {'data': response})
    db.session.commit()
    return ok(response)


@scene_bp.post('/projects/<project_id>/assets')
def upload_asset(project_id):
    owner = owner_id()
    owned_project(project_id, owner)
    file = request.files.get('file')
    if file is None:
        raise SceneError('INPUT_INVALID', 'Image file required')
    payload = file.stream.read(current_app.config['MAX_UPLOAD_BYTES'] + 1)
    if len(payload) > current_app.config['MAX_UPLOAD_BYTES']:
        raise SceneError('UPLOAD_TOO_LARGE', 'Image exceeds upload limit', 413)
    operation = f'upload_asset:{project_id}'
    filename = (file.filename or '').lower()
    fingerprint = {'filename': filename, 'byte_size': len(payload),
                   'sha256': hashlib.sha256(payload).hexdigest()}
    _, key, request_hash, existing = unique_request(operation, fingerprint)
    if existing:
        response = dict(existing.response_json['data'])
        asset = Asset.query.filter_by(id=response['asset_id'], owner_id=owner,
                                      project_id=project_id, state='ready').first()
        if asset is None:
            raise SceneError('ASSET_UNAVAILABLE', 'Uploaded asset is unavailable', 503)
        response['url'] = signed_url(asset)
        return ok(response, existing.response_status)
    asset = put_image(owner, project_id, payload)
    response = {'asset_id': asset.id, 'url': signed_url(asset),
                'sha256': asset.sha256, 'width_px': asset.width_px,
                'height_px': asset.height_px}
    remember(owner, operation, key, request_hash, 201, {'data': response})
    db.session.commit()
    return ok(response, 201)


@scene_bp.get('/assets/<asset_id>/content')
def asset_content(asset_id):
    asset = db.session.get(Asset, asset_id)
    if asset is None or asset.state != 'ready' or not verify_url(asset, request.args.get('expires'), request.args.get('sig')):
        raise SceneError('NOT_FOUND', 'Asset unavailable', 404)
    try:
        checked_bytes(asset)
    except (OSError, ValueError) as exc:
        raise SceneError('ASSET_UNAVAILABLE', 'Asset is missing or corrupt', 503) from exc
    return send_file(path_for(asset), mimetype=asset.mime_type, download_name=f'{asset.id}.{asset.storage_key.rsplit(".", 1)[-1]}',
                     as_attachment=request.args.get('download') == '1', max_age=0)


@scene_bp.get('/fonts/noto-sans-sc')
def scene_font():
    font = Path(__file__).resolve().parents[1] / 'fonts' / 'NotoSansSC-Regular.ttf'
    return send_file(font, mimetype='font/otf', max_age=86400,
                     as_attachment=request.args.get('download') == '1',
                     download_name='NotoSansCJKsc-Regular.otf')


@scene_bp.get('/font-manifest')
def scene_font_manifest():
    owner_id()
    directory = Path(__file__).resolve().parents[1] / 'fonts'
    try:
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        font = (directory / manifest['font_file']).resolve()
        if (manifest['font_manifest_id'] != 'fonts-v1' or
                not font.is_relative_to(directory.resolve()) or
                manifest['family'] != 'Noto Sans CJK SC' or
                hashlib.sha256(font.read_bytes()).hexdigest() != manifest['sha256']):
            raise ValueError('font manifest mismatch')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SceneError('FONT_UNAVAILABLE', 'Fixed Scene font is missing or corrupt', 503) from exc
    return ok({'font_manifest_id': manifest['font_manifest_id'],
               'family': manifest['family'], 'style': manifest['style'],
               'format': manifest['format'], 'sha256': manifest['sha256'],
               'download_url': '/api/v2/fonts/noto-sans-sc?download=1'})


@scene_bp.post('/projects/<project_id>/generation-plans')
def create_plan(project_id):
    owner = owner_id()
    owned_project(project_id, owner)
    data = body()
    _, key, request_hash, existing = unique_request(f'plan:{project_id}', data)
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    project = Project.query.filter_by(id=project_id, owner_id=owner,
        editor_mode='scene_v1', deleted_at=None).populate_existing().with_for_update().first()
    if project is None:
        raise SceneError('NOT_FOUND', 'Project not found', 404)
    pages = Page.query.filter_by(project_id=project.id, deleted_at=None).order_by(
        Page.id).populate_existing().with_for_update().all()
    pages.sort(key=lambda page: (page.order_index, page.id))
    if data.get('base_project_version') != project.row_version:
        raise SceneError('PROJECT_VERSION_CONFLICT', 'Project changed', 409)
    expected_pages = data.get('expected_pages')
    if expected_pages is not None:
        actual_pages = [{'page_id': p.id, 'page_version': p.row_version,
                         'revision_id': p.head_revision_id} for p in pages]
        if expected_pages != actual_pages:
            raise SceneError('PAGE_VERSION_CONFLICT', 'Page changed since outline confirmation', 409)
    manifest_pages = []
    for p in pages:
        reference = None
        if p.template_asset_id:
            template = ProjectTemplateAsset.query.filter_by(id=p.template_asset_id, project_id=project.id, deleted_at=None).first()
            if template is None:
                raise SceneError('TEMPLATE_NOT_FOUND', 'Page reference template unavailable')
            preview = Asset.query.filter_by(id=template.preview_asset_id, owner_id=owner,
                project_id=project.id, kind='template_preview', state='ready').first()
            if preview is None:
                raise SceneError('ASSET_UNAVAILABLE', 'Reference preview is unavailable', 503)
            try:
                checked_bytes(preview)
            except (OSError, ValueError) as exc:
                raise SceneError('ASSET_UNAVAILABLE', 'Reference preview is missing or corrupt', 503) from exc
            reference = {'template_asset_id': template.id, 'preview_asset_id': preview.id,
                         'preview_sha256': preview.sha256, 'style_profile': template.get_analysis(),
                         'analysis_hash': template.analysis_hash,
                         'style_text': p.template_style_text}
        manifest_pages.append({'page_id': p.id, 'outline': p.get_outline_content(),
            'page_version': p.row_version, 'base_revision_id': p.head_revision_id,
            'reference': reference})
    manifest = {'project_id': project.id, 'canvas': {'width_pt': float(project.canvas_width_pt), 'height_pt': float(project.canvas_height_pt)},
                'font_manifest_id': project.font_manifest_id, 'model_config': project.model_config_json,
                'pages': manifest_pages}
    plan = GenerationPlan(id=new_id(), owner_id=owner, project_id=project.id,
                          base_project_version=project.row_version,
                          manifest_json=manifest, manifest_hash=digest(manifest), created_by=owner)
    db.session.add(plan)
    project.active_plan_id = plan.id
    response = {'plan_id': plan.id, 'manifest_hash': plan.manifest_hash}
    remember(owner, f'plan:{project_id}', key, request_hash, 201, {'data': response})
    db.session.commit()
    return ok(response, 201)


@scene_bp.post('/projects/<project_id>/template-documents')
def upload_template_document(project_id):
    owner = owner_id()
    owned_project(project_id, owner)
    file = request.files.get('file')
    if file is None:
        raise SceneError('INPUT_INVALID', 'Template file required')
    filename = (file.filename or '').lower()
    source_type = 'pptx' if filename.endswith('.pptx') else 'image' if filename.endswith(('.png', '.jpg', '.jpeg', '.webp')) else None
    if source_type is None:
        raise SceneError('TEMPLATE_TYPE_UNSUPPORTED', 'Use PPTX, PNG, JPEG or WebP')
    payload = file.stream.read(current_app.config['MAX_UPLOAD_BYTES'] + 1)
    if len(payload) > current_app.config['MAX_UPLOAD_BYTES']:
        raise SceneError('UPLOAD_TOO_LARGE', 'Template exceeds upload limit', 413)
    operation = f'upload_template:{project_id}'
    request_data = {'filename': filename, 'source_type': source_type,
                    'sha256': hashlib.sha256(payload).hexdigest(), 'byte_size': len(payload)}
    _, key, request_hash, existing = unique_request(operation, request_data)
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    project = task_project(project_id, owner)
    if source_type == 'pptx' and (os.name != 'posix' or
        not current_app.config.get('SCENE_RENDER_UNIQUE_UID') or
        not scene_readiness()['checks']['renderer']):
        raise SceneError('TEMPLATE_RENDERER_UNAVAILABLE',
                         'PPTX upload requires a ready per-job isolated renderer', 503)
    if source_type == 'pptx':
        count, warnings = inspect_pptx(payload)
        mime, suffix = 'application/vnd.openxmlformats-officedocument.presentationml.presentation', 'pptx'
    else:
        mime, suffix = inspect_reference_image(payload)
        count, warnings = 1, []
    admit_tasks(owner, 'import_template')
    asset = put_bytes(owner, project_id, 'template_source', payload, mime, suffix,
                      provenance={'source_kind': source_type})
    document = TemplateDocument(id=new_id(), owner_id=owner, project_id=project_id,
        source_asset_id=asset.id, source_type=source_type, source_page_count=count,
        warnings_json=warnings, status='uploaded')
    task = SceneTaskItem(id=new_id(), owner_id=owner, project_id=project_id,
        operation='import_template', resource_id=document.id,
        input_json={'source_asset_id': asset.id, 'sha256': asset.sha256},
        input_hash=digest({'source_asset_id': asset.id, 'sha256': asset.sha256}))
    document.import_task_id = task.id
    db.session.add_all([document, task])
    response = {'document_id': document.id, 'task_id': task.id, 'status': 'uploaded'}
    remember(owner, operation, key, request_hash, 202, {'data': response})
    db.session.commit()
    return ok(response, 202)


@scene_bp.get('/projects/<project_id>/template-documents/<document_id>')
def get_template_document(project_id, document_id):
    owner = owner_id()
    owned_project(project_id, owner)
    document = TemplateDocument.query.filter_by(id=document_id, project_id=project_id, owner_id=owner).first()
    if document is None:
        raise SceneError('NOT_FOUND', 'Template document not found', 404)
    return ok({'document_id': document.id, 'status': document.status,
               'source_page_count': document.source_page_count,
               'warnings': document.warnings_json, 'error_code': document.error_code,
               'task_id': document.import_task_id})


@scene_bp.post('/projects/<project_id>/template-documents/<document_id>/analyze')
def analyze_document(project_id, document_id):
    owner = owner_id()
    owned_project(project_id, owner)
    data = body()
    operation = f'analyze_template:{document_id}'
    _, key, request_hash, existing = unique_request(operation, data)
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    project = task_project(project_id, owner)
    # Match finalizers and manual edits: template rows BEFORE their document.
    # The project lock also serializes deletion, new analyses and analysis retries.
    assets = ProjectTemplateAsset.query.filter_by(template_document_id=document_id,
        project_id=project.id, deleted_at=None).order_by(ProjectTemplateAsset.id).populate_existing().with_for_update().all()
    document = TemplateDocument.query.filter_by(id=document_id, project_id=project_id,
        owner_id=owner).populate_existing().with_for_update().first()
    if document is None:
        raise SceneError('NOT_FOUND', 'Template document not found', 404)
    indexes = data.get('selected_page_indexes')
    if (not isinstance(indexes, list) or not indexes or
            len(indexes) > current_app.config['MAX_TEMPLATE_PAGES'] or
            any(type(index) is not int or index < 1 for index in indexes) or
            len(indexes) != len(set(indexes))):
        raise SceneError('PAGE_SELECTION_INVALID', 'Select distinct 1-based page indexes')
    credential = ModelCredential.query.filter_by(id=data.get('credential_id'), owner_id=owner).first()
    if not credential or credential.status == 'revoked':
        raise SceneError('CREDENTIAL_NOT_FOUND', 'Select your model API Key')
    model = (project.model_config_json or {}).get('text_model')
    if not model:
        raise SceneError('MODEL_NOT_CONFIGURED', 'Set a text model first')
    by_index = {item.source_page_index: item for item in assets}
    if any(not isinstance(index, int) or index not in by_index for index in indexes):
        raise SceneError('PAGE_SELECTION_INVALID', 'Page index out of range')
    # The document has one selected-page set and one aggregate status. Starting
    # another batch while a previous one is queued/running would overwrite that
    # set and can charge the same owner twice for the same reference page.
    if SceneTaskItem.query.filter(SceneTaskItem.owner_id == owner,
        SceneTaskItem.project_id == project.id,
        SceneTaskItem.operation == 'analyze_template',
        SceneTaskItem.resource_id.in_([asset.id for asset in assets]),
        SceneTaskItem.state.in_(['queued', 'running'])).first():
        raise SceneError('ANALYSIS_IN_PROGRESS', 'Finish the current template analysis first', 409)
    if document.status not in ('preview_ready', 'ready'):
        raise SceneError('TEMPLATE_NOT_READY', 'Static previews are not ready')
    admit_tasks(owner, 'analyze_template', len(indexes))
    group = new_id()
    tasks = []
    for index in indexes:
        asset = by_index[index]
        preview = Asset.query.filter_by(id=asset.preview_asset_id, owner_id=owner,
            project_id=project.id, kind='template_preview', state='ready').first()
        if preview is None:
            raise SceneError('ASSET_UNAVAILABLE', 'Selected reference preview is unavailable', 503)
        try:
            checked_bytes(preview)
        except (OSError, ValueError) as exc:
            raise SceneError('ASSET_UNAVAILABLE', 'Selected reference preview is missing or corrupt', 503) from exc
        frozen = {'template_asset_id': asset.id, 'base_analysis_revision': asset.analysis_revision,
                  'preview_asset_id': preview.id, 'preview_sha256': preview.sha256,
                  'text_model': model}
        task = SceneTaskItem(id=new_id(), owner_id=owner, project_id=project.id,
            credential_id=credential.id, group_id=group, logical_key=f'analyze:{asset.id}',
            operation='analyze_template', resource_id=asset.id,
            input_json=frozen, input_hash=digest(frozen))
        db.session.add(task)
        asset.analysis_status = 'running'
        tasks.append({'source_page_index': index, 'task_id': task.id})
    document.selected_page_indexes_json = indexes
    document.status = 'analyzing'
    response = {'task_group_id': group, 'tasks': tasks}
    remember(owner, operation, key, request_hash, 202, {'data': response})
    db.session.commit()
    return ok(response, 202)


@scene_bp.get('/projects/<project_id>/template-assets')
def list_template_assets(project_id):
    owner = owner_id()
    owned_project(project_id, owner)
    query = ProjectTemplateAsset.query.filter_by(project_id=project_id, deleted_at=None).filter(
        ProjectTemplateAsset.template_document_id.isnot(None))
    if request.args.get('document_id'):
        query = query.filter_by(template_document_id=request.args['document_id'])
    rows, next_cursor = paged_rows(query, ProjectTemplateAsset,
        ProjectTemplateAsset.sort_order, ascending=True)
    result = []
    for item in rows:
        preview = Asset.query.filter_by(id=item.preview_asset_id, owner_id=owner, project_id=project_id).first()
        thumb = Asset.query.filter_by(id=item.thumbnail_asset_id, owner_id=owner, project_id=project_id).first()
        result.append({'template_asset_id': item.id, 'template_document_id': item.template_document_id,
                       'source_page_index': item.source_page_index,
                       'preview_url': signed_url(preview) if preview else None,
                       'thumbnail_url': signed_url(thumb) if thumb else None,
                       'analysis_status': item.analysis_status,
                       'analysis_revision': item.analysis_revision,
                       'analysis': item.get_analysis()})
    return ok({'items': result, 'next_cursor': next_cursor})


@scene_bp.delete('/projects/<project_id>/template-assets/<template_asset_id>')
def delete_template_reference(project_id, template_asset_id):
    owner = owner_id()
    owned_project(project_id, owner)
    data = body()
    operation = f'delete_template_reference:{template_asset_id}'
    _, key, request_hash, existing = unique_request(operation, data)
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    project = Project.query.filter_by(id=project_id, owner_id=owner,
        editor_mode='scene_v1', deleted_at=None).populate_existing().with_for_update().first()
    if project is None:
        raise SceneError('NOT_FOUND', 'Project not found', 404)
    if type(data.get('base_project_version')) is not int or data['base_project_version'] != project.row_version:
        raise SceneError('PROJECT_VERSION_CONFLICT', 'Project changed', 409)
    pages = Page.query.filter_by(project_id=project.id, template_asset_id=template_asset_id,
        deleted_at=None).order_by(Page.id).populate_existing().with_for_update().all()
    actual_pages = [{'page_id': page.id, 'page_version': page.row_version,
                     'revision_id': page.head_revision_id} for page in pages]
    expected_pages = data.get('expected_pages')
    if (not isinstance(expected_pages, list) or any(not isinstance(page, dict) or
            not isinstance(page.get('page_id'), str) or type(page.get('page_version')) is not int
            for page in expected_pages)):
        raise SceneError('INPUT_INVALID', 'Expected bound page versions are required')
    if sorted(expected_pages, key=lambda page: page['page_id']) != actual_pages:
        raise SceneError('PAGE_VERSION_CONFLICT', 'Bound pages changed; review references before removal', 409)
    template = ProjectTemplateAsset.query.filter_by(id=template_asset_id, project_id=project_id,
        deleted_at=None).populate_existing().with_for_update().first()
    if template is None or not template.template_document_id:
        raise SceneError('NOT_FOUND', 'Template reference not found', 404)
    if (type(data.get('base_analysis_revision')) is not int or
            data['base_analysis_revision'] != template.analysis_revision):
        raise SceneError('ANALYSIS_VERSION_CONFLICT', 'Template analysis changed', 409)
    document = TemplateDocument.query.filter_by(id=template.template_document_id,
        project_id=project_id, owner_id=owner).populate_existing().with_for_update().one()
    document_references = db.session.query(ProjectTemplateAsset.id).filter_by(template_document_id=document.id)
    if SceneTaskItem.query.filter(SceneTaskItem.owner_id == owner,
        SceneTaskItem.project_id == project_id, SceneTaskItem.operation == 'analyze_template',
        SceneTaskItem.resource_id.in_(document_references),
        SceneTaskItem.state.in_(['queued', 'running'])).first():
        raise SceneError('ANALYSIS_IN_PROGRESS', 'Finish or cancel the current template analysis before removal', 409)
    template.deleted_at = now()
    for page in pages:
        page.template_asset_id = None
        # Keep user-authored style instructions and the immutable Scene head.
        page.row_version += 1
    project.row_version += 1
    selected = document.selected_page_indexes_json or []
    if template.source_page_index in selected:
        selected = [index for index in selected if index != template.source_page_index]
        document.selected_page_indexes_json = selected
        completed = ProjectTemplateAsset.query.filter(
            ProjectTemplateAsset.template_document_id == document.id,
            ProjectTemplateAsset.deleted_at.is_(None),
            ProjectTemplateAsset.source_page_index.in_(selected),
            ProjectTemplateAsset.analysis_status == 'completed').count()
        document.status = 'preview_ready' if not selected else 'ready' if completed == len(selected) else 'partial_failed'
        if document.status in ('preview_ready', 'ready'):
            document.error_code = None
    response = project_data(project, include_pages=True)
    remember(owner, operation, key, request_hash, 200, {'data': response})
    db.session.commit()
    return ok(response)


@scene_bp.patch('/projects/<project_id>/template-assets/<template_asset_id>')
def edit_template_profile(project_id, template_asset_id):
    owner = owner_id()
    owned_project(project_id, owner)
    data = body()
    operation = f'edit_template_profile:{template_asset_id}'
    _, key, request_hash, existing = unique_request(operation, data)
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    template = ProjectTemplateAsset.query.filter_by(id=template_asset_id, project_id=project_id,
        deleted_at=None).populate_existing().with_for_update().first()
    if template is None or not template.template_document_id:
        raise SceneError('NOT_FOUND', 'Template reference not found', 404)
    if data.get('base_analysis_revision') != template.analysis_revision:
        raise SceneError('ANALYSIS_VERSION_CONFLICT', 'Template analysis changed', 409)
    profile = data.get('analysis')
    profile = normalize_style_profile(profile)
    previous = template.get_analysis() or {}
    profile.update({key: previous[key] for key in ('source_asset_id', 'source_sha256',
                    'analysis_model', 'analysis_version') if key in previous})
    template.set_analysis(profile)
    template.analysis_hash = digest(profile)
    template.analysis_revision += 1
    template.analysis_schema_version = 1
    template.analysis_status = 'completed'
    template.user_edited_analysis = True
    document = TemplateDocument.query.filter_by(id=template.template_document_id,
        project_id=project_id, owner_id=owner).with_for_update().first()
    selected = document.selected_page_indexes_json or []
    if selected and ProjectTemplateAsset.query.filter(
        ProjectTemplateAsset.template_document_id == document.id,
        ProjectTemplateAsset.source_page_index.in_(selected),
        ProjectTemplateAsset.analysis_status == 'completed').count() >= len(selected):
        document.status = 'ready'
        document.error_code = None
    response = {'template_asset_id': template.id, 'analysis_revision': template.analysis_revision,
                'analysis_hash': template.analysis_hash}
    remember(owner, operation, key, request_hash, 200, {'data': response})
    db.session.commit()
    return ok(response)


@scene_bp.patch('/projects/<project_id>/pages/<page_id>/template')
def bind_page_template(project_id, page_id):
    owner = owner_id()
    owned_page(owned_project(project_id, owner), page_id)
    data = body()
    operation = f'bind_page_template:{page_id}'
    _, key, request_hash, existing = unique_request(operation, data)
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    project = Project.query.filter_by(id=project_id, owner_id=owner,
        editor_mode='scene_v1', deleted_at=None).populate_existing().with_for_update().first()
    if project is None:
        raise SceneError('NOT_FOUND', 'Project not found', 404)
    page = Page.query.filter_by(id=page_id, project_id=project.id,
        deleted_at=None).populate_existing().with_for_update().first()
    if page is None:
        raise SceneError('NOT_FOUND', 'Page not found', 404)
    if data.get('base_page_version') != page.row_version:
        raise SceneError('PAGE_VERSION_CONFLICT', 'Page changed', 409)
    asset_id = data.get('template_asset_id')
    style_text = data.get('style_text', '')
    if asset_id is not None and not isinstance(asset_id, str):
        raise SceneError('TEMPLATE_NOT_FOUND', 'Template reference ID invalid')
    if not isinstance(style_text, str) or len(style_text) > 3000 or any(
        ord(char) < 32 and char not in '\n\t' for char in style_text):
        raise SceneError('STYLE_TEXT_INVALID', 'Style instructions must be bounded plain text')
    if asset_id and not ProjectTemplateAsset.query.filter_by(id=asset_id, project_id=project.id, deleted_at=None).first():
        raise SceneError('NOT_FOUND', 'Template reference not found', 404)
    page.template_asset_id = asset_id
    page.template_style_text = style_text
    page.row_version += 1
    project.row_version += 1
    response = page_data(page)
    remember(owner, operation, key, request_hash, 200, {'data': response})
    db.session.commit()
    return ok(response)


@scene_bp.post('/projects/<project_id>/generation-tasks')
def generate_scenes(project_id):
    owner = owner_id()
    owned_project(project_id, owner)
    data = body()
    _, key, request_hash, existing = unique_request(f'generate:{project_id}', data)
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    project = task_project(project_id, owner)
    plan = GenerationPlan.query.filter_by(id=data.get('plan_id'), project_id=project.id, owner_id=owner).first()
    if not plan or project.active_plan_id != plan.id or project.row_version != plan.base_project_version:
        raise SceneError('PLAN_STALE', 'Confirm the current outline before generation', 409)
    credential = ModelCredential.query.filter_by(id=data.get('credential_id'), owner_id=owner).first()
    if not credential or credential.status == 'revoked':
        raise SceneError('CREDENTIAL_NOT_FOUND', 'Select your model API Key', 422)
    targets = data.get('targets')
    if (not isinstance(targets, list) or
            not 1 <= len(targets) <= current_app.config['MAX_GENERATED_PAGES'] or
            any(not isinstance(target, dict) or
                not isinstance(target.get('page_id'), str) or
                type(target.get('base_page_version')) is not int or
                not (target.get('base_revision_id') is None or
                     isinstance(target.get('base_revision_id'), str))
                for target in targets)):
        raise SceneError('TARGETS_INVALID', 'Select one or more pages')
    plan_pages = {p['page_id']: p for p in plan.manifest_json['pages']}
    target_ids = {target['page_id'] for target in targets}
    if len(target_ids) != len(targets):
        raise SceneError('TARGETS_INVALID', 'Duplicate page target')
    # Planning writes take the project lock first, then page locks in stable ID
    # order. Hold both while enqueueing so an already-stale plan cannot launch
    # a paid task after a concurrent outline/template/Scene update.
    pages = Page.query.filter(Page.project_id == project.id, Page.id.in_(target_ids),
        Page.deleted_at.is_(None)).order_by(Page.id).populate_existing().with_for_update().all()
    by_id = {page.id: page for page in pages}
    if len(by_id) != len(target_ids):
        raise SceneError('NOT_FOUND', 'Page not found', 404)
    for target in targets:
        page = by_id[target['page_id']]
        frozen = plan_pages.get(page.id)
        if not frozen or page.row_version != target.get('base_page_version') or page.head_revision_id != target.get('base_revision_id') or frozen['page_version'] != page.row_version:
            raise SceneError('PAGE_VERSION_CONFLICT', 'Page changed since plan confirmation', 409, {'page_id': page.id})
    admit_tasks(owner, 'generate_scene', len(targets))
    group = new_id()
    tasks = []
    for target in targets:
        page = by_id[target['page_id']]
        input_data = {'plan_id': plan.id, 'base_revision_id': page.head_revision_id,
                      'base_page_version': page.row_version, 'model_config': plan.manifest_json['model_config']}
        task = SceneTaskItem(id=new_id(), owner_id=owner, project_id=project.id,
            page_id=page.id, credential_id=credential.id, group_id=group,
            logical_key=f'generate:{page.id}', operation='generate_scene', resource_id=page.id,
            input_json=input_data, input_hash=digest(input_data), max_attempts=4)
        db.session.add(task)
        tasks.append({'page_id': page.id, 'task_id': task.id})
    response = {'task_group_id': group, 'tasks': tasks}
    remember(owner, f'generate:{project_id}', key, request_hash, 202, {'data': response})
    db.session.commit()
    return ok(response, 202)


@scene_bp.post('/projects/<project_id>/pages/<page_id>/ai-edits')
def ai_edit(project_id, page_id):
    owner = owner_id()
    owned_project(project_id, owner)
    data = body()
    _, key, request_hash, existing = unique_request(f'ai_edit:{page_id}', data)
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    project = task_project(project_id, owner)
    page = Page.query.filter_by(id=page_id, project_id=project.id,
        deleted_at=None).populate_existing().with_for_update().first()
    if page is None:
        raise SceneError('NOT_FOUND', 'Page not found', 404)
    if page.head_revision_id != data.get('base_revision_id') or page.row_version != data.get('base_page_version'):
        raise SceneError('SCENE_VERSION_CONFLICT', 'Page changed', 409)
    instruction = data.get('instruction')
    if not isinstance(instruction, str) or not 1 <= len(instruction) <= 4000:
        raise SceneError('INSTRUCTION_INVALID', 'Instruction required')
    ids = data.get('element_ids') or []
    if (not isinstance(ids, list) or len(ids) > 200 or
            any(not isinstance(element_id, str) for element_id in ids) or
            len(ids) != len(set(ids))):
        raise SceneError('SCOPE_INVALID', 'Invalid selected elements')
    scene = current_scene(page).scene_json
    valid_ids = {e['id'] for e in scene['elements'] if not e['locked']}
    if not set(ids) <= valid_ids:
        raise SceneError('EDIT_SCOPE_CONFLICT', 'Selection contains locked or missing objects')
    scope = {'kind': 'selected_elements' if ids else 'page', 'element_ids': ids}
    credential = ModelCredential.query.filter_by(id=data.get('credential_id'), owner_id=owner).first()
    if not credential or credential.status == 'revoked':
        raise SceneError('CREDENTIAL_NOT_FOUND', 'Select your model API Key')
    admit_tasks(owner, 'ai_edit')
    frozen = {'base_revision_id': page.head_revision_id, 'base_page_version': page.row_version,
              'instruction': instruction, 'scope': scope, 'model_config': project.model_config_json or {}}
    group = new_id()
    task = SceneTaskItem(id=new_id(), owner_id=owner, project_id=project.id,
        page_id=page.id, credential_id=credential.id, group_id=group, logical_key=f'edit:{page.id}',
        operation='ai_edit', resource_id=page.id, input_json=frozen, input_hash=digest(frozen),
        max_attempts=4)
    db.session.add(task)
    response = {'task_id': task.id, 'task_group_id': group}
    remember(owner, f'ai_edit:{page_id}', key, request_hash, 202, {'data': response})
    db.session.commit()
    return ok(response, 202)


@scene_bp.get('/projects/<project_id>/candidates')
def list_candidates(project_id):
    owner = owner_id()
    owned_project(project_id, owner)
    query = SceneCandidate.query.filter_by(project_id=project_id, owner_id=owner)
    if request.args.get('page_id'):
        query = query.filter_by(page_id=request.args['page_id'])
    state = request.args.get('state')
    if state:
        if state not in ('pending', 'accepted', 'rejected', 'stale'):
            raise SceneError('INPUT_INVALID', 'Unsupported candidate state')
        query = query.filter_by(state=state)
    rows, next_cursor = paged_rows(query, SceneCandidate, SceneCandidate.created_at)
    return ok({'items': [{'candidate_id': c.id, 'page_id': c.page_id, 'state': c.state,
                'revision_id': c.proposed_revision_id, 'base_page_version': c.base_page_version,
                'change_summary': c.change_summary_json} for c in rows],
               'next_cursor': next_cursor})


@scene_bp.post('/projects/<project_id>/candidates/accept')
def accept(project_id):
    owner = owner_id()
    project = owned_project(project_id, owner)
    data = body()
    operation = f'accept_candidate:{project_id}'
    _, key, request_hash, existing = unique_request(operation, data)
    if existing:
        return ok([refreshed_scene_data(item, owner, project_id)
                   for item in existing.response_json['data']], existing.response_status)
    ids = data.get('candidate_ids')
    try:
        result = accept_candidates(project, owner, ids)
    except SceneError as exc:
        if exc.code == 'CANDIDATE_STALE' and isinstance(ids, list):
            db.session.rollback()  # no candidate in the batch may have been accepted
            for candidate in SceneCandidate.query.filter(
                SceneCandidate.id.in_(ids), SceneCandidate.owner_id == owner,
                SceneCandidate.project_id == project_id,
                SceneCandidate.state == 'pending').all():
                page = db.session.get(Page, candidate.page_id)
                if page is None or page.head_revision_id != candidate.base_revision_id or page.row_version != candidate.base_page_version:
                    candidate.state = 'stale'
            db.session.commit()
        raise
    remember(owner, operation, key, request_hash, 200, {'data': result})
    db.session.commit()
    return ok(result)


@scene_bp.post('/projects/<project_id>/candidates/<candidate_id>/reject')
def reject(project_id, candidate_id):
    owner = owner_id()
    owned_project(project_id, owner)
    data = body()
    operation = f'reject_candidate:{candidate_id}'
    _, key, request_hash, existing = unique_request(operation, data)
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    candidate = SceneCandidate.query.filter_by(id=candidate_id, project_id=project_id, owner_id=owner).first()
    if candidate is None:
        raise SceneError('NOT_FOUND', 'Candidate not found', 404)
    if candidate.state == 'pending':
        candidate.state = 'rejected'
    response = {'candidate_id': candidate.id, 'state': candidate.state}
    remember(owner, operation, key, request_hash, 200, {'data': response})
    db.session.commit()
    return ok(response)


@scene_bp.post('/projects/<project_id>/snapshots')
def create_snapshot(project_id):
    owner = owner_id()
    project = owned_project(project_id, owner)
    data = body()
    _, key, request_hash, existing = unique_request(f'snapshot:{project_id}', data)
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    snapshot = make_snapshot(project, owner, data)
    response = {'snapshot_id': snapshot.id, 'manifest_hash': snapshot.manifest_hash}
    remember(owner, f'snapshot:{project_id}', key, request_hash, 201, {'data': response})
    db.session.commit()
    return ok(response, 201)


@scene_bp.get('/projects/<project_id>/snapshots/preflight')
def preflight_snapshot(project_id):
    owner = owner_id()
    project = owned_project(project_id, owner)
    raw = request.args.get('page_ids', '')
    page_ids = raw.split(',') if raw else []
    maximum = current_app.config['MAX_GENERATED_PAGES']
    if not 1 <= len(page_ids) <= maximum or len(page_ids) != len(set(page_ids)) or any(not value for value in page_ids):
        raise SceneError('SNAPSHOT_INVALID', f'Select 1..{maximum} distinct pages')
    pages = Page.query.filter(Page.id.in_(page_ids), Page.project_id == project_id,
                              Page.deleted_at.is_(None)).all()
    by_id = {page.id: page for page in pages}
    if len(by_id) != len(page_ids):
        raise SceneError('NOT_FOUND', 'Selected page not found', 404)
    payload = {'project_version': project.row_version, 'pending_candidate_policy': 'export_accepted',
               'pages': [{'page_id': page_id, 'page_version': by_id[page_id].row_version,
                          'revision_id': by_id[page_id].head_revision_id or ''} for page_id in page_ids]}
    try:
        result = make_snapshot(project, owner, payload, persist=False, collect_blockers=True)
    except SceneError as exc:
        result = {'ready': False, 'blockers': [{'code': exc.code, 'message': exc.message,
                   'page_id': exc.details.get('page_id'), 'details': exc.details}],
                  'page_count': len(page_ids)}
    response, status = ok(result)
    response.headers['Cache-Control'] = 'no-store'
    return response, status


@scene_bp.post('/projects/<project_id>/exports')
def create_export(project_id):
    owner = owner_id()
    owned_project(project_id, owner)
    data = body()
    _, key, request_hash, existing = unique_request(f'export:{project_id}', data)
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    project = task_project(project_id, owner)
    snapshot = DeckSnapshot.query.filter_by(id=data.get('snapshot_id'), project_id=project_id, owner_id=owner).first()
    if snapshot is None:
        raise SceneError('NOT_FOUND', 'Snapshot not found', 404)
    fmt = data.get('format')
    if fmt not in ('pptx', 'pdf'):
        raise SceneError('FORMAT_INVALID', 'Use pptx or pdf')
    options = data.get('options') or {}
    admit_tasks(owner, 'export')
    export = SceneExport(id=new_id(), owner_id=owner, project_id=project_id,
                         snapshot_id=snapshot.id, format=fmt, options_json=options,
                         options_hash=digest(options), status='queued')
    task = SceneTaskItem(id=new_id(), owner_id=owner, project_id=project_id,
                         operation='export', resource_id=export.id,
                         input_json={'snapshot_id': snapshot.id, 'format': fmt},
                         input_hash=digest({'snapshot_id': snapshot.id, 'format': fmt}))
    db.session.add_all([export, task])
    response = {'export_id': export.id, 'task_id': task.id, 'status': 'queued'}
    remember(owner, f'export:{project_id}', key, request_hash, 202, {'data': response})
    db.session.commit()
    return ok(response, 202)


@scene_bp.get('/projects/<project_id>/exports')
def list_scene_exports(project_id):
    owner = owner_id()
    owned_project(project_id, owner)
    try:
        limit = int(request.args.get('limit', '20'))
    except ValueError as exc:
        raise SceneError('PAGE_LIMIT_INVALID', 'Limit must be an integer') from exc
    if not 1 <= limit <= 100:
        raise SceneError('PAGE_LIMIT_INVALID', 'Limit must be 1..100')
    query = SceneExport.query.filter_by(project_id=project_id, owner_id=owner)
    cursor = request.args.get('cursor')
    if cursor:
        previous = query.filter_by(id=cursor).first()
        if previous is None:
            raise SceneError('CURSOR_INVALID', 'Export cursor unavailable', 422)
        query = query.filter(or_(SceneExport.created_at < previous.created_at,
            and_(SceneExport.created_at == previous.created_at, SceneExport.id < previous.id)))
    rows = query.order_by(SceneExport.created_at.desc(), SceneExport.id.desc()).limit(limit + 1).all()
    next_cursor = rows[limit - 1].id if len(rows) > limit else None
    return ok({'items': [{'export_id': row.id, 'format': row.format,
                           'status': row.status, 'created_at': row.created_at.isoformat()}
                          for row in rows[:limit]], 'next_cursor': next_cursor})


@scene_bp.get('/projects/<project_id>/exports/<export_id>')
def get_export(project_id, export_id):
    owner = owner_id()
    owned_project(project_id, owner)
    export = SceneExport.query.filter_by(id=export_id, project_id=project_id, owner_id=owner).first()
    if export is None:
        raise SceneError('NOT_FOUND', 'Export not found', 404)
    asset = db.session.get(Asset, export.file_asset_id) if export.file_asset_id else None
    report = db.session.get(Asset, export.report_asset_id) if export.report_asset_id else None
    quality = None
    if asset and export.status in ('succeeded', 'needs_review'):
        try:
            checked_bytes(asset)
            if report:
                quality = json.loads(checked_bytes(report))
        except (OSError, ValueError):
            return ok({'export_id': export.id, 'status': 'failed', 'format': export.format,
                       'error_code': 'ASSET_UNAVAILABLE', 'download_url': None,
                       'report_url': None})
    visual_urls = []
    for page in (quality or {}).get('visual', {}).get('pages', []):
        proof = Asset.query.filter_by(id=page.get('contact_sheet_asset_id'), owner_id=owner,
                                      project_id=project_id, kind='visual_comparison', state='ready').first()
        if proof:
            visual_urls.append({'page_id': page['page_id'], 'url': signed_url(proof)})
    return ok({'export_id': export.id, 'status': export.status, 'format': export.format,
               'error_code': export.error_code,
               'download_url': signed_url(asset) if asset and export.status == 'succeeded' else None,
               'review_file_url': signed_url(asset) if asset and export.status == 'needs_review' else None,
               'report_url': signed_url(report) if report else None,
               'report_sha256': report.sha256 if report else None,
               'visual_comparison': (quality or {}).get('visual_comparison'),
               'waivable_warning_codes': (quality or {}).get('waivable_warning_codes', []),
               'visual_evidence': visual_urls})


@scene_bp.post('/projects/<project_id>/exports/<export_id>/review')
def accept_export_review(project_id, export_id):
    owner = owner_id()
    owned_project(project_id, owner)
    data = body()
    operation = f'accept_export_review:{export_id}'
    _, key, request_hash, existing = unique_request(operation, data)
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    export = SceneExport.query.filter_by(id=export_id, project_id=project_id,
                                         owner_id=owner).with_for_update().first()
    if export is None:
        raise SceneError('NOT_FOUND', 'Export not found', 404)
    if export.status == 'succeeded' and export.review_report_sha256 == data.get('report_sha256'):
        response = {'export_id': export.id, 'status': export.status,
                    'review_report_sha256': export.review_report_sha256}
        remember(owner, operation, key, request_hash, 200, {'data': response})
        db.session.commit()
        return ok(response)
    if export.status != 'needs_review':
        raise SceneError('REVIEW_STATE_INVALID', 'Export is not awaiting visual review', 409)
    report = Asset.query.filter_by(id=export.report_asset_id, owner_id=owner,
                                   project_id=project_id, kind='export_report', state='ready').first()
    if report is None or data.get('report_sha256') != report.sha256:
        raise SceneError('REVIEW_REPORT_MISMATCH', 'Report hash changed or missing', 409)
    file_asset = Asset.query.filter_by(id=export.file_asset_id, owner_id=owner,
                                       project_id=project_id, kind='export', state='ready').first()
    if file_asset is None:
        raise SceneError('ASSET_UNAVAILABLE', 'Export file unavailable', 503)
    try:
        quality = json.loads(checked_bytes(report))
        checked_bytes(file_asset)
    except (OSError, ValueError) as exc:
        raise SceneError('ASSET_UNAVAILABLE', 'Export or report is missing or corrupt', 503) from exc
    snapshot = db.session.get(DeckSnapshot, export.snapshot_id)
    snapshot_valid = bool(snapshot and quality.get('snapshot_id') == export.snapshot_id and
                          quality.get('snapshot_hash') == snapshot.manifest_hash and
                          digest(snapshot.manifest_json) == snapshot.manifest_hash)
    pages = quality.get('visual', {}).get('pages', [])
    evidence_ready = bool(snapshot and len(pages) == len(snapshot.manifest_json['pages']))
    for page, expected in zip(pages, snapshot.manifest_json['pages'] if snapshot else []):
        proof = Asset.query.filter_by(id=page.get('contact_sheet_asset_id'), owner_id=owner,
            project_id=project_id, kind='visual_comparison', state='ready').first()
        if page.get('page_id') != expected['page_id'] or proof is None:
            evidence_ready = False
            break
        try:
            checked_bytes(proof)
        except (OSError, ValueError):
            evidence_ready = False
            break
    rendered_check = quality.get('visual', {}).get('rendered_text_layout')
    rendered_text_valid = export.format != 'pptx'
    if export.format == 'pptx' and snapshot and isinstance(rendered_check, dict):
        checked_pages = rendered_check.get('pages')
        rendered_text_valid = bool(
            rendered_check.get('version') == 1 and rendered_check.get('status') == 'passed' and
            isinstance(rendered_check.get('rendered_pdf_sha256'), str) and
            re.fullmatch(r'[a-f0-9]{64}', rendered_check['rendered_pdf_sha256']) and
            isinstance(checked_pages, list) and all(isinstance(p, dict) for p in checked_pages) and
            [p.get('page_id') for p in checked_pages] == [p['page_id'] for p in snapshot.manifest_json['pages']])
    if not rendered_text_valid:
        raise SceneError('PPTX_RENDER_UNVERIFIED',
                         'This PPTX lacks current target-render verification; generate a new export', 422)
    if (quality.get('structural_and_text') != 'passed' or
        not snapshot_valid or
        quality.get('visual_comparison') != 'needs_review' or
        quality.get('waivable_warning_codes') != ['VISUAL_DIFF_UNCALIBRATED'] or
        data.get('acknowledged_warning_codes') != ['VISUAL_DIFF_UNCALIBRATED'] or
        not evidence_ready):
        raise SceneError('REVIEW_EVIDENCE_REQUIRED', 'Inspect comparison and acknowledge visual differences', 422)
    from models.scene_v1 import now
    export.review_report_sha256 = report.sha256
    export.reviewed_by = owner
    export.reviewed_at = now()
    export.status = 'succeeded'
    response = {'export_id': export.id, 'status': export.status,
                'review_report_sha256': export.review_report_sha256}
    remember(owner, operation, key, request_hash, 200, {'data': response})
    db.session.commit()
    return ok(response)


@scene_bp.get('/tasks/<task_id>')
def get_task(task_id):
    owner = owner_id()
    task = SceneTaskItem.query.filter_by(id=task_id, owner_id=owner).first()
    if task is None:
        raise SceneError('NOT_FOUND', 'Task not found', 404)
    telemetry.bind(**telemetry.task_context(task, active_request=True))
    return ok({'task_id': task.id, 'request_id': task.request_id, 'retry_request_id': task.retry_request_id,
               'state': task.state, 'operation': task.operation,
               'result': task.result_json, 'error_code': task.error_code,
               'attempt_count': task.attempt_count, 'max_attempts': task.max_attempts,
               'next_run_at': task.next_run_at.isoformat() if task.next_run_at else None,
               'possible_charge': possible_charge_on_retry(task)})


@scene_bp.get('/projects/<project_id>/tasks')
def list_project_tasks(project_id):
    owner = owner_id()
    owned_project(project_id, owner)
    tasks, next_cursor = paged_rows(SceneTaskItem.query.filter_by(project_id=project_id, owner_id=owner),
        SceneTaskItem, SceneTaskItem.created_at)
    return ok({'items': [{'task_id': task.id, 'page_id': task.page_id, 'operation': task.operation,
                'state': task.state, 'error_code': task.error_code,
                'possible_charge': possible_charge_on_retry(task)}
               for task in tasks], 'next_cursor': next_cursor})


@scene_bp.get('/task-groups/<group_id>')
def get_task_group(group_id):
    owner = owner_id()
    tasks = SceneTaskItem.query.filter_by(group_id=group_id, owner_id=owner).order_by(SceneTaskItem.created_at).all()
    if not tasks:
        raise SceneError('NOT_FOUND', 'Task group not found', 404)
    pages = [task for task in tasks if task.operation in ('generate_scene', 'ai_edit')]
    relevant = pages or tasks
    succeeded = sum(task.state == 'succeeded' for task in relevant)
    attention = any(task.state == 'outcome_unknown' for task in tasks) or any(
        task.state == 'waiting_assets' and task.error_code for task in pages)
    if attention:
        group_state = 'needs_attention'
    elif succeeded == len(relevant):
        group_state = 'succeeded'
    elif all(task.state == 'cancelled' for task in relevant) and all(
            task.state in ('cancelled', 'succeeded') for task in tasks):
        group_state = 'cancelled'
    elif any(task.state in ('failed', 'cancelled') for task in relevant):
        group_state = 'partial_failed' if succeeded else 'failed'
    elif any(task.state in ('running', 'waiting_assets') for task in relevant):
        group_state = 'running'
    else:
        group_state = 'queued'
    return ok({'task_group_id': group_id, 'state': group_state,
               'completed_pages': succeeded, 'total_pages': len(relevant), 'items': [
        {'task_id': task.id, 'page_id': task.page_id, 'operation': task.operation,
         'logical_key': task.logical_key, 'state': task.state,
         'result': task.result_json, 'error_code': task.error_code,
         'possible_charge': possible_charge_on_retry(task)}
        for task in tasks]})


@scene_bp.post('/tasks/<task_id>/cancel')
def cancel_task(task_id):
    owner = owner_id()
    data = body()
    operation = f'cancel_task:{task_id}'
    _, key, request_hash, existing = unique_request(operation, data)
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    task = SceneTaskItem.query.filter_by(id=task_id, owner_id=owner).first()
    if task is None:
        raise SceneError('NOT_FOUND', 'Task not found', 404)
    if task.state in ('queued', 'running', 'waiting_assets'):
        from models.scene_v1 import now
        targets = [task]
        if task.operation in ('generate_scene', 'ai_edit'):
            targets += SceneTaskItem.query.filter_by(group_id=task.group_id, page_id=task.page_id,
                operation='generate_asset', owner_id=owner).all()
        elif task.operation == 'generate_asset':
            parent = SceneTaskItem.query.filter_by(id=task.input_json.get('parent_task_id'),
                owner_id=owner).filter(SceneTaskItem.operation.in_(
                    ['generate_scene', 'ai_edit'])).first()
            if parent:
                targets.append(parent)
        for target in targets:
            if target.state in ('queued', 'running', 'waiting_assets'):
                target.cancel_requested_at = now()
                if target.state in ('queued', 'waiting_assets'):
                    target.state = 'cancelled'
    response = {'task_id': task.id, 'state': task.state}
    remember(owner, operation, key, request_hash, 200, {'data': response})
    db.session.commit()
    return ok(response)


@scene_bp.post('/tasks/<task_id>/retry')
def retry_task(task_id):
    owner = owner_id()
    task = SceneTaskItem.query.filter_by(id=task_id, owner_id=owner).first()
    if task is None:
        raise SceneError('NOT_FOUND', 'Task not found', 404)
    data = body()
    operation = f'retry_task:{task_id}'
    _, key, request_hash, existing = unique_request(operation, data)
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    project = task_project(task.project_id, owner)
    task = SceneTaskItem.query.filter_by(id=task_id, owner_id=owner).filter(
        SceneTaskItem.state.in_(['failed', 'outcome_unknown'])).populate_existing().with_for_update().first()
    if task is None:
        raise SceneError('TASK_NOT_RETRYABLE', 'Task cannot be retried', 409)
    if task.operation == 'analyze_template':
        template = ProjectTemplateAsset.query.filter_by(id=task.resource_id, project_id=project.id,
            deleted_at=None).first()
        if template is None:
            raise SceneError('NOT_FOUND', 'Template reference not found', 404)
    possible_charge = possible_charge_on_retry(task)
    if (task.state == 'outcome_unknown' or possible_charge) and data.get('acknowledge_possible_charge') is not True:
        raise SceneError('POSSIBLE_CHARGE_ACK_REQUIRED', 'Confirm possible duplicate model charge')
    if task.state not in ('failed', 'outcome_unknown') or task.attempt_count >= task.max_attempts:
        raise SceneError('TASK_NOT_RETRYABLE', 'Task cannot be retried', 409)
    admit_tasks(owner, task.operation, result=task.result_json)
    task.state = 'queued'
    task.queued_at = now()
    task.retry_request_id = telemetry.request_id()
    task.next_run_at = None
    task.error_code = None
    task.dispatch_state = 'not_sent'
    task.cancel_requested_at = None
    response = {'task_id': task.id, 'state': task.state}
    remember(owner, operation, key, request_hash, 200, {'data': response})
    db.session.commit()
    telemetry.retry_committed(task, 'retry_manual')
    return ok(response)


@scene_bp.get('/model-credentials')
def list_credentials():
    owner = owner_id()
    rows, next_cursor = paged_rows(ModelCredential.query.filter_by(owner_id=owner),
        ModelCredential, ModelCredential.created_at)
    return ok({'items': [credential_summary(c) for c in rows], 'next_cursor': next_cursor})


@scene_bp.post('/model-credentials')
def add_credential():
    owner = owner_id()
    data = body()
    operation = 'add_model_credential'
    key = request.headers.get('Idempotency-Key')
    request_hash = credential_request_fingerprint(data)
    existing = idempotent(owner, operation, key, request_hash)
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    label, base_url, api_key = data.get('label'), data.get('base_url'), data.get('api_key')
    if not all(isinstance(value, str) for value in (label, base_url, api_key)):
        raise SceneError('CREDENTIAL_INVALID', 'Label, gateway URL and API Key must be strings')
    credential = create_credential(owner, label.strip(), base_url, api_key)
    response = credential_summary(credential)
    remember(owner, operation, key, request_hash, 201, {'data': response})
    db.session.commit()
    return ok(response, 201)


@scene_bp.post('/model-credentials/<credential_id>/check')
def check_credential(credential_id):
    owner = owner_id()
    data = body()
    project = owned_project(data.get('project_id'), owner)
    credential = ModelCredential.query.filter_by(id=credential_id, owner_id=owner).first()
    if credential is None or credential.status == 'revoked':
        raise SceneError('CREDENTIAL_NOT_FOUND', 'Credential unavailable', 404)
    kind = data.get('kind', 'model_listing')
    if kind not in ('model_listing', 'text_generation', 'image_generation'):
        raise SceneError('INPUT_INVALID', 'Unsupported credential check kind')
    model_id = data.get('model_id')
    if kind != 'model_listing':
        if (not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 200 or
            data.get('acknowledge_possible_charge') is not True):
            raise SceneError('POSSIBLE_CHARGE_ACK_REQUIRED',
                             'Select a model and confirm that capability testing may charge your Key')
        model_id = model_id.strip()
    elif model_id is not None:
        raise SceneError('INPUT_INVALID', 'Model listing does not take model_id')
    _, key, request_hash, existing = unique_request(f'check_credential:{credential_id}', data)
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    project = task_project(project.id, owner)
    admit_tasks(owner, 'check_credential')
    frozen = {'kind': kind, **({'model_id': model_id} if model_id else {})}
    task = SceneTaskItem(id=new_id(), owner_id=owner, project_id=project.id,
        credential_id=credential.id, group_id=new_id(), logical_key=f'check:{credential.id}',
        operation='check_credential', resource_id=credential.id,
        input_json=frozen, input_hash=digest(frozen))
    db.session.add(task)
    response = {'task_id': task.id, 'possible_charge': kind != 'model_listing',
                'note': ('Only checks the model list; text/image generation remain unverified'
                         if kind == 'model_listing' else 'Paid capability probe using your own Key')}
    remember(owner, f'check_credential:{credential_id}', key, request_hash, 202, {'data': response})
    db.session.commit()
    return ok(response, 202)


@scene_bp.delete('/model-credentials/<credential_id>')
def revoke_credential(credential_id):
    owner = owner_id()
    operation = f'revoke_credential:{credential_id}'
    _, key, request_hash, existing = unique_request(operation, {})
    if existing:
        return ok(existing.response_json['data'], existing.response_status)
    credential = ModelCredential.query.filter_by(id=credential_id, owner_id=owner).first()
    if credential is None:
        raise SceneError('NOT_FOUND', 'Credential not found', 404)
    credential.status = 'revoked'
    response = credential_summary(credential)
    remember(owner, operation, key, request_hash, 200, {'data': response})
    db.session.commit()
    return ok(response)
