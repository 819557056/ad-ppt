"""Private immutable object storage for Scene inputs and outputs."""
import hashlib
import hmac
import io
import time

from flask import current_app
from PIL import Image, ImageOps

from models import db
from models.scene_v1 import Asset, new_id
from .storage_quota import private_root, write_asset_bytes
from .telemetry import timed_stage


def _root():
    return private_root()


def path_for(asset):
    root = _root()
    path = (root / asset.storage_key).resolve()
    if not path.is_relative_to(root):
        raise ValueError('invalid storage key')
    return path


@timed_stage('asset_write')
def put_bytes(owner_id, project_id, kind, payload, mime_type, suffix, *, dimensions=None, provenance=None):
    aid = new_id()
    key = f'owners/{owner_id}/projects/{project_id}/assets/{aid}/content.{suffix}'
    asset = Asset(id=aid, owner_id=owner_id, project_id=project_id, kind=kind,
                  storage_key=key, sha256=hashlib.sha256(payload).hexdigest(),
                  byte_size=len(payload), mime_type=mime_type,
                  width_px=dimensions[0] if dimensions else None,
                  height_px=dimensions[1] if dimensions else None,
                  provenance_json=provenance or {}, state='ready')
    write_asset_bytes(owner_id, key, payload)
    db.session.add(asset)
    return asset


def put_image(owner_id, project_id, payload, *, kind='upload', provenance=None):
    if len(payload) > current_app.config['MAX_UPLOAD_BYTES']:
        raise ValueError('image exceeds upload limit')
    with Image.open(io.BytesIO(payload)) as image:
        if getattr(image, 'n_frames', 1) != 1 or image.width * image.height > 40_000_000:
            raise ValueError('animated or oversized image unsupported')
        if image.format not in ('PNG', 'JPEG', 'WEBP'):
            raise ValueError('image format unsupported')
        normalized = ImageOps.exif_transpose(image)
        normalized.load()
        target = io.BytesIO()
        if normalized.mode in ('RGBA', 'LA'):
            normalized.convert('RGBA').save(target, format='PNG')
            mime, suffix = 'image/png', 'png'
        else:
            normalized.convert('RGB').save(target, format='PNG')
            mime, suffix = 'image/png', 'png'
        return put_bytes(owner_id, project_id, kind, target.getvalue(), mime, suffix,
                         dimensions=normalized.size, provenance={**(provenance or {}),
                            'source_kind': kind, 'original_sha256': hashlib.sha256(payload).hexdigest()})


def checked_bytes(asset):
    payload = path_for(asset).read_bytes()
    if hashlib.sha256(payload).hexdigest() != asset.sha256:
        raise ValueError('asset hash mismatch')
    return payload


def signed_url(asset, expires_in=300):
    expires = int(time.time()) + expires_in
    message = f'{asset.id}:{asset.owner_id}:{expires}:read'.encode()
    signature = hmac.new(current_app.secret_key.encode(), message, hashlib.sha256).hexdigest()
    return f'/api/v2/assets/{asset.id}/content?expires={expires}&sig={signature}'


def verify_url(asset, expires, signature):
    try:
        expires = int(expires)
    except (TypeError, ValueError):
        return False
    if expires < time.time() or expires > time.time() + 3600:
        return False
    expected = hmac.new(current_app.secret_key.encode(),
                        f'{asset.id}:{asset.owner_id}:{expires}:read'.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature or '', expected)
