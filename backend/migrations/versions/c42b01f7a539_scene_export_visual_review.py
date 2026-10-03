"""export visual review binding

Revision ID: c42b01f7a539
Revises: 846e029db996
"""
from alembic import op
import sqlalchemy as sa

revision = 'c42b01f7a539'
down_revision = '846e029db996'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('scene_exports') as batch:
        batch.add_column(sa.Column('review_report_sha256', sa.String(length=64)))
        batch.add_column(sa.Column('reviewed_by', sa.String(length=36)))
        batch.add_column(sa.Column('reviewed_at', sa.DateTime(timezone=True)))
        batch.create_foreign_key('fk_scene_exports_reviewed_by', 'principals',
                                 ['reviewed_by'], ['id'])


def downgrade():
    raise RuntimeError('Export review audit records may exist; do not destructively downgrade')
