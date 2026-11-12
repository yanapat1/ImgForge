"""Migration 0016 (Data Center -> Bucket, Sessions -> Image, Analysis -> Data): old rows come out under the new names,
'data' (once the Data Center) and 'analysis' (now Data) swap without colliding, and downgrade gives the old rows back."""

import json
import uuid

from alembic import command
from sqlalchemy import text

from conftest import make_user


def _run(fn, rev):
    from dataviz import db
    from dataviz.db import migrate
    with db.engine().connect() as c:
        fn(migrate._config(c), rev)
        c.commit()


def _get(c, sql, **kw):
    return c.execute(text(sql), kw).scalar()


def test_0016_renames_tables_and_stored_names_both_ways(pg):
    from dataviz import db
    u = make_user('mig')
    _run(command.downgrade, '0015')
    bid, sid_d, sid_a, m1 = uuid.uuid4().hex, str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    with db.begin() as c:
        c.execute(text("""INSERT INTO app.dc_buckets(id, owner_id, name, kind, root) VALUES (:b, :u, 'cars', 'managed', '/x')"""),
                  {'b': bid, 'u': u['id']})
        c.execute(text("INSERT INTO app.dc_tags(bucket_id, path, tag) VALUES (:b, '', 'yolo')"), {'b': bid})
        c.execute(text("INSERT INTO app.an_sessions(id, owner_id, name) VALUES (:s, :u, 'q')"), {'s': sid_a, 'u': u['id']})
        c.execute(text("""UPDATE app.tiers SET permissions = '["data.manage", "analysis.use", "analysis.write", "llm.use"]'
                          WHERE id = 'member'"""))
        for sid, role, page in ((sid_d, 'data', {'role': 'data', 'screen': 'home', 'label': 'Home'}),
                                (sid_a, 'analysis', {'role': 'analysis', 'screen': 'analysis', 'label': 'Data Analysis'})):
            c.execute(text("""INSERT INTO app.llm_sessions(id, owner_id, role, last_page)
                              VALUES (:s, :u, :r, CAST(:p AS jsonb))"""), {'s': sid, 'u': u['id'], 'r': role, 'p': json.dumps(page)})
        c.execute(text("""INSERT INTO app.llm_messages(id, session_id, seq, role, page)
                          VALUES (:m, :s, 1, 'user', CAST(:p AS jsonb))"""),
                  {'m': m1, 's': sid_d, 'p': json.dumps({'role': 'data', 'screen': 'data', 'label': 'Data Center · val'})})
        c.execute(text("""INSERT INTO app.user_prefs(user_id, data) VALUES (:u, CAST(:d AS jsonb))
                          ON CONFLICT (user_id) DO UPDATE SET data = EXCLUDED.data"""),
                  {'u': u['id'], 'd': json.dumps({'dc_view': 'grid', 'homeTab': 'label', 'analysis_favorites': {'items': []},
                                                  'theme': 'dark'})})
    _run(command.upgrade, 'head')
    with db.connect() as c:
        assert _get(c, 'SELECT name FROM app.buckets WHERE id = :b', b=bid) == 'cars'
        assert _get(c, 'SELECT tag FROM app.bucket_tags WHERE bucket_id = :b', b=bid) == 'yolo'
        assert _get(c, 'SELECT name FROM app.data_sessions WHERE id = :s', s=sid_a) == 'q'
        assert _get(c, "SELECT to_regclass('app.dc_buckets')") is None
        assert _get(c, "SELECT indexname FROM pg_indexes WHERE tablename = 'bucket_tags' AND indexname LIKE 'ux_%'") \
            == 'ux_bucket_tags_bucket_path_tag'
        assert _get(c, "SELECT permissions FROM app.tiers WHERE id = 'member'") == ['bucket.manage', 'data.use', 'data.write', 'llm.use']
        assert _get(c, 'SELECT role FROM app.llm_sessions WHERE id = :s', s=sid_d) == 'bucket'
        assert _get(c, 'SELECT role FROM app.llm_sessions WHERE id = :s', s=sid_a) == 'data'
        assert _get(c, 'SELECT last_page FROM app.llm_sessions WHERE id = :s', s=sid_d) == {'role': 'bucket', 'screen': 'image', 'label': 'Image'}
        assert _get(c, 'SELECT last_page FROM app.llm_sessions WHERE id = :s', s=sid_a) == {'role': 'data', 'screen': 'data', 'label': 'Data'}
        assert _get(c, 'SELECT page FROM app.llm_messages WHERE id = :m', m=m1) == {'role': 'bucket', 'screen': 'bucket', 'label': 'Bucket · val'}
        assert _get(c, 'SELECT data FROM app.user_prefs WHERE user_id = :u', u=u['id']) == {
            'bk_view': 'grid', 'imageTab': 'label', 'data_favorites': {'items': []}, 'theme': 'dark'}
    _run(command.downgrade, '0015')
    with db.connect() as c:
        assert _get(c, 'SELECT name FROM app.dc_buckets WHERE id = :b', b=bid) == 'cars'
        assert _get(c, "SELECT permissions FROM app.tiers WHERE id = 'member'") == ['data.manage', 'analysis.use', 'analysis.write', 'llm.use']
        assert _get(c, 'SELECT role FROM app.llm_sessions WHERE id = :s', s=sid_d) == 'data'
        assert _get(c, 'SELECT role FROM app.llm_sessions WHERE id = :s', s=sid_a) == 'analysis'
        assert _get(c, 'SELECT last_page FROM app.llm_sessions WHERE id = :s', s=sid_d) == {'role': 'data', 'screen': 'home', 'label': 'Home'}
        assert _get(c, 'SELECT page FROM app.llm_messages WHERE id = :m', m=m1) == {'role': 'data', 'screen': 'data', 'label': 'Data Center · val'}
        assert _get(c, 'SELECT data FROM app.user_prefs WHERE user_id = :u', u=u['id'])['homeTab'] == 'label'
    _run(command.upgrade, 'head')
