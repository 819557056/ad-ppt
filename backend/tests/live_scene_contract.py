"""Opt-in black-box contracts through the real Scene API/worker, never global Keys.

This helper does not import Flask or dotenv. Default CI only exercises it with an
in-memory transport. A live run needs two explicit acknowledgements and an
already-provisioned credential. Its journal permits explicit same-key resume,
not automatic retries or a new paid job after an ambiguous response.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
import ipaddress
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from PIL import Image, ImageDraw
from services.scene.validation import digest as scene_digest

CONFIRM = 'run-paid-scene-contracts'
BUDGET_ACK = 'dedicated-key-upstream-spending-cap-configured'
MAX_RESUME_SECONDS = 6 * 24 * 3600  # Less than the server's seven-day dedup retention.
MAX_BODY_BYTES = 2 * 1024 * 1024


class ContractError(RuntimeError):
    """Messages must not include HTTP bodies, headers, prompts or secrets."""


def require(condition, message):
    if not condition:
        raise ContractError(message)


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def identifier(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ContractError('API returned an invalid resource identifier') from None


def safe_url(value, *, gateway=False):
    require(isinstance(value, str) and len(value) <= 2048 and not any(
        c.isspace() or ord(c) < 32 for c in value), 'Invalid explicit endpoint')
    try:
        u = urlsplit(value)
        port = u.port
        loopback = u.hostname == 'localhost'
        if u.hostname and not loopback:
            try:
                loopback = ipaddress.ip_address(u.hostname).is_loopback
            except ValueError:
                pass
        valid = (u.scheme == 'https' or (not gateway and u.scheme == 'http' and loopback))
        require(valid and u.hostname and not u.username and not u.password and
                not u.query and not u.fragment and '\\' not in value and
                (port is None or port > 0), 'Use HTTPS (HTTP only for loopback Scene API)')
        require(u.path.rstrip('/').endswith('/v1') if gateway else u.path in ('', '/'),
                'Expected a gateway /v1 URL or a root Scene API origin')
    except ValueError:
        raise ContractError('Invalid explicit endpoint') from None
    return value.rstrip('/')


@dataclass(frozen=True)
class LiveConfig:
    origin: str
    access_code: str = field(repr=False)
    credential_id: str
    gateway: str
    text_model: str
    image_model: str
    run_dir: Path
    timeout_seconds: int = 300

    @classmethod
    def from_environment(cls, env):
        # No partial configuration, provider Key or .env is inspected by default.
        if not env.get('SCENE_LIVE_CONFIRM'):
            return None
        require(env.get('SCENE_LIVE_CONFIRM') == CONFIRM, 'Explicit live-test confirmation required')
        require(env.get('SCENE_LIVE_BUDGET_ACK') == BUDGET_ACK,
                'Configure a dedicated Key spending cap upstream and acknowledge it')
        names = ('ORIGIN', 'ACCESS_CODE', 'CREDENTIAL_ID', 'GATEWAY', 'TEXT_MODEL', 'IMAGE_MODEL', 'RUN_DIR')
        values = {n: env.get('SCENE_LIVE_' + n, '') for n in names}
        require(all(isinstance(v, str) and v and not any(ord(c) < 32 for c in v)
                    for v in values.values()), 'Missing or invalid explicit Scene live-test settings')
        require(len(values['ACCESS_CODE']) <= 4096 and values['ACCESS_CODE'].isascii() and all(
            len(values[n]) <= 150 for n in ('TEXT_MODEL', 'IMAGE_MODEL')), 'Live-test setting too long')
        try:
            timeout = int(env.get('SCENE_LIVE_TIMEOUT_SECONDS', '300'))
        except ValueError:
            raise ContractError('Live timeout must be 30..1800 seconds') from None
        require(30 <= timeout <= 1800, 'Live timeout must be 30..1800 seconds')
        run_dir = Path(values['RUN_DIR'])
        require(run_dir.is_absolute(), 'Use an explicit absolute evidence directory')
        return cls(safe_url(values['ORIGIN']), values['ACCESS_CODE'], identifier(values['CREDENTIAL_ID']),
                   safe_url(values['GATEWAY'], gateway=True), values['TEXT_MODEL'],
                   values['IMAGE_MODEL'], run_dir, timeout)

    def fingerprint(self):
        return digest({'origin': self.origin, 'credential_id': self.credential_id,
                       'gateway': self.gateway, 'text_model': self.text_model,
                       'image_model': self.image_model,
                       'helper_sha256': sha256(Path(__file__).read_bytes()).hexdigest()})


def reference_png():
    """Synthetic reference: no user file, template sample wording or instructions."""
    image = Image.new('RGB', (320, 180), '#FFFFFF')
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 24, 200, 38), fill='#17324D')
    draw.rectangle((20, 62, 150, 70), fill='#17324D')
    draw.ellipse((220, 72, 296, 148), fill='#E9B949')
    output = BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


class ContractRun:
    def __init__(self, config, *, transport=None, clock=time.time, monotonic=time.monotonic, sleep=time.sleep):
        self.config, self.clock, self.monotonic, self.sleep = config, clock, monotonic, sleep
        self.transport = transport  # Only offline harness tests supply one.
        self.journal = None

    def save(self):
        directory = self.config.run_dir
        temporary = directory / ('.journal-' + uuid4().hex)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as output:
                json.dump(self.journal, output, ensure_ascii=True, indent=2, allow_nan=False)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, directory / 'contract.json')
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def journal_lock(self):
        directory = self.config.run_dir
        require(not directory.is_symlink() and not getattr(directory, 'is_junction', lambda: False)(),
                'Evidence directory must not be a symlink or junction')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = directory / '.running'
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise ContractError('Run already locked; inspect the process and existing tasks before clearing a stale lock') from None
        try:
            with os.fdopen(fd, 'w') as output:
                output.write(str(os.getpid()))
            journal = directory / 'contract.json'
            if journal.exists():
                require(journal.is_file() and not journal.is_symlink() and journal.stat().st_size <= MAX_BODY_BYTES,
                        'Invalid existing journal; do not recreate pending paid work')
                try:
                    self.journal = json.loads(journal.read_text(encoding='utf-8'))
                    require(self.journal['version'] == 1 and self.journal['config_hash'] == self.config.fingerprint(),
                            'Journal/configuration changed; inspect existing tasks instead of replaying')
                    age = self.clock() - self.journal['created_at']
                    require(0 <= age < MAX_RESUME_SECONDS, 'Journal expired; inspect tasks, never automatically recreate them')
                    require(isinstance(self.journal['steps'], dict) and isinstance(self.journal['tasks'], dict),
                            'Invalid existing journal')
                except (KeyError, ValueError, TypeError):
                    raise ContractError('Invalid existing journal; inspect pending work before proceeding') from None
            else:
                require(set(directory.iterdir()) == {lock}, 'Evidence directory is not empty; refusing a new run')
                self.journal = {'version': 1, 'config_hash': self.config.fingerprint(), 'run_id': str(uuid4()),
                                'created_at': self.clock(), 'steps': {}, 'tasks': {}, 'status': 'prepared',
                                'scope': 'JSON, b64 image and reference-image/schema transport only; not billing or semantic fidelity proof'}
                self.save()
            yield
        finally:
            lock.unlink(missing_ok=True)

    def request(self, method, path, expected=200, **kwargs):
        require(path.startswith('/api/v2/'), 'Only Scene API routes are permitted')
        try:
            with self.http.stream(method, self.config.origin + path, **kwargs) as response:
                # Do not print upstream error bodies, reflected secrets or signed URLs.
                require(response.status_code == expected, 'Scene API rejected contract request (HTTP %d)' % response.status_code)
                raw = bytearray()
                for part in response.iter_bytes(chunk_size=65536):
                    require(len(raw) + len(part) <= MAX_BODY_BYTES, 'Scene API response exceeds contract limit')
                    raw.extend(part)
                try:
                    result = json.loads(raw)
                    require(isinstance(result, dict) and isinstance(result['data'], dict), 'Invalid Scene API envelope')
                    return result['data']
                except (KeyError, TypeError, ValueError):
                    raise ContractError('Invalid Scene API envelope') from None
        except httpx.HTTPError:
            raise ContractError('Scene connection interrupted; inspect journal and resume explicitly with the same directory') from None

    def submit(self, step, method, path, payload, expected, fields, *, file_bytes=None):
        frozen = {'method': method, 'path': path, 'payload': payload,
                  'file_sha256': sha256(file_bytes).hexdigest() if file_bytes else None}
        entry = self.journal['steps'].get(step)
        if entry is None:
            entry = {'request_hash': digest(frozen), 'idempotency_key': str(uuid4()), 'response': None}
            self.journal['steps'][step] = entry
            self.save()  # Must precede even the first HTTP byte.
        require(isinstance(entry, dict) and entry.get('request_hash') == digest(frozen),
                'Resume request differs from persisted request')
        key = identifier(entry.get('idempotency_key'))
        if entry.get('response') is not None:
            require(isinstance(entry['response'], dict), 'Invalid cached contract response')
            return self.clean_response(entry['response'], fields)
        kwargs = {'headers': {'Idempotency-Key': key}}
        if file_bytes:
            kwargs['files'] = {'file': ('scene-contract-reference.png', file_bytes, 'image/png')}
        else:
            kwargs['json'] = payload
        response = self.request(method, path, expected, **kwargs)
        # Explicit allowlist, never serialize an arbitrary server response.
        entry['response'] = self.clean_response(response, fields)
        self.save()
        return entry['response']

    @staticmethod
    def clean_response(response, fields):
        require(all(name in response for name in fields), 'Missing Scene contract response field')
        cleaned = {}
        for name in fields:
            value = response[name]
            if name.endswith('_id'):
                value = identifier(value)
            elif name == 'project_version':
                require(type(value) is int and value >= 0, 'Invalid project version')
            elif name == 'tasks':
                require(isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict),
                        'Expected exactly one selected reference task')
                value = [{'task_id': identifier(value[0].get('task_id'))}]
            else:
                raise ContractError('Unsupported persisted response field')
            cleaned[name] = value
        return cleaned

    def wait_task(self, task_id, operation):
        task_id = identifier(task_id)
        deadline = self.monotonic() + self.config.timeout_seconds
        while self.monotonic() < deadline:
            task = self.request('GET', '/api/v2/tasks/' + task_id)
            require(task.get('task_id') == task_id and task.get('operation') == operation,
                    'Unexpected contract task identity')
            state = task.get('state')
            require(state in ('queued', 'running', 'waiting_assets', 'succeeded', 'failed', 'cancelled', 'outcome_unknown'),
                    'Unknown task state')
            attempts = task.get('attempt_count')
            require(type(attempts) is int and 0 <= attempts <= 100, 'Invalid task attempt count')
            code = task.get('error_code')
            safe_code = code if isinstance(code, str) and re.fullmatch('[A-Z_0-9]{1,80}', code) else None
            self.journal['tasks'][task_id] = {'operation': operation, 'state': state, 'attempt_count': attempts,
                                            'error_code': safe_code, 'possible_charge': task.get('possible_charge') is True,
                                            'request_id': identifier(task['request_id']) if task.get('request_id') else None}
            self.save()
            if state == 'succeeded':
                result = task.get('result') or {}
                require(isinstance(result, dict), 'Invalid task result')
                return result
            require(state not in ('failed', 'cancelled', 'outcome_unknown'),
                    'Contract task needs attention; no retry or next paid probe was submitted')
            self.sleep(1)
        raise ContractError('Contract task still pending; no cancellation, retry or next paid probe submitted')

    def preflight(self):
        ready = self.request('GET', '/api/v2/readiness')
        require(ready.get('ready') is True, 'Scene deployment is not ready')
        cursor, seen = None, set()
        for _ in range(100):
            path = '/api/v2/model-credentials?limit=100' + ('&cursor=' + cursor if cursor else '')
            listed = self.request('GET', path)
            require(isinstance(listed.get('items'), list), 'Invalid credential listing')
            for credential in listed['items']:
                require(isinstance(credential, dict), 'Invalid credential listing item')
                if credential.get('credential_id') == self.config.credential_id:
                    require(credential.get('status') != 'revoked' and credential.get('base_url') == self.config.gateway,
                            'Selected credential gateway/status differs from explicit configuration')
                    return
            cursor = listed.get('next_cursor')
            if not cursor:
                break
            require(isinstance(cursor, str) and re.fullmatch('[A-Za-z0-9_.=-]{1,2048}', cursor) and cursor not in seen,
                    'Invalid credential cursor')
            seen.add(cursor)
        raise ContractError('Explicit test credential not available to this owner')

    def execute(self):
        self.preflight()
        project = self.submit('project', 'POST', '/api/v2/projects',
            {'title': 'Scene live contract ' + self.journal['run_id'], 'pages': [{'title': 'Synthetic contract', 'points': []}]},
            201, ('project_id', 'project_version'))
        base = '/api/v2/projects/' + identifier(project['project_id'])
        self.submit('models', 'PATCH', base + '/model-config',
            {'base_project_version': project['project_version'], 'model_config': {
                'text_model': self.config.text_model, 'image_model': self.config.image_model}}, 200, ('project_id',))
        for kind, model in (('model_listing', None), ('text_generation', self.config.text_model),
                            ('image_generation', self.config.image_model)):
            body = {'project_id': project['project_id'], 'kind': kind}
            if model:
                body.update(model_id=model, acknowledge_possible_charge=True)
            posted = self.submit(kind, 'POST', '/api/v2/model-credentials/' + self.config.credential_id + '/check',
                                 body, 202, ('task_id',))
            result = self.wait_task(posted['task_id'], 'check_credential')
            if model:
                require(result.get('verified') is True and result.get('model_id') == model and result.get('kind') == kind,
                        'Capability check did not verify the requested model')
        reference = reference_png()
        uploaded = self.submit('reference_upload', 'POST', base + '/template-documents', {}, 202,
                               ('document_id', 'task_id'), file_bytes=reference)
        self.wait_task(uploaded['task_id'], 'import_template')
        document_id = identifier(uploaded['document_id'])
        current = self.request('GET', base)
        require(current.get('model_config') == {'text_model': self.config.text_model, 'image_model': self.config.image_model},
                'Test project models changed; no reference probe submitted')
        analysis = self.submit('reference_analysis', 'POST', base + '/template-documents/' + document_id + '/analyze',
            {'credential_id': self.config.credential_id, 'selected_page_indexes': [1]}, 202, ('tasks',))
        require(isinstance(analysis['tasks'], list) and len(analysis['tasks']) == 1, 'Expected one selected reference task')
        result = self.wait_task(analysis['tasks'][0]['task_id'], 'analyze_template')
        require(isinstance(result.get('analysis_hash'), str) and re.fullmatch('[0-9a-f]{64}', result['analysis_hash']),
                'Reference analysis did not produce a validated style profile')
        document = self.request('GET', base + '/template-documents/' + document_id)
        require(document.get('status') == 'ready' and document.get('source_page_count') == 1,
                'Reference document did not finish')
        references = self.request('GET', base + '/template-assets?document_id=' + document_id + '&limit=100')
        items = references.get('items')
        require(isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict),
                'Expected one frozen reference page')
        profile = items[0].get('analysis')
        require(items[0].get('template_asset_id') == result.get('template_asset_id') and
                items[0].get('analysis_status') == 'completed' and isinstance(profile, dict) and
                profile.get('analysis_model') == self.config.text_model and profile.get('analysis_version') == 'v1',
                'Reference profile belongs to a different model or result')
        try:
            require(scene_digest(profile) == result['analysis_hash'], 'Reference profile changed after the checked task')
        except (TypeError, ValueError):
            raise ContractError('Invalid reference profile canonical data') from None
        # Resuming a completed run is a readback, never a fresh provider verification.
        self.journal.setdefault('completed_at', self.clock())
        self.journal.update(status='passed', last_observed_at=self.clock(), project_id=project['project_id'],
                            reference_sha256=sha256(reference).hexdigest(),
                            paid_operations_submitted=3, provider_billing='not independently verified',
                            automatic_retries='None by harness; server may retry rejected rate limits within task max_attempts')
        self.save()

    def run(self):
        try:
            with self.journal_lock():
                # Authorization was validated before constructing the network client.
                with httpx.Client(timeout=httpx.Timeout(30, connect=5), follow_redirects=False,
                                  trust_env=False, transport=self.transport,
                                  headers={'X-Access-Code': self.config.access_code}) as self.http:
                    try:
                        self.execute()
                    except ContractError:
                        self.journal['status'] = 'needs_attention'
                        self.save()
                        raise
        except OSError:
            raise ContractError('Evidence I/O failed; inspect the journal and existing tasks before any resume') from None
        return self.config.run_dir / 'contract.json'
