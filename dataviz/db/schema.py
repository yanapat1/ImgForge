"""
Every table the server keeps in PostgreSQL, in three database schemas:

    app   accounts, login sessions, sessions (= the cards of the home page) + sharing, per-user connections /
          LLM providers / models / preferences, audit log
    lbl   labeling annotations (one shared set of tables, every row carries project_id = the label session id)
    vec   feature vectors (pgvector): one registry row per (dataset fingerprint, feature) + one table per set

This module is what the code queries. The Alembic migrations (db/migrations/versions) create it; a test
(tests/test_db_schema.py) checks that `upgrade head` and this metadata agree, so a column added here without a
migration fails the suite.

Conventions: ids are text (existing ids are uuid hex of various lengths and must survive the legacy import),
users.id is a uuid hex32. Times in `app` are timestamptz; the labeling store keeps epoch seconds (double)
because its API, its card stats and the visualizer's live-source fingerprint all work in epoch floats. JSON is
JSONB. Secrets are Fernet ciphertext (db/crypto.py) in bytea columns named *secrets*.
"""

from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, DateTime, Float, ForeignKey, Index, Integer,
                        LargeBinary, MetaData, PrimaryKeyConstraint, String, Table, Text, func, text)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

NAMING = {'ix': 'ix_%(table_name)s_%(column_0_N_name)s', 'uq': 'uq_%(table_name)s_%(column_0_N_name)s',
          'ck': 'ck_%(table_name)s_%(constraint_name)s', 'fk': 'fk_%(table_name)s_%(column_0_name)s',
          'pk': 'pk_%(table_name)s'}
metadata = MetaData(naming_convention=NAMING)

ROLES = ('viewer', 'labeler')              # session_members.role; the owner / admins are implicit
VISIBILITY = ('private', 'global')
SESSION_KINDS = ('viz', 'label')


def _now(name, **kw):
    return Column(name, DateTime(timezone=True), nullable=False, server_default=func.now(), **kw)


def _ts(name, **kw):
    return Column(name, DateTime(timezone=True), **kw)


def _user_fk(name='user_id', ondelete='CASCADE', nullable=False, **kw):
    return Column(name, String(32), ForeignKey('app.users.id', ondelete=ondelete), nullable=nullable, **kw)


def _session_fk(name='session_id', **kw):
    return Column(name, String(36), ForeignKey('app.sessions.id', ondelete='CASCADE'), nullable=False, **kw)


# ==================================================================================================== app

users = Table(
    'users', metadata,
    Column('id', String(32), primary_key=True),
    Column('username', String(32), nullable=False),
    Column('email', String(254), nullable=False),
    Column('display_name', String(80), nullable=False),
    Column('password_hash', Text, nullable=False),
    Column('is_admin', Boolean, nullable=False, server_default=text('false')),
    Column('is_active', Boolean, nullable=False, server_default=text('true')),
    Column('avatar', LargeBinary),                       # 256 px WebP, re-encoded by the server
    Column('avatar_mime', String(40)),
    Column('avatar_ver', Integer, nullable=False, server_default=text('0')),   # bumps on change: cache-busting URL
    Column('failed_logins', Integer, nullable=False, server_default=text('0')),
    _ts('locked_until'),
    _now('created_at'),
    _ts('last_login_at'),
    Index('ux_users_username', func.lower(text('username')), unique=True),
    Index('ux_users_email', func.lower(text('email')), unique=True),
    schema='app')

auth_sessions = Table(
    'auth_sessions', metadata,
    Column('token_hash', LargeBinary, primary_key=True),          # sha256 of the cookie value, never the token
    _user_fk(),
    _now('created_at'),
    _now('last_seen_at'),
    _ts('expires_at', nullable=False),                            # absolute limit; idle limit uses last_seen_at
    Column('ip', String(64)),
    Column('user_agent', String(300)),
    Index('ix_auth_sessions_user', 'user_id'),
    schema='app')

app_settings = Table(
    'app_settings', metadata,
    Column('key', String(80), primary_key=True),
    Column('value', JSONB, nullable=False),
    schema='app')

audit_log = Table(
    'audit_log', metadata,
    Column('id', BigInteger, primary_key=True, autoincrement=True),
    _now('at'),
    _user_fk(ondelete='SET NULL', nullable=True),
    Column('action', String(60), nullable=False),
    Column('target', String(200)),
    Column('meta', JSONB),
    Column('ip', String(64)),
    Index('ix_audit_log_at', 'at'),
    schema='app')

user_prefs = Table(
    'user_prefs', metadata,
    Column('user_id', String(32), ForeignKey('app.users.id', ondelete='CASCADE'), primary_key=True),
    Column('data', JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    _now('updated_at'),
    schema='app')

# a user's own S3 / FTP / SFTP / PostgreSQL / MSSQL / model-server connections; `config` holds the public
# fields, `secrets` the Fernet-encrypted secret ones (secret_key, access_key, private_key, api_key)
connections = Table(
    'connections', metadata,
    Column('id', String(36), primary_key=True),
    _user_fk('owner_id'),
    Column('kind', String(20), nullable=False),
    Column('name', String(200), nullable=False),
    Column('config', JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column('secrets', LargeBinary),
    _now('created_at'), _now('updated_at'),
    Index('ux_connections_owner_name', 'owner_id', func.lower(text('name')), unique=True),
    schema='app')

llm_providers = Table(
    'llm_providers', metadata,
    Column('id', String(36), primary_key=True),
    _user_fk('owner_id'),
    Column('name', String(200), nullable=False),
    Column('kind', String(20), nullable=False),
    Column('config', JSONB, nullable=False, server_default=text("'{}'::jsonb")),   # base_url, model, verify, ...
    Column('secrets', LargeBinary),                                                # {api_key}
    _now('created_at'), _now('updated_at'),
    Index('ix_llm_providers_owner', 'owner_id'),
    schema='app')

llm_user_config = Table(
    'llm_user_config', metadata,
    Column('user_id', String(32), ForeignKey('app.users.id', ondelete='CASCADE'), primary_key=True),
    Column('active_provider_id', String(36), ForeignKey('app.llm_providers.id', ondelete='SET NULL')),
    Column('features', JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column('budget', JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    schema='app')

models = Table(
    'models', metadata,
    Column('id', String(36), primary_key=True),
    _user_fk('owner_id'),
    Column('name', String(200), nullable=False),
    Column('config', JSONB, nullable=False, server_default=text("'{}'::jsonb")),   # the models.json entry, minus secrets
    Column('secrets', LargeBinary),                                                # {api_key, headers}
    _now('created_at'), _now('updated_at'),
    Index('ux_models_owner_name', 'owner_id', func.lower(text('name')), unique=True),
    schema='app')

# one row per card of the home page, both kinds; access is decided here for both (auth/access.py)
sessions = Table(
    'sessions', metadata,
    Column('id', String(36), primary_key=True),
    Column('kind', String(8), nullable=False),
    _user_fk('owner_id', ondelete='RESTRICT'),        # deleting a user transfers or deletes their sessions first
    Column('name', String(200), nullable=False),
    Column('visibility', String(10), nullable=False, server_default=text("'private'")),
    Column('tags', ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    _now('created_at'), _now('updated_at'),
    CheckConstraint("kind IN ('viz', 'label')", name='kind'),
    CheckConstraint("visibility IN ('private', 'global')", name='visibility'),
    Index('ux_sessions_owner_kind_name', 'owner_id', 'kind', func.lower(text('name')), unique=True),
    Index('ix_sessions_visibility', 'visibility'),
    schema='app')

viz_sessions = Table(
    'viz_sessions', metadata,
    Column('session_id', String(36), ForeignKey('app.sessions.id', ondelete='CASCADE'), primary_key=True),
    Column('source_kind', String(12), nullable=False),            # local | s3 | labeling | embeddings | union
    Column('path', Text),
    Column('connection_id', String(36), ForeignKey('app.connections.id', ondelete='RESTRICT')),
    Column('bucket', String(255)),
    Column('prefix', Text),
    Column('mode', String(10)),
    Column('project_id', String(36), ForeignKey('app.sessions.id', ondelete='SET NULL')),   # gone: card shows broken
    Column('task_id', String(36)),
    Column('only_done', Boolean, nullable=False, server_default=text('false')),
    Column('options', JSONB, nullable=False, server_default=text("'{}'::jsonb")),   # 0003: unit, class_from_folder
    CheckConstraint("source_kind IN ('local', 's3', 'labeling', 'embeddings', 'union')", name='source_kind'),  # 0004
    schema='app')

# the members of a combined ('union') visualize session, in order; alias = the name prefix of its images (data/union.py)
union_members = Table(
    'union_members', metadata,
    Column('union_id', String(36), ForeignKey('app.sessions.id', ondelete='CASCADE'), nullable=False),
    Column('member_id', String(36), ForeignKey('app.sessions.id', ondelete='CASCADE'), nullable=False),
    Column('position', Integer, nullable=False),
    Column('alias', String(24), nullable=False),
    PrimaryKeyConstraint('union_id', 'member_id'),
    Index('ux_union_members_union_id_alias', 'union_id', 'alias', unique=True),
    Index('ix_union_members_member_id', 'member_id'),
    schema='app')

label_projects = Table(
    'label_projects', metadata,
    Column('session_id', String(36), ForeignKey('app.sessions.id', ondelete='CASCADE'), primary_key=True),
    Column('description', Text, nullable=False, server_default=text("''")),
    Column('source', JSONB, nullable=False),
    Column('export_default', JSONB),
    Column('n_media', Integer, nullable=False, server_default=text('0')),
    Column('task_ids', ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column('model_connections', ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column('review', Boolean, nullable=False, server_default=text('false')),
    schema='app')

session_members = Table(
    'session_members', metadata,
    _session_fk(),
    _user_fk(),
    Column('role', String(10), nullable=False),
    _user_fk('added_by', ondelete='SET NULL', nullable=True),
    _now('added_at'),
    PrimaryKeyConstraint('session_id', 'user_id'),
    CheckConstraint("role IN ('viewer', 'labeler')", name='role'),
    Index('ix_session_members_user', 'user_id'),
    schema='app')

session_opens = Table(
    'session_opens', metadata,
    _user_fk(),
    _session_fk(),
    _now('opened_at'),
    PrimaryKeyConstraint('user_id', 'session_id'),
    schema='app')

# per user per session: the visualizer UI (was ui_settings.json), filter + presets, label page position / task
user_session_prefs = Table(
    'user_session_prefs', metadata,
    _user_fk(),
    _session_fk(),
    Column('data', JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    _now('updated_at'),
    PrimaryKeyConstraint('user_id', 'session_id'),
    schema='app')

llm_chats = Table(
    'llm_chats', metadata,
    _user_fk(),
    _session_fk(),
    Column('messages', JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    _now('updated_at'),
    PrimaryKeyConstraint('user_id', 'session_id'),
    schema='app')

# shared by everyone with access to the session: curation issues marked "ignore"
dataset_state = Table(
    'dataset_state', metadata,
    Column('session_id', String(36), ForeignKey('app.sessions.id', ondelete='CASCADE'), primary_key=True),
    Column('data', JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    _now('updated_at'),
    schema='app')

# box tags of the visualizer (was tags.json); sample ids are positions in one dataset version
box_tags = Table(
    'box_tags', metadata,
    Column('fingerprint', String(64), nullable=False),
    Column('tag', String(100), nullable=False),
    Column('sample_ids', ARRAY(Integer), nullable=False, server_default=text("'{}'::int[]")),
    PrimaryKeyConstraint('fingerprint', 'tag'),
    schema='app')

curation_undo = Table(
    'curation_undo', metadata,
    Column('id', String(40), primary_key=True),
    _session_fk(),
    _user_fk(ondelete='SET NULL', nullable=True),
    _now('at'),
    Column('summary', JSONB),
    Column('files', JSONB),
    Index('ix_curation_undo_session', 'session_id', 'at'),
    schema='app')

jobs_log = Table(
    'jobs_log', metadata,
    Column('id', String(40), primary_key=True),
    _user_fk(ondelete='SET NULL', nullable=True),
    Column('session_id', String(36)),                 # no FK: the log outlives a deleted session
    Column('label', String(300)),
    Column('status', String(12)),
    _ts('started_at'), _ts('ended_at'),
    Column('error', Text),
    Index('ix_jobs_log_started', 'started_at'),
    schema='app')

# ==================================================================================================== lbl
# the labeling store (labeling/store.py). Same columns as the portable labeling/schema.py, plus project_id on
# every table and the two former SQLite-only columns (model_run_at, review). Times: epoch seconds.


def _project_fk():
    return Column('project_id', String(36), ForeignKey('lbl.projects.id', ondelete='CASCADE'), nullable=False)


lbl_projects = Table(
    'projects', metadata,
    Column('id', String(36), ForeignKey('app.sessions.id', ondelete='CASCADE'), primary_key=True),
    Column('name', Text, nullable=False),
    Column('description', Text),
    Column('created_at', Float), Column('updated_at', Float),
    Column('meta', JSONB),
    schema='lbl')

lbl_tasks = Table(
    'tasks', metadata,
    Column('id', String(36), primary_key=True),
    _project_fk(),
    Column('name', Text, nullable=False),
    Column('task_type', String(40), nullable=False),
    Column('position', Integer, nullable=False, server_default=text('0')),
    Column('config', JSONB),
    Column('created_at', Float),
    Column('is_deleted', Boolean, nullable=False, server_default=text('false')),
    Index('ix_lbl_tasks_project', 'project_id', 'position'),
    schema='lbl')

lbl_label_classes = Table(
    'label_classes', metadata,
    Column('id', String(36), primary_key=True),
    _project_fk(),
    Column('task_id', String(36), ForeignKey('lbl.tasks.id', ondelete='CASCADE'), nullable=False),
    Column('name', Text, nullable=False),
    Column('color', String(7), nullable=False),
    Column('position', Integer, nullable=False, server_default=text('0')),
    Column('hotkey', String(20)),
    Column('parent_id', String(36)),
    Column('attributes_schema', JSONB),
    Column('is_deleted', Boolean, nullable=False, server_default=text('false')),
    Index('ix_lbl_classes_task', 'task_id', 'position'),
    Index('ix_lbl_classes_project', 'project_id'),
    schema='lbl')

lbl_media = Table(
    'media', metadata,
    Column('id', String(36), primary_key=True),
    _project_fk(),
    Column('media_type', String(20), nullable=False, server_default=text("'image'")),
    Column('uri', Text),
    Column('file_name', Text, nullable=False),
    Column('mime', String(80)),
    Column('checksum', String(40)),
    Column('size_bytes', BigInteger),
    Column('width', Integer), Column('height', Integer), Column('depth', Integer),
    Column('duration_ms', BigInteger), Column('frame_count', Integer),
    Column('position', Integer, nullable=False, server_default=text('0')),
    Column('meta', JSONB),
    Column('created_at', Float),
    Index('ix_lbl_media_order', 'project_id', 'position', 'id'),
    Index('ux_lbl_media_name', 'project_id', 'file_name', unique=True),
    Index('ix_media_project_id_checksum', 'project_id', 'checksum'),                     # 0004
    schema='lbl')

lbl_annotations = Table(
    'annotations', metadata,
    Column('id', String(36), primary_key=True),
    _project_fk(),
    Column('task_id', String(36), ForeignKey('lbl.tasks.id', ondelete='CASCADE'), nullable=False),
    Column('media_id', String(36), ForeignKey('lbl.media.id', ondelete='CASCADE'), nullable=False),
    Column('class_id', String(36)),
    Column('geometry_type', String(30), nullable=False),
    Column('geometry', JSONB),
    Column('bbox_x', Float), Column('bbox_y', Float), Column('bbox_w', Float), Column('bbox_h', Float),
    Column('frame_index', Integer),
    Column('t_start_ms', BigInteger), Column('t_end_ms', BigInteger),
    Column('char_start', Integer), Column('char_end', Integer),
    Column('value_text', Text),
    Column('attributes', JSONB),
    Column('score', Float),
    Column('source', String(20), nullable=False, server_default=text("'manual'")),
    Column('parent_id', String(36)),
    Column('z_order', Integer, nullable=False, server_default=text('0')),
    Column('created_by', String(32), ForeignKey('app.users.id', ondelete='SET NULL')),
    Column('created_at', Float), Column('updated_at', Float),
    Column('version', Integer, nullable=False, server_default=text('1')),
    Column('is_deleted', Boolean, nullable=False, server_default=text('false')),
    Column('change_seq', BigInteger),        # 0004: set by a trigger from lbl.change_seq on every write (data/live.py)
    Index('ix_lbl_ann_task_media', 'task_id', 'media_id'),
    Index('ix_lbl_ann_media_live', 'media_id', postgresql_where=text('NOT is_deleted')),
    Index('ix_lbl_ann_class_live', 'class_id', postgresql_where=text('NOT is_deleted')),
    Index('ix_lbl_ann_project', 'project_id'),
    Index('ix_annotations_project_id_change_seq', 'project_id', 'change_seq'),
    schema='lbl')

lbl_annotation_relations = Table(
    'annotation_relations', metadata,
    Column('id', String(36), primary_key=True),
    _project_fk(),
    Column('from_id', String(36), nullable=False),
    Column('to_id', String(36), nullable=False),
    Column('relation_type', Text, nullable=False),
    Column('attributes', JSONB),
    Column('is_deleted', Boolean, nullable=False, server_default=text('false')),
    schema='lbl')

lbl_media_task_status = Table(
    'media_task_status', metadata,
    Column('media_id', String(36), ForeignKey('lbl.media.id', ondelete='CASCADE'), nullable=False),
    Column('task_id', String(36), ForeignKey('lbl.tasks.id', ondelete='CASCADE'), nullable=False),
    _project_fk(),
    Column('status', String(12), nullable=False, server_default=text("'todo'")),
    Column('n_annotations', Integer, nullable=False, server_default=text('0')),
    Column('version', Integer, nullable=False, server_default=text('0')),
    Column('committed_by', String(32), ForeignKey('app.users.id', ondelete='SET NULL')),
    Column('committed_at', Float), Column('updated_at', Float),
    Column('model_run_at', Float),
    Column('review', String(10)),
    Column('change_seq', BigInteger),        # 0004: as lbl.annotations
    PrimaryKeyConstraint('media_id', 'task_id'),
    Index('ix_lbl_mts_task_status', 'task_id', 'status'),
    Index('ix_lbl_mts_project', 'project_id'),
    Index('ix_media_task_status_project_id_change_seq', 'project_id', 'change_seq'),
    schema='lbl')

lbl_export_runs = Table(
    'export_runs', metadata,
    Column('id', String(36), primary_key=True),
    _project_fk(),
    Column('task_id', String(36)),
    Column('target', String(20), nullable=False),
    Column('target_uri', Text),
    Column('options', JSONB),
    Column('started_at', Float), Column('finished_at', Float),
    Column('n_media', Integer), Column('n_annotations', Integer),
    Column('status', String(12)),
    Column('error', Text),
    schema='lbl')

# ==================================================================================================== vec
# one row per computed feature space of one dataset version; its vectors live in vec.emb_<id> (a table per set
# so the halfvec column has a fixed dimension and an HNSW index can be built on it). See ml/vectors.py.

feature_sets = Table(
    'feature_sets', metadata,
    Column('id', Integer, primary_key=True, autoincrement=True),
    Column('fingerprint', String(64), nullable=False),
    Column('feature_key', String(200), nullable=False),
    Column('dim', Integer, nullable=False),
    Column('n', Integer, nullable=False),
    Column('complete', Boolean, nullable=False, server_default=text('false')),   # true once every row is written
    Column('has_index', Boolean, nullable=False, server_default=text('false')),
    _now('created_at'),
    Index('ux_feature_sets_fp_key', 'fingerprint', 'feature_key', unique=True),
    schema='vec')

SCHEMAS = ('app', 'lbl', 'vec')
EXTENSIONS = ('vector',)
