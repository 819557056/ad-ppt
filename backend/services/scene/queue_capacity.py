"""Bound active Scene work, including image jobs not materialized yet.

Admission happens in the same transaction as API task insertion/retry. Workers
never acquire this mutex: a pre-draft parent reserves all six possible children,
so publishing its bounded DAG can only reduce (not increase) total units. This
avoids blocking a paid result or deadlocking finalizers behind a full queue.
"""
from flask import current_app
from sqlalchemy import case, func, text

from models import db
from models.scene_v1 import SceneTaskItem
from services.scene.versioning import SceneError

MAX_GENERATED_ASSETS = 6
ACTIVE_STATES = ('queued', 'running', 'waiting_assets')
DRAFT_OPERATIONS = ('generate_scene', 'ai_edit')


def task_units(operation, result=None):
    return 1 + MAX_GENERATED_ASSETS if operation in DRAFT_OPERATIONS and not (result or {}).get('draft_asset_id') else 1


def _limits():
    limits = (current_app.config['SCENE_QUEUE_CAPACITY_UNITS'],
              current_app.config['OWNER_QUEUE_CAPACITY_UNITS'])
    if any(type(value) is not int or value < 1 for value in limits):
        raise SceneError('QUEUE_CONFIG_INVALID', 'Queue capacity must be a positive integer', 503)
    return limits


def _lock_admissions():
    # Call after business row locks and idempotency lookup, before task writes.
    # Admission code never takes another existing business row lock afterward.
    # Two-int PostgreSQL advisory namespace is separate from idempotency's bigint.
    dialect = db.session.get_bind().dialect.name
    with db.session.no_autoflush:
        if dialect == 'postgresql':
            db.session.execute(text('SELECT pg_advisory_xact_lock(1396919877, 1)'))
        elif dialect == 'sqlite':
            # An empty UPDATE acquires SQLite's database-wide writer reservation
            # without changing data, including when the task table is empty.
            # Thus the subsequent aggregate sees every earlier admission commit.
            db.session.execute(text('UPDATE scene_task_items SET state = state WHERE 1 = 0'))
        else:
            raise SceneError('QUEUE_BACKEND_UNSUPPORTED', 'Queue admission requires PostgreSQL or SQLite', 503)


def _usage(owner_id=None):
    draft = SceneTaskItem.result_json['draft_asset_id'].as_string()
    units = case((SceneTaskItem.operation.in_(DRAFT_OPERATIONS) &
                  (draft.is_(None) | (draft == '')), 1 + MAX_GENERATED_ASSETS), else_=1)
    query = db.session.query(func.count(SceneTaskItem.id), func.coalesce(func.sum(units), 0)).filter(
        SceneTaskItem.state.in_(ACTIVE_STATES))
    if owner_id is not None:
        query = query.filter(SceneTaskItem.owner_id == owner_id)
    count, reserved = query.one()
    return int(count), int(reserved)


def queue_capacity(owner_id):
    global_limit, owner_limit = _limits()
    def summary(owner, limit):
        count, reserved = _usage(owner)
        return {'active_items': count, 'reserved_units': reserved,
                'limit_units': limit, 'available_units': max(0, limit - reserved)}
    return {'owner': summary(owner_id, owner_limit), 'global': summary(None, global_limit),
            'max_generated_assets_per_page': MAX_GENERATED_ASSETS}


def admit_tasks(owner_id, operation, count=1, *, result=None):
    """Reserve a batch by checking before inserting/requeueing, never committing.

    Same-key replays must return before this call. The caller must persist the
    admitted tasks in this transaction, or roll it back; do not use this as an
    independent preflight check or call it after adding pending task objects.
    """
    if type(count) is not int or count < 1:
        raise ValueError('Task admission count must be positive')
    global_limit, owner_limit = _limits()
    requested = task_units(operation, result) * count
    _lock_admissions()
    for scope, owner, limit in (('owner', owner_id, owner_limit), ('global', None, global_limit)):
        _, reserved = _usage(owner)
        if requested + reserved > limit:
            raise SceneError('QUEUE_CAPACITY_EXCEEDED',
                '任务队列容量不足，请等待现有任务结束或取消任务后重试；批量生成可减少所选页数。', 429,
                {'scope': scope, 'limit_units': limit, 'reserved_units': reserved, 'requested_units': requested})
