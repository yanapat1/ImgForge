"""Change marks of the Bucket page (bucket/changes.py).

Revision ID: 0017
Revises: 0016

- app.bucket_changes: one row per file / folder of a bucket that changed since it was downloaded or marked seen:
  kind edited | created | copied | moved | deleted | uploaded | external, source bucket | visual | label | external,
  by whom, when. A text_pattern_ops index serves the prefix lookups (the marks of one folder: one range scan).
"""

import sqlalchemy as sa
from alembic import op

revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'bucket_changes',
        sa.Column('bucket_id', sa.String(32), sa.ForeignKey('app.buckets.id', ondelete='CASCADE'), nullable=False),
        sa.Column('path', sa.Text, nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('source', sa.String(16), nullable=False),
        sa.Column('user_id', sa.String(32), sa.ForeignKey('app.users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('bucket_id', 'path', name='pk_bucket_changes'),
        schema='app')
    op.create_index('ix_bucket_changes_prefix', 'bucket_changes', ['bucket_id', 'path'], schema='app',
                    postgresql_ops={'path': 'text_pattern_ops'})


def downgrade():
    op.drop_table('bucket_changes', schema='app')
