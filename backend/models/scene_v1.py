"""Persistent, owner-scoped records for the native Scene workflow.

Scene JSON and snapshot manifests are immutable after insertion. Derived reports and
export status live on separate mutable records.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import JSONB
from . import db


def new_id():
    return str(uuid.uuid4())


def now():
    return datetime.now(timezone.utc)


def trace_request_id():
    from services.scene.telemetry import request_id
    return request_id()


Json = db.JSON().with_variant(JSONB, 'postgresql')


class Principal(db.Model):
    __tablename__ = 'principals'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    kind = db.Column(db.String(24), nullable=False)
    display_name = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='active')
    external_issuer = db.Column(db.String(255))
    external_subject = db.Column(db.String(255))
    created_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)
    __table_args__ = (db.UniqueConstraint('external_issuer', 'external_subject'),)


class SceneWorkerHeartbeat(db.Model):
    __tablename__ = 'scene_worker_heartbeats'
    worker_id = db.Column(db.String(100), primary_key=True)
    heartbeat_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)


class ModelCredential(db.Model):
    __tablename__ = 'model_credentials'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    owner_id = db.Column(db.String(36), db.ForeignKey('principals.id'), nullable=False, index=True)
    provider_kind = db.Column(db.String(32), nullable=False)
    base_url = db.Column(db.Text, nullable=False)
    label = db.Column(db.String(100), nullable=False)
    key_suffix = db.Column(db.String(8), nullable=False)
    encrypted_secret = db.Column(db.LargeBinary, nullable=False)
    nonce = db.Column(db.LargeBinary, nullable=False)
    key_version = db.Column(db.String(32), nullable=False)
    secret_fingerprint = db.Column(db.String(64), nullable=False)
    capabilities_json = db.Column(Json, nullable=False, default=dict)
    status = db.Column(db.String(20), nullable=False, default='unknown')
    created_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)


class Asset(db.Model):
    __tablename__ = 'assets'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    owner_id = db.Column(db.String(36), db.ForeignKey('principals.id'), nullable=False, index=True)
    project_id = db.Column(db.String(36), db.ForeignKey('projects.id'), nullable=False, index=True)
    kind = db.Column(db.String(32), nullable=False)
    storage_key = db.Column(db.Text, nullable=False, unique=True)
    sha256 = db.Column(db.String(64), nullable=False)
    byte_size = db.Column(db.BigInteger, nullable=False)
    mime_type = db.Column(db.String(100), nullable=False)
    width_px = db.Column(db.Integer)
    height_px = db.Column(db.Integer)
    source_asset_id = db.Column(db.String(36), db.ForeignKey('assets.id'))
    provenance_json = db.Column(Json, nullable=False, default=dict)
    state = db.Column(db.String(20), nullable=False, default='ready')
    created_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)


class TemplateDocument(db.Model):
    __tablename__ = 'template_documents'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    owner_id = db.Column(db.String(36), db.ForeignKey('principals.id'), nullable=False)
    project_id = db.Column(db.String(36), db.ForeignKey('projects.id'), nullable=False)
    source_asset_id = db.Column(db.String(36), db.ForeignKey('assets.id'), nullable=False)
    source_type = db.Column(db.String(20), nullable=False)
    source_page_count = db.Column(db.Integer)
    selected_page_indexes_json = db.Column(Json, nullable=False, default=list)
    status = db.Column(db.String(24), nullable=False, default='uploaded')
    import_task_id = db.Column(db.String(36))
    warnings_json = db.Column(Json, nullable=False, default=list)
    renderer_manifest_json = db.Column(Json, nullable=False, default=dict)
    error_code = db.Column(db.String(80))
    error_details = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)


class GenerationPlan(db.Model):
    __tablename__ = 'generation_plans'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    owner_id = db.Column(db.String(36), db.ForeignKey('principals.id'), nullable=False)
    project_id = db.Column(db.String(36), db.ForeignKey('projects.id'), nullable=False, index=True)
    base_project_version = db.Column(db.BigInteger, nullable=False)
    manifest_json = db.Column(Json, nullable=False)
    manifest_hash = db.Column(db.String(64), nullable=False)
    created_by = db.Column(db.String(36), db.ForeignKey('principals.id'), nullable=False)
    confirmed_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)


class PageSceneRevision(db.Model):
    __tablename__ = 'page_scene_revisions'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    owner_id = db.Column(db.String(36), db.ForeignKey('principals.id'), nullable=False)
    project_id = db.Column(db.String(36), db.ForeignKey('projects.id'), nullable=False)
    page_id = db.Column(db.String(36), db.ForeignKey('pages.id'), nullable=False, index=True)
    seq = db.Column(db.BigInteger, nullable=False)
    parent_revision_id = db.Column(db.String(36), db.ForeignKey('page_scene_revisions.id'))
    schema_version = db.Column(db.Integer, nullable=False, default=1)
    scene_json = db.Column(Json, nullable=False)
    scene_hash = db.Column(db.String(64), nullable=False)
    origin = db.Column(db.String(20), nullable=False)
    generation_plan_id = db.Column(db.String(36), db.ForeignKey('generation_plans.id'))
    created_by = db.Column(db.String(36), db.ForeignKey('principals.id'), nullable=False)
    validation_json = db.Column(Json, nullable=False, default=dict)
    created_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)
    __table_args__ = (db.UniqueConstraint('page_id', 'seq'),)


class RevisionAsset(db.Model):
    __tablename__ = 'revision_assets'
    revision_id = db.Column(db.String(36), db.ForeignKey('page_scene_revisions.id'), primary_key=True)
    asset_id = db.Column(db.String(36), db.ForeignKey('assets.id'), primary_key=True)
    role = db.Column(db.String(24), primary_key=True)
    project_id = db.Column(db.String(36), db.ForeignKey('projects.id'), nullable=False)


class SceneCandidate(db.Model):
    __tablename__ = 'scene_candidates'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    owner_id = db.Column(db.String(36), db.ForeignKey('principals.id'), nullable=False)
    project_id = db.Column(db.String(36), db.ForeignKey('projects.id'), nullable=False, index=True)
    page_id = db.Column(db.String(36), db.ForeignKey('pages.id'), nullable=False)
    proposed_revision_id = db.Column(db.String(36), db.ForeignKey('page_scene_revisions.id'), nullable=False)
    base_revision_id = db.Column(db.String(36))
    base_page_version = db.Column(db.BigInteger, nullable=False)
    generation_plan_id = db.Column(db.String(36), db.ForeignKey('generation_plans.id'))
    scope_json = db.Column(Json, nullable=False, default=dict)
    state = db.Column(db.String(20), nullable=False, default='pending')
    change_summary_json = db.Column(Json, nullable=False, default=dict)
    created_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)
    accepted_at = db.Column(db.DateTime(timezone=True))


class DeckSnapshot(db.Model):
    __tablename__ = 'deck_snapshots'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    owner_id = db.Column(db.String(36), db.ForeignKey('principals.id'), nullable=False)
    project_id = db.Column(db.String(36), db.ForeignKey('projects.id'), nullable=False, index=True)
    confirmed_by = db.Column(db.String(36), db.ForeignKey('principals.id'), nullable=False)
    manifest_json = db.Column(Json, nullable=False)
    manifest_hash = db.Column(db.String(64), nullable=False)
    confirmed_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)


class SnapshotPage(db.Model):
    __tablename__ = 'snapshot_pages'
    snapshot_id = db.Column(db.String(36), db.ForeignKey('deck_snapshots.id'), primary_key=True)
    ordinal = db.Column(db.Integer, primary_key=True)
    page_id = db.Column(db.String(36), db.ForeignKey('pages.id'), nullable=False)
    revision_id = db.Column(db.String(36), db.ForeignKey('page_scene_revisions.id'), nullable=False)
    __table_args__ = (db.UniqueConstraint('snapshot_id', 'page_id'),)


class SceneExport(db.Model):
    __tablename__ = 'scene_exports'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    owner_id = db.Column(db.String(36), db.ForeignKey('principals.id'), nullable=False)
    project_id = db.Column(db.String(36), db.ForeignKey('projects.id'), nullable=False)
    snapshot_id = db.Column(db.String(36), db.ForeignKey('deck_snapshots.id'), nullable=False)
    format = db.Column(db.String(8), nullable=False)
    options_json = db.Column(Json, nullable=False, default=dict)
    options_hash = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(24), nullable=False, default='queued')
    file_asset_id = db.Column(db.String(36), db.ForeignKey('assets.id'))
    report_asset_id = db.Column(db.String(36), db.ForeignKey('assets.id'))
    error_code = db.Column(db.String(80))
    review_report_sha256 = db.Column(db.String(64))
    reviewed_by = db.Column(db.String(36), db.ForeignKey('principals.id'))
    reviewed_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)
    completed_at = db.Column(db.DateTime(timezone=True))


class SceneTaskItem(db.Model):
    __tablename__ = 'scene_task_items'
    request_id = db.Column(db.String(36), nullable=False, default=trace_request_id, index=True)
    retry_request_id = db.Column(db.String(36))
    queued_at = db.Column(db.DateTime(timezone=True), default=now)
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    owner_id = db.Column(db.String(36), db.ForeignKey('principals.id'), nullable=False)
    project_id = db.Column(db.String(36), db.ForeignKey('projects.id'), nullable=False)
    operation = db.Column(db.String(32), nullable=False)
    resource_id = db.Column(db.String(36), nullable=False)
    page_id = db.Column(db.String(36), db.ForeignKey('pages.id'))
    credential_id = db.Column(db.String(36), db.ForeignKey('model_credentials.id'))
    logical_key = db.Column(db.String(120))
    group_id = db.Column(db.String(36), index=True)
    input_json = db.Column(Json, nullable=False)
    input_hash = db.Column(db.String(64), nullable=False)
    state = db.Column(db.String(24), nullable=False, default='queued', index=True)
    attempt_count = db.Column(db.Integer, nullable=False, default=0)
    max_attempts = db.Column(db.Integer, nullable=False, default=2)
    dispatch_state = db.Column(db.String(24), nullable=False, default='not_sent')
    next_run_at = db.Column(db.DateTime(timezone=True))
    cancel_requested_at = db.Column(db.DateTime(timezone=True))
    lease_owner = db.Column(db.String(100))
    lease_expires_at = db.Column(db.DateTime(timezone=True))
    fence_token = db.Column(db.BigInteger, nullable=False, default=0)
    result_json = db.Column(Json, nullable=False, default=dict)
    error_code = db.Column(db.String(80))
    created_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=now, onupdate=now, nullable=False)
    __table_args__ = (db.UniqueConstraint('group_id', 'logical_key'),)


class SceneTaskAttempt(db.Model):
    __tablename__ = 'scene_task_attempts'
    request_id = db.Column(db.String(36), nullable=False, default=trace_request_id, index=True)
    queue_wait_seconds = db.Column(db.Float)
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    task_item_id = db.Column(db.String(36), db.ForeignKey('scene_task_items.id'), nullable=False)
    attempt_no = db.Column(db.Integer, nullable=False)
    fence_token = db.Column(db.BigInteger, nullable=False)
    worker_id = db.Column(db.String(100), nullable=False)
    started_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)
    finished_at = db.Column(db.DateTime(timezone=True))
    provider_request_id = db.Column(db.String(200))
    dispatch_state = db.Column(db.String(24), nullable=False, default='not_sent')
    request_fingerprint = db.Column(db.String(64))
    usage_json = db.Column(Json, nullable=False, default=dict)
    error_code = db.Column(db.String(80))
    __table_args__ = (db.UniqueConstraint('task_item_id', 'attempt_no'),)


class ApiIdempotencyRecord(db.Model):
    __tablename__ = 'api_idempotency_records'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    owner_id = db.Column(db.String(36), db.ForeignKey('principals.id'), nullable=False)
    operation = db.Column(db.String(80), nullable=False)
    idempotency_key = db.Column(db.String(200), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    response_status = db.Column(db.Integer, nullable=False)
    response_json = db.Column(Json, nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)
    __table_args__ = (db.UniqueConstraint('owner_id', 'operation', 'idempotency_key'),)


class SceneMaintenanceRun(db.Model):
    """Durable commit marker for resumable, offline retention runs."""
    __tablename__ = 'scene_maintenance_runs'
    id = db.Column(db.String(36), primary_key=True, default=new_id)
    plan_sha256 = db.Column(db.String(64), nullable=False, unique=True)
    plan_json = db.Column(Json, nullable=False)
    after_database_sha256 = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(24), nullable=False, default='db_pruned')
    created_at = db.Column(db.DateTime(timezone=True), default=now, nullable=False)
    completed_at = db.Column(db.DateTime(timezone=True))


class SceneMetric(db.Model):
    """Bounded aggregates, not per-user/event records or a billing ledger."""
    __tablename__ = 'scene_metrics'
    metric = db.Column(db.String(80), primary_key=True)
    operation = db.Column(db.String(32), primary_key=True)
    stage = db.Column(db.String(40), primary_key=True)
    outcome = db.Column(db.String(24), primary_key=True)
    source = db.Column(db.String(24), primary_key=True)
    bucket = db.Column(db.String(16), primary_key=True, default='')
    value = db.Column(db.Float, nullable=False, default=0)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=now)
