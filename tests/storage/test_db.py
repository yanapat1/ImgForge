"""
Database export against real PostgreSQL 16 / MSSQL 2022 containers (docker; skipped with the reason when
docker, the image or the port is unavailable): schema creation, idempotent upserts (export twice = same
rows), Thai class names, soft-deleted annotations mirrored, one export_runs row per task, per-task views.
"""

import sqlite3
import time
import uuid

import orjson
import pytest
from sqlalchemy import create_engine, func, select, text

from dataviz.labeling import db_export, schema as S

pytest_plugins = ['conftest_io']             # fixtures: pg_conn, mssql_conn (docker)

N_MEDIA = 20


class MiniStore:
    """The smallest ProjectStore stand-in: schema.make_metadata('') in SQLite, raw rows from iter_rows."""

    def __init__(self, path):
        self.path = str(path)
        eng = create_engine(f'sqlite:///{self.path}')
        S.make_metadata('').create_all(eng)
        eng.dispose()

    def insert(self, table, rows):
        with sqlite3.connect(self.path) as c:
            cols = list(rows[0])
            c.executemany(f'INSERT INTO {table} ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})',
                          [tuple(r[k] for k in cols) for r in rows])

    def iter_rows(self, table, where=None, batch=1000):
        q, args = f'SELECT * FROM {table}', []
        if where:
            q += ' WHERE ' + ' AND '.join(f'{k} = ?' for k in where)
            args = list(where.values())
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        try:
            cur = c.execute(q, args)
            while rows := cur.fetchmany(batch):
                yield [dict(r) for r in rows]
        finally:
            c.close()


def _id():
    return uuid.uuid4().hex


def build_mini(tmp_path):
    """1 project, 2 tasks (detect: แมว / dog, segment: house), 20 images; detect has 2 boxes per image
    (one soft-deleted), segment 1 polygon; statuses alternate done / in_progress."""
    st = MiniStore(tmp_path / 'annotations.db')
    pid, t = _id(), time.time()
    det, seg = _id(), _id()
    cat, dog, house = _id(), _id(), _id()
    st.insert('projects', [{'id': pid, 'name': 'โครงการ cats', 'description': '', 'created_at': t, 'updated_at': t,
                            'meta': '{}'}])
    st.insert('tasks', [{'id': det, 'project_id': pid, 'name': 'Detect', 'task_type': 'object_detection', 'position': 0,
                         'config': '{}', 'created_at': t, 'is_deleted': 0},
                        {'id': seg, 'project_id': pid, 'name': 'Segment', 'task_type': 'segmentation', 'position': 1,
                         'config': '{}', 'created_at': t, 'is_deleted': 0}])
    st.insert('label_classes', [
        {'id': cat, 'task_id': det, 'name': 'แมว', 'color': '#ff0000', 'position': 0, 'is_deleted': 0},
        {'id': dog, 'task_id': det, 'name': 'dog', 'color': '#00ff00', 'position': 1, 'is_deleted': 0},
        {'id': house, 'task_id': seg, 'name': 'บ้าน house', 'color': '#0000ff', 'position': 0, 'is_deleted': 0}])
    media, anns, status = [], [], []
    for i in range(N_MEDIA):
        mid = _id()
        media.append({'id': mid, 'project_id': pid, 'media_type': 'image', 'file_name': f'ภาพ_{i:03d}.jpg', 'width': 640,
                      'height': 480, 'position': i, 'meta': '{"rel": "a"}', 'created_at': t})
        for k, deleted in ((0, 0), (1, 1)):
            anns.append({'id': _id(), 'project_id': pid, 'task_id': det, 'media_id': mid, 'class_id': cat if k == 0 else dog,
                         'geometry_type': 'bbox', 'geometry': orjson.dumps({'x': .1, 'y': .2, 'w': .3, 'h': .4}).decode(),
                         'bbox_x': .1, 'bbox_y': .2, 'bbox_w': .3, 'bbox_h': .4, 'attributes': '{}', 'source': 'manual',
                         'z_order': k, 'created_at': t, 'updated_at': '2024-05-01T10:00:00Z', 'version': 1,
                         'is_deleted': deleted})
        anns.append({'id': _id(), 'project_id': pid, 'task_id': seg, 'media_id': mid, 'class_id': house,
                     'geometry_type': 'polygon', 'geometry': '{"points": [[0.1,0.1],[0.5,0.1],[0.3,0.6]]}',
                     'bbox_x': .1, 'bbox_y': .1, 'bbox_w': .4, 'bbox_h': .5, 'attributes': None, 'source': 'manual',
                     'z_order': 0, 'created_at': t, 'updated_at': t, 'version': 2, 'is_deleted': 0})
        for tid, n in ((det, 1), (seg, 1)):
            status.append({'media_id': mid, 'task_id': tid, 'status': 'done' if i % 2 == 0 else 'in_progress',
                           'n_annotations': n, 'version': 1, 'committed_at': t if i % 2 == 0 else None, 'updated_at': t})
    st.insert('media', media)
    st.insert('annotations', anns)
    st.insert('media_task_status', status)
    project = {'id': pid, 'name': 'โครงการ cats'}
    return st, project, {'det': det, 'seg': seg}


@pytest.fixture(params=['postgres', 'mssql'])
def db_conn(request):
    base = request.getfixturevalue('pg_conn' if request.param == 'postgres' else 'mssql_conn')
    conn = dict(base)
    tag = uuid.uuid4().hex[:6]
    if request.param == 'postgres':
        conn['schema'] = f'lbl_{tag}'                  # a fresh schema per test (created by ensure_schema)
    else:
        conn['table_prefix'] = f't{tag}_'              # a fresh table set per test
    yield conn
    eng = db_export.engine_for(conn)
    try:
        with eng.begin() as c:
            if request.param == 'postgres':
                c.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{conn["schema"]}" CASCADE')
            else:
                for v in c.exec_driver_sql(f"SELECT name FROM sys.views WHERE name LIKE '{conn['table_prefix']}%'").scalars().all():
                    c.exec_driver_sql(f'DROP VIEW [{v}]')
                md = S.make_metadata(conn['table_prefix'])
                md.drop_all(c)
    finally:
        eng.dispose()


def counts(conn, pid):
    md = S.make_metadata(db_export._prefix(conn), db_export._schema(conn))
    t = S.tables(md, db_export._prefix(conn))
    eng = db_export.engine_for(conn)
    try:
        with eng.connect() as c:
            # the fixture gives every test a fresh schema / prefix, so whole-table counts are this project's
            out = {name: c.execute(select(func.count()).select_from(t[name]).where(t[name].c.project_id == pid)).scalar()
                   for name in ('tasks', 'media', 'annotations', 'export_runs')}
            out['projects'] = c.execute(select(func.count()).select_from(t['projects']).where(t['projects'].c.id == pid)).scalar()
            out['label_classes'] = c.execute(select(func.count()).select_from(t['label_classes'])).scalar()
            out['media_task_status'] = c.execute(select(func.count()).select_from(t['media_task_status'])).scalar()
            out['deleted'] = c.execute(select(func.count()).select_from(t['annotations'])
                                       .where(t['annotations'].c.is_deleted == True)).scalar()  # noqa: E712
            return out
    finally:
        eng.dispose()


def test_export_idempotent_unicode_runs_views(db_conn, tmp_path):
    st, project, tids = build_mini(tmp_path)
    task_ids = [tids['det'], tids['seg']]
    res1 = db_export.export_to_db(db_conn, st, project, task_ids, {'create_views': True})
    assert res1['tasks'][tids['det']]['n_annotations'] == N_MEDIA
    assert res1['tasks'][tids['det']]['n_rows'] == 2 * N_MEDIA                      # soft-deleted rows sent too
    assert res1['tasks'][tids['seg']]['n_media'] == N_MEDIA
    c1 = counts(db_conn, project['id'])
    assert c1 == {'projects': 1, 'tasks': 2, 'media': N_MEDIA, 'annotations': 3 * N_MEDIA, 'export_runs': 2,
                  'label_classes': 3, 'media_task_status': 2 * N_MEDIA, 'deleted': N_MEDIA}
    res2 = db_export.export_to_db(db_conn, st, project, task_ids, {'create_views': True})
    c2 = counts(db_conn, project['id'])
    assert c2 == {**c1, 'export_runs': 4}                                         # data idempotent, runs are a log
    assert res2['tasks'][tids['det']]['run_id'] != res1['tasks'][tids['det']]['run_id']

    prefix, sch = db_export._prefix(db_conn), db_export._schema(db_conn)
    t = S.tables(S.make_metadata(prefix, sch), prefix)
    eng = db_export.engine_for(db_conn)
    try:
        with eng.connect() as c:
            names = set(c.execute(select(t['label_classes'].c.name)).scalars())
            assert names == {'แมว', 'dog', 'บ้าน house'}                            # NVARCHAR / UTF-8 round trip
            assert c.execute(select(t['projects'].c.name)).scalar() == 'โครงการ cats'
            assert c.execute(select(t['media'].c.file_name).where(t['media'].c.position == 3)).scalar() == 'ภาพ_003.jpg'
            runs = c.execute(select(t['export_runs'].c.task_id, t['export_runs'].c.status,
                                    t['export_runs'].c.n_annotations, t['export_runs'].c.target)).all()
            assert sorted((r[0], r[1], r[2], r[3]) for r in runs) == sorted(
                [(tids['det'], 'done', N_MEDIA, 'db'), (tids['seg'], 'done', N_MEDIA, 'db')] * 2)
            geo = c.execute(select(t['annotations'].c.geometry).where(t['annotations'].c.task_id == tids['seg'])).scalars().first()
            assert geo == {'points': [[0.1, 0.1], [0.5, 0.1], [0.3, 0.6]]}
            upd = c.execute(select(t['annotations'].c.updated_at).where(t['annotations'].c.task_id == tids['det'])).scalars().first()
            assert upd.tzinfo is not None and upd.year == 2024 and upd.utcoffset().total_seconds() == 0
            assert c.execute(select(t['schema_info'].c.value).where(t['schema_info'].c.key == 'schema_version')).scalar() == '1'
            # per-task views: live rows of that task only, with class names
            q = db_export.engine_for(db_conn).dialect.identifier_preparer.quote
            vname = res1['tasks'][tids['det']]['view']
            assert vname == f'{prefix}v_detect'
            full = f'{q(sch)}.{q(vname)}' if sch else q(vname)
            rows = c.execute(text(f'SELECT class_name, file_name FROM {full}')).all()
            assert len(rows) == N_MEDIA and {r[0] for r in rows} == {'แมว'}
    finally:
        eng.dispose()

    info = db_export.test(db_conn)
    assert info['ok'] and info['tables_exist'] and info['schema_version'] == 1 and info['server_version']


def test_only_done_and_reexport_one_task(db_conn, tmp_path):
    st, project, tids = build_mini(tmp_path)
    res = db_export.export_to_db(db_conn, st, project, [tids['seg']], {'only_done': True})
    assert res['tasks'][tids['seg']]['n_media'] == N_MEDIA // 2
    assert res['tasks'][tids['seg']]['n_annotations'] == N_MEDIA // 2
    c = counts(db_conn, project['id'])
    assert c['tasks'] == 1 and c['annotations'] == N_MEDIA // 2 and c['export_runs'] == 1 and c['media'] == N_MEDIA


def test_real_project_store(pg, db_conn, tmp_path):
    """Against the real ProjectStore (PostgreSQL lbl.*; its iter_rows already converts types)."""
    from conftest import make_user, new_store
    pid = uuid.uuid4().hex
    st = new_store(make_user('exporter')['id'], 'ร้าน store', pid)
    task = st.add_task('Detect', 'object_detection', None, [{'name': 'แมว'}, {'name': 'dog'}])
    st.add_media([{'file_name': f'f{i}.jpg', 'width': 100, 'height': 80} for i in range(5)])
    mids = st.media_ids()
    cls = task['classes'][0]['id']
    for mid in mids:
        st.put_annotations(mid, task['id'], [{'id': uuid.uuid4().hex, 'class_id': cls, 'geometry_type': 'bbox',
                                              'geometry': {'x': .1, 'y': .1, 'w': .2, 'h': .2}}], 0)
    st.set_status(mids[0], [task['id']], 'commit')
    project = {'id': pid, 'name': 'ร้าน store'}
    r1 = db_export.export_to_db(db_conn, st, project, [task['id']], {})
    r2 = db_export.export_to_db(db_conn, st, project, [task['id']], {})
    assert r1['tasks'][task['id']]['n_annotations'] == r2['tasks'][task['id']]['n_annotations'] == 5
    c = counts(db_conn, pid)
    assert c['annotations'] == 5 and c['media'] == 5 and c['label_classes'] == 2 and c['export_runs'] == 2
    st.close()


def test_engine_url_escaping():
    eng = db_export.engine_for({'kind': 'postgres', 'host': 'h', 'username': 'u@x', 'secret_key': 'p@ss/w:rd#',
                                'database': 'd'})
    assert eng.url.password == 'p@ss/w:rd#' and eng.url.port == 5432
    assert '***' in eng.url.render_as_string(hide_password=True)
    eng.dispose()
    ms = db_export.engine_for({'kind': 'mssql', 'host': 'h', 'username': 'sa', 'secret_key': 'x', 'database': 'd'})
    assert ms.dialect.name == 'mssql' and ms.url.port == 1433
    ms.dispose()
    with pytest.raises(ValueError):
        db_export._prefix({'table_prefix': 'bad;drop'})
    with pytest.raises(ValueError):
        db_export.engine_for({'kind': 's3'})
    assert db_export.to_datetime(0).isoformat() == '1970-01-01T00:00:00+00:00'
    assert db_export.to_datetime('2024-05-01T10:00:00').tzinfo is not None


def _pose_store(tmp_path):
    """A real ProjectStore: pose task (person / crowd); image 0 has a crowd box B, a person set K linked to B and
    a lone crowd box C; image 1 a person set only."""
    from conftest import make_user, new_store
    pid = uuid.uuid4().hex
    st = new_store(make_user('exporter')['id'], 'pose', pid)
    task = st.add_task('Pose', 'keypoints', {'keypoints': ['a', 'b']}, ['person', 'crowd'])
    person, crowd = (c['id'] for c in task['classes'])
    st.add_media([{'file_name': f'f{i}.jpg', 'width': 100, 'height': 80} for i in range(2)])
    m0, m1 = st.media_ids()
    kp = {'class_id': person, 'geometry_type': 'keypoints', 'geometry': {'points': [[.2, .2, 2], [.3, .3, 2]]}}
    bx = {'class_id': crowd, 'geometry_type': 'bbox', 'geometry': {'x': .1, 'y': .1, 'w': .5, 'h': .5}}
    ids = {k: uuid.uuid4().hex for k in 'BKCL'}
    st.put_annotations(m0, task['id'], [{**bx, 'id': ids['B']}, {**kp, 'id': ids['K'], 'parent_id': ids['B']},
                                        {**bx, 'id': ids['C']}], 0)
    st.put_annotations(m1, task['id'], [{**kp, 'id': ids['L']}], 0)
    return st, {'id': pid, 'name': 'pose'}, task['id'], person, (m0, m1), ids


def test_class_filter_export_mirrors_exclusions(pg, db_conn, tmp_path):
    """Re-exporting with a class filter after an unfiltered export: the excluded classes' remote rows are
    soft-deleted, kept keypoints keep their parent box (no dangling parent_id), the status counts are the
    exported ones."""
    st, project, tid, person, (m0, m1), ids = _pose_store(tmp_path)
    db_export.export_to_db(db_conn, st, project, [tid], {})
    res = db_export.export_to_db(db_conn, st, project, [tid], {'classes': {tid: [person]}})
    assert res['tasks'][tid]['n_annotations'] == 3                     # K, L and K's parent B
    prefix, sch = db_export._prefix(db_conn), db_export._schema(db_conn)
    t = S.tables(S.make_metadata(prefix, sch), prefix)
    eng = db_export.engine_for(db_conn)
    try:
        with eng.connect() as c:
            a = t['annotations']
            rows = {r[0]: (bool(r[1]), r[2]) for r in c.execute(select(a.c.id, a.c.is_deleted, a.c.parent_id))}
            assert rows == {ids['B']: (False, None), ids['K']: (False, ids['B']), ids['C']: (True, None),
                            ids['L']: (False, None)}
            s = t['media_task_status']
            n = dict(c.execute(select(s.c.media_id, s.c.n_annotations)).all())
            assert n == {m0: 2, m1: 1}
    finally:
        eng.dispose()
    st.close()


def test_drop_excluded_sql_offline(tmp_path):
    """The soft-delete of excluded classes, compiled for both dialects without a server; the MSSQL text is run
    on SQLite (same SQL apart from the %s placeholders) to check what it touches."""
    from datetime import datetime, timezone
    from sqlalchemy.dialects import mssql, postgresql
    now = datetime(2024, 5, 1, tzinfo=timezone.utc)
    t = S.tables(S.make_metadata('lbl_'), 'lbl_')['annotations']
    pg = str(db_export._drop_excluded_stmt(t, 'T', {'b', 'a'}, now).compile(dialect=postgresql.dialect()))
    assert 'UPDATE lbl_annotations SET updated_at=' in pg and 'is_deleted' in pg and 'NOT IN' in pg
    assert 'class_id IS NULL' in pg
    assert 'NOT IN' not in str(db_export._drop_excluded_stmt(t, 'T', set(), now).compile(dialect=postgresql.dialect()))
    q = mssql.dialect().identifier_preparer
    sql, params = db_export._drop_excluded_mssql(q, t, 'T', {'b', 'a'}, now)
    assert sql == ('UPDATE lbl_annotations SET is_deleted = 1, updated_at = %s WHERE task_id = %s AND '
                   'is_deleted = 0 AND (class_id IS NULL OR class_id NOT IN (%s, %s))')
    assert params == ('2024-05-01 00:00:00+00:00', 'T', 'a', 'b')
    sql0, params0 = db_export._drop_excluded_mssql(q, t, 'T', set(), now)
    assert sql0.endswith('is_deleted = 0') and params0 == ('2024-05-01 00:00:00+00:00', 'T')
    c = sqlite3.connect(tmp_path / 'x.db')
    c.execute('CREATE TABLE lbl_annotations (id, task_id, class_id, is_deleted, updated_at)')
    c.executemany('INSERT INTO lbl_annotations VALUES (?,?,?,0,NULL)',
                  [('1', 'T', 'a'), ('2', 'T', 'c'), ('3', 'T', None), ('4', 'U', 'c')])
    c.execute(sql.replace('%s', '?'), params)
    assert dict(c.execute('SELECT id, is_deleted FROM lbl_annotations')) == {'1': 0, '2': 1, '3': 1, '4': 0}
    c.close()
