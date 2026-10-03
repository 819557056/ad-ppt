"""Offline Scene retention: review a plan, prune records, then resume-safe files.

Stop every API, worker, renderer and external writer first. No automatic cleanup
runs inside the web application. A database journal is committed BEFORE unlink.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import sys

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from models.scene_v1 import RevisionAsset, SceneMaintenanceRun
from services.scene.retention import (GC_MODELS, POLICY_DEFAULTS, build_plan,
    canonical_asset_file, database_identity, database_state, fingerprint, inventory, utc)
from services.scene.storage_quota import _unsafe, volume_lock
from services.scene.errors import SceneError


@contextmanager
def locked_database(connection):
    """Database locks precede the volume lock in every phase (including resume)."""
    dialect = connection.dialect.name
    try:
        if dialect == 'sqlite':
            connection.exec_driver_sql('PRAGMA foreign_keys=ON')
            connection.exec_driver_sql('PRAGMA busy_timeout=10000')
            connection.commit()
            connection.exec_driver_sql('BEGIN EXCLUSIVE')
        elif dialect == 'postgresql':
            connection.execute(text("SET LOCAL lock_timeout = '10s'"))
            clients = connection.execute(text("SELECT count(*) FROM pg_stat_activity "
                "WHERE datname=current_database() AND backend_type='client backend' "
                "AND pid<>pg_backend_pid()")).scalar_one()
            if clients:
                raise ValueError('Other database clients remain; keep services stopped')
            quote = connection.dialect.identifier_preparer.quote
            tables = inspect(connection).get_table_names()
            connection.exec_driver_sql('LOCK TABLE ' + ', '.join(quote(name) for name in sorted(tables)) +
                                       ' IN ACCESS EXCLUSIVE MODE')
        else:
            raise ValueError('Only PostgreSQL and explicitly offline SQLite are supported')
        with Session(bind=connection, expire_on_commit=False) as session:
            yield session
    finally:
        if connection.in_transaction():
            connection.rollback()


def _root(asset_root):
    root = Path(asset_root).absolute()
    if not root.is_dir() or _unsafe(root.lstat()):
        raise ValueError('Asset root is missing or linked')
    return root.resolve()


def _verify_plan(plan, root, session):
    if not isinstance(plan, dict) or plan.get('version') != 1:
        raise ValueError('Unsupported retention plan')
    if fingerprint({key: value for key, value in plan.items() if key != 'plan_sha256'}) != plan.get('plan_sha256'):
        raise ValueError('Retention plan hash mismatch')
    required = {'root', 'root_device', 'root_inode', 'database_identity', 'database_sha256',
                'after_database_sha256', 'preserved_files_sha256', 'inventory_sha256',
                'as_of', 'policy', 'records', 'files', 'summary'}
    if not required <= set(plan):
        raise ValueError('Retention plan is incomplete')
    info = root.stat()
    if (plan['root'], plan['root_device'], plan['root_inode']) != (str(root), info.st_dev, info.st_ino):
        raise ValueError('Retention asset root identity changed')
    if plan['database_identity'] != database_identity(session):
        raise ValueError('Retention database identity changed')


def _batches(values, size=400):
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def _prune_records(session, plan):
    """Delete children before parents, without disabling FK validation."""
    for model in GC_MODELS:
        table = model.__table__
        ids = plan['records'][table.name]
        if not ids:
            continue
        if table.name == 'page_scene_revisions':
            for batch in _batches(ids):
                session.execute(RevisionAsset.__table__.delete().where(RevisionAsset.revision_id.in_(batch)))
        parent_column = {'assets': 'source_asset_id', 'page_scene_revisions': 'parent_revision_id'}.get(table.name)
        if parent_column:
            remaining = set(ids)
            rows = session.execute(select(table.c.id, table.c[parent_column])).all()
            parents = {identifier: parent for identifier, parent in rows if identifier in remaining and parent in remaining}
            child_counts = {identifier: 0 for identifier in remaining}
            for parent in parents.values():
                child_counts[parent] += 1
            leaves = sorted(identifier for identifier, count in child_counts.items() if count == 0)
            ordered = []
            while leaves:
                identifier = leaves.pop()
                ordered.append(identifier)
                parent = parents.get(identifier)
                if parent is not None:
                    child_counts[parent] -= 1
                    if child_counts[parent] == 0:
                        leaves.append(parent)
            if len(ordered) != len(ids):
                raise ValueError('Cyclic retained-record dependency; cleanup refused')
            layers = [ordered]
        else:
            layers = [ids]
        for layer in layers:
            for batch in _batches(layer):
                result = session.execute(table.delete().where(table.c.id.in_(batch)))
                if result.rowcount != len(batch):
                    raise ValueError('Retention record count changed')


def _sync_directory(path):
    if os.name == 'posix':
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _remove_file(root, item):
    """Single-file unlink only. Never recurse or remove unknown directory trees."""
    relative = item['path']
    if canonical_asset_file(relative) is None:
        raise ValueError('Refusing noncanonical cleanup path')
    path = root / relative
    cursor = root
    for part in Path(relative).parts[:-1]:
        cursor /= part
        if not cursor.exists():
            if cursor.is_symlink():
                raise ValueError('Cleanup directory became a link')
            return
        info = cursor.lstat()
        if _unsafe(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError('Cleanup directory is no longer private')
    if not path.exists() and not path.is_symlink():
        return
    info = path.lstat()
    if _unsafe(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not path.resolve().is_relative_to(root):
        raise ValueError('Cleanup file is no longer private')
    from services.scene.retention import _sha256
    if info.st_size != item['byte_size'] or info.st_mtime_ns != item['mtime_ns'] or _sha256(path) != item['sha256']:
        raise ValueError('Cleanup file changed; resume refused')
    path.unlink()
    _sync_directory(path.parent)
    # Only the now-empty per-asset directory; keep owner/project structure.
    try:
        path.parent.rmdir()
    except OSError:
        pass
    else:
        _sync_directory(path.parent.parent)


def _receipt(run):
    return {'version': 1, 'run_id': run.id, 'plan_sha256': run.plan_sha256,
        'status': run.status, 'summary': run.plan_json['summary'],
        'completed_at': utc(run.completed_at).isoformat() if run.completed_at else None}


def _finish(connection, root, run_id, *, max_entries, lock_seconds):
    with locked_database(connection) as session:
        run = session.get(SceneMaintenanceRun, run_id)
        if run is None:
            raise ValueError('Retention run not found')
        if run.status == 'completed':
            return _receipt(run)  # Idempotent receipt retrieval; never unlink again.
        if run.status != 'db_pruned':
            raise ValueError('Unsupported retention journal state')
        plan = run.plan_json
        _verify_plan(plan, root, session)
        if run.plan_sha256 != plan['plan_sha256'] or run.after_database_sha256 != plan['after_database_sha256']:
            raise ValueError('Retention journal mismatch')
        with volume_lock(root, timeout=lock_seconds):
            if fingerprint(database_state(session)) != run.after_database_sha256:
                raise ValueError('Database changed since pruning; resume refused')
            files = inventory(root, max_entries)
            expected = {item['path']: item for item in plan['files']}
            preserved = [item for path, item in sorted(files.items()) if path not in expected]
            if fingerprint(preserved) != plan['preserved_files_sha256']:
                raise ValueError('Preserved asset files changed; resume refused')
            for path, item in expected.items():
                if path in files and files[path] != item:
                    raise ValueError('Cleanup file changed; resume refused')
            for item in plan['files']:
                _remove_file(root, item)
            run.status = 'completed'
            run.completed_at = datetime.now(timezone.utc)
            session.flush()
            receipt = _receipt(run)
            connection.commit()
            return receipt


def maintain(database_url, asset_root, operation='plan', *, offline_confirmed=False,
             plan=None, confirm_sha256=None, run_id=None, policy=None,
             max_entries=200000, lock_seconds=10):
    if offline_confirmed is not True:
        raise ValueError('Explicit offline maintenance confirmation is required')
    if type(max_entries) is not int or max_entries < 1:
        raise ValueError('Invalid maintenance inventory limit')
    if operation not in ('plan', 'apply', 'resume'):
        raise ValueError('Unsupported retention operation')
    root = _root(asset_root)
    engine = create_engine(database_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            if operation == 'resume':
                return _finish(connection, root, run_id, max_entries=max_entries, lock_seconds=lock_seconds)
            with locked_database(connection) as session:
                with volume_lock(root, timeout=lock_seconds):
                    if operation == 'plan':
                        return build_plan(session, root, policy, max_entries=max_entries)
                    _verify_plan(plan, root, session)
                    if confirm_sha256 != plan['plan_sha256']:
                        raise ValueError('Confirm the exact reviewed plan SHA-256')
                    rebuilt = build_plan(session, root, plan['policy'], as_of=plan['as_of'], max_entries=max_entries)
                    if rebuilt != plan:
                        raise ValueError('Retention plan is stale; regenerate and review it')
                    _prune_records(session, plan)
                    if fingerprint(database_state(session)) != plan['after_database_sha256']:
                        raise ValueError('Post-prune database state differs; rolling back')
                    run = SceneMaintenanceRun(plan_sha256=plan['plan_sha256'], plan_json=plan,
                        after_database_sha256=plan['after_database_sha256'], status='db_pruned')
                    session.add(run)
                    session.flush()
                    run_id = run.id
                    # Durable journal and pruning share ONE commit. No unlink yet.
                    connection.commit()
            # Reacquire in the SAME DB -> file order. Any drift fails closed.
            return _finish(connection, root, run_id, max_entries=max_entries, lock_seconds=lock_seconds)
    finally:
        engine.dispose()


def _output_path(value, root):
    path = Path(value).absolute()
    if path.resolve().is_relative_to(root):
        raise ValueError('Plans and receipts must be outside the asset root')
    if path.exists() or path.is_symlink() or not path.parent.is_dir():
        raise ValueError('Output needs an existing parent and a new filename')
    return path


def write_private_json(path, value):
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-url', default=os.environ.get('DATABASE_URL'))
    parser.add_argument('--asset-root', default=os.environ.get('ASSET_STORE_ROOT'))
    parser.add_argument('--offline-confirmed', action='store_true',
        default=os.environ.get('SCENE_MAINTENANCE_CONFIRMED') == 'yes')
    sub = parser.add_subparsers(dest='operation', required=True)
    preview = sub.add_parser('plan')
    preview.add_argument('--output', required=True)
    for name, default in POLICY_DEFAULTS.items():
        preview.add_argument('--' + name.replace('_', '-'), type=int,
            default=os.environ.get('SCENE_RETENTION_' + name.upper(), str(default)))
    apply = sub.add_parser('apply')
    apply.add_argument('--plan', required=True, type=Path)
    apply.add_argument('--confirm-sha256', required=True)
    apply.add_argument('--receipt', required=True)
    resume = sub.add_parser('resume')
    resume.add_argument('--run-id', required=True)
    resume.add_argument('--receipt', required=True)
    args = parser.parse_args()
    if not args.database_url or not args.asset_root:
        parser.error('DATABASE_URL and ASSET_STORE_ROOT are required')
    try:
        root = _root(args.asset_root)
        output = _output_path(args.output if args.operation == 'plan' else args.receipt, root)
        kwargs = {'offline_confirmed': args.offline_confirmed,
            'max_entries': int(os.environ.get('SCENE_STORAGE_MAX_ENTRIES', '200000')),
            'lock_seconds': int(os.environ.get('SCENE_STORAGE_LOCK_SECONDS', '10'))}
        if args.operation == 'plan':
            kwargs['policy'] = {name: getattr(args, name) for name in POLICY_DEFAULTS}
        elif args.operation == 'apply':
            if args.plan.resolve().is_relative_to(root) or args.plan.stat().st_size > 256 * 1024 * 1024:
                raise ValueError('Plan must be bounded and outside the asset root')
            kwargs.update(plan=json.loads(args.plan.read_text(encoding='utf-8')), confirm_sha256=args.confirm_sha256)
        else:
            kwargs['run_id'] = args.run_id
        result = maintain(args.database_url, root, args.operation, **kwargs)
        write_private_json(output, result)
        print(json.dumps({key: result[key] for key in ('plan_sha256', 'run_id', 'status', 'summary') if key in result}))
        return 0
    except (ValueError, OSError, SQLAlchemyError, SceneError) as exc:
        # SQLAlchemy can include query parameters; never dump credentials or SQL.
        detail = str(exc) if isinstance(exc, ValueError) else (exc.code if isinstance(exc, SceneError) else type(exc).__name__)
        print('Retention refused or interrupted: ' + detail +
            '. Keep services stopped; inspect scene_maintenance_runs and resume any db_pruned run.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
