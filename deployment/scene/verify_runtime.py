"""Offline Scene build-input verifier and measured Linux runtime manifest.

No network, package installation, environment dump, or lock regeneration. Text
inputs normalize CRLF so Windows and Linux checkouts have the same identity.
Changing a dependency requires explicit review and updating runtime.lock.json.
"""
import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess
from pathlib import Path, PurePosixPath

COMPONENTS = ('backend', 'frontend', 'renderer')
LOCK_PATH = 'deployment/scene/runtime.lock.json'
DIGEST = re.compile(r'[0-9a-f]{64}')


class RuntimeMismatch(ValueError):
    """The build cannot be identified by the reviewed dependency lock."""


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def load_lock(root):
    lock = json.loads((Path(root) / LOCK_PATH).read_text(encoding='utf-8'))
    if lock.get('schema_version') != 1:
        raise RuntimeMismatch('Unsupported runtime lock schema')
    for name, image in lock['images'].items():
        if not re.fullmatch(r'[^\s@$]+@sha256:[0-9a-f]{64}', image['image']):
            raise RuntimeMismatch(f'Unpinned image: {name}')
        for arch in ('amd64', 'arm64'):
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', image['platforms'][arch]):
                raise RuntimeMismatch(f'Unpinned platform: {name}/{arch}')
    for relative, item in lock['files'].items():
        path = PurePosixPath(relative)
        if (path.is_absolute() or '..' in path.parts or '\\' in relative or ':' in relative
                or not DIGEST.fullmatch(item['sha256'])
                or item['encoding'] not in ('binary', 'lf')
                or not item['components']
                or any(role not in COMPONENTS for role in item['components'])):
            raise RuntimeMismatch('Invalid locked file entry')
    return lock


def fingerprint(lock):
    return sha256(canonical_bytes(lock))


def verify(root, component=None):
    root = Path(root).resolve()
    lock = load_lock(root)
    for relative, item in lock['files'].items():
        if component and component not in item['components']:
            continue
        path = root / relative
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise RuntimeMismatch(f'Missing or escaping locked input: {relative}')
        data = path.read_bytes()
        if item['encoding'] == 'lf':
            data = data.replace(b'\r\n', b'\n')
        if sha256(data) != item['sha256']:
            raise RuntimeMismatch(f'Locked input changed: {relative}')

    for relative, specification in lock['dockerfiles'].items():
        if component and component != specification['component']:
            continue
        text = (root / relative).read_text(encoding='utf-8')
        actual = re.findall(r'^FROM\s+(\S+)', text, flags=re.MULTILINE)
        expected = [lock['images'][name]['image'] for name in specification['images']]
        if actual != expected:
            raise RuntimeMismatch(f'Docker image drift: {relative}')

    # Hashes above pin the source lists and shell verifier. This also prevents
    # a hand-edited lock from disagreeing with its human-readable APT evidence.
    for name, distribution in lock['apt'].items():
        if component and component not in distribution['components']:
            continue
        actual = (root / f'deployment/scene/{name}.inrelease-sha256').read_text().splitlines()
        expected = sorted(item['sha256'] for item in distribution['releases'])
        if actual != expected or any(not DIGEST.fullmatch(item) for item in actual):
            raise RuntimeMismatch(f'APT snapshot drift: {name}')
    if component is None:
        compose = (root / 'docker-compose.scene.yml').read_text(encoding='utf-8')
        actual = re.findall(r'^\s+image:\s+(\S+)\s*$', compose, flags=re.MULTILINE)
        if actual != [lock['images']['postgres']['image']]:
            raise RuntimeMismatch('Compose image drift')
        actual = re.findall(r'^\s+dockerfile:\s+(\S+)\s*$', compose, flags=re.MULTILINE)
        if actual != ['backend/Dockerfile.scene', 'backend/Dockerfile.scene',
                      'renderer/Dockerfile', 'frontend/Dockerfile.scene']:
            raise RuntimeMismatch('Compose build target drift')
    return lock


def run(arguments, cwd):
    # Tool outputs never include environment/config or provider credentials.
    result = subprocess.run(arguments, cwd=cwd, check=True, capture_output=True,
                            text=True, timeout=90)
    return (result.stdout + result.stderr).strip()


def check_browser(measured, expected):
    if measured != expected:
        raise RuntimeMismatch('Installed Playwright/Chromium differs from runtime lock')


def engine_sources(root, component):
    """Include our compiler/converter code, not just third-party package versions."""
    root = Path(root)
    if component == 'renderer':
        paths = [root / 'renderer' / name for name in (
            'daemon.py', 'observability.py', 'convert_template.py', 'render_pdf.mjs',
            'render_preview.mjs', 'render_text_layout.mjs')]
    else:
        paths = [root / 'backend' / name for name in (
            'schemas/slide_scene_v1.schema.json', 'services/scene/validation.py',
            'services/scene/versioning.py', 'services/scene/render_jobs.py',
            'services/scene/layout_contract.py')]
        for directory in ('services/exports', 'services/templates'):
            found = sorted((root / 'backend' / directory).glob('*.py'))
            if not found:
                raise RuntimeMismatch(f'Missing engine sources: backend/{directory}')
            paths.extend(found)
    return {path.relative_to(root).as_posix(): sha256(path.read_bytes().replace(b'\r\n', b'\n'))
            for path in sorted(paths)}


def record(root, component):
    if component not in ('backend', 'renderer'):
        raise RuntimeMismatch('Only Python runtimes use the JSON runtime recorder')
    root = Path(root).resolve()
    lock = verify(root, component)
    if platform.system() != 'Linux':
        raise RuntimeMismatch('Runtime manifests must be measured inside Linux images')
    machine = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
    if not machine:
        raise RuntimeMismatch('Unsupported runtime architecture')
    packages = sorted(
        [{'name': d.metadata['Name'], 'version': d.version} for d in importlib.metadata.distributions()],
        key=lambda item: (item['name'].lower(), item['version']))
    result = {'schema_version': 1, 'component': component,
              'lock_sha256': fingerprint(lock), 'architecture': machine,
              'python_version': platform.python_version(), 'python_packages': packages,
              'engine_sources': engine_sources(root, component),
              'system_packages': sorted(run(['dpkg-query', '-W', '-f=${Package}\t${Version}\n'], root).splitlines())}
    if component == 'backend':
        if result['python_version'] != lock['tools']['backend_python']:
            raise RuntimeMismatch('Backend Python version differs from runtime lock')
        result['uv_version'] = run(['uv', '--version'], root)
        if result['uv_version'].split()[:2] != ['uv', lock['tools']['uv']]:
            raise RuntimeMismatch('uv version differs from runtime lock')
    else:
        result['node_version'] = run(['node', '--version'], root)
        result['libreoffice_version'] = run(['libreoffice', '--version'], root)
        result['browser'] = json.loads(run(['node', 'deployment/scene/check_browser.mjs'], root))
        check_browser(result['browser'], lock['tools']['browser'])
        for name, version in lock['tools']['renderer_python_packages'].items():
            if importlib.metadata.version(name) != version:
                raise RuntimeMismatch(f'Renderer dependency drift: {name}')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    sub = parser.add_subparsers(dest='command', required=True)
    command = sub.add_parser('verify')
    command.add_argument('--component', choices=COMPONENTS)
    sub.add_parser('fingerprint')
    command = sub.add_parser('record')
    command.add_argument('--component', choices=('backend', 'renderer'), required=True)
    command.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'verify':
            lock = verify(args.root, args.component)
            print(f'Scene runtime inputs verified: {fingerprint(lock)}')
        elif args.command == 'fingerprint':
            print(fingerprint(load_lock(args.root)))
        else:
            payload = record(args.root, args.component)
            args.output.write_text(json.dumps(payload, sort_keys=True, indent=2) + '\n', encoding='utf-8', newline='\n')
    except RuntimeMismatch as exc:
        parser.exit(1, f'Scene runtime verification failed: {exc}\n')
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError,
            importlib.metadata.PackageNotFoundError) as exc:
        # Do not emit subprocess stderr or host paths on a build/maintenance failure.
        parser.exit(1, f'Scene runtime verification failed ({type(exc).__name__}). Run verify before building.\n')


if __name__ == '__main__':
    main()
