"""Opt-in real nginx diagnostics contract, not a full Docker deployment test.

SCENE_TEST_NGINX_PATH must point to a reviewed Linux binary. Only this process'
configuration, loopback ports and temporary files are used; no system service,
/etc/hosts, existing site configuration or public listeners are changed.
"""
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]


def test_real_nginx_correlates_upstream_and_redacts_signed_urls(tmp_path):
    configured = os.environ.get('SCENE_TEST_NGINX_PATH')
    if not configured or sys.platform != 'linux':
        pytest.skip('Set SCENE_TEST_NGINX_PATH to a reviewed Linux nginx binary')
    binary = Path(configured).resolve(strict=True)
    request_id = str(uuid4())
    seen = []

    class Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            payload = b'{"data":"PRIVATE-BODY"}'
            self.send_response(500 if '/failure' in self.path else 200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('X-Request-ID', request_id)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    conf = (ROOT / 'frontend/nginx.scene.conf').read_text(encoding='utf-8')
    # Adapt only destinations needed for an unprivileged isolated QA process.
    # The committed log format, routing and error_log directives are unchanged.
    conf = conf.replace('listen 80;', f'listen 127.0.0.1:{port};')
    conf = conf.replace('http://backend:5000', f'http://127.0.0.1:{upstream.server_port}')
    conf = conf.replace('/var/log/nginx/access.log', str(tmp_path / 'access.jsonl'))
    conf = conf.replace('/usr/share/nginx/html', str(tmp_path / 'html'))
    (tmp_path / 'html').mkdir()
    (tmp_path / 'html/index.html').write_text('<!doctype html><title>QA</title>')
    config = tmp_path / 'nginx.conf'
    paths = '\n'.join(f'{kind}_temp_path "{tmp_path / kind}";' for kind in (
        'client_body', 'proxy', 'fastcgi', 'uwsgi', 'scgi'))
    config.write_text('daemon off;\npid "' + str(tmp_path / 'nginx.pid') + '";\n'
        'error_log stderr;\nevents { worker_connections 64; }\nhttp {\n' + paths + '\n' + conf + '\n}\n')
    process = None
    try:
        checked = subprocess.run([str(binary), '-t', '-p', str(tmp_path) + '/', '-c', str(config)],
            capture_output=True, text=True, timeout=10)
        assert checked.returncode == 0, checked.stderr
        process = subprocess.Popen([str(binary), '-p', str(tmp_path) + '/', '-c', str(config)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 5
        while True:
            assert process.poll() is None, 'nginx exited before readiness'
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.1):
                    break
            except OSError:
                assert time.monotonic() < deadline, 'nginx never listened'
                time.sleep(.02)

        def get(path, *, oversize=False):
            connection = HTTPConnection('127.0.0.1', port, timeout=5)
            try:
                headers = {'Authorization': 'Bearer PRIVATE-KEY', 'Cookie': 'PRIVATE-COOKIE',
                    'Referer': 'https://PRIVATE-HOST/?token=PRIVATE-REFERRER'}
                if oversize:
                    headers['Content-Length'] = str(51 * 1024 * 1024)
                connection.request('POST' if oversize else 'GET', path, headers=headers)
                response = connection.getresponse()
                result = response.status, response.getheader('X-Request-ID'), response.read()
                return result
            finally:
                connection.close()

        status, identifier, body = get('/api/v2/projects?token=PRIVATE-QUERY')
        assert status == 200 and identifier == request_id and b'PRIVATE-BODY' in body
        assert get('/api%2fv2/assets/example/content?sig=PRIVATE-SIGNATURE')[0] == 200
        assert get('/api/v2/failure?key=PRIVATE-FAILURE')[0] == 500
        assert get('/api/v2/upload?token=PRIVATE-OVERSIZE', oversize=True)[0] == 413
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)
        assert get('/api/v2/projects?token=PRIVATE-UNAVAILABLE')[0] == 502
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)
        if process is not None:
            process.terminate()
            try:
                out, err = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                out, err = process.communicate(timeout=5)
            assert b'PRIVATE-' not in out + err
    lines = (tmp_path / 'access.jsonl').read_text()
    assert 'PRIVATE-' not in lines
    records = [json.loads(line) for line in lines.splitlines()]
    assert [row['status'] for row in records] == [200, 200, 500, 413, 502]
    assert [row['request_id'] for row in records[:3]] == [request_id] * 3
    assert records[1]['uri'] == '/api/v2/assets/example/content'
    assert all(set(row) == {'event', 'time', 'method', 'uri', 'status', 'request_id'} for row in records)
    assert seen[0] == '/api/v2/projects?token=PRIVATE-QUERY'  # Forward, don't strip auth before the API.
