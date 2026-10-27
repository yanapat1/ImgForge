"""Visualize session options: what one point is (unit: auto | boxes | images, data/views.py) and whether the
sub-folder names are the classes of an images-only session.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('viz_sessions', sa.Column('options', JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
                  schema='app')


def downgrade():
    op.drop_column('viz_sessions', 'options', schema='app')
