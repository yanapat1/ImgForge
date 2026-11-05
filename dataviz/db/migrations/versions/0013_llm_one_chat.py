"""One ◆ chat across every page (llm/sessions.py role 'app'): each message remembers the page it was written on.

Revision ID: 0013
Revises: 0012

- app.llm_messages.page: {role, screen, label, session_id?, bucket?, path?, tab?, continued?}, the page the message
  was written on (the history the model reads marks where the user moved between pages).
- app.llm_sessions.last_page: the page of the newest message (the History list shows it).
- app.llm_sessions.plan: the chat's cross-page checklist (the todo tool), kept over page changes and compaction.
- ix_llm_sessions_owner_last_opened_at: the History list of one-chat sessions (every role of a user).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('llm_messages', sa.Column('page', JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
                  schema='app')
    op.add_column('llm_sessions', sa.Column('last_page', JSONB, nullable=False,
                                            server_default=sa.text("'{}'::jsonb")), schema='app')
    op.add_column('llm_sessions', sa.Column('plan', JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
                  schema='app')
    op.create_index('ix_llm_sessions_owner_last_opened_at', 'llm_sessions', ['owner_id', 'last_opened_at'],
                    schema='app')


def downgrade():
    op.drop_index('ix_llm_sessions_owner_last_opened_at', 'llm_sessions', schema='app')
    op.drop_column('llm_sessions', 'plan', schema='app')
    op.drop_column('llm_sessions', 'last_page', schema='app')
    op.drop_column('llm_messages', 'page', schema='app')
