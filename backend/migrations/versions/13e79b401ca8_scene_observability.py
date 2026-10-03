"""Scene trace identity and bounded operational metric aggregates.

Revision ID: 13e79b401ca8
Revises: 02f8479a3c61
"""
from alembic import op
import sqlalchemy as sa

revision = '13e79b401ca8'
down_revision = '02f8479a3c61'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('scene_task_items') as batch:
        batch.add_column(sa.Column('request_id', sa.String(36)))
        batch.add_column(sa.Column('retry_request_id', sa.String(36)))
        batch.add_column(sa.Column('queued_at', sa.DateTime(timezone=True)))
    # Historical requests cannot be reconstructed: use the existing task UUID as
    # a synthetic correlation ID and leave historical queue delay unmeasured.
    op.execute(sa.text('UPDATE scene_task_items SET request_id = id'))
    with op.batch_alter_table('scene_task_items') as batch:
        batch.alter_column('request_id', existing_type=sa.String(36), nullable=False)
    with op.batch_alter_table('scene_task_attempts') as batch:
        batch.add_column(sa.Column('request_id', sa.String(36)))
        batch.add_column(sa.Column('queue_wait_seconds', sa.Float()))
    op.execute(sa.text('UPDATE scene_task_attempts SET request_id = '
        '(SELECT request_id FROM scene_task_items WHERE scene_task_items.id = scene_task_attempts.task_item_id)'))
    with op.batch_alter_table('scene_task_attempts') as batch:
        batch.alter_column('request_id', existing_type=sa.String(36), nullable=False)
    op.create_index('ix_scene_task_items_request_id', 'scene_task_items', ['request_id'])
    op.create_index('ix_scene_task_attempts_request_id', 'scene_task_attempts', ['request_id'])
    op.create_table('scene_metrics',
        sa.Column('metric', sa.String(80), primary_key=True),
        sa.Column('operation', sa.String(32), primary_key=True),
        sa.Column('stage', sa.String(40), primary_key=True),
        sa.Column('outcome', sa.String(24), primary_key=True),
        sa.Column('source', sa.String(24), primary_key=True),
        sa.Column('bucket', sa.String(16), primary_key=True),
        sa.Column('value', sa.Float(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False))


def downgrade():
    bind = op.get_bind()
    if any(bind.execute(sa.text(f'SELECT count(*) FROM {table}')).scalar()
           for table in ('scene_task_items', 'scene_task_attempts', 'scene_metrics')):
        raise RuntimeError('Scene telemetry evidence exists; preserve it instead of downgrading')
    op.drop_table('scene_metrics')
    op.drop_index('ix_scene_task_items_request_id', table_name='scene_task_items')
    op.drop_index('ix_scene_task_attempts_request_id', table_name='scene_task_attempts')
    with op.batch_alter_table('scene_task_attempts') as batch:
        batch.drop_column('queue_wait_seconds')
        batch.drop_column('request_id')
    with op.batch_alter_table('scene_task_items') as batch:
        batch.drop_column('queued_at')
        batch.drop_column('retry_request_id')
        batch.drop_column('request_id')
