"""Data Center S3 links can be loaded into a local folder (datacenter/service.py, "load into local").

Revision ID: 0007
Revises: 0006

- app.dc_buckets.mode: stream (read S3 on every request, read-only) | local (an editable local copy in
  local_root, filled and re-synced from S3: same-name files are replaced, local-only files are kept).
- app.dc_buckets.local_root: that folder (under storage.data_root); kept when switching back to stream.
- app.dc_buckets.synced_at: end of the last finished pull; NULL = the copy is not usable yet (still streamed).
"""

import sqlalchemy as sa
from alembic import op

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('dc_buckets', sa.Column('mode', sa.String(8), nullable=False, server_default=sa.text("'stream'")),
                  schema='app')
    op.add_column('dc_buckets', sa.Column('local_root', sa.Text), schema='app')
    op.add_column('dc_buckets', sa.Column('synced_at', sa.DateTime(timezone=True)), schema='app')
    op.create_check_constraint('ck_dc_buckets_mode', 'dc_buckets', "mode IN ('stream', 'local')", schema='app')


def downgrade():
    op.drop_constraint('ck_dc_buckets_mode', 'dc_buckets', schema='app')
    for c in ('synced_at', 'local_root', 'mode'):
        op.drop_column('dc_buckets', c, schema='app')
