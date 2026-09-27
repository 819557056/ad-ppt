"""Add the hosted beta invitation list.

Revision ID: c42f8e9a1b70
Revises: b82a4ddcb102
"""
from alembic import op
import sqlalchemy as sa

revision = 'c42f8e9a1b70'
down_revision = 'b82a4ddcb102'
branch_labels = None
depends_on = None


def upgrade():
    # A production backport may have created this table before Alembic catches up.
    if sa.inspect(op.get_bind()).has_table('waitlist_signups'):
        return
    op.create_table(
        'waitlist_signups',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('email', sa.String(length=254), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table('waitlist_signups')
