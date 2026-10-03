"""Versioned, reviewable HTTP contract for the Scene v1 API.

The route table is intentionally independent of Flask's URL map. The contract
test fails if a handler is added, removed, or moved without updating this file.
"""
import json
import re
from pathlib import Path


# METHOD, blueprint-relative path, endpoint, success status, required data fields.
# A dash in the final column means a binary response or an array data payload.
_ROUTES = """
GET /readiness readiness 200 ready,checks
GET /queue-capacity get_queue_capacity 200 owner,global,max_generated_assets_per_page
GET /storage-capacity get_storage_capacity 200 owner,global,disk_free_bytes,min_free_bytes,includes_uncommitted_files
POST /projects create_project 201 project_id,title,project_version,pages
GET /projects list_projects 200 items,next_cursor
GET /projects/<project_id> get_project 200 project_id,title,project_version,pages
DELETE /projects/<project_id> archive_project 200 project_id,archived
POST /projects/<project_id>/pages add_page 201 page_id,page_version,revision_id
DELETE /projects/<project_id>/pages/<page_id> delete_scene_page 200 project_id,pages
PATCH /projects/<project_id>/model-config set_model_config 200 project_id,model_config
PATCH /projects/<project_id>/outline save_outline 200 project_id,pages
POST /projects/<project_id>/outline-tasks generate_outline_task 202 task_id
GET /projects/<project_id>/outline-drafts/<asset_id> get_outline_draft 200 pages,base_project_version
GET /projects/<project_id>/pages/<page_id>/scene get_scene 200 page_id,page_version,revision_id,scene_hash,scene,asset_urls
PATCH /projects/<project_id>/pages/<page_id>/scene patch_scene 200 page_id,page_version,revision_id,scene_hash,scene,asset_urls
GET /projects/<project_id>/pages/<page_id>/revisions list_revisions 200 items,next_cursor
POST /projects/<project_id>/pages/<page_id>/restore restore_scene 200 page_id,page_version,revision_id,scene_hash,scene,asset_urls
POST /projects/<project_id>/assets upload_asset 201 asset_id,url,sha256,width_px,height_px
GET /assets/<asset_id>/content asset_content 200 -
GET /fonts/noto-sans-sc scene_font 200 -
GET /font-manifest scene_font_manifest 200 font_manifest_id,family,style,format,sha256,download_url
POST /projects/<project_id>/generation-plans create_plan 201 plan_id,manifest_hash
POST /projects/<project_id>/template-documents upload_template_document 202 document_id,task_id,status
GET /projects/<project_id>/template-documents/<document_id> get_template_document 200 document_id,status,source_page_count,warnings,error_code,task_id
POST /projects/<project_id>/template-documents/<document_id>/analyze analyze_document 202 task_group_id,tasks
GET /projects/<project_id>/template-assets list_template_assets 200 items,next_cursor
DELETE /projects/<project_id>/template-assets/<template_asset_id> delete_template_reference 200 project_id,project_version,pages
PATCH /projects/<project_id>/template-assets/<template_asset_id> edit_template_profile 200 template_asset_id,analysis_revision,analysis_hash
PATCH /projects/<project_id>/pages/<page_id>/template bind_page_template 200 page_id,page_version
POST /projects/<project_id>/generation-tasks generate_scenes 202 task_group_id,tasks
POST /projects/<project_id>/pages/<page_id>/ai-edits ai_edit 202 task_id,task_group_id
GET /projects/<project_id>/candidates list_candidates 200 items,next_cursor
POST /projects/<project_id>/candidates/accept accept 200 -
POST /projects/<project_id>/candidates/<candidate_id>/reject reject 200 candidate_id,state
POST /projects/<project_id>/snapshots create_snapshot 201 snapshot_id,manifest_hash
GET /projects/<project_id>/snapshots/preflight preflight_snapshot 200 ready,blockers,page_count
POST /projects/<project_id>/exports create_export 202 export_id,task_id,status
GET /projects/<project_id>/exports list_scene_exports 200 items,next_cursor
GET /projects/<project_id>/exports/<export_id> get_export 200 export_id,status,format,error_code,download_url,report_url
POST /projects/<project_id>/exports/<export_id>/review accept_export_review 200 export_id,status,review_report_sha256
GET /tasks/<task_id> get_task 200 task_id,state,operation,result,error_code
GET /projects/<project_id>/tasks list_project_tasks 200 items,next_cursor
GET /task-groups/<group_id> get_task_group 200 task_group_id,state,completed_pages,total_pages,items
POST /tasks/<task_id>/cancel cancel_task 200 task_id,state
POST /tasks/<task_id>/retry retry_task 200 task_id,state
GET /model-credentials list_credentials 200 items,next_cursor
POST /model-credentials add_credential 201 credential_id,label,status,key_suffix
POST /model-credentials/<credential_id>/check check_credential 202 task_id,possible_charge,note
DELETE /model-credentials/<credential_id> revoke_credential 200 credential_id,label,status,key_suffix
GET /openapi.json scene_openapi 200 -
"""


# Type suffixes: s=str, i=int, b=bool, o=object, a=array.
# Optional fields are marked with ?; the server may impose additional semantic
# constraints (version/CAS, schema, locked objects) described by each operation.
_BODIES = {
    'create_project': 'title:s prompt:s? pages:a? canvas:o? model_config:o?',
    'add_page': 'base_project_version:i outline:o? insert_at:i? copy_from_page_id:s? copy_from_page_version:i? copy_from_revision_id:s?',
    'delete_scene_page': 'base_project_version:i base_page_version:i',
    'set_model_config': 'base_project_version:i model_config:o',
    'save_outline': 'base_project_version:i pages:a',
    'generate_outline_task': 'base_project_version:i brief:s slide_count:i credential_id:s',
    'patch_scene': 'base_revision_id:s base_page_version:i commands:a',
    'restore_scene': 'base_revision_id:s base_page_version:i target_revision_id:s',
    'create_plan': 'base_project_version:i expected_pages:a?',
    'analyze_document': 'selected_page_indexes:a credential_id:s',
    'edit_template_profile': 'base_analysis_revision:i analysis:o',
    'delete_template_reference': 'base_project_version:i base_analysis_revision:i expected_pages:a',
    'bind_page_template': 'base_page_version:i template_asset_id:s? style_text:s?',
    'generate_scenes': 'plan_id:s targets:a credential_id:s',
    'ai_edit': 'base_revision_id:s base_page_version:i instruction:s credential_id:s element_ids:a?',
    'accept': 'candidate_ids:a',
    'create_snapshot': 'project_version:i pages:a pending_candidate_policy:s?',
    'create_export': 'snapshot_id:s format:s options:o?',
    'accept_export_review': 'report_sha256:s acknowledged_warning_codes:a',
    'retry_task': 'acknowledge_possible_charge:b?',
    'add_credential': 'label:s base_url:s api_key:s',
    'check_credential': 'project_id:s kind:s? model_id:s? acknowledge_possible_charge:b?',
}
_MULTIPART = {'upload_asset', 'upload_template_document'}
_ARRAY_DATA = {'accept'}
_BINARY_DATA = {'asset_content', 'scene_font'}
_PAGED = {'list_projects', 'list_revisions', 'list_template_assets', 'list_candidates',
          'list_scene_exports', 'list_project_tasks', 'list_credentials'}
_IDEMPOTENT = {'create_project', 'archive_project', 'add_page', 'delete_scene_page',
               'restore_scene', 'set_model_config', 'save_outline',
               'upload_asset', 'upload_template_document', 'edit_template_profile', 'bind_page_template',
               'generate_outline_task', 'patch_scene', 'create_plan', 'generate_scenes',
               'analyze_document', 'delete_template_reference',
               'ai_edit', 'accept', 'reject', 'create_snapshot', 'create_export',
               'accept_export_review', 'cancel_task', 'revoke_credential',
               'add_credential', 'check_credential', 'retry_task'}


def route_contracts():
    routes = {}
    for line in _ROUTES.strip().splitlines():
        method, path, endpoint, status, required = line.split()
        key = (method, '/api/v2' + path)
        if key in routes:
            raise ValueError(f'Duplicate Scene API contract: {key}')
        routes[key] = {'endpoint': endpoint, 'status': int(status),
                       'required_data': [] if required == '-' else required.split(',')}
    return routes


def _typed_field(name):
    if name == 'base_project_version':
        return {'type': ['integer', 'null'], 'minimum': 0}
    if name == 'max_generated_assets_per_page':
        return {'type': 'integer'}
    if name in ('disk_free_bytes', 'min_free_bytes'):
        return {'type': 'integer', 'minimum': 0}
    if name == 'includes_uncommitted_files':
        return {'type': 'boolean'}
    if name in ('owner', 'global'):
        return {'type': 'object', 'properties': {field: {'type': 'integer', 'minimum': 0} for field in
            ('active_items', 'reserved_units', 'limit_units', 'available_units',
             'used_bytes', 'limit_bytes', 'available_bytes', 'file_count')}}
    if name == 'outline':
        return {'$ref': '#/components/schemas/OutlineContent'}
    if name == 'scene':
        return {'$ref': '#/components/schemas/SlideScene'}
    if name == 'next_cursor':
        return {'type': ['string', 'null']}
    if name in ('result', 'error_code', 'download_url', 'report_url', 'task_id'):
        return {'type': ['string', 'object', 'null']} if name == 'result' else {'type': ['string', 'null']}
    if name in ('items', 'pages', 'tasks', 'warnings', 'blockers', 'acknowledged_warning_codes',
                'selected_page_indexes', 'targets', 'commands', 'element_ids', 'candidate_ids'):
        return {'type': 'array'}
    if name in ('canvas', 'model_config', 'analysis', 'outline', 'options', 'checks', 'asset_urls'):
        return {'type': 'object'}
    if name in ('ready', 'archived', 'possible_charge', 'acknowledge_possible_charge'):
        return {'type': 'boolean'}
    if name.endswith(('_version', '_count', '_px')) or name in ('slide_count', 'completed_pages', 'total_pages'):
        return {'type': 'integer'}
    return {'type': 'string'}


def _body_schema(endpoint):
    fields = _BODIES.get(endpoint)
    if fields is None:
        return None
    required, properties = [], {}
    for token in fields.split():
        name, kind = token.split(':')
        optional = kind.endswith('?')
        kind = kind.rstrip('?')
        properties[name] = {'type': {'s': 'string', 'i': 'integer', 'b': 'boolean',
                                    'o': 'object', 'a': 'array'}[kind]}
        if not optional:
            required.append(name)
    return {'type': 'object', 'properties': properties, 'required': required,
            'additionalProperties': True}


def _slide_schema():
    source = Path(__file__).resolve().parents[2] / 'schemas' / 'slide_scene_v1.schema.json'
    schema = json.loads(source.read_text(encoding='utf-8'))

    def rewrite(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == '$ref' and isinstance(item, str) and item.startswith('#/$defs/'):
                    value[key] = '#/components/schemas/SlideScene/' + item[2:]
                else:
                    rewrite(item)
        elif isinstance(value, list):
            for item in value:
                rewrite(item)
    rewrite(schema)
    return schema


def build_openapi():
    from services.scene.outline import OutlinePage
    paths = {}
    for (method, flask_path), meta in route_contracts().items():
        path = re.sub(r'<([^>]+)>', r'{\1}', flask_path)
        endpoint = meta['endpoint']
        parameters = [{'name': name, 'in': 'path', 'required': True,
                       'schema': {'type': 'string', 'format': 'uuid'}}
                      for name in re.findall(r'<([^>]+)>', flask_path)]
        if endpoint in _PAGED:
            parameters += [{'name': 'limit', 'in': 'query', 'schema': {'type': 'integer', 'minimum': 1, 'maximum': 100}},
                           {'name': 'cursor', 'in': 'query', 'schema': {'type': 'string'}}]
        if endpoint == 'get_scene':
            parameters.append({'name': 'revision_id', 'in': 'query', 'schema': {'type': 'string', 'format': 'uuid'}})
        if endpoint == 'list_template_assets':
            parameters.append({'name': 'document_id', 'in': 'query', 'schema': {'type': 'string', 'format': 'uuid'}})
        if endpoint == 'list_candidates':
            parameters += [{'name': name, 'in': 'query', 'schema': {'type': 'string'}} for name in ('page_id', 'state')]
        if endpoint == 'preflight_snapshot':
            parameters.append({'name': 'page_ids', 'in': 'query', 'required': True,
                               'schema': {'type': 'string', 'description': 'Comma-separated page IDs in export order'}})
        if endpoint == 'asset_content':
            parameters += [{'name': 'expires', 'in': 'query', 'required': True, 'schema': {'type': 'integer'}},
                           {'name': 'sig', 'in': 'query', 'required': True, 'schema': {'type': 'string'}},
                           {'name': 'download', 'in': 'query', 'schema': {'type': 'string', 'enum': ['1']}}]
        if endpoint in _IDEMPOTENT:
            parameters.append({'name': 'Idempotency-Key', 'in': 'header', 'required': True,
                               'schema': {'type': 'string', 'minLength': 1, 'maxLength': 200}})
        if endpoint == 'scene_openapi':
            success = {'description': 'OpenAPI 3.1 document', 'content': {'application/json': {
                'schema': {'type': 'object', 'required': ['openapi', 'info', 'paths', 'components']}}}}
        elif endpoint in _BINARY_DATA:
            success = {'description': 'Authenticated immutable asset' if endpoint == 'asset_content' else 'Fixed Scene font',
                       'content': {'application/octet-stream': {'schema': {'type': 'string', 'format': 'binary'}}}}
        else:
            data_schema = {'type': 'array'} if endpoint in _ARRAY_DATA else {
                'type': 'object', 'required': meta['required_data'],
                'properties': {name: _typed_field(name) for name in meta['required_data']},
                'additionalProperties': True}
            if endpoint == 'get_outline_draft':
                data_schema['properties']['pages'] = {'type': 'array', 'minItems': 1,
                    'items': {'$ref': '#/components/schemas/OutlineContent'}}
            success = {'description': 'Scene API response', 'content': {'application/json': {'schema': {
                'type': 'object', 'required': ['data', 'request_id'],
                'properties': {'data': data_schema, 'request_id': {'type': 'string'}},
                'additionalProperties': False}}}}
        responses = {str(meta['status']): success}
        if endpoint == 'readiness':
            responses['503'] = success
        if endpoint not in ('scene_font', 'asset_content', 'scene_openapi'):
            responses.update({str(code): {'$ref': '#/components/responses/SceneError'}
                              for code in (401, 403, 404, 409, 413, 422, 429, 503, 507)
                              if code != meta['status'] and not (endpoint == 'readiness' and code == 503)})
        elif endpoint == 'asset_content':
            responses.update({str(code): {'$ref': '#/components/responses/SceneError'} for code in (404, 503)})
        elif endpoint == 'scene_openapi':
            responses.update({str(code): {'$ref': '#/components/responses/SceneError'} for code in (401, 404, 503)})
        operation = {'operationId': endpoint, 'tags': ['Scene v1'], 'parameters': parameters,
                     'responses': responses, 'security': [] if endpoint == 'scene_font' else
                     [{'AssetSignature': []}] if endpoint == 'asset_content' else [{'AccessCode': []}]}
        body = _body_schema(endpoint)
        if body is not None:
            operation['requestBody'] = {'required': True, 'content': {'application/json': {'schema': body}}}
        elif endpoint in _MULTIPART:
            operation['requestBody'] = {'required': True, 'content': {'multipart/form-data': {'schema': {
                'type': 'object', 'required': ['file'], 'properties': {
                    'file': {'type': 'string', 'format': 'binary'}}}}}}
        paths.setdefault(path, {})[method.lower()] = operation
    error_schema = {'type': 'object', 'required': ['error', 'request_id'],
                    'properties': {'request_id': {'type': 'string'}, 'error': {'type': 'object',
                        'required': ['code', 'message', 'stage', 'retryable', 'details'],
                        'properties': {'code': {'type': 'string'}, 'message': {'type': 'string'},
                                       'stage': {'type': 'string'}, 'retryable': {'type': 'boolean'},
                                       'details': {'type': 'object'}}}}}
    components = {
        'securitySchemes': {
            'AccessCode': {'type': 'apiKey', 'in': 'header', 'name': 'X-Access-Code'},
            'AssetSignature': {'type': 'apiKey', 'in': 'query', 'name': 'sig'}},
        'responses': {'SceneError': {'description': 'Structured Scene error',
            'content': {'application/json': {'schema': {'$ref': '#/components/schemas/SceneError'}}}}},
        'schemas': {'SlideScene': _slide_schema(), 'SceneError': error_schema,
                    'OutlineContent': OutlinePage.model_json_schema()},
    }
    return {'openapi': '3.1.0', 'info': {'title': 'Banana Slides Scene API', 'version': '1.0.0'},
            'paths': paths, 'components': components}
