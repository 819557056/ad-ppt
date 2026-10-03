"""Verify a quiescent Scene database against immutable assets and snapshots.

This command is read-only. It never prints or archives plaintext model keys.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
from collections import defaultdict
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from models.scene_v1 import (Asset, DeckSnapshot, GenerationPlan, ModelCredential,
                             PageSceneRevision, RevisionAsset, SceneExport, SnapshotPage)
from services.scene.validation import asset_refs, digest


def _sha256(path):
    hash_value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            hash_value.update(chunk)
    return hash_value.hexdigest()


def _verify_credentials(session, encoded_key, key_version):
    credentials = session.scalars(select(ModelCredential)).all()
    if not credentials:
        return 0
    if not encoded_key:
        raise ValueError('CREDENTIAL_ENCRYPTION_KEY is required to verify stored credentials')
    try:
        secret = base64.urlsafe_b64decode(encoded_key + '=' * (-len(encoded_key) % 4))
    except (ValueError, TypeError) as exc:
        raise ValueError('Credential encryption key is invalid') from exc
    if len(secret) != 32:
        raise ValueError('Credential encryption key must decode to 32 bytes')
    for credential in credentials:
        if credential.key_version != key_version:
            raise ValueError(f'Credential key version unavailable: {credential.id}')
        try:
            associated = f'{credential.owner_id}:{credential.id}'.encode()
            plaintext = AESGCM(secret).decrypt(credential.nonce, credential.encrypted_secret,
                associated)
            fingerprint = hmac.new(secret, associated + plaintext, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(fingerprint, credential.secret_fingerprint):
                raise ValueError('credential fingerprint mismatch')
        except Exception as exc:
            raise ValueError(f'Credential cannot be decrypted: {credential.id}') from exc
    return len(credentials)


def audit_scene(database_url, asset_root, *, check_credentials=False,
                encryption_key='', key_version='v1'):
    root = Path(asset_root).resolve()
    if not root.is_dir():
        raise ValueError('Asset store root does not exist')
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            schema_revision = session.execute(text('SELECT version_num FROM alembic_version')).scalar_one()
            assets = []
            ready_assets = {}
            for asset in session.scalars(select(Asset).where(Asset.state == 'ready').order_by(Asset.id)):
                path = (root / asset.storage_key).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    raise ValueError(f'Asset missing or outside store: {asset.id}')
                if path.stat().st_size != asset.byte_size or _sha256(path) != asset.sha256:
                    raise ValueError(f'Asset size/hash mismatch: {asset.id}')
                assets.append({'asset_id': asset.id, 'storage_key': asset.storage_key,
                               'sha256': asset.sha256, 'byte_size': asset.byte_size})
                ready_assets[asset.id] = asset
            plan_count = 0
            for plan in session.scalars(select(GenerationPlan)):
                if digest(plan.manifest_json) != plan.manifest_hash:
                    raise ValueError(f'Generation plan hash mismatch: {plan.id}')
                plan_count += 1
            scene_count = 0
            revision_by_id = {}
            revision_assets = defaultdict(set)
            for reference in session.scalars(select(RevisionAsset)):
                revision_assets[reference.revision_id].add((reference.asset_id, reference.role,
                                                             reference.project_id))
            for scene in session.scalars(select(PageSceneRevision)):
                if digest(scene.scene_json) != scene.scene_hash:
                    raise ValueError(f'Scene revision hash mismatch: {scene.id}')
                expected = {(asset_id, role, scene.project_id)
                            for asset_id, role in asset_refs(scene.scene_json)}
                if revision_assets.pop(scene.id, set()) != expected:
                    raise ValueError(f'Scene asset references mismatch: {scene.id}')
                for asset_id, _, _ in expected:
                    asset = ready_assets.get(asset_id)
                    if (asset is None or asset.owner_id != scene.owner_id or
                            asset.project_id != scene.project_id):
                        raise ValueError(f'Scene asset ownership mismatch: {scene.id}/{asset_id}')
                revision_by_id[scene.id] = scene
                scene_count += 1
            if revision_assets:
                raise ValueError('Orphan Scene asset reference')
            snapshot_count = 0
            snapshots = {}
            asset_by_id = {item['asset_id']: item for item in assets}
            for snapshot in session.scalars(select(DeckSnapshot)):
                if digest(snapshot.manifest_json) != snapshot.manifest_hash:
                    raise ValueError(f'Snapshot hash mismatch: {snapshot.id}')
                manifest = snapshot.manifest_json
                for asset_id, expected_hash in manifest.get('assets', {}).items():
                    if asset_by_id.get(asset_id, {}).get('sha256') != expected_hash:
                        raise ValueError(f'Snapshot asset mismatch: {snapshot.id}/{asset_id}')
                pages = list(session.scalars(select(SnapshotPage).where(
                    SnapshotPage.snapshot_id == snapshot.id).order_by(SnapshotPage.ordinal)))
                if len(pages) != len(manifest.get('pages', [])):
                    raise ValueError(f'Snapshot page count mismatch: {snapshot.id}')
                referenced_assets = set()
                for ordinal, (page, expected) in enumerate(zip(pages, manifest['pages']), 1):
                    revision = revision_by_id.get(page.revision_id)
                    if (page.ordinal != ordinal or page.page_id != expected['page_id'] or
                        page.revision_id != expected['revision_id'] or revision is None or
                        revision.page_id != page.page_id or revision.project_id != snapshot.project_id or
                        revision.owner_id != snapshot.owner_id or
                        revision.scene_hash != expected['scene_hash']):
                        raise ValueError(f'Snapshot revision mismatch: {snapshot.id}/{ordinal}')
                    referenced_assets.update(asset_id for asset_id, _ in asset_refs(revision.scene_json))
                if referenced_assets != set(manifest.get('assets', {})):
                    raise ValueError(f'Snapshot asset references mismatch: {snapshot.id}')
                snapshots[snapshot.id] = snapshot
                snapshot_count += 1
            export_count = 0
            for export in session.scalars(select(SceneExport)):
                snapshot = snapshots.get(export.snapshot_id)
                if (snapshot is None or snapshot.owner_id != export.owner_id or
                        snapshot.project_id != export.project_id):
                    raise ValueError(f'Export snapshot mismatch: {export.id}')
                if export.status in ('needs_review', 'succeeded'):
                    output = ready_assets.get(export.file_asset_id)
                    report_asset = ready_assets.get(export.report_asset_id)
                    if (output is None or report_asset is None or output.kind != 'export' or
                            report_asset.kind != 'export_report' or
                            output.owner_id != export.owner_id or output.project_id != export.project_id or
                            report_asset.owner_id != export.owner_id or
                            report_asset.project_id != export.project_id):
                        raise ValueError(f'Export assets mismatch: {export.id}')
                    try:
                        report = json.loads((root / report_asset.storage_key).read_bytes())
                    except (OSError, ValueError) as exc:
                        raise ValueError(f'Export report unreadable: {export.id}') from exc
                    if not isinstance(report, dict):
                        raise ValueError(f'Export report invalid: {export.id}')
                    if (report.get('snapshot_id') != snapshot.id or
                            report.get('snapshot_hash') != snapshot.manifest_hash or
                            report.get('format') != export.format or
                            output.provenance_json.get('snapshot_id') != snapshot.id or
                            output.provenance_json.get('report_asset_id') != report_asset.id or
                            output.provenance_json.get('snapshot_hash') != snapshot.manifest_hash):
                        raise ValueError(f'Export report provenance mismatch: {export.id}')
                    if export.status == 'succeeded':
                        if export.review_report_sha256 != report_asset.sha256:
                            raise ValueError(f'Export review hash mismatch: {export.id}')
                        visual = report.get('visual')
                        pages = visual.get('pages', []) if isinstance(visual, dict) else []
                        if (report.get('structural_and_text') != 'passed' or
                                report.get('visual_comparison') != 'needs_review' or
                                report.get('waivable_warning_codes') != ['VISUAL_DIFF_UNCALIBRATED'] or
                                export.reviewed_by != export.owner_id or export.reviewed_at is None or
                                len(pages) != len(snapshot.manifest_json['pages'])):
                            raise ValueError(f'Export review evidence mismatch: {export.id}')
                        for page, expected in zip(pages, snapshot.manifest_json['pages']):
                            if not isinstance(page, dict):
                                raise ValueError(f'Export visual proof mismatch: {export.id}')
                            proof = ready_assets.get(page.get('contact_sheet_asset_id'))
                            if (page.get('page_id') != expected['page_id'] or proof is None or
                                    proof.kind != 'visual_comparison' or proof.owner_id != export.owner_id or
                                    proof.project_id != export.project_id):
                                raise ValueError(f'Export visual proof mismatch: {export.id}')
                export_count += 1
            credential_count = (_verify_credentials(session, encryption_key, key_version)
                                if check_credentials else session.query(ModelCredential).count())
            report = {'schema_revision': schema_revision, 'asset_count': len(assets),
                      'generation_plan_count': plan_count,
                      'scene_revision_count': scene_count, 'snapshot_count': snapshot_count,
                      'export_count': export_count,
                      'credential_count': credential_count, 'assets': assets}
            return report
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-url', default=os.environ.get('DATABASE_URL'))
    parser.add_argument('--asset-root', default=os.environ.get('ASSET_STORE_ROOT'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--check-credentials', action='store_true')
    args = parser.parse_args()
    if not args.database_url or not args.asset_root:
        parser.error('DATABASE_URL and ASSET_STORE_ROOT are required')
    report = audit_scene(args.database_url, args.asset_root,
        check_credentials=args.check_credentials,
        encryption_key=os.environ.get('CREDENTIAL_ENCRYPTION_KEY', ''),
        key_version=os.environ.get('CREDENTIAL_ENCRYPTION_KEY_ID', 'v1'))
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
    if args.output:
        args.output.write_text(payload, encoding='utf-8')
    else:
        print(payload, end='')


if __name__ == '__main__':
    main()
