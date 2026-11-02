"""Folders of a streamed S3 link downloaded into its local copy, one by one (datacenter/service.fetch_local).

Revision ID: 0011
Revises: 0010

- app.dc_s3_local: one row per downloaded folder (or file) of an S3 link, path '' = the whole bucket (a full "load
  into local" pull). state fresh | outdated | fetching | error; diff {new, changed, removed, sample} from the last
  check against S3; objects / bytes of the last download. The files are a read-only copy in dc_buckets.local_root.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'dc_s3_local',
        sa.Column('bucket_id', sa.String(32), sa.ForeignKey('app.dc_buckets.id', ondelete='CASCADE'), nullable=False),
        sa.Column('path', sa.Text, nullable=False),
        sa.Column('state', sa.String(16), nullable=False, server_default=sa.text("'fetching'")),
        sa.Column('objects', sa.BigInteger, nullable=False, server_default=sa.text('0')),
        sa.Column('bytes', sa.BigInteger, nullable=False, server_default=sa.text('0')),
        sa.Column('diff', JSONB),
        sa.Column('fetched_at', sa.DateTime(timezone=True)),
        sa.Column('checked_at', sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint('bucket_id', 'path', name='pk_dc_s3_local'),
        schema='app')


def downgrade():
    op.drop_table('dc_s3_local', schema='app')
