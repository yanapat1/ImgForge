"""The pages are renamed: Data Center -> Bucket, Sessions -> Image, Analysis -> Data.

Revision ID: 0016
Revises: 0015

Tables: dc_buckets -> buckets, dc_<x> -> bucket_<x>, an_<x> -> data_<x> (their indexes and constraints follow the
naming convention: the old table name in a name becomes the new one). Stored names move with them, in one CASE per
value so 'data' (once the Data Center) and 'analysis' (now Data) never collide:

    tier permissions        data.manage -> bucket.manage, analysis.use / .write -> data.use / .write
    assistant roles         'data' -> 'bucket', 'analysis' -> 'data' (llm_sessions, llm_user_prompts, plugin manifests,
                            llm_user_config.chat keys); feature data_center -> bucket_data
    page markers            llm_messages.page / llm_sessions.last_page: screen data -> bucket, analysis -> data,
                            home -> image (the old home was the sessions list), and the label text
    user prefs              dc_* -> bk_*, analysis_favorites / analysis_ai_access / analysis_sample_added -> data_*,
                            homeTab / homeScope -> imageTab / imageScope
    app setting             analysis_sample -> data_sample
"""

import json

import sqlalchemy as sa
from alembic import op

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None

TABLES = [('dc_buckets', 'buckets'), ('dc_tags', 'bucket_tags'), ('dc_format_cache', 'bucket_format_cache'),
          ('dc_s3_local', 'bucket_s3_local'), ('dc_undo', 'bucket_undo'),
          ('an_sessions', 'data_sessions'), ('an_history', 'data_history'), ('an_scripts', 'data_scripts'),
          ('an_column_docs', 'data_column_docs'), ('an_dashboards', 'data_dashboards')]
PERMS = {'data.manage': 'bucket.manage', 'analysis.use': 'data.use', 'analysis.write': 'data.write'}
ROLES = {'data': 'bucket', 'analysis': 'data'}
SCREENS = {'data': 'bucket', 'analysis': 'data', 'home': 'image'}
FEATURES = {'data_center': 'bucket_data'}
PREFS = {'dc_view': 'bk_view', 'dc_cards': 'bk_cards', 'dc_sort': 'bk_sort', 'dc_bucket_sort': 'bk_bucket_sort',
         'dc_ai_open': 'bk_ai_open', 'dc_export': 'bk_export', 'dc_tree_w': 'bk_tree_w', 'dc_ai_h': 'bk_ai_h',
         'analysis_favorites': 'data_favorites', 'analysis_ai_access': 'data_ai_access',
         'analysis_sample_added': 'data_sample_added', 'homeTab': 'imageTab', 'homeScope': 'imageScope'}
SETTINGS = {'analysis_sample': 'data_sample'}
LABELS = [('Data Center', 'Bucket'), ('Data Analysis', 'Data')]


def _inv(m):
    return {v: k for k, v in m.items()}


def _case(col, m):
    """CASE col WHEN old THEN new ... ELSE col END, every pair at once (no chain: data -> bucket and analysis -> data)."""
    whens = ' '.join(f"WHEN '{a}' THEN '{b}'" for a, b in m.items())
    return f'CASE {col} {whens} ELSE {col} END'


def _tables(pairs):
    conn = op.get_bind()
    for old, new in pairs:
        op.execute(f'ALTER TABLE app.{old} RENAME TO {new}')
        # constraints first (a primary key / unique constraint renames its index too), then the plain indexes
        for (name,) in conn.execute(sa.text("""SELECT conname FROM pg_constraint
                                               WHERE conrelid = 'app.{t}'::regclass""".format(t=new))).fetchall():
            if old in name:
                op.execute(f'ALTER TABLE app.{new} RENAME CONSTRAINT "{name}" TO "{name.replace(old, new)}"')
        for (name,) in conn.execute(sa.text("SELECT indexname FROM pg_indexes WHERE schemaname = 'app' AND tablename = :t"),
                                    {'t': new}).fetchall():
            if old in name:
                op.execute(f'ALTER INDEX app."{name}" RENAME TO "{name.replace(old, new)}"')


def _remap_json(conn, table, key_col, col, fn, where=''):
    """Rewrite one JSONB column row by row in Python (only rows that change are written; `where` narrows the read)."""
    rows = conn.execute(sa.text(f'SELECT {key_col}, {col} FROM app.{table}' + (f' WHERE {where}' if where else ''))).fetchall()
    for k, v in rows:
        nv = fn(v)
        if nv != v:
            conn.execute(sa.text(f'UPDATE app.{table} SET {col} = CAST(:v AS jsonb) WHERE {key_col} = :k'),
                         {'v': json.dumps(nv), 'k': k})


def _keys(m):
    def fn(d):
        if not isinstance(d, dict):
            return d
        out = {k: v for k, v in d.items() if k not in m}
        for a, b in m.items():
            if a in d and b not in out:
                out[b] = d[a]
        return out
    return fn


def _page(screens, roles, labels):
    names = {'home': 'Home', 'image': 'Image'}
    def fn(p):
        if not isinstance(p, dict):
            return p
        p = dict(p)
        if p.get('screen') in screens:
            old, p['screen'] = p['screen'], screens[p['screen']]
            if p.get('label') == names.get(old) and p['screen'] in names:       # the sessions page: Home -> Image
                p['label'] = names[p['screen']]
        if p.get('role') in roles:
            p['role'] = roles[p['role']]
        if isinstance(p.get('label'), str):
            for a, b in labels:
                p['label'] = p['label'].replace(a, b)
        return p
    return fn


def _manifest(roles):
    def fn(m):
        if not isinstance(m, dict):
            return m
        m = json.loads(json.dumps(m))
        for group in ('tools', 'skills', 'prompts'):
            for item in m.get(group) or []:
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get('roles'), list):
                    item['roles'] = [roles.get(r, r) for r in item['roles']]
                if isinstance(item.get('role'), str):
                    item['role'] = roles.get(item['role'], item['role'])
        return m
    return fn


def _values(perms, roles, screens, features, prefs, settings, labels):
    conn = op.get_bind()
    olds = ', '.join(f"'{p}'" for p in perms)
    op.execute(f"""UPDATE app.tiers SET permissions = (
                       SELECT coalesce(jsonb_agg({_case('p', perms)}), '[]'::jsonb) FROM jsonb_array_elements_text(permissions) p)
                   WHERE permissions ?| array[{olds}]""")
    rl = ', '.join(f"'{r}'" for r in roles)
    op.execute(f'UPDATE app.llm_sessions SET role = {_case("role", roles)} WHERE role IN ({rl})')
    op.execute(f'UPDATE app.llm_user_prompts SET role = {_case("role", roles)} WHERE role IN ({rl})')
    page = _page(screens, roles, labels)
    sl = ', '.join(f"'{x}'" for x in screens)
    _remap_json(conn, 'llm_messages', 'id', 'page', page, f"page->>'screen' IN ({sl}) OR page->>'role' IN ({rl})")
    _remap_json(conn, 'llm_sessions', 'id', 'last_page', page,
                f"last_page->>'screen' IN ({sl}) OR last_page->>'role' IN ({rl})")
    _remap_json(conn, 'llm_user_config', 'user_id', 'chat', _keys(roles))
    _remap_json(conn, 'llm_user_config', 'user_id', 'features', _keys(features))
    _remap_json(conn, 'llm_plugins', 'id', 'manifest', _manifest(roles))
    _remap_json(conn, 'user_prefs', 'user_id', 'data', _keys(prefs))
    for a, b in settings.items():
        op.execute(f"UPDATE app.app_settings SET key = '{b}' WHERE key = '{a}' "
                   f"AND NOT EXISTS (SELECT 1 FROM app.app_settings WHERE key = '{b}')")


def upgrade():
    _tables(TABLES)
    _values(PERMS, ROLES, SCREENS, FEATURES, PREFS, SETTINGS, LABELS)


def downgrade():
    _values(_inv(PERMS), _inv(ROLES), _inv(SCREENS), _inv(FEATURES), _inv(PREFS), _inv(SETTINGS),
            [('Bucket', 'Data Center')])
    _tables([(new, old) for old, new in TABLES])
