"""Initial schema: app (accounts, sessions, sharing, per-user settings), lbl (labeling store), vec (pgvector).

Revision ID: 0001
Revises:

Creates the tables from a frozen copy of db/schema.py (migrations/frozen/schema_0001.py, never edited), so later changes to
schema.py go into later migrations and a fresh database still replays every step.
"""

from alembic import op

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    from dataviz.db.migrations.frozen import schema_0001 as s
    bind = op.get_bind()
    for ext in s.EXTENSIONS:
        op.execute(f'CREATE EXTENSION IF NOT EXISTS {ext}')
    for name in s.SCHEMAS:
        op.execute(f'CREATE SCHEMA IF NOT EXISTS {name}')
    s.metadata.create_all(bind)


def downgrade():
    for name in ('vec', 'lbl', 'app'):
        op.execute(f'DROP SCHEMA IF EXISTS {name} CASCADE')
