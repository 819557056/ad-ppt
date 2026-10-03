"""Bounded, secret-free Scene tracing and best-effort cross-process metrics.

Business commits happen first. Telemetry never reads a request body, URL, header,
exception message/traceback, credential, prompt, or provider request ID.
"""
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
import json
import logging
import math
import re
import sys
import time
from uuid import UUID, uuid4
from urllib.parse import unquote

from flask import g, request

TRACE = ContextVar('scene_trace', default=None)
LOG = logging.getLogger('scene.telemetry')
IDS = ('request_id', 'origin_request_id', 'project_id', 'page_id', 'task_id', 'item_id', 'snapshot_id')
OPERATIONS = frozenset(('http', 'worker', 'export', 'import_template', 'analyze_template',
    'generate_scene', 'ai_edit', 'generate_asset', 'generate_outline', 'check_credential'))
STAGES = frozenset(('request', 'attempt', 'queue', 'model_text', 'model_image', 'model_list',
    'draft_validate', 'candidate_publish', 'candidate_finalize', 'outline', 'template_import',
    'template_analysis', 'snapshot_validate', 'text_layout', 'pptx_compile', 'pptx_verify', 'pptx_render_verify',
    'pdf_compile', 'pdf_verify', 'visual_compare', 'render_queue', 'render_pdf', 'render_pptx', 'render_pptx_pdf',
    'render_html_preview', 'render_text_layout', 'asset_write', 'retry_manual', 'retry_automatic',
    'lease_recovery', 'heartbeat'))
OUTCOMES = frozenset(('success', 'client_error', 'server_error', 'failed', 'outcome_unknown',
    'queued', 'running', 'waiting_assets', 'succeeded', 'cancelled', 'lease_lost', 'other'))
SOURCES = frozenset(('none', 'input', 'auth', 'concurrency', 'capacity', 'storage', 'render',
    'model_auth', 'model_quota', 'model_rate_limit', 'model_unknown', 'model_response',
    'model_connection', 'database', 'internal', 'lease', 'validation'))
BUCKETS = (.01, .05, .1, .5, 1, 5, 30, 120, 600, 3600, float('inf'))
ERROR_CODES = frozenset('''
ANALYSIS_INVALID ANALYSIS_IN_PROGRESS ANALYSIS_VERSION_CONFLICT ASSET_NOT_READY
ASSET_STEP_NEEDS_ATTENTION ASSET_TASK_MISSING ASSET_UNAVAILABLE ATTEMPTS_EXHAUSTED
AUTH_REQUIRED CANDIDATES_INVALID CANDIDATE_INVALID CANDIDATE_STALE
CANVAS_INVALID CREDENTIAL_CONFIG_MISSING CREDENTIAL_INVALID CREDENTIAL_KEY_VERSION
CREDENTIAL_NOT_FOUND CREDENTIAL_REVOKED CURSOR_INVALID DATABASE_ERROR
DRAFT_MISSING EDIT_SCOPE_CONFLICT FIRST_GENERATION_UNACCEPTED FONT_GLYPH_UNAVAILABLE
FONT_MISMATCH FONT_UNAVAILABLE FORMAT_INVALID GATEWAY_INVALID
GATEWAY_NOT_ALLOWED HTTP_ERROR IDEMPOTENCY_CONFLICT IDEMPOTENCY_KEY_REQUIRED
IMAGE_MODEL_NOT_CONFIGURED IMAGE_RESPONSE_INVALID INPUT_INVALID INSTRUCTION_INVALID
INTERNAL_ERROR LEASE_EXPIRED METRICS_UNAVAILABLE MODEL_AUTH_FAILED
MODEL_CAPABILITY_INVALID MODEL_CHECK_FAILED MODEL_CONFIG_INVALID MODEL_CONNECT_FAILED
MODEL_EDIT_INVALID MODEL_FAILED MODEL_NOT_CONFIGURED MODEL_OUTCOME_UNKNOWN
MODEL_QUOTA_EXHAUSTED MODEL_RATE_LIMITED MODEL_RESPONSE_INVALID MODEL_RESPONSE_TOO_LARGE
MODEL_SCENE_INVALID MODEL_UNSUPPORTED NOT_FOUND ORIGIN_FORBIDDEN
OUTLINE_INPUT_INVALID OUTLINE_INVALID OWNER_DISABLED PAGE_LIMIT
PAGE_LIMIT_INVALID PAGE_POSITION_INVALID PAGE_SELECTION_INVALID PAGE_VERSION_CONFLICT
PARENT_UNAVAILABLE PENDING_CANDIDATE_CHOICE_REQUIRED PLAN_NOT_FOUND PLAN_STALE
PPTX_RENDER_UNVERIFIED PPTX_RENDER_TEXT_MISMATCH PPTX_RENDER_PAGE_MISMATCH
PPTX_RENDER_CLIPPED PPTX_RENDER_REFLOW PPTX_RENDER_FONT_MISMATCH
POSSIBLE_CHARGE_ACK_REQUIRED PROJECT_ARCHIVED PROJECT_INVALID PROJECT_VERSION_CONFLICT
QUEUE_BACKEND_UNSUPPORTED QUEUE_CAPACITY_EXCEEDED QUEUE_CONFIG_INVALID REFERENCE_IMAGE_INVALID
REFERENCE_IMAGE_TOO_LARGE RENDERER_BUSY RENDERER_CONFIG_INVALID RENDERER_ORPHANED_JOB
RENDERER_PATH_INVALID RENDERER_UID_EXHAUSTED RENDERER_UNAVAILABLE RENDER_FAILED
RENDER_INCOMPLETE RENDER_OUTPUT_INVALID RENDER_TIMEOUT RENDER_TOO_LARGE
REVIEW_EVIDENCE_REQUIRED REVIEW_REPORT_MISMATCH REVIEW_STATE_INVALID SCENE_AUTH_NOT_CONFIGURED
SCENE_DISABLED SCENE_HASH_MISMATCH SCENE_INVALID SCENE_MISSING
SCENE_VERSION_CONFLICT SCOPE_INVALID SNAPSHOT_INVALID SNAPSHOT_VERSION_CONFLICT
STORAGE_BUSY STORAGE_CONFIG_INVALID STORAGE_DISK_LOW STORAGE_ENTRY_LIMIT
STORAGE_KEY_INVALID STORAGE_LAYOUT_INVALID STORAGE_OWNER_INVALID STORAGE_QUOTA_EXCEEDED
STORAGE_UNAVAILABLE STORAGE_WRITE_FAILED STYLE_TEXT_INVALID TARGETS_INVALID
TASK_FENCE_LOST TASK_INPUT_INVALID TASK_NOT_RETRYABLE TASK_OPERATION_INVALID
TEMPLATE_INVALID TEMPLATE_NOT_FOUND TEMPLATE_NOT_READY TEMPLATE_RENDERER_UNAVAILABLE
TEMPLATE_TYPE_UNSUPPORTED TEXT_OVERFLOW UPLOAD_TOO_LARGE
'''.split())
METRICS = frozenset(('scene_http_requests_total', 'scene_task_transitions_total',
    'scene_retries_total', 'scene_version_conflicts_total', 'scene_render_failures_total',
    'scene_stage_duration_seconds_sum', 'scene_stage_duration_seconds_count',
    'scene_stage_duration_seconds_bucket', 'scene_queue_wait_seconds_sum',
    'scene_queue_wait_seconds_count', 'scene_queue_wait_seconds_bucket'))
EVENTS = frozenset(('scene.request', 'scene.stage', 'scene.task_enqueued', 'scene.task_transition',
    'scene.attempt_started', 'scene.attempt_finished', 'scene.retry', 'scene.lease_lost',
    'scene.heartbeat_failed', 'scene.visual_unavailable', 'scene.metrics_unavailable'))


@dataclass
class Trace:
    ids: dict
    operation: str
    error_code: str | None = None
    metrics: dict = field(default_factory=lambda: defaultdict(float))


def valid_id(value):
    try:
        return str(UUID(value)) if isinstance(value, str) else None
    except (ValueError, TypeError, AttributeError):
        return None


def request_id():
    trace = TRACE.get()
    return trace.ids['request_id'] if trace else str(uuid4())


def bind(**values):
    trace = TRACE.get()
    if trace:
        trace.ids.update({key: valid_id(value) for key, value in values.items() if key in IDS})


def set_error(code):
    trace = TRACE.get()
    if trace:
        trace.error_code = code if code in ERROR_CODES else 'INTERNAL_ERROR'


def code_of(exc):
    from sqlalchemy.exc import SQLAlchemyError
    if isinstance(exc, SQLAlchemyError):
        return 'DATABASE_ERROR'
    value = getattr(exc, 'code', None)
    return value if isinstance(value, str) and value in ERROR_CODES else 'INTERNAL_ERROR'


def failure_source(code):
    if not code:
        return 'none'
    if code in ('AUTH_REQUIRED', 'ORIGIN_FORBIDDEN', 'SCENE_AUTH_NOT_CONFIGURED') or code.startswith('CREDENTIAL_'):
        return 'auth'
    if code == 'DATABASE_ERROR':
        return 'database'
    if code == 'MODEL_OUTCOME_UNKNOWN':
        return 'model_unknown'
    if code == 'MODEL_AUTH_FAILED':
        return 'model_auth'
    if code == 'MODEL_RATE_LIMITED':
        return 'model_rate_limit'
    if code == 'MODEL_QUOTA_EXHAUSTED':
        return 'model_quota'
    if code == 'MODEL_CONNECT_FAILED':
        return 'model_connection'
    if code in ('TASK_FENCE_LOST', 'LEASE_EXPIRED'):
        return 'lease'
    if code.startswith(('STORAGE_', 'ASSET_STORE_')):
        return 'storage'
    if code.startswith(('QUEUE_', 'OWNER_QUEUE_')):
        return 'capacity'
    if code == 'TEMPLATE_RENDERER_UNAVAILABLE' or code.startswith(('RENDER', 'PDF_', 'PPTX_')):
        return 'render'
    if code.startswith(('MODEL_', 'IMAGE_MODEL_', 'IMAGE_RESPONSE_')):
        return 'model_response'
    if 'VERSION' in code or code in ('PLAN_STALE', 'CANDIDATE_STALE', 'IDEMPOTENCY_CONFLICT'):
        return 'concurrency'
    if code.startswith(('SCENE_', 'FONT_', 'TEXT_', 'DRAFT_', 'EDIT_', 'ASSET_')):
        return 'validation'
    return 'internal' if code == 'INTERNAL_ERROR' else 'input'


def emit(event, *, context=None, operation=None, stage=None, outcome=None, error_code=None,
         duration_seconds=None, attempt_no=None, status=None, endpoint=None):
    """Allowlist-only log schema; arbitrary extras cannot leak through kwargs."""
    if event not in EVENTS:
        return
    trace = TRACE.get()
    if trace is None and context is None:
        return
    fields = {key: None for key in IDS}
    if trace:
        fields.update(trace.ids)
    if context:
        fields.update({key: valid_id(value) for key, value in context.items() if key in IDS})
    selected_operation = operation or (trace.operation if trace else None)
    payload = {'event': event, 'time': datetime.now(timezone.utc).isoformat(), **fields,
               'operation': selected_operation if selected_operation in OPERATIONS else 'worker'}
    if stage in STAGES:
        payload['stage'] = stage
    if outcome in OUTCOMES:
        payload['outcome'] = outcome
    if error_code is not None:
        payload['error_code'] = error_code if error_code in ERROR_CODES else 'INTERNAL_ERROR'
        payload['failure_source'] = failure_source(payload['error_code'])
    if isinstance(duration_seconds, (float, int)) and math.isfinite(duration_seconds):
        payload['duration_seconds'] = round(max(0, duration_seconds), 6)
    if type(attempt_no) is int and 0 < attempt_no < 10000:
        payload['attempt_no'] = attempt_no
    if type(status) is int and 100 <= status <= 599:
        payload['status'] = status
    if isinstance(endpoint, str) and re.fullmatch(r'[a-z_]{1,80}', endpoint):
        payload['endpoint'] = endpoint  # Server-registered endpoint, never the actual URL.
    try:
        LOG.info(json.dumps(payload, sort_keys=True, separators=(',', ':')))
    except Exception:
        pass  # Broken log output must not repeat a paid call or undo a commit.


def add_metric(name, value=1, *, operation=None, stage='attempt', outcome='success', source='none', bucket=''):
    trace = TRACE.get()
    if trace is None or name not in METRICS or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return
    operation = operation or trace.operation
    if (operation not in OPERATIONS or stage not in STAGES or outcome not in OUTCOMES or source not in SOURCES
            or bucket not in ('', *[str(b) for b in BUCKETS])):
        return
    trace.metrics[(name, operation, stage, outcome, source, bucket)] += value


def observe(name, value, **labels):
    value = max(0, float(value))
    if not math.isfinite(value):
        return
    add_metric(name + '_sum', value, **labels)
    add_metric(name + '_count', **labels)
    for bound in BUCKETS:
        add_metric(name + '_bucket', 1 if value <= bound else 0, bucket=str(bound), **labels)


@contextmanager
def stage(name):
    start = time.monotonic()
    outcome, code = 'success', None
    try:
        yield
    except Exception as exc:
        outcome, code = 'failed', code_of(exc)
        raise
    finally:
        elapsed = time.monotonic() - start
        emit('scene.stage', stage=name, outcome=outcome, error_code=code, duration_seconds=elapsed)
        observe('scene_stage_duration_seconds', elapsed, stage=name, outcome=outcome, source=failure_source(code))
        if outcome == 'failed' and name.startswith('render_') and name != 'render_queue':
            add_metric('scene_render_failures_total', stage=name, outcome=outcome, source=failure_source(code))


def timed_stage(name):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with stage(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate


@contextmanager
def scope(*, operation='worker', **ids):
    trace = Trace({key: valid_id(ids.get(key)) for key in IDS}, operation)
    trace.ids['request_id'] = trace.ids['request_id'] or str(uuid4())
    token = TRACE.set(trace)
    try:
        yield trace
    finally:
        TRACE.reset(token)


def task_context(item, *, active_request=False):
    return {'request_id': request_id() if active_request else item.retry_request_id or item.request_id,
            'origin_request_id': item.request_id, 'project_id': item.project_id,
            'page_id': item.page_id, 'task_id': item.group_id or item.id, 'item_id': item.id,
            'snapshot_id': (item.input_json or {}).get('snapshot_id') if item.operation == 'export' else None}


def retry_committed(item, stage_name):
    emit('scene.retry', context=task_context(item, active_request=TRACE.get() is not None and TRACE.get().operation == 'http'),
         operation=item.operation, stage=stage_name, outcome='queued')
    add_metric('scene_retries_total', operation=item.operation, stage=stage_name, outcome='queued')


def flush_metrics(engine, trace):
    """Called only after the business session has released its connection/locks.

One short atomic upsert transaction, no ORM events. Failure drops this batch and
emits a safe log; metrics are operational best-effort, never billing evidence.
"""
    if not trace.metrics:
        return
    from sqlalchemy import text
    from models.scene_v1 import SceneMetric
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    columns = ('metric', 'operation', 'stage', 'outcome', 'source', 'bucket')
    records = [dict(zip(columns, key), value=value, updated_at=datetime.now(timezone.utc))
               for key, value in sorted(trace.metrics.items())]
    trace.metrics.clear()
    try:
        with engine.connect() as connection:
            original_timeout = None
            try:
                if engine.dialect.name == 'postgresql':
                    connection.execute(text("SET LOCAL lock_timeout = '250ms'"))
                    connection.execute(text("SET LOCAL statement_timeout = '500ms'"))
                    insert = pg_insert
                elif engine.dialect.name == 'sqlite':
                    original_timeout = connection.execute(text('PRAGMA busy_timeout')).scalar()
                    connection.execute(text('PRAGMA busy_timeout=250'))
                    insert = sqlite_insert
                else:
                    raise ValueError('Unsupported metrics dialect')
                statement = insert(SceneMetric).values(records)
                statement = statement.on_conflict_do_update(index_elements=list(columns), set_={
                    'value': SceneMetric.value + statement.excluded.value,
                    'updated_at': statement.excluded.updated_at})
                connection.execute(statement)
                connection.commit()
            finally:
                if original_timeout is not None:
                    connection.rollback()
                    connection.execute(text(f'PRAGMA busy_timeout={int(original_timeout)}'))
                    connection.commit()
    except Exception:
        emit('scene.metrics_unavailable', context=trace.ids, operation=trace.operation,
             error_code='METRICS_UNAVAILABLE', outcome='failed')


class SceneJsonHandler(logging.StreamHandler):
    def handleError(self, record):
        # StreamHandler normally prints a traceback to stderr on output failure.
        # A broken log sink must remain silent, even with logging.raiseExceptions.
        pass


class SceneAccessFilter(logging.Filter):
    def filter(self, record):
        # Werkzeug logs after request teardown, when Flask's context is gone.
        # Drop duplicate Scene access lines rather than leaking signed URL querystrings.
        # Werkzeug wraps non-200 request lines in ANSI SGR sequences even in
        # container logs; the final 'm' otherwise defeats the method word boundary.
        message = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', record.getMessage())
        return re.search(r'\b(?:GET|POST|PATCH|PUT|DELETE|HEAD|OPTIONS) (?:https?://[^/\s]+)?/api/v2(?:[/ ?])', unquote(message)) is None


def install(app):
    """Register before authentication, and keep the legacy logging format intact."""
    if not any(getattr(handler, '_scene_json_handler', False) for handler in LOG.handlers):
        handler = SceneJsonHandler(sys.stdout)
        handler._scene_json_handler = True
        handler.setFormatter(logging.Formatter('%(message)s'))
        LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False
    werkzeug = logging.getLogger('werkzeug')
    if not any(isinstance(item, SceneAccessFilter) for item in werkzeug.filters):
        werkzeug.addFilter(SceneAccessFilter())
    install_session_events()

    @app.before_request
    def begin_scene_trace():
        if request.path == '/api/v2' or request.path.startswith('/api/v2/'):
            trace = Trace({key: None for key in IDS}, 'http')
            trace.ids['request_id'] = str(uuid4())  # Never trust a caller's trace header.
            g.scene_trace_token = TRACE.set(trace)
            g.scene_trace = trace
            g.scene_trace_start = time.monotonic()
            bind(**{key: value for key, value in (request.view_args or {}).items() if key in IDS})

    @app.after_request
    def finish_scene_trace(response):
        trace = getattr(g, 'scene_trace', None)
        if trace is None:
            return response
        code = trace.error_code
        if response.status_code >= 400 and code is None:
            code = 'HTTP_ERROR' if response.status_code < 500 else 'INTERNAL_ERROR'
        response.headers['X-Request-ID'] = trace.ids['request_id']
        elapsed = time.monotonic() - g.scene_trace_start
        outcome = 'server_error' if response.status_code >= 500 else 'client_error' if response.status_code >= 400 else 'success'
        source = failure_source(code)
        emit('scene.request', stage='request', outcome=outcome, error_code=code,
             duration_seconds=elapsed, status=response.status_code,
             endpoint=(request.endpoint or 'unmatched').rsplit('.', 1)[-1])
        add_metric('scene_http_requests_total', stage='request', outcome=outcome, source=source)
        observe('scene_stage_duration_seconds', elapsed, stage='request', outcome=outcome, source=source)
        if response.status_code == 409 and code in ('PAGE_VERSION_CONFLICT', 'PROJECT_VERSION_CONFLICT',
                'SCENE_VERSION_CONFLICT', 'SNAPSHOT_VERSION_CONFLICT', 'ANALYSIS_VERSION_CONFLICT',
                'CANDIDATE_STALE', 'PLAN_STALE'):
            add_metric('scene_version_conflicts_total', stage='request', outcome='client_error', source=source)
        return response

    @app.teardown_request
    def close_scene_trace(_exception):
        trace = g.pop('scene_trace', None)
        token = g.pop('scene_trace_token', None)
        if trace is not None:
            from models import db
            try:
                # Flask-SQLAlchemy would remove this at app-context teardown anyway.
                # Doing it here guarantees telemetry never waits on our own business lock.
                engine = db.engine
                db.session.rollback()
                flush_metrics(engine, trace)
            except Exception:
                emit('scene.metrics_unavailable', context=trace.ids, operation=trace.operation,
                     error_code='METRICS_UNAVAILABLE', outcome='failed')
            finally:
                TRACE.reset(token)


def install_session_events():
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    if event.contains(Session, 'after_flush', _capture_committed_events):
        return
    event.listen(Session, 'after_flush', _capture_committed_events)
    event.listen(Session, 'after_commit', _publish_committed_events)
    event.listen(Session, 'after_rollback', _discard_events)


def _safe_session_hook(function):
    @wraps(function)
    def guarded(session, *args):
        try:
            return function(session, *args)
        except Exception:
            session.info.pop('scene_telemetry_events', None)
            emit('scene.metrics_unavailable', error_code='METRICS_UNAVAILABLE', outcome='failed')
    return guarded


@_safe_session_hook
def _capture_committed_events(session, _flush_context):
    trace = TRACE.get()
    if trace is None:
        return
    from sqlalchemy import inspect
    from models.scene_v1 import SceneTaskItem, SceneTaskAttempt
    pending = session.info.setdefault('scene_telemetry_events', {})
    for item in session.new | session.dirty:
        if isinstance(item, SceneTaskItem):
            history = inspect(item).attrs.state.history
            if item in session.new or history.has_changes():
                context = task_context(item, active_request=TRACE.get() is not None and TRACE.get().operation == 'http')
                event_name = 'scene.task_enqueued' if item in session.new else 'scene.task_transition'
                pending[('task', item.id)] = (event_name, context, item.operation, item.state, item.error_code, None, None)
        elif isinstance(item, SceneTaskAttempt) and item in session.new:
            task = session.identity_map.get((SceneTaskItem, (item.task_item_id,), None))
            if task:
                context = task_context(task)
                context['request_id'] = item.request_id
                pending[('attempt', item.id)] = ('scene.attempt_started', context, task.operation,
                    'running', None, item.queue_wait_seconds, item.attempt_no)


@_safe_session_hook
def _publish_committed_events(session):
    for event, context, operation, outcome, code, wait, attempt in session.info.pop('scene_telemetry_events', {}).values():
        emit(event, context=context, operation=operation, outcome=outcome, error_code=code, attempt_no=attempt)
        if event == 'scene.task_transition':
            add_metric('scene_task_transitions_total', operation=operation, outcome=outcome, source=failure_source(code))
        if wait is not None:
            observe('scene_queue_wait_seconds', wait, operation=operation, stage='queue', outcome='success')


def _discard_events(session):
    session.info.pop('scene_telemetry_events', None)
