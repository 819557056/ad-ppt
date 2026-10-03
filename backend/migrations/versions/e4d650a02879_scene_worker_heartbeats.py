"""scene worker readiness heartbeat

Revision ID: e4d650a02879
Revises: c42b01f7a539
"""
from alembic import op
import sqlalchemy as sa

revision = 'e4d650a02879'
down_revision = 'c42b01f7a539'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('scene_worker_heartbeats',
        sa.Column('worker_id', sa.String(length=100), primary_key=True),
        sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_scene_worker_heartbeats_heartbeat_at',
                    'scene_worker_heartbeats', ['heartbeat_at'])


def downgrade():
    op.drop_index('ix_scene_worker_heartbeats_heartbeat_at', table_name='scene_worker_heartbeats')
    op.drop_table('scene_worker_heartbeats')
