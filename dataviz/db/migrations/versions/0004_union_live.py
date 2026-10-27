"""Combined datasets + the labeling change feed.

Revision ID: 0004
Revises: 0003

- viz_sessions.source_kind 'union': one visualize session over several others (data/union.py), its members in
  app.union_members. A deleted member leaves its combined datasets (CASCADE); only the owner's own combined
  datasets block the delete, in the API (data/connections.py delete_dataset).
- lbl.change_seq: every insert / update of lbl.annotations and lbl.media_task_status takes the next number of one
  sequence (trigger) and NOTIFYs channel 'lbl_change' with the project id. Writes to a project hold its advisory
  lock (labeling/store.py ProjectStore.write), so within a project the numbers follow commit order and
  "rows with change_seq > cursor" never skips a row (data/live.py).
- lbl.media(project_id, checksum): content-hash lookups (data/promote.py dedup).
"""

import sqlalchemy as sa
from alembic import op

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

TABLE, NAME = 'viz_sessions', 'ck_viz_sessions_source_kind'
FEED = ('annotations', 'media_task_status')


def upgrade():
    op.drop_constraint(NAME, TABLE, schema='app', type_='check')
    op.create_check_constraint(NAME, TABLE, "source_kind IN ('local', 's3', 'labeling', 'embeddings', 'union')",
                               schema='app')
    op.create_table(
        'union_members',
        sa.Column('union_id', sa.String(36), sa.ForeignKey('app.sessions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('member_id', sa.String(36), sa.ForeignKey('app.sessions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('position', sa.Integer, nullable=False),
        sa.Column('alias', sa.String(24), nullable=False),
        sa.PrimaryKeyConstraint('union_id', 'member_id', name='pk_union_members'),
        schema='app')
    op.create_index('ux_union_members_union_id_alias', 'union_members', ['union_id', 'alias'], unique=True,
                    schema='app')
    op.create_index('ix_union_members_member_id', 'union_members', ['member_id'], schema='app')

    op.execute('CREATE SEQUENCE lbl.change_seq')
    op.execute("""CREATE FUNCTION lbl.bump_change_seq() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN NEW.change_seq := nextval('lbl.change_seq'); RETURN NEW; END $$""")
    op.execute("""CREATE FUNCTION lbl.notify_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN PERFORM pg_notify('lbl_change', NEW.project_id); RETURN NULL; END $$""")
    for t in FEED:
        op.add_column(t, sa.Column('change_seq', sa.BigInteger), schema='lbl')
        op.create_index(f'ix_{t}_project_id_change_seq', t, ['project_id', 'change_seq'], schema='lbl')
        op.execute(f'CREATE TRIGGER trg_{t}_seq BEFORE INSERT OR UPDATE ON lbl.{t} '
                   f'FOR EACH ROW EXECUTE FUNCTION lbl.bump_change_seq()')
        op.execute(f'CREATE TRIGGER trg_{t}_notify AFTER INSERT OR UPDATE ON lbl.{t} '
                   f'FOR EACH ROW EXECUTE FUNCTION lbl.notify_change()')
    op.create_index('ix_media_project_id_checksum', 'media', ['project_id', 'checksum'], schema='lbl')


def downgrade():
    op.drop_index('ix_media_project_id_checksum', 'media', schema='lbl')
    for t in FEED:
        op.execute(f'DROP TRIGGER IF EXISTS trg_{t}_notify ON lbl.{t}')
        op.execute(f'DROP TRIGGER IF EXISTS trg_{t}_seq ON lbl.{t}')
        op.drop_index(f'ix_{t}_project_id_change_seq', t, schema='lbl')
        op.drop_column(t, 'change_seq', schema='lbl')
    op.execute('DROP FUNCTION IF EXISTS lbl.notify_change()')
    op.execute('DROP FUNCTION IF EXISTS lbl.bump_change_seq()')
    op.execute('DROP SEQUENCE IF EXISTS lbl.change_seq')
    op.execute("DELETE FROM app.sessions WHERE id IN "
               "(SELECT session_id FROM app.viz_sessions WHERE source_kind = 'union')")
    op.drop_table('union_members', schema='app')
    op.drop_constraint(NAME, TABLE, schema='app', type_='check')
    op.create_check_constraint(NAME, TABLE, "source_kind IN ('local', 's3', 'labeling', 'embeddings')", schema='app')
