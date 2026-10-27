"""User tiers: named permission sets admins define (auth/tiers.py).

Revision ID: 0005
Revises: 0004

- app.tiers: name, description, permissions (JSONB list of auth/tiers.PERMS keys), is_default (one at most).
- app.users.tier_id: NULL = the default tier; a deleted tier sets its users back to NULL.
- Seeded: Viewer (nothing), Member (default: what every user could do before tiers), Contributor (Member + file
  models, which run code on the server).
"""

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

MEMBER = ['dataset.create', 'label.create', 'connection.add', 'model.add', 'llm.use']
SEED = [
    ('viewer', 'Viewer', 'Opens only what is shared with them; labels where they are a labeler', [], False),
    ('member', 'Member', 'Adds their own datasets, labeling projects, connections, API models and uses the LLM',
     MEMBER, True),
    ('contributor', 'Contributor', 'Member + uploads model files (.pt / .onnx / adapters) to label and visualize with',
     MEMBER + ['model.upload'], False),
]


def upgrade():
    op.create_table(
        'tiers',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('name', sa.String(40), nullable=False),
        sa.Column('description', sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column('permissions', JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column('is_default', sa.Boolean, nullable=False, server_default=sa.text('false')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id', name='pk_tiers'),
        schema='app')
    op.create_index('ux_tiers_name', 'tiers', [sa.text('lower(name)')], unique=True, schema='app')
    op.create_index('ux_tiers_is_default', 'tiers', ['is_default'], unique=True, schema='app',
                    postgresql_where=sa.text('is_default'))
    op.add_column('users', sa.Column('tier_id', sa.String(32), nullable=True), schema='app')
    op.create_foreign_key('fk_users_tier_id', 'users', 'tiers', ['tier_id'], ['id'], source_schema='app',
                          referent_schema='app', ondelete='SET NULL')
    ins = sa.text("""INSERT INTO app.tiers(id, name, description, permissions, is_default)
                     VALUES (:id, :n, :d, CAST(:p AS jsonb), :dflt)""")
    for tid, name, desc, perms, dflt in SEED:
        op.get_bind().execute(ins, {'id': tid, 'n': name, 'd': desc, 'p': json.dumps(perms), 'dflt': dflt})


def downgrade():
    op.drop_constraint('fk_users_tier_id', 'users', schema='app', type_='foreignkey')
    op.drop_column('users', 'tier_id', schema='app')
    op.drop_table('tiers', schema='app')
