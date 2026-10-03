"""Per-owner encrypted model credentials; never falls back to global settings."""
import base64
import hashlib
import hmac
import os
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask import current_app

from models import db
from models.scene_v1 import ModelCredential, new_id
from .versioning import SceneError


def _master_key():
    encoded = current_app.config.get('CREDENTIAL_ENCRYPTION_KEY') or ''
    try:
        key = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
    except Exception as exc:
        raise SceneError('CREDENTIAL_CONFIG_MISSING', 'Encryption key invalid', 503) from exc
    if len(key) != 32:
        raise SceneError('CREDENTIAL_CONFIG_MISSING', '32-byte encryption key required', 503)
    return key


def credential_request_fingerprint(payload):
    """HMAC the full credential request so short secrets cannot be guessed from DB hashes."""
    from .validation import digest
    return hmac.new(_master_key(), digest(payload).encode('ascii'), hashlib.sha256).hexdigest()


def validate_gateway(url):
    if not isinstance(url, str) or len(url) > 2048 or any(char.isspace() or ord(char) < 32 for char in url):
        raise SceneError('GATEWAY_INVALID', 'HTTPS gateway URL required')
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SceneError('GATEWAY_INVALID', 'HTTPS gateway URL required')
    origin = f'{parsed.scheme}://{parsed.netloc}'
    allowed = {x.strip().rstrip('/') for x in current_app.config.get('MODEL_GATEWAY_ALLOWLIST', '').split(',') if x.strip()}
    if origin not in allowed:
        raise SceneError('GATEWAY_NOT_ALLOWED', 'Gateway origin is not configured', 422)
    if not parsed.path.rstrip('/').endswith('/v1'):
        raise SceneError('GATEWAY_INVALID', 'Gateway base URL must end in /v1')
    return url.rstrip('/')


def create_credential(owner_id, label, base_url, api_key):
    if (not isinstance(label, str) or not label or len(label) > 100 or
            any(ord(char) < 32 for char in label) or not isinstance(api_key, str) or
            not api_key or len(api_key) > 4096 or any(ord(char) < 32 for char in api_key)):
        raise SceneError('CREDENTIAL_INVALID', 'Label and API Key required')
    base_url = validate_gateway(base_url)
    secret = _master_key()
    credential_id = new_id()
    nonce = os.urandom(12)
    associated = f'{owner_id}:{credential_id}'.encode()
    encrypted = AESGCM(secret).encrypt(nonce, api_key.encode('utf-8'), associated)
    fingerprint = hmac.new(secret, associated + api_key.encode('utf-8'), hashlib.sha256).hexdigest()
    credential = ModelCredential(id=credential_id, owner_id=owner_id,
        provider_kind='sub2api_openai_compatible', base_url=base_url, label=label,
        key_suffix=api_key[-4:], encrypted_secret=encrypted, nonce=nonce,
        key_version=current_app.config['CREDENTIAL_ENCRYPTION_KEY_ID'],
        secret_fingerprint=fingerprint, capabilities_json={}, status='unknown')
    db.session.add(credential)
    return credential


def credential_summary(credential):
    return {'credential_id': credential.id, 'label': credential.label,
            'provider_kind': credential.provider_kind, 'base_url': credential.base_url,
            'key_suffix': credential.key_suffix, 'status': credential.status,
            'capabilities': credential.capabilities_json}


def decrypt_credential(credential):
    if credential.status == 'revoked':
        raise SceneError('CREDENTIAL_REVOKED', 'Model credential revoked')
    if credential.key_version != current_app.config['CREDENTIAL_ENCRYPTION_KEY_ID']:
        raise SceneError('CREDENTIAL_KEY_VERSION', 'Encryption key version unavailable', 503)
    return AESGCM(_master_key()).decrypt(credential.nonce, credential.encrypted_secret,
                                        f'{credential.owner_id}:{credential.id}'.encode()).decode('utf-8')
