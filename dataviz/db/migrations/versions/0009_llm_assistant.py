"""The ◆ assistants v2: per-user prompts / skills, plugins, saved chat sessions, chat + web search settings.

Revision ID: 0009
Revises: 0008

- app.llm_user_config.chat / web / web_secrets: per assistant (model, mode, effort ...), web search (level, engine,
  special words) and the search engine's key (Fernet)
- app.llm_plugins: installed declarative plugins (manifest JSONB, secrets for their HTTP tools)
- app.llm_user_prompts: the user's own instructions and skills (kind prompt | skill, role data | visual | all)
- app.llm_sessions + app.llm_messages: saved chats (soft delete: deleted_at, purged after the retention)
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None

J = sa.text("'{}'::jsonb")


def _owner():
    return sa.Column('owner_id', sa.String(32), sa.ForeignKey('app.users.id', ondelete='CASCADE'), nullable=False)


def _now(name):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade():
    op.add_column('llm_user_config', sa.Column('chat', JSONB, nullable=False, server_default=J), schema='app')
    op.add_column('llm_user_config', sa.Column('web', JSONB, nullable=False, server_default=J), schema='app')
    op.add_column('llm_user_config', sa.Column('web_secrets', sa.LargeBinary), schema='app')
    op.create_table(
        'llm_plugins',
        sa.Column('id', sa.String(36), primary_key=True), _owner(),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('version', sa.String(40), nullable=False, server_default=''),
        sa.Column('manifest', JSONB, nullable=False, server_default=J),
        sa.Column('secrets', sa.LargeBinary),
        sa.Column('enabled', sa.Boolean, nullable=False, server_default=sa.text('true')),
        _now('installed_at'),
        schema='app')
    op.create_index('ux_llm_plugins_owner_name', 'llm_plugins', ['owner_id', sa.text('lower(name)')], unique=True,
                    schema='app')
    op.create_table(
        'llm_user_prompts',
        sa.Column('id', sa.String(36), primary_key=True), _owner(),
        sa.Column('kind', sa.String(10), nullable=False),
        sa.Column('role', sa.String(10), nullable=False, server_default='all'),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('description', sa.Text, nullable=False, server_default=''),
        sa.Column('body', sa.Text, nullable=False, server_default=''),
        sa.Column('enabled', sa.Boolean, nullable=False, server_default=sa.text('true')),
        sa.Column('plugin_id', sa.String(36), sa.ForeignKey('app.llm_plugins.id', ondelete='CASCADE')),
        _now('created_at'), _now('updated_at'),
        schema='app')
    op.create_index('ix_llm_user_prompts_owner', 'llm_user_prompts', ['owner_id', 'kind'], schema='app')
    op.create_table(
        'llm_sessions',
        sa.Column('id', sa.String(36), primary_key=True), _owner(),
        sa.Column('role', sa.String(10), nullable=False),
        sa.Column('title', sa.String(200), nullable=False, server_default=''),
        sa.Column('scope', JSONB, nullable=False, server_default=J),
        sa.Column('provider_id', sa.String(36)),
        sa.Column('model', sa.String(200)),
        sa.Column('mode', sa.String(10)),
        sa.Column('effort', sa.String(10)),
        sa.Column('pinned', sa.Boolean, nullable=False, server_default=sa.text('false')),
        sa.Column('n_messages', sa.Integer, nullable=False, server_default='0'),
        sa.Column('usage', JSONB, nullable=False, server_default=J),
        _now('created_at'), _now('updated_at'), _now('last_opened_at'),
        sa.Column('deleted_at', sa.DateTime(timezone=True)),
        sa.Column('delete_reason', sa.String(10)),
        schema='app')
    op.create_index('ix_llm_sessions_owner_role_last_opened_at', 'llm_sessions', ['owner_id', 'role', 'last_opened_at'],
                    schema='app')
    op.create_index('ix_llm_sessions_deleted_at', 'llm_sessions', ['deleted_at'], schema='app',
                    postgresql_where=sa.text('deleted_at IS NOT NULL'))
    op.create_table(
        'llm_messages',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('session_id', sa.String(36), sa.ForeignKey('app.llm_sessions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('seq', sa.Integer, nullable=False),
        sa.Column('role', sa.String(10), nullable=False),
        sa.Column('text', sa.Text, nullable=False, server_default=''),
        sa.Column('trace', JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('usage', JSONB, nullable=False, server_default=J),
        _now('created_at'),
        schema='app')
    op.create_index('ux_llm_messages_session_id_seq', 'llm_messages', ['session_id', 'seq'], unique=True, schema='app')


def downgrade():
    op.drop_table('llm_messages', schema='app')
    op.drop_table('llm_sessions', schema='app')
    op.drop_table('llm_user_prompts', schema='app')
    op.drop_table('llm_plugins', schema='app')
    for c in ('web_secrets', 'web', 'chat'):
        op.drop_column('llm_user_config', c, schema='app')
