"""Scene deployment readiness, separate from Flask's liveness check."""
import hashlib
import json
import shutil
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from flask import current_app
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from models import db
from models.scene_v1 import SceneTaskItem, SceneWorkerHeartbeat, SceneMaintenanceRun, now
from .layout_contract import validate_identity


FONT_DIR = Path(__file__).resolve().parents[2] / 'fonts'


def scene_readiness():
    checks = {'maintenance': False, 'retention_config': all(
                  type(current_app.config.get('SCENE_RETENTION_' + name)) is int and
                  1 <= current_app.config['SCENE_RETENTION_' + name] <= 36500
                  for name in ('CANDIDATE_DAYS', 'TASK_DAYS', 'ATTEMPT_DAYS', 'ASSET_DAYS', 'ORPHAN_HOURS')),
              'database': False, 'worker': False, 'asset_store': False,
              'font_manifest': False, 'renderer': False,
              'storage_config': all(type(current_app.config.get(key)) is int and current_app.config[key] > 0
                  for key in ('SCENE_STORAGE_QUOTA_BYTES', 'OWNER_STORAGE_QUOTA_BYTES',
                              'SCENE_STORAGE_LOCK_SECONDS', 'SCENE_STORAGE_MAX_ENTRIES')) and
                  type(current_app.config.get('SCENE_MIN_FREE_BYTES')) is int and current_app.config['SCENE_MIN_FREE_BYTES'] >= 0,
              'queue_config': all(type(current_app.config.get(key)) is int and current_app.config[key] > 0
                  for key in ('SCENE_QUEUE_CAPACITY_UNITS', 'OWNER_QUEUE_CAPACITY_UNITS'))}
    try:
        db.session.execute(text('SELECT 1')).scalar_one()
        threshold = now() - timedelta(seconds=current_app.config['SCENE_READY_HEARTBEAT_SECONDS'])
        checks['worker'] = bool(SceneWorkerHeartbeat.query.filter(
            SceneWorkerHeartbeat.heartbeat_at >= threshold).first() or
            SceneTaskItem.query.filter(SceneTaskItem.state == 'running',
                SceneTaskItem.lease_expires_at > now()).first())
        checks['maintenance'] = SceneMaintenanceRun.query.filter(SceneMaintenanceRun.status != 'completed').first() is None
        checks['database'] = True
    except SQLAlchemyError:
        db.session.rollback()

    configured_root = Path(current_app.config['ASSET_STORE_ROOT']).absolute()
    try:
        info = configured_root.lstat()
        is_link = configured_root.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) & 0x400)
        root = configured_root.resolve()
        if not is_link and root.is_dir() and shutil.disk_usage(root).free >= current_app.config['SCENE_MIN_FREE_BYTES']:
            with tempfile.NamedTemporaryFile(prefix='.scene-ready-', dir=root) as probe:
                probe.write(b'ready')
                probe.flush()
            checks['asset_store'] = True
    except (OSError, RuntimeError):
        pass

    try:
        manifest = json.loads((FONT_DIR / 'manifest.json').read_text(encoding='utf-8'))
        font = FONT_DIR / manifest['font_file']
        checks['font_manifest'] = (manifest['font_manifest_id'] == 'fonts-v1' and
            font.resolve().is_relative_to(FONT_DIR.resolve()) and
            hashlib.sha256(font.read_bytes()).hexdigest() == manifest['sha256'])
    except (OSError, ValueError, KeyError, TypeError):
        pass

    job_root = current_app.config.get('SCENE_RENDER_JOB_ROOT')
    if job_root:
        try:
            status_file = Path(job_root).resolve() / '.renderer_status.json'
            payload = status_file.read_bytes()
            if len(payload) <= 4096:
                status = json.loads(payload)
                identity = validate_identity(status.get('text_layout_identity'))
                age = time.time() - status['updated_at']
                checks['renderer'] = (0 <= age <= current_app.config['SCENE_READY_HEARTBEAT_SECONDS'] and
                    checks['font_manifest'] and identity['font_sha256'] == manifest['sha256'] and
                    status.get('chromium_ok') is True and status.get('libreoffice_ok') is True and
                    status.get('pptx_pdf_ok') is True and
                    (not current_app.config.get('SCENE_RENDER_UNIQUE_UID') or
                     status.get('isolation_mode') == 'unique_uid'))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return {'ready': all(checks.values()), 'checks': checks}
