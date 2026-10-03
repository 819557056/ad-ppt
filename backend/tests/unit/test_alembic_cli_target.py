"""`alembic upgrade head` must migrate the application database.

Regression: alembic.ini used to hardcode `sqlalchemy.url = sqlite:///placeholder.db`,
which migrations/env.py preferred over DATABASE_URL. The documented command (and
the launchd `alembic upgrade head && python app.py`) therefore migrated a scratch
file and left the real database un-migrated; the API then answered
`no such column: settings.image_quality` until someone migrated it by hand.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _current_head() -> str:
    config = AlembicConfig(str(BACKEND_ROOT / 'alembic.ini'))
    config.set_main_option('script_location', str(BACKEND_ROOT / 'migrations'))
    return ScriptDirectory.from_config(config).get_heads()[0]


def _run_alembic_upgrade(env: dict, target='head') -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, '-m', 'alembic', 'upgrade', target],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def cli_env(tmp_path):
    db_path = tmp_path / 'cli_target.db'
    env = {k: v for k, v in os.environ.items() if k != 'BANANA_SKIP_AUTO_MIGRATE'}
    env['DATABASE_URL'] = f'sqlite:///{db_path}'
    return db_path, env


def test_cli_migrates_database_url(cli_env):
    """The CLI must honour DATABASE_URL instead of a placeholder URL."""
    db_path, env = cli_env

    result = _run_alembic_upgrade(env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert db_path.exists(), (
        'alembic upgrade ran against a different database than DATABASE_URL'
    )

    with sqlite3.connect(db_path) as conn:
        version = conn.execute('SELECT version_num FROM alembic_version').fetchone()[0]
        columns = {row[1] for row in conn.execute("PRAGMA table_info('settings')")}

    assert version == _current_head()
    assert 'image_quality' in columns


def test_cli_is_idempotent(cli_env):
    """Re-running upgrade head on an up-to-date database is a no-op."""
    db_path, env = cli_env
    assert _run_alembic_upgrade(env).returncode == 0

    second = _run_alembic_upgrade(env)

    assert second.returncode == 0, second.stdout + second.stderr
    with sqlite3.connect(db_path) as conn:
        assert conn.execute('SELECT version_num FROM alembic_version').fetchone()[0] == _current_head()


def test_cli_creates_missing_sqlite_directory(tmp_path):
    """A fresh clone has no backend/instance directory yet; the documented
    `alembic upgrade head` must still work before the app has ever started."""
    db_path = tmp_path / 'fresh' / 'nested' / 'instance.db'
    env = {k: v for k, v in os.environ.items() if k != 'BANANA_SKIP_AUTO_MIGRATE'}
    env['DATABASE_URL'] = f'sqlite:///{db_path}'

    result = _run_alembic_upgrade(env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert db_path.exists()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute('SELECT version_num FROM alembic_version').fetchone()[0] == _current_head()


def test_explicit_config_override_still_wins(tmp_path):
    """app.py sets sqlalchemy.url programmatically (desktop/自定义库路径);
    that override must beat DATABASE_URL."""
    override_db = tmp_path / 'override.db'
    other_db = tmp_path / 'env.db'
    env = {k: v for k, v in os.environ.items() if k != 'BANANA_SKIP_AUTO_MIGRATE'}
    env['DATABASE_URL'] = f'sqlite:///{other_db}'

    result = subprocess.run(
        [
            sys.executable,
            '-c',
            (
                'from alembic import command\n'
                'from alembic.config import Config\n'
                "cfg = Config('alembic.ini')\n"
                f"cfg.set_main_option('sqlalchemy.url', 'sqlite:///{override_db.as_posix()}')\n"
                "command.upgrade(cfg, 'head')\n"
            ),
        ],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert override_db.exists()
    assert not other_db.exists(), 'the explicit override must take precedence'
    with sqlite3.connect(override_db) as conn:
        assert conn.execute('SELECT version_num FROM alembic_version').fetchone()[0] == _current_head()


def test_legacy_template_boolean_backfill_remains_sqlite_compatible(cli_env, tmp_path):
    from uuid import uuid4
    db_path, env = cli_env
    before = _run_alembic_upgrade(env, '018_add_project_title')
    assert before.returncode == 0, before.stdout + before.stderr
    source = tmp_path / 'existing-template.png'
    source.write_bytes(b'existing-template-bytes')
    project_id, page_id = str(uuid4()), str(uuid4())
    with sqlite3.connect(db_path) as connection:
        connection.execute("INSERT INTO projects (id, creation_type, status, created_at, updated_at, "
            "template_image_path, template_style) VALUES (?, 'idea', 'DRAFT', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?)",
            (project_id, str(source), 'Keep legacy style'))
        connection.execute("INSERT INTO pages (id, project_id, order_index, status, created_at, updated_at) "
            "VALUES (?, ?, 0, 'DRAFT', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)", (page_id, project_id))
    upgraded = _run_alembic_upgrade(env)
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    with sqlite3.connect(db_path) as connection:
        row = connection.execute('SELECT a.user_edited_analysis, a.image_path, p.template_style_text '
            'FROM pages p JOIN project_template_assets a ON a.id=p.template_asset_id WHERE p.id=?', (page_id,)).fetchone()
        assert row == (0, str(source), 'Keep legacy style')
        assert connection.execute('SELECT editor_mode FROM projects WHERE id=?', (project_id,)).fetchone() == ('legacy_image',)
    assert source.read_bytes() == b'existing-template-bytes'


def test_scene_trace_migration_preserves_existing_task_and_attempt(cli_env):
    from uuid import uuid4
    db_path, env = cli_env
    result = _run_alembic_upgrade(env, '02f8479a3c61')
    assert result.returncode == 0, result.stdout + result.stderr
    project, task, attempt = [str(uuid4()) for _ in range(3)]
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO projects (id, creation_type, status, created_at, updated_at) "
            "VALUES (?, 'idea', 'DRAFT', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)", (project,))
        conn.execute("INSERT INTO scene_task_items (id, owner_id, project_id, operation, resource_id, input_json, "
            "input_hash, state, attempt_count, fence_token, result_json, created_at, updated_at) "
            "VALUES (?, '00000000-0000-4000-8000-000000000001', ?, 'export', ?, '{}', ?, 'failed', 1, 1, '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (task, project, str(uuid4()), 'a'*64))
        conn.execute("INSERT INTO scene_task_attempts (id, task_item_id, attempt_no, fence_token, worker_id, started_at, dispatch_state, usage_json) "
            "VALUES (?, ?, 1, 1, 'test', CURRENT_TIMESTAMP, 'resolved', '{}')", (attempt, task))
    result = _run_alembic_upgrade(env)
    assert result.returncode == 0, result.stdout + result.stderr
    with sqlite3.connect(db_path) as conn:
        assert conn.execute('SELECT request_id, retry_request_id, queued_at, state FROM scene_task_items').fetchone() == (task, None, None, 'failed')
        assert conn.execute('SELECT request_id, queue_wait_seconds FROM scene_task_attempts').fetchone() == (task, None)
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        assert conn.execute('SELECT count(*) FROM scene_metrics').fetchone() == (0,)
