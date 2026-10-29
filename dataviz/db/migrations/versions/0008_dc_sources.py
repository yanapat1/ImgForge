"""Data Center buckets remember the web source they were imported from (datacenter/sources, "From the web").

Revision ID: 0008
Revises: 0007

- app.dc_buckets.source_kind: kaggle | huggingface | roboflow | url | zenodo | openml (NULL = not imported)
- app.dc_buckets.source_ref: the canonical ref (owner/dataset, user/dataset@rev, the link, ...)
- app.dc_buckets.source_meta: {connection_id, opts, path, title, version, files, bytes} of the last import, so
  "Update from source" can run it again
- app.dc_buckets.imported_at: end of the last finished import
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('dc_buckets', sa.Column('source_kind', sa.String(16)), schema='app')
    op.add_column('dc_buckets', sa.Column('source_ref', sa.Text), schema='app')
    op.add_column('dc_buckets', sa.Column('source_meta', JSONB), schema='app')
    op.add_column('dc_buckets', sa.Column('imported_at', sa.DateTime(timezone=True)), schema='app')


def downgrade():
    for c in ('imported_at', 'source_meta', 'source_ref', 'source_kind'):
        op.drop_column('dc_buckets', c, schema='app')
