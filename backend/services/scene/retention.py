"""Reference-aware retention plans. Only the offline maintenance command applies them."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from uuid import UUID

from sqlalchemy import MetaData, Table, inspect, select, text

from models import Page, Project, ProjectTemplateAsset
from models.scene_v1 import (ApiIdempotencyRecord, Asset, DeckSnapshot, GenerationPlan,
    ModelCredential, PageSceneRevision, RevisionAsset, SceneCandidate, SceneExport,
    SceneTaskAttempt, SceneTaskItem, SnapshotPage, TemplateDocument, SceneMaintenanceRun)
from .storage_quota import LOCK_NAME, _unsafe
from .validation import asset_refs, digest

POLICY_DEFAULTS = {'candidate_days': 30, 'task_days': 90, 'attempt_days': 180,
                   'asset_days': 30, 'orphan_hours': 24}
GC_MODELS = (ApiIdempotencyRecord, SceneTaskAttempt, SceneCandidate, SceneTaskItem, PageSceneRevision, Asset)
ROOT_MODELS = (Project, Page, ProjectTemplateAsset, TemplateDocument, GenerationPlan,
               DeckSnapshot, SceneExport, ModelCredential)
TABLES = {model.__tablename__: model.__table__ for model in (*ROOT_MODELS, *GC_MODELS, RevisionAsset, SnapshotPage)}
JSON_ASSET_KINDS = {'scene_draft', 'edit_draft', 'outline_draft', 'export_report'}
DERIVED_ASSET_KINDS = JSON_ASSET_KINDS | {'generated_image', 'visual_comparison', 'export', 'template_preview'}
UUID_RE = re.compile(r'(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise ValueError('Retention timestamp is missing or invalid')
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def policy_values(policy):
    if not isinstance(policy, dict) or set(policy) != set(POLICY_DEFAULTS) or any(
            type(value) is not int or not 1 <= value <= 36500 for value in policy.values()):
        raise ValueError('Retention periods must be bounded positive integers')
    return dict(policy)


def _canonical(value):
    if isinstance(value, datetime):
        return utc(value).isoformat()
    if isinstance(value, bytes):
        return {'binary_sha256': hashlib.sha256(value).hexdigest()}
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)  # Decimal canvas values, not arbitrary executable objects.


def fingerprint(value):
    return hashlib.sha256(json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _references(value):
    if isinstance(value, str):
        yield value
        yield from (match.lower() for match in UUID_RE.findall(value))
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _references(key)
            yield from _references(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _references(child)


def _sha256(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def inventory(root, max_entries=200000):
    root = Path(root).absolute()
    if not root.is_dir() or _unsafe(root.lstat()):
        raise ValueError('Retention needs a regular existing asset root')
    root = root.resolve()
    result, pending, entries = {}, [(root, 0)], 0
    while pending:
        directory, depth = pending.pop()
        if depth > 32:
            raise ValueError('Asset nesting exceeds maintenance limit')
        with os.scandir(directory) as children:
            for child in children:
                info = os.stat(child.path, follow_symlinks=False)
                entries += 1
                if entries > max_entries:
                    raise ValueError('Asset entry count exceeds maintenance limit')
                if _unsafe(info):
                    raise ValueError('Links/reparse points are forbidden during retention')
                path = Path(child.path)
                if stat.S_ISDIR(info.st_mode):
                    pending.append((path, depth + 1))
                elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError('Non-private regular file in asset store')
                elif path != root / LOCK_NAME:
                    relative = path.relative_to(root).as_posix()
                    result[relative] = {'path': relative, 'byte_size': info.st_size,
                        'mtime_ns': info.st_mtime_ns, 'sha256': _sha256(path)}
    return result


def canonical_asset_file(path):
    parts = PurePosixPath(path).parts
    if (len(parts) != 7 or PurePosixPath(path).as_posix() != path or parts[0] != 'owners' or
            parts[2] != 'projects' or parts[4] != 'assets' or
            not re.fullmatch(r'content\.[a-z0-9]{1,12}(?:\.staging)?', parts[6])):
        return None
    try:
        if any(str(UUID(parts[index])) != parts[index] for index in (1, 3, 5)):
            return None
    except ValueError:
        return None
    return parts[5]


def database_state(session):
    inspector = inspect(session.connection())
    if session.connection().dialect.name == 'postgresql':
        extras = [name for name in inspector.get_schema_names()
                  if name not in ('public', 'information_schema') and not name.startswith('pg_')]
        if extras:
            raise ValueError('Retention requires the audited public-only application schema')
        triggers = session.execute(text("SELECT c.relname FROM pg_trigger t "
            "JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE NOT t.tgisinternal AND n.nspname='public'")).scalars()
    else:
        triggers = session.execute(text("SELECT tbl_name FROM sqlite_master WHERE type='trigger'")).scalars()
    if set(triggers) & ({model.__tablename__ for model in GC_MODELS} |
                        {'revision_assets', 'scene_maintenance_runs'}):
        raise ValueError('Custom deletion triggers require an audited retention adapter')
    rows = {}
    for name, table in TABLES.items():
        if {column['name'] for column in inspector.get_columns(name)} != set(table.c.keys()):
            raise ValueError('Database columns differ from the audited retention models')
        # Credentials are permanent roots, but their encrypted/key fields are
        # irrelevant to reachability and must never enter the cleanup plan.
        columns = ([table.c.id, table.c.owner_id, table.c.capabilities_json]
                   if name == 'model_credentials' else list(table.columns))
        rows[name] = [dict(row) for row in session.execute(select(*columns)).mappings()]
        rows[name].sort(key=lambda row: fingerprint(row))
    # Future/legacy real FKs into GC tables also protect their targets. Reflect
    # the live schema, not just today's ORM declarations. No unrelated content
    # or credentials are read; only the constrained key columns are selected.
    external = []
    gc_tables = {model.__tablename__ for model in GC_MODELS}
    for name in sorted(inspector.get_table_names()):
        if name in TABLES or name == 'scene_maintenance_runs':
            continue
        for foreign in inspector.get_foreign_keys(name):
            if foreign.get('referred_table') not in gc_tables:
                continue
            table = Table(name, MetaData(), autoload_with=session.connection())
            values = [list(row) for row in session.execute(select(
                *(table.c[column] for column in foreign['constrained_columns'])))]
            external.append({'table': name, 'target': foreign['referred_table'],
                             'columns': foreign['referred_columns'],
                             'values': sorted(values, key=fingerprint)})
    schema = (list(session.execute(text('SELECT version_num FROM alembic_version ORDER BY version_num')).scalars())
              if inspector.has_table('alembic_version') else [])
    return {'rows': rows, 'external_references': external, 'schema_revisions': schema}


def database_identity(session):
    """Bind a plan to its actual database, without persisting URL credentials."""
    connection = session.connection()
    dialect = connection.dialect.name
    if dialect == 'sqlite':
        entries = connection.exec_driver_sql('PRAGMA database_list').fetchall()
        filename = next((row[2] for row in entries if row[1] == 'main'), '')
        if not filename:
            raise ValueError('Retention requires a persistent SQLite database')
        path = Path(filename).resolve()
        info = path.stat()
        value = {'dialect': dialect, 'path': str(path), 'device': info.st_dev, 'inode': info.st_ino}
    elif dialect == 'postgresql':
        value = dict(session.execute(text("SELECT current_database() AS name, "
            "(SELECT oid::bigint FROM pg_database WHERE datname=current_database()) AS oid, "
            "inet_server_addr()::text AS address, inet_server_port() AS port")).mappings().one())
        value['cluster_id'] = session.execute(text('SELECT system_identifier::text FROM pg_control_system()')).scalar_one()
        value['dialect'] = dialect
    else:
        raise ValueError('Retention supports only PostgreSQL and offline SQLite')
    return fingerprint(value)


def assert_quiescent(state):
    active = [task for task in state['rows']['scene_task_items']
              if task['state'] in ('queued', 'running', 'waiting_assets')]
    if active:
        raise ValueError(f'Finish or cancel all active Scene tasks before retention ({len(active)} remain)')


def _mark_graph(state, files, root, policy, as_of):
    rows = state['rows']
    objects, graph, by_id, eligible = {}, defaultdict(set), defaultdict(set), set()
    def add(node, value, identifier=None):
        objects[node] = value
        if identifier:
            by_id[identifier].add(node)
    for table in (*ROOT_MODELS, *GC_MODELS):
        name = table.__tablename__
        for row in rows[name]:
            add((name, row['id']), row, row['id'])
    groups = defaultdict(list)
    for task in rows['scene_task_items']:
        if task['group_id']:
            groups[task['group_id']].append(('scene_task_items', task['id']))
    for group_id, members in groups.items():
        node = ('task_group', group_id)
        add(node, {}, group_id)
        eligible.add(node)
        graph[node].update(members)
        for member in members:
            graph[member].add(node)
    file_by_asset = defaultdict(set)
    for path, item in files.items():
        aid = canonical_asset_file(path)
        node = ('file', path)
        add(node, item, aid)
        if aid:
            file_by_asset[aid].add(node)
    for table in ('generation_plans', 'deck_snapshots'):
        if any(digest(row['manifest_json']) != row['manifest_hash'] for row in rows[table]):
            raise ValueError('Immutable manifest hash mismatch; retention refused')
    pages = {row['id']: row for row in rows['pages']}
    projects = {row['id']: row for row in rows['projects']}
    tasks = {row['id']: row for row in rows['scene_task_items']}
    attempts_by_task = defaultdict(list)
    for row in rows['scene_task_attempts']:
        attempts_by_task[row['task_item_id']].append(row)
    unknown_tasks = {task['id'] for task in tasks.values() if task['state'] == 'outcome_unknown' or
        task['dispatch_state'] == 'may_have_been_sent' or any(
            attempt['dispatch_state'] == 'may_have_been_sent' or attempt['error_code'] == 'MODEL_OUTCOME_UNKNOWN'
            for attempt in attempts_by_task[task['id']])}
    def old(row, days, *fields):
        times = [utc(row[field]) for field in fields if row.get(field) is not None]
        return bool(times) and max(times) <= as_of - timedelta(days=days)
    candidate_revisions = set()
    for row in rows['scene_candidates']:
        page = pages.get(row['page_id'])
        stale = bool(page and (page['deleted_at'] or page['head_revision_id'] != row['base_revision_id'] or
                              page['row_version'] != row['base_page_version']))
        if (row['state'] in ('rejected', 'stale') or row['state'] == 'pending' and stale) and old(row, policy['candidate_days'], 'created_at'):
            eligible.add(('scene_candidates', row['id']))
            candidate_revisions.add(row['proposed_revision_id'])
    for row in rows['page_scene_revisions']:
        if row['id'] in candidate_revisions and row['origin'] in ('ai_generate', 'ai_edit'):
            eligible.add(('page_scene_revisions', row['id']))
    for row in tasks.values():
        if row['state'] in ('succeeded', 'failed', 'cancelled') and row['id'] not in unknown_tasks and old(
                row, policy['task_days'], 'created_at', 'updated_at'):
            eligible.add(('scene_task_items', row['id']))
    for row in rows['scene_task_attempts']:
        task = tasks.get(row['task_item_id'])
        if task and task['id'] not in unknown_tasks and task['state'] in ('succeeded', 'failed', 'cancelled') and old(
                row, policy['attempt_days'], 'started_at', 'finished_at') and old(task, policy['attempt_days'], 'updated_at'):
            eligible.add(('scene_task_attempts', row['id']))
    for row in rows['api_idempotency_records']:
        if utc(row['expires_at']) <= as_of:
            eligible.add(('api_idempotency_records', row['id']))
    for row in rows['assets']:
        project = projects.get(row['project_id'])
        archived = bool(project and project['deleted_at'] and utc(project['deleted_at']) <= as_of - timedelta(days=policy['asset_days']))
        if row['state'] in ('ready', 'staging', 'failed') and (row['kind'] in DERIVED_ASSET_KINDS or archived) and old(row, policy['asset_days'], 'created_at'):
            eligible.add(('assets', row['id']))
        node = ('assets', row['id'])
        path = row['storage_key']
        item = files.get(path)
        if row['state'] == 'ready' and (item is None or item['byte_size'] != row['byte_size'] or item['sha256'] != row['sha256']):
            raise ValueError(f'Asset integrity check failed: {row["id"]}')
        for file_node in file_by_asset[row['id']]:
            graph[node].add(file_node)
            graph[file_node].add(node)  # Fresh bytes protect an old row, too.
        if path in files:
            graph[node].add(('file', path))
            graph[('file', path)].add(node)
        if row['kind'] in JSON_ASSET_KINDS and item:
            if item['byte_size'] > 32 * 1024 * 1024:
                raise ValueError('JSON asset exceeds maintenance parser limit')
            try:
                payload = json.loads((root / path).read_bytes())
            except (ValueError, OSError) as exc:
                raise ValueError('JSON asset is unreadable; retention refused') from exc
            for identifier in _references(payload):
                graph[node].update(by_id.get(identifier, ()))
    cutoff_ns = int((as_of - timedelta(hours=policy['orphan_hours'])).timestamp() * 1_000_000_000)
    for path, item in files.items():
        if canonical_asset_file(path) and item['mtime_ns'] <= cutoff_ns:
            eligible.add(('file', path))
    # FK/text/JSON references include signed URLs and immutable manifest IDs.
    # Exclude row IDs themselves; never treat reference-looking text as commands.
    for node, row in list(objects.items()):
        if node[0] == 'file':
            continue
        for key, value in row.items():
            if key == 'id':
                continue
            for identifier in _references(value):
                graph[node].update(by_id.get(identifier, ()))
    for row in rows['revision_assets']:
        graph[('page_scene_revisions', row['revision_id'])].update(by_id.get(row['asset_id'], ()))
    expected_refs = defaultdict(set)
    for row in rows['revision_assets']:
        expected_refs[row['revision_id']].add((row['asset_id'], row['role']))
    for row in rows['page_scene_revisions']:
        if digest(row['scene_json']) != row['scene_hash'] or set(asset_refs(row['scene_json'])) != expected_refs.pop(row['id'], set()):
            raise ValueError('Scene hash/reference index mismatch; retention refused')
    if expected_refs:
        raise ValueError('Orphan revision reference index; retention refused')
    for row in rows['snapshot_pages']:
        graph[('deck_snapshots', row['snapshot_id'])].update(by_id.get(row['revision_id'], ()))
    protected = set(objects) - eligible
    for external in state['external_references']:
        keys = {tuple(value) for value in external['values'] if all(item is not None for item in value)}
        for row in rows[external['target']]:
            if tuple(row[column] for column in external['columns']) in keys:
                protected.add((external['target'], row['id']))
    pending = list(protected)
    while pending:
        node = pending.pop()
        for target in graph[node] - protected:
            protected.add(target)
            pending.append(target)
    return eligible - protected


def build_plan(session, asset_root, policy=None, *, as_of=None, max_entries=200000):
    if session.execute(select(SceneMaintenanceRun.id).where(SceneMaintenanceRun.status != 'completed')).first():
        raise ValueError('Unfinished retention run exists; resume it before making a new plan')
    policy = policy_values(POLICY_DEFAULTS if policy is None else policy)
    as_of = utc(as_of or datetime.now(timezone.utc))
    if as_of > datetime.now(timezone.utc) + timedelta(seconds=1):
        raise ValueError('Cleanup cutoff cannot be in the future')
    root = Path(asset_root).absolute()
    if not root.is_dir() or _unsafe(root.lstat()):
        raise ValueError('Asset root is missing or linked')
    root = root.resolve()
    state = database_state(session)
    assert_quiescent(state)
    files = inventory(root, max_entries)
    removable = _mark_graph(state, files, root, policy, as_of)
    records = {model.__tablename__: sorted(identifier for table, identifier in removable if table == model.__tablename__)
               for model in GC_MODELS}
    record_sets = {name: set(ids) for name, ids in records.items()}
    revision_ids = record_sets['page_scene_revisions']
    after = {'rows': {name: [row for row in rows if (row.get('id') not in record_sets.get(name, set()) and
        not (name == 'revision_assets' and row['revision_id'] in revision_ids))]
        for name, rows in state['rows'].items()}, 'external_references': state['external_references'],
        'schema_revisions': state['schema_revisions']}
    selected_files = sorted((files[identifier] for table, identifier in removable if table == 'file'), key=lambda item: item['path'])
    preserved = [item for path, item in sorted(files.items()) if ('file', path) not in removable]
    info = root.stat()
    plan = {'version': 1, 'as_of': as_of.isoformat(), 'policy': policy,
        'root': str(root), 'root_device': info.st_dev, 'root_inode': info.st_ino,
        'database_identity': database_identity(session), 'database_sha256': fingerprint(state), 'after_database_sha256': fingerprint(after),
        'inventory_sha256': fingerprint([files[path] for path in sorted(files)]),
        'preserved_files_sha256': fingerprint(preserved), 'records': records, 'files': selected_files,
        'summary': {'record_count': sum(map(len, records.values())), 'file_count': len(selected_files),
                    'byte_size': sum(item['byte_size'] for item in selected_files)}}
    plan['plan_sha256'] = fingerprint(plan)
    return plan
