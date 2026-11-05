"""Compacted ◆ chats (llm/engine/summarize.py): a rolling summary of the older part of a chat.

Revision ID: 0012
Revises: 0011

- app.llm_sessions.summary: the structured summary of every message up to summary_seq (goal, preferences, app
  state, work done with exact numbers, open tasks, facts). The model gets the summary + the messages after it; the
  page still shows every message.
- summary_seq: the last message seq the summary covers (NULL = never compacted); summary_tokens: its estimate.
- parent_id: the chat this one was handed off from (the assistant moving between pages).
"""

import sqlalchemy as sa
from alembic import op

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('llm_sessions', sa.Column('summary', sa.Text), schema='app')
    op.add_column('llm_sessions', sa.Column('summary_seq', sa.Integer), schema='app')
    op.add_column('llm_sessions', sa.Column('summary_tokens', sa.Integer), schema='app')
    op.add_column('llm_sessions', sa.Column('parent_id', sa.String(36)), schema='app')


def downgrade():
    for c in ('parent_id', 'summary_tokens', 'summary_seq', 'summary'):
        op.drop_column('llm_sessions', c, schema='app')
