"""Data Analysis zone: sessions, query history, scripts, column descriptions, dashboards.

Revision ID: 0014
Revises: 0013

Private to their owner: unlike visualize sessions these are never shared, because free SQL runs with the owner's
credentials. The default tier gains analysis.use; changing data (analysis.write) stays something an admin grants.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def _now(name):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _session():
    return sa.Column('session_id', sa.String(36), sa.ForeignKey('app.an_sessions.id', ondelete='CASCADE'), nullable=False)


def upgrade():
    op.create_table('an_sessions',
                    sa.Column('id', sa.String(36), primary_key=True),
                    sa.Column('owner_id', sa.String(32), sa.ForeignKey('app.users.id', ondelete='CASCADE'), nullable=False),
                    sa.Column('name', sa.String(200), nullable=False),
                    sa.Column('state', JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
                    _now('created_at'), _now('updated_at'), schema='app')
    op.create_index('ix_an_sessions_owner', 'an_sessions', ['owner_id'], schema='app')
    op.create_table('an_history',
                    sa.Column('id', sa.Integer, primary_key=True, autoincrement=True), _session(),
                    sa.Column('connection_id', sa.String(36)), sa.Column('database', sa.String(200)),
                    sa.Column('name', sa.String(200), nullable=False, server_default=''),
                    sa.Column('sql', sa.Text, nullable=False), sa.Column('kind', sa.String(8), nullable=False),
                    sa.Column('status', sa.String(12), nullable=False), sa.Column('rows', sa.Integer),
                    sa.Column('ms', sa.Integer), sa.Column('error', sa.Text), _now('at'), schema='app')
    op.create_index('ix_an_history_session_at', 'an_history', ['session_id', 'at'], schema='app')
    op.create_table('an_scripts',
                    sa.Column('id', sa.String(36), primary_key=True), _session(),
                    sa.Column('name', sa.String(200), nullable=False),
                    sa.Column('position', sa.Integer, nullable=False, server_default=sa.text('0')),
                    sa.Column('connection_id', sa.String(36)), sa.Column('database', sa.String(200)),
                    sa.Column('body', sa.Text, nullable=False, server_default=''), _now('updated_at'), schema='app')
    op.create_index('ix_an_scripts_session', 'an_scripts', ['session_id', 'position'], schema='app')
    op.create_table('an_column_docs',
                    sa.Column('connection_id', sa.String(36), nullable=False),
                    sa.Column('database', sa.String(200), nullable=False, server_default=''),
                    sa.Column('schema_name', sa.String(200), nullable=False, server_default=''),
                    sa.Column('table_name', sa.String(200), nullable=False),
                    sa.Column('column_name', sa.String(200), nullable=False, server_default=''),
                    sa.Column('description', sa.Text, nullable=False),
                    sa.Column('source', sa.String(8), nullable=False, server_default='user'),
                    sa.Column('updated_by', sa.String(32), sa.ForeignKey('app.users.id', ondelete='CASCADE'), nullable=False),
                    _now('updated_at'),
                    sa.PrimaryKeyConstraint('connection_id', 'database', 'schema_name', 'table_name', 'column_name'),
                    schema='app')
    op.create_table('an_dashboards',
                    sa.Column('id', sa.String(36), primary_key=True), _session(),
                    sa.Column('name', sa.String(200), nullable=False),
                    sa.Column('layout', JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
                    _now('updated_at'), schema='app')
    op.create_index('ix_an_dashboards_session', 'an_dashboards', ['session_id'], schema='app')
    op.execute("""UPDATE app.tiers SET permissions = permissions || '["analysis.use"]'::jsonb
                  WHERE is_default AND NOT permissions ? 'analysis.use'""")


def downgrade():
    op.execute("UPDATE app.tiers SET permissions = permissions - 'analysis.use' - 'analysis.write'")
    for t in ('an_dashboards', 'an_column_docs', 'an_scripts', 'an_history', 'an_sessions'):
        op.drop_table(t, schema='app')
