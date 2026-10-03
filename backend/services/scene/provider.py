"""Narrow OpenAI-compatible model gateway adapter with no shared-key fallback."""
import base64
import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from .versioning import SceneError


class ProviderOutcomeUnknown(SceneError):
    def __init__(self):
        super().__init__('MODEL_OUTCOME_UNKNOWN', 'Provider request may have been charged; review before retry', 409)


def _bounded_body(response, limit):
    """Bound decoded response bytes while streaming; a post-read check is too late."""
    content = bytearray()
    for chunk in response.iter_bytes(chunk_size=64 * 1024):
        if len(chunk) > limit - len(content):
            raise ValueError('gateway response exceeds limit')
        content.extend(chunk)
    return bytes(content)


def list_models(base_url, api_key):
    """Non-billable connectivity probe; model listing is not capability proof."""
    try:
        with httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=False,
                          trust_env=False) as client:
            with client.stream('GET', f'{base_url}/models',
                               headers={'Authorization': f'Bearer {api_key}'}) as response:
                if response.status_code in (401, 403):
                    raise SceneError('MODEL_AUTH_FAILED', 'Model API Key rejected', 422)
                if response.status_code == 402:
                    raise SceneError('MODEL_QUOTA_EXHAUSTED', 'Model quota exhausted', 422)
                if response.status_code != 200:
                    raise SceneError('MODEL_CHECK_FAILED', 'Gateway model listing unavailable', 502)
                try:
                    body = _bounded_body(response, 1024 * 1024)
                except ValueError as exc:
                    raise SceneError('MODEL_CHECK_FAILED', 'Gateway model listing unavailable', 502) from exc
    except httpx.RequestError as exc:
        raise SceneError('MODEL_CONNECT_FAILED', 'Could not connect to model gateway', 503) from exc
    try:
        payload = json.loads(body)
        entries = payload['data']
        if not isinstance(entries, list):
            raise ValueError()
        models = [entry['id'] for entry in entries if isinstance(entry, dict) and
                  isinstance(entry.get('id'), str) and len(entry['id']) <= 200]
        return sorted(set(models))[:1000]
    except (KeyError, TypeError, ValueError) as exc:
        raise SceneError('MODEL_CHECK_FAILED', 'Gateway model listing invalid', 502) from exc


def _post(base_url, api_key, path, payload):
    try:
        with httpx.Client(timeout=httpx.Timeout(180, connect=15), follow_redirects=False,
                          trust_env=False) as client:
            with client.stream('POST', f'{base_url}/{path}',
                               headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                               json=payload) as response:
                status = response.status_code
                request_id = response.headers.get('x-request-id')
                retry_header = response.headers.get('Retry-After', '0')
                if status in (401, 403):
                    raise SceneError('MODEL_AUTH_FAILED', 'Model API Key rejected', 422)
                if status == 402:
                    raise SceneError('MODEL_QUOTA_EXHAUSTED', 'Model quota exhausted', 422)
                if status == 429:
                    try:
                        retry_after = float(retry_header)
                    except ValueError:
                        try:
                            retry_after = (parsedate_to_datetime(retry_header) -
                                           datetime.now(timezone.utc)).total_seconds()
                        except (TypeError, ValueError, OverflowError):
                            retry_after = 0
                    retry_after = min(300, max(0, retry_after))
                    raise SceneError('MODEL_RATE_LIMITED', 'Model gateway rate limited request', 429,
                                     {'retry_after_seconds': retry_after})
                if status in (400, 404, 422):
                    raise SceneError('MODEL_UNSUPPORTED', 'Model or capability unsupported', 422)
                if not 200 <= status < 300:
                    raise SceneError('MODEL_FAILED', f'Model gateway returned HTTP {status}', 502)
                try:
                    body = _bounded_body(response, 25 * 1024 * 1024)
                except ValueError as exc:
                    raise SceneError('MODEL_RESPONSE_TOO_LARGE', 'Model response exceeds limit') from exc
    except httpx.ConnectError as exc:
        raise SceneError('MODEL_CONNECT_FAILED', 'Could not connect to model gateway', 503) from exc
    except httpx.RequestError as exc:
        raise ProviderOutcomeUnknown() from exc
    try:
        return json.loads(body), request_id
    except ValueError as exc:
        raise SceneError('MODEL_RESPONSE_INVALID', 'Model gateway returned invalid JSON') from exc


def chat_json(base_url, api_key, model, system_prompt, user_prompt):
    if not model or not isinstance(model, str):
        raise SceneError('MODEL_NOT_CONFIGURED', 'Text model not configured')
    payload, request_id = _post(base_url, api_key, 'chat/completions', {
        'model': model, 'temperature': 0.2,
        'messages': [{'role': 'system', 'content': system_prompt},
                     {'role': 'user', 'content': user_prompt}]})
    try:
        content = payload['choices'][0]['message']['content']
        if isinstance(content, list):
            content = ''.join(part.get('text', '') for part in content if part.get('type') == 'text')
        if not isinstance(content, str) or len(content) > 250_000:
            raise ValueError()
        return json.loads(content), request_id, payload.get('usage') or {}
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise SceneError('MODEL_SCENE_INVALID', 'Model did not return a JSON object') from exc


def generate_image(base_url, api_key, model, prompt):
    if not model:
        raise SceneError('IMAGE_MODEL_NOT_CONFIGURED', 'Image model not configured')
    payload, request_id = _post(base_url, api_key, 'images/generations', {
        'model': model, 'prompt': prompt, 'size': '1024x1024', 'response_format': 'b64_json'})
    try:
        encoded = payload['data'][0]['b64_json']
        image = base64.b64decode(encoded, validate=True)
        if len(image) > 20 * 1024 * 1024:
            raise ValueError('image too large')
        return image, request_id, payload.get('usage') or {}
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise SceneError('IMAGE_RESPONSE_INVALID', 'Image model must return bounded b64_json') from exc
