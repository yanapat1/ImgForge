"""The Data Center: buckets, their tags, cached format detections and the undo log (dataviz/datacenter/).

Revision ID: 0006
Revises: 0005

- app.dc_buckets: one data root of one owner: managed (a folder under storage.data_root), local (a linked folder
  in storage.allowed_roots) or s3 (a linked bucket + prefix through one of the owner's connections, read-only).
- app.dc_tags: tags on (bucket, path), path '' = the bucket; auto = the format:<key> tag format detection writes.
- app.dc_format_cache: the last detection of a (bucket, path), keyed by a fingerprint of its top two levels.
- app.dc_undo: one row per ops batch: the steps that reverse it (deleted files wait in <data_root>/.trash/<id>/).
- Tiers: Member and Contributor get the new 'data.manage' permission.
"""

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

PERM = 'data.manage'


def _bucket_fk():
    return sa.Column('bucket_id', sa.String(32), sa.ForeignKey('app.dc_buckets.id', ondelete='CASCADE'),
                     nullable=False)


def upgrade():
    op.create_table(
        'dc_buckets',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('owner_id', sa.String(32), sa.ForeignKey('app.users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('kind', sa.String(8), nullable=False),
        sa.Column('root', sa.Text, nullable=False),
        sa.Column('connection_id', sa.String(36), sa.ForeignKey('app.connections.id', ondelete='SET NULL')),
        sa.Column('s3_bucket', sa.String(255)),
        sa.Column('prefix', sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id', name='pk_dc_buckets'),
        sa.CheckConstraint("kind IN ('managed', 'local', 's3')", name='ck_dc_buckets_kind'),
        schema='app')
    op.create_index('ux_dc_buckets_owner_name', 'dc_buckets', ['owner_id', sa.text('lower(name)')], unique=True,
                    schema='app')
    op.create_table(
        'dc_tags',
        _bucket_fk(),
        sa.Column('path', sa.Text, nullable=False, server_default=sa.text("''")),
        sa.Column('tag', sa.String(100), nullable=False),
        sa.Column('auto', sa.Boolean, nullable=False, server_default=sa.text('false')),
        schema='app')
    op.create_index('ux_dc_tags_bucket_path_tag', 'dc_tags', ['bucket_id', 'path', sa.text('lower(tag)')],
                    unique=True, schema='app')
    op.create_index('ix_dc_tags_tag', 'dc_tags', [sa.text('lower(tag)')], schema='app')
    op.create_table(
        'dc_format_cache',
        _bucket_fk(),
        sa.Column('path', sa.Text, nullable=False),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('result', JSONB, nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('bucket_id', 'path', name='pk_dc_format_cache'),
        schema='app')
    op.create_table(
        'dc_undo',
        sa.Column('id', sa.String(32), primary_key=True),
        _bucket_fk(),
        sa.Column('user_id', sa.String(32), sa.ForeignKey('app.users.id', ondelete='SET NULL')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('manifest', JSONB, nullable=False),
        sa.PrimaryKeyConstraint('id', name='pk_dc_undo'),
        schema='app')
    op.create_index('ix_dc_undo_bucket', 'dc_undo', ['bucket_id', 'created_at'], schema='app')
    _perm(add=True)


def _perm(add):
    bind = op.get_bind()
    for tid in ('member', 'contributor'):
        r = bind.execute(sa.text('SELECT permissions FROM app.tiers WHERE id = :id'), {'id': tid}).first()
        if r is None:
            continue
        perms = [p for p in (r.permissions or []) if p != PERM] + ([PERM] if add else [])
        bind.execute(sa.text('UPDATE app.tiers SET permissions = CAST(:p AS jsonb) WHERE id = :id'),
                     {'p': json.dumps(perms), 'id': tid})


def downgrade():
    _perm(add=False)
    for t in ('dc_undo', 'dc_format_cache', 'dc_tags', 'dc_buckets'):
        op.drop_table(t, schema='app')
