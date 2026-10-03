"""Durable offline retention journal.

Revision ID: 02f8479a3c61
Revises: e4d650a02879
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '02f8479a3c61'
down_revision = 'e4d650a02879'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('scene_maintenance_runs',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('plan_sha256', sa.String(64), nullable=False, unique=True),
        sa.Column('plan_json', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=False),
        sa.Column('after_database_sha256', sa.String(64), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True)))


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text('SELECT count(*) FROM scene_maintenance_runs')).scalar():
        raise RuntimeError('Retention journal exists; preserve recovery evidence instead of downgrading')
    op.drop_table('scene_maintenance_runs')
