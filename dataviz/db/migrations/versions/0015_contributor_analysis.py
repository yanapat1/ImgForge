"""Contributors may use Data Analysis too.

Revision ID: 0015
Revises: 0014

0014 gave analysis.use to the default tier (member) only, so the seeded contributor tier, which is member plus
model.upload, had fewer rights than member. Read-only queries on one's own connections fit every tier that has the
member's rights.
"""

from alembic import op

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""UPDATE app.tiers SET permissions = permissions || '["analysis.use"]'::jsonb
                  WHERE id = 'contributor' AND NOT permissions ? 'analysis.use'""")


def downgrade():
    op.execute("UPDATE app.tiers SET permissions = permissions - 'analysis.use' WHERE id = 'contributor'")
