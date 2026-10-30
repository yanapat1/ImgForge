"""Data Center buckets can cut new videos into frames by themselves (datacenter/video.py, "auto-extract").

Revision ID: 0010
Revises: 0009

- app.dc_buckets.video_auto: the extraction options (video.options) applied to every video that lands in the
  bucket by upload, import or S3 load; NULL = off
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('dc_buckets', sa.Column('video_auto', JSONB), schema='app')


def downgrade():
    op.drop_column('dc_buckets', 'video_auto', schema='app')
