"""Visualize sessions of kind 'embeddings': vectors + a metadata table from one folder (data/embedding_source.py).

Revision ID: 0002
Revises: 0001

Only the CHECK on app.viz_sessions.source_kind changes; the folder is stored in the existing `path` column.
"""

from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

TABLE, NAME = 'viz_sessions', 'ck_viz_sessions_source_kind'


def upgrade():
    op.drop_constraint(NAME, TABLE, schema='app', type_='check')
    op.create_check_constraint(NAME, TABLE, "source_kind IN ('local', 's3', 'labeling', 'embeddings')", schema='app')


def downgrade():
    op.execute("DELETE FROM app.sessions WHERE id IN "
               "(SELECT session_id FROM app.viz_sessions WHERE source_kind = 'embeddings')")
    op.drop_constraint(NAME, TABLE, schema='app', type_='check')
    op.create_check_constraint(NAME, TABLE, "source_kind IN ('local', 's3', 'labeling')", schema='app')
