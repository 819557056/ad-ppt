"""Offline drift checks and executable maintenance guards (no Docker daemon).

The Linux-only cases run real Bash/sha256sum against a recording fake Docker CLI;
they verify fail-closed ordering, not the actual container build or Office render.
"""
import copy
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('scene_runtime_verify', ROOT / 'deployment/scene/verify_runtime.py')
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


@pytest.fixture
def checkout(tmp_path):
    lock = runtime.load_lock(ROOT)
    for relative in [*lock['files'], runtime.LOCK_PATH, 'docker-compose.scene.yml']:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    return tmp_path


@pytest.mark.parametrize('component', [None, 'backend', 'frontend', 'renderer'])
def test_reviewed_runtime_inputs_verify(component):
    runtime.verify(ROOT, component)


@pytest.mark.parametrize('relative', [
    'uv.lock', 'pyproject.toml', 'frontend/package-lock.json', 'renderer/package-lock.json',
    'renderer/requirements.lock.txt', 'renderer/smoke.pptx', 'backend/fonts/NotoSansSC-Regular.ttf',
    'deployment/scene/ubuntu.sources',
])
def test_modified_dependency_fails_closed(checkout, relative):
    path = checkout / relative
    path.write_bytes(path.read_bytes() + b'changed')
    with pytest.raises(runtime.RuntimeMismatch, match='Locked input changed'):
        runtime.verify(checkout)


def test_missing_smoke_input_refuses_renderer_build(checkout):
    (checkout / 'renderer/smoke.pptx').unlink()
    with pytest.raises(runtime.RuntimeMismatch, match='Missing or escaping'):
        runtime.verify(checkout, 'renderer')


def test_crlf_does_not_change_text_input_identity(checkout):
    lock = runtime.load_lock(checkout)
    for relative, item in lock['files'].items():
        if item['encoding'] == 'lf':
            path = checkout / relative
            path.write_bytes(path.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', b'\r\n'))
    assert runtime.fingerprint(runtime.verify(checkout)) == runtime.fingerprint(lock)


def test_image_drift_detected_even_when_dockerfile_hash_is_updated(checkout):
    path = checkout / 'renderer/Dockerfile'
    path.write_text(path.read_text().replace('mcr.microsoft.com/playwright:', 'other.example/playwright:'))
    lock = runtime.load_lock(checkout)
    lock['files']['renderer/Dockerfile']['sha256'] = runtime.sha256(path.read_bytes().replace(b'\r\n', b'\n'))
    (checkout / runtime.LOCK_PATH).write_text(json.dumps(lock))
    with pytest.raises(runtime.RuntimeMismatch, match='Docker image drift'):
        runtime.verify(checkout)


@pytest.mark.parametrize('change', ['postgres:16.6', 'postgres:latest'])
def test_unpinned_compose_image_rejected(checkout, change):
    path = checkout / 'docker-compose.scene.yml'
    pinned = runtime.load_lock(checkout)['images']['postgres']['image']
    path.write_text(path.read_text().replace(pinned, change))
    with pytest.raises(runtime.RuntimeMismatch, match='Compose image drift'):
        runtime.verify(checkout)


def test_legacy_build_target_rejected(checkout):
    path = checkout / 'docker-compose.scene.yml'
    path.write_text(path.read_text().replace('backend/Dockerfile.scene', 'backend/Dockerfile'))
    with pytest.raises(runtime.RuntimeMismatch, match='Compose build target drift'):
        runtime.verify(checkout)


@pytest.mark.parametrize('relative', ['../outside', '/absolute', 'C:/secret', 'back\\slash'])
def test_lock_cannot_read_outside_checkout(checkout, relative):
    lock = runtime.load_lock(checkout)
    lock['files'][relative] = copy.deepcopy(lock['files']['uv.lock'])
    (checkout / runtime.LOCK_PATH).write_text(json.dumps(lock))
    with pytest.raises(runtime.RuntimeMismatch, match='Invalid locked file'):
        runtime.load_lock(checkout)


def test_renderer_npm_lock_is_minimal_and_registry_only():
    package = json.loads((ROOT / 'renderer/package.json').read_text())
    lock = json.loads((ROOT / 'renderer/package-lock.json').read_text())
    assert package['dependencies'] == {'playwright': '1.57.0'}
    assert set(lock['packages']) == {'', 'node_modules/playwright', 'node_modules/playwright-core', 'node_modules/fsevents'}
    for name, item in lock['packages'].items():
        if name:
            assert item['resolved'].startswith('https://registry.npmjs.org/')
            assert item['integrity'].startswith('sha512-')
            assert not item.get('link')


@pytest.mark.parametrize('field', ['playwright', 'chromium_revision', 'chromium_version'])
def test_browser_version_checks_actual_runtime(field):
    expected = runtime.load_lock(ROOT)['tools']['browser']
    runtime.check_browser(dict(expected), expected)
    actual = dict(expected, **{field: 'unexpected'})
    with pytest.raises(runtime.RuntimeMismatch, match='Installed Playwright'):
        runtime.check_browser(actual, expected)


def test_worker_does_not_inherit_the_api_http_healthcheck():
    compose = (ROOT / 'docker-compose.scene.yml').read_text()
    worker = compose.split('  worker:\n', 1)[1].split('  renderer:\n', 1)[0]
    assert 'healthcheck:\n      disable: true' in worker


def test_no_source_build_or_dependency_install_at_startup():
    backend = (ROOT / 'backend/Dockerfile.scene').read_text()
    renderer = (ROOT / 'renderer/Dockerfile').read_text()
    frontend = (ROOT / 'frontend/Dockerfile.scene').read_text()
    assert 'uv sync --locked --no-dev --no-install-project --no-build' in backend
    assert '--require-hashes --only-binary=:all:' in renderer
    for content in (backend, renderer, frontend):
        cmd = next(line for line in content.splitlines() if line.startswith('CMD '))
        assert not any(term in cmd for term in ('uv ', 'pip ', 'npm ', 'npx '))
        assert '|| npm' not in content
        assert 'playwright install' not in content
    assert 'npm ci --ignore-scripts' in renderer and 'npm ci --ignore-scripts' in frontend
    assert 'smoke_runtime()' in renderer


def test_runtime_record_uses_measured_packages_and_no_timestamp(monkeypatch):
    class Distribution:
        metadata = {'Name': 'example-package'}
        version = '1.0'
    monkeypatch.setattr(runtime.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(runtime.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(runtime.platform, 'python_version', lambda: '3.12.15')
    monkeypatch.setattr(runtime.importlib.metadata, 'distributions', lambda: [Distribution()])
    def execute(args, cwd):
        return 'uv 0.12.22' if args[0] == 'uv' else 'zlib\t1.2\ncurl\t8.0'
    monkeypatch.setattr(runtime, 'run', execute)
    measured = runtime.record(ROOT, 'backend')
    assert measured == runtime.record(ROOT, 'backend')
    assert measured['python_packages'] == [{'name': 'example-package', 'version': '1.0'}]
    assert measured['system_packages'] == ['curl\t8.0', 'zlib\t1.2']
    monkeypatch.setattr(runtime.platform, 'python_version', lambda: '3.13.0')
    with pytest.raises(runtime.RuntimeMismatch, match='Python version'):
        runtime.record(ROOT, 'backend')


LINUX = pytest.mark.skipif(platform.system() != 'Linux', reason='Requires Linux Bash/sha256sum; also run in isolated WSL QA')


@LINUX
@pytest.mark.parametrize('corruption', [None, 'changed', 'missing', 'extra'])
def test_apt_script_checks_exact_release_set(tmp_path, corruption):
    lists = tmp_path / 'lists'
    lists.mkdir()
    data = [b'signed release one', b'signed release two']
    for index, content in enumerate(data):
        (lists / f'{index}_InRelease').write_bytes(content)
    expected = tmp_path / 'expected'
    expected.write_text('\n'.join(sorted(hashlib.sha256(item).hexdigest() for item in data)) + '\n')
    if corruption == 'changed':
        (lists / '0_InRelease').write_bytes(b'tampered')
    elif corruption == 'missing':
        (lists / '0_InRelease').unlink()
    elif corruption == 'extra':
        (lists / 'unexpected_InRelease').write_bytes(b'extra source')
    result = subprocess.run(['bash', str(ROOT / 'deployment/scene/verify-apt.sh'), str(expected), str(lists)], capture_output=True)
    assert (result.returncode == 0) == (corruption is None)


@LINUX
@pytest.mark.parametrize('name', ['scene-backup.sh', 'scene-restore.sh', 'scene-runtime-common.sh', 'scene-retention.sh'])
def test_maintenance_shell_syntax(name):
    subprocess.run(['bash', '-n', str(ROOT / 'scripts' / name)], check=True)


FAKE_DOCKER = r'''import json, os, pathlib, shutil, sys
args = sys.argv[1:]
fixture = pathlib.Path(os.environ['FIXTURE'])
with (fixture / 'calls.jsonl').open('a') as log:
    log.write(json.dumps(args) + '\n')
if args[0] == 'cp':
    shutil.copyfile(fixture / 'container.runtime', args[2])
elif args[0] == 'inspect':
    print((fixture / 'postgres.ref').read_text().strip() if args[2] == '{{.Config.Image}}' else 'sha256:'+'a'*64)
elif args[0] == 'compose':
    args = args[5:]
    if args[0] == 'version':
        print('Fake compose')
    elif args[0] == 'ps':
        if os.environ.get('FAIL_PS') == '1':
            sys.exit(1)
        if '--status' in args:
            print('postgres')
        elif 'postgres' in args:
            print('a'*64)
        elif os.environ.get('EXISTING') == '1':
            print('b'*64)
    elif args[0] == 'run':
        entry = args[args.index('--entrypoint')+1]
        if entry == '/bin/cat':
            service = args[args.index('--entrypoint')+2]
            path = fixture / ('runtime.lock.json' if args[-1].endswith('runtime.lock.json') else service+'.runtime')
            sys.stdout.write(path.read_text())
        elif 'ops.scene_asset_audit' in args[-1]:
            print('{}')
    elif args[0] == 'exec':
        if 'postgres' == args[-2] and '--version' == args[-1]:
            print('postgres (PostgreSQL) 16.6')
        elif 'psql' in args:
            print('0')
'''


@pytest.fixture
def maintenance(tmp_path):
    root = tmp_path / 'repo'
    for relative in ['scripts/scene-backup.sh', 'scripts/scene-restore.sh', 'scripts/scene-runtime-common.sh',
                     'deployment/scene/runtime.lock.json', 'backend/fonts/manifest.json', 'docker-compose.scene.yml']:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    fixture = tmp_path / 'fixture'
    fixture.mkdir()
    for role in ('backend', 'worker', 'renderer', 'frontend'):
        (fixture / f'{role}.runtime').write_text(f'{role} stable measured runtime\n')
    shutil.copyfile(ROOT / runtime.LOCK_PATH, fixture / 'runtime.lock.json')
    (fixture / 'postgres.ref').write_text(runtime.load_lock(ROOT)['images']['postgres']['image'])
    binary = tmp_path / 'bin'
    binary.mkdir()
    docker = binary / 'docker'
    docker.write_text('#!' + sys.executable + '\n' + FAKE_DOCKER)
    docker.chmod(0o700)
    env = dict(os.environ, FIXTURE=str(fixture), PATH=str(binary) + os.pathsep + os.environ['PATH'],
               SCENE_RESTORE_CONFIRM='restore-into-empty-stack')
    return root, fixture, env


def capture(maintenance, output):
    root, fixture, env = maintenance
    source = 'root=$1; dc=(docker compose --project-directory "$root" -f "$root/docker-compose.scene.yml"); source "$root/scripts/scene-runtime-common.sh"; scene_capture_runtime "$2"'
    return subprocess.run(['bash', '-euc', source, '_', str(root), str(output)], env=env, capture_output=True, text=True)


@LINUX
def test_capture_compares_existing_containers_not_only_new_image_tags(maintenance, tmp_path):
    root, fixture, env = maintenance
    assert capture(maintenance, tmp_path / 'good').returncode == 0
    env['EXISTING'] = '1'
    (fixture / 'container.runtime').write_text('old runtime\n')
    result = capture(maintenance, tmp_path / 'bad')
    assert result.returncode != 0
    assert 'Existing backend runtime differs' in result.stderr


@LINUX
@pytest.mark.parametrize('mismatch', [None, 'renderer', 'missing', 'postgres', 'source_lock'])
def test_restore_runtime_guards_precede_every_database_write(maintenance, tmp_path, mismatch):
    root, fixture, env = maintenance
    backup = tmp_path / 'backup'
    backup.mkdir()
    assert capture(maintenance, backup / 'runtime').returncode == 0
    if mismatch == 'renderer':
        (fixture / 'renderer.runtime').write_text('new rendering engine\n')
    elif mismatch == 'missing':
        (backup / 'runtime/renderer.runtime').unlink()
    elif mismatch == 'postgres':
        (fixture / 'postgres.ref').write_text('postgres:latest')
    elif mismatch == 'source_lock':
        (root / runtime.LOCK_PATH).write_text('{}')
    for name in ('COMPLETE', 'database.dump', 'scene-assets.tar', 'legacy-uploads.tar'):
        (backup / name).write_text('fixture\n')
    (backup / 'scene-audit.json').write_text('{}\n')
    shutil.copyfile(ROOT / 'backend/fonts/manifest.json', backup / 'font-manifest.json')
    entries = [f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(backup)}' for p in backup.rglob('*') if p.is_file()]
    (backup / 'SHA256SUMS').write_text('\n'.join(entries) + '\n')
    result = subprocess.run(['bash', str(root / 'scripts/scene-restore.sh'), str(backup)], env=env, capture_output=True, text=True)
    calls = [json.loads(line) for line in (fixture / 'calls.jsonl').read_text().splitlines()]
    writes = [args for args in calls if 'pg_restore' in args or any('UPDATE scene_task_items' in arg for arg in args)]
    if mismatch:
        assert result.returncode != 0
        assert not writes
        assert 'refused' in result.stderr or 'locks differ' in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert len(writes) == 2


@LINUX
def test_capture_does_not_silently_ignore_container_listing_failure(maintenance, tmp_path):
    _, _, env = maintenance
    env['FAIL_PS'] = '1'
    result = capture(maintenance, tmp_path / 'failed')
    assert result.returncode != 0


@LINUX
def test_backup_rejects_mixed_checkout_before_dumping_database(maintenance, tmp_path):
    root, fixture, env = maintenance
    (root / runtime.LOCK_PATH).write_text('{}')
    env['SCENE_MAINTENANCE_CONFIRMED'] = 'yes'
    output = tmp_path / 'incomplete-backup'
    result = subprocess.run(['bash', str(root / 'scripts/scene-backup.sh'), str(output)], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'locks differ' in result.stderr
    assert not (output / 'COMPLETE').exists()
    calls = [json.loads(line) for line in (fixture / 'calls.jsonl').read_text().splitlines()]
    assert not any('pg_dump' in args for args in calls)


def test_engine_manifest_changes_when_our_converter_changes(tmp_path):
    for relative in runtime.engine_sources(ROOT, 'renderer'):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, path)
    previous = runtime.engine_sources(tmp_path, 'renderer')
    script = tmp_path / 'renderer/render_pdf.mjs'
    script.write_text(script.read_text() + '\n// Changed converter algorithm.\n')
    assert runtime.engine_sources(tmp_path, 'renderer') != previous
    script.unlink()
    with pytest.raises(OSError):
        runtime.engine_sources(tmp_path, 'renderer')
