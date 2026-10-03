"""Read-only operator exporter: python -m ops.scene_metrics [--output metrics.prom].

Uses DATABASE_URL and ASSET_STORE_ROOT, never the application startup/migration
path or a public endpoint. Labels contain no owner/project/task/credential IDs.
"""
import argparse
from collections import defaultdict
import math
import os
from pathlib import Path
import shutil
import tempfile

from flask import Flask
from sqlalchemy import create_engine, select, func

from models.scene_v1 import SceneMetric, SceneTaskItem
from services.scene.telemetry import METRICS, OPERATIONS, STAGES, OUTCOMES, SOURCES, BUCKETS
from services.scene.storage_quota import _scan, _unsafe, volume_lock


def render_metrics(engine, asset_root, *, max_entries=200000):
    root = Path(asset_root).absolute()
    if not root.is_dir() or _unsafe(root.lstat()) or type(max_entries) is not int or max_entries < 1:
        raise ValueError('Invalid metrics storage configuration')
    families = {'scene_http_requests_total': 'counter', 'scene_task_transitions_total': 'counter',
                'scene_retries_total': 'counter', 'scene_version_conflicts_total': 'counter',
                'scene_render_failures_total': 'counter', 'scene_stage_duration_seconds': 'histogram',
                'scene_queue_wait_seconds': 'histogram', 'scene_tasks': 'gauge',
                'scene_asset_storage_bytes': 'gauge', 'scene_asset_storage_files': 'gauge',
                'scene_disk_free_bytes': 'gauge', 'scene_disk_used_bytes': 'gauge',
                'scene_disk_total_bytes': 'gauge', 'scene_metrics_scrape_success': 'gauge'}
    lines = [f'# TYPE {name} {kind}' for name, kind in families.items()]
    with engine.connect() as connection:
        for row in connection.execute(select(SceneMetric.__table__)).mappings():
            if (row['metric'] not in METRICS or row['operation'] not in OPERATIONS or row['stage'] not in STAGES
                    or row['outcome'] not in OUTCOMES or row['source'] not in SOURCES
                    or row['bucket'] not in ('', *[str(value) for value in BUCKETS])
                    or not math.isfinite(row['value']) or row['value'] < 0):
                raise ValueError('Invalid persisted metric')
            labels = ','.join(f'{key}="{row[key]}"' for key in ('operation', 'stage', 'outcome', 'source'))
            if row['bucket']:
                labels += ',le="' + ('+Inf' if row['bucket'] == 'inf' else row['bucket']) + '"'
            lines.append(f'{row["metric"]}{{{labels}}} {row["value"]:.12g}')
        tasks = defaultdict(int)
        for operation, state, count in connection.execute(select(SceneTaskItem.operation, SceneTaskItem.state,
                func.count()).group_by(SceneTaskItem.operation, SceneTaskItem.state)):
            tasks[(operation if operation in OPERATIONS else 'worker', state if state in OUTCOMES else 'other')] += count
        for (operation, state), value in sorted(tasks.items()):
            lines.append(f'scene_tasks{{operation="{operation}",state="{state}"}} {value}')
    # Release the DB connection before the volume lock: same lock order as writers.
    context = Flask('scene_metrics_storage')
    context.config['SCENE_STORAGE_MAX_ENTRIES'] = max_entries
    with context.app_context(), volume_lock(root, timeout=1):
        usage = _scan(root, '00000000-0000-0000-0000-000000000000')
        disk = shutil.disk_usage(root)
    lines += [f'scene_asset_storage_bytes {usage["global_bytes"]}',
              f'scene_asset_storage_files {usage["global_files"]}',
              f'scene_disk_free_bytes {disk.free}', f'scene_disk_used_bytes {disk.used}',
              f'scene_disk_total_bytes {disk.total}', 'scene_metrics_scrape_success 1']
    return '\n'.join(sorted(lines)) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    url, root = os.getenv('DATABASE_URL'), os.getenv('ASSET_STORE_ROOT')
    if not url or not root:
        parser.error('DATABASE_URL and ASSET_STORE_ROOT are required')
    engine = None
    try:
        # URL parsing/import failures may contain credentials too. Keep engine
        # construction under the same redacted boundary as queries and I/O.
        engine = create_engine(url, pool_pre_ping=True)
        payload = render_metrics(engine, root, max_entries=int(os.getenv('SCENE_STORAGE_MAX_ENTRIES', '200000')))
        if args.output:
            target = args.output.absolute()
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                    dir=target.parent, prefix='.scene-metrics-', delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                except BaseException:
                    stream.close()
                    temporary.unlink(missing_ok=True)
                    raise
            try:
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        else:
            print(payload, end='')
    except Exception:
        parser.exit(1, 'Scene metrics scrape failed; no valid snapshot published.\n')
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass  # Cleanup must not replace the safe diagnostic above.


if __name__ == '__main__':
    main()
