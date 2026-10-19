"""db/legacy.py: the one-time import of the old JSON / SQLite stores into PostgreSQL."""

import json
import sqlite3
import time

import pytest
from sqlalchemy import text

from conftest import make_user

PID = 'a' * 32
PID_GONE = 'b' * 32                       # a labeling dataset whose session no longer exists
T0 = 1_700_000_000.0


def _old_db(path):
    """An annotations.db exactly like the old SQLite store made it: the portable schema + the two local columns."""
    from sqlalchemy import create_engine
    from dataviz.labeling.schema import make_metadata
    eng = create_engine(f'sqlite:///{path}')
    make_metadata().create_all(eng)
    eng.dispose()
    c = sqlite3.connect(path)
    c.execute('ALTER TABLE media_task_status ADD COLUMN model_run_at REAL')
    c.execute('ALTER TABLE media_task_status ADD COLUMN review TEXT')
    c.execute('INSERT INTO projects(id, name, description, created_at, updated_at, meta) VALUES (?,?,?,?,?,?)',
              (PID, 'Cats', 'desc', T0, T0 + 5, json.dumps({'source': {'kind': 'upload'}})))
    c.execute('INSERT INTO tasks(id, project_id, name, task_type, position, config, created_at, is_deleted) '
              'VALUES (?,?,?,?,?,?,?,?)', ('t1', PID, 'boxes', 'object_detection', 0, json.dumps({'multi': True}), T0, 0))
    for i, (cid, name) in enumerate((('c1', 'cat'), ('c2', 'dog'))):
        c.execute('INSERT INTO label_classes(id, task_id, name, color, position, is_deleted) VALUES (?,?,?,?,?,?)',
                  (cid, 't1', name, '#ff0000', i, 0))
    for i in range(2):
        c.execute('INSERT INTO media(id, project_id, media_type, file_name, width, height, position, meta, created_at) '
                  'VALUES (?,?,?,?,?,?,?,?,?)', (f'm{i}', PID, 'image', f'img{i}.jpg', 640, 480, i, None, T0))
    for i, (mid, cid, deleted) in enumerate((('m0', 'c1', 0), ('m0', 'c2', 0), ('m1', 'c1', 1))):
        c.execute('INSERT INTO annotations(id, project_id, task_id, media_id, class_id, geometry_type, geometry, '
                  'bbox_x, bbox_y, bbox_w, bbox_h, source, z_order, created_by, created_at, updated_at, version, '
                  'is_deleted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (f'a{i}', PID, 't1', mid, cid, 'bbox', json.dumps({'x': .1, 'y': .2, 'w': .3, 'h': .4}),
                   .1, .2, .3, .4, 'manual', i, 'someone', T0 + i, T0 + i, 1, deleted))
    c.execute('INSERT INTO media_task_status(media_id, task_id, status, n_annotations, version, committed_by, '
              'committed_at, updated_at, model_run_at, review) VALUES (?,?,?,?,?,?,?,?,?,?)',
              ('m0', 't1', 'done', 2, 3, 'someone', T0 + 9, T0 + 9, T0 + 8, 'manual'))
    c.execute('INSERT INTO media_task_status(media_id, task_id, status, n_annotations, version, updated_at) '
              'VALUES (?,?,?,?,?,?)', ('m1', 't1', 'todo', 0, 1, T0))
    c.execute('INSERT INTO media_task_status(media_id, task_id, status, n_annotations, version, updated_at) '
              'VALUES (?,?,?,?,?,?)', ('m-gone', 't1', 'todo', 0, 1, T0))          # orphan: skipped
    c.commit()
    c.close()


def _write(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data) if not isinstance(data, str) else data)


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / 'ws'
    local = tmp_path / 'data' / 'local_ds'
    local.mkdir(parents=True)
    _write(root / 'connections.json', {
        'connections': [
            {'id': 'cs3', 'endpoint': 'https://s3.example', 'region': 'eu', 'access_key': 'AKIAOLD',
             'secret_key': 'S3SECRETVALUE', 'verify_ssl': True},
            {'id': 'cmod', 'kind': 'model', 'name': 'My server', 'url': 'http://m:8000', 'api_key': 'MODELKEY123',
             'timeout_s': 30},
            {'id': 'cdup', 'kind': 'model', 'name': 'my SERVER', 'url': 'http://m2:8000'},
        ],
        'datasets': [
            {'id': 'd1', 'name': 'Street', 'kind': 's3', 'connection_id': 'cs3', 'bucket': 'bkt', 'prefix': 'a/b',
             'mode': 'stream', 'created': T0, 'tags': ['prod']},
            {'id': 'd2', 'name': 'street', 'kind': 'labeling', 'project_id': PID, 'task_id': 't1',
             'only_done': True, 'created': T0 + 1, 'tags': []},
            {'id': 'd3', 'name': 'Gone', 'kind': 'labeling', 'project_id': PID_GONE, 'task_id': 'tx', 'created': T0},
            {'id': 'd4', 'name': 'Local', 'kind': 'local', 'path': str(local.resolve()), 'created': T0},
        ],
        'active': 'd1'})
    _write(root / 'llm.json', {
        'providers': [{'id': 'p1', 'name': 'GPT', 'kind': 'openai', 'base_url': 'https://api.openai.com/v1',
                       'model': 'gpt-x', 'api_key': 'sk-LLMSECRET', 'vision': True, 'verified_at': T0}],
        'active': 'p1', 'features': {'assistant': True}, 'budget': {'max_input_tokens': 50000}})
    _write(root / 'models.json', {'models': [
        {'id': 'mod1', 'name': 'det', 'family': 'yolo', 'format': 'api', 'task_type': 'object_detection',
         'api': {'preset': 'generic', 'url': 'http://x', 'api_key': 'MODELAPIKEY', 'headers': {'X-Token': 'HDRSECRET'}},
         'created': '2024-01-01 10:00:00'},
        {'id': 'mod2', 'name': 'w', 'family': 'yolo', 'format': 'pt', 'path': '/models/w.pt'}]})
    _write(root / 'label_settings.json', {'hotkeys': {'next': 'd'}, 'autosave': True})
    _write(root / 'labeling' / 'projects.json', {'projects': [
        {'id': PID, 'name': 'Cats', 'description': 'desc', 'created': T0, 'updated': T0 + 5,
         'source': {'kind': 'upload'}, 'export_default': {'format': 'yolo'}, 'n_media': 2, 'task_ids': ['t1'],
         'model_connections': ['cmod'], 'tags': ['pets'], 'review': True}]})
    (root / 'labeling' / PID).mkdir(parents=True)
    _old_db(root / 'labeling' / PID / 'annotations.db')
    w = root / 'Street__deadbeef'
    _write(w / 'workspace.json', {'dataset': 's3://bkt/a/b', 'fingerprint': 'deadbeef' * 2,
                                  'last_opened': '2024-01-02 00:00:00'})
    _write(w / 'tags.json', {'good': [3, 1, 2], 'bad': [7]})
    _write(w / 'ui_settings.json', {'point_size': 4, 'theme': 'dark', 'filter': 'OLD'})
    w2 = root / 'Local__cafebabe'
    _write(w2 / 'workspace.json', {'dataset': str(local.resolve()), 'fingerprint': 'cafebabe' * 2})
    _write(w2 / 'tags.json', {'x': [0]})
    _write(root / 'curation' / 'd1' / 'settings.json', {'curation_ignored': {'dup': ['k1']}, 'filter': 'cls == 1',
                                                        'filter_presets': [{'name': 'p', 'q': 'x'}]})
    return root


def _all(sql, **kw):
    from dataviz import db
    with db.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql), kw)]


def _counts():
    tables = ['app.connections', 'app.sessions', 'app.viz_sessions', 'app.label_projects', 'app.llm_providers',
              'app.models', 'app.box_tags', 'app.user_session_prefs', 'app.dataset_state', 'lbl.projects',
              'lbl.tasks', 'lbl.label_classes', 'lbl.media', 'lbl.annotations', 'lbl.media_task_status']
    return {t: _all(f'SELECT count(*) AS n FROM {t}')[0]['n'] for t in tables}


def test_import_everything(pg, ws):
    from dataviz.db import crypto, legacy
    admin = make_user('admin', admin=True)
    logs = []
    s = legacy.import_legacy(ws, admin['id'], log=logs.append)
    assert s['skipped'] == [], s
    assert s['connections'] == 3 and s['viz_sessions'] == 4 and s['label_sessions'] == 1

    # connections: config without secrets, secrets encrypted, names de-duplicated, kind default s3
    conns = {r['id']: r for r in _all('SELECT * FROM app.connections')}
    assert conns['cs3']['kind'] == 's3' and conns['cs3']['name'] == 'https://s3.example'
    assert conns['cs3']['config'] == {'endpoint': 'https://s3.example', 'region': 'eu', 'verify_ssl': True}
    raw = bytes(conns['cs3']['secrets'])
    assert b'S3SECRETVALUE' not in raw and b'AKIAOLD' not in raw
    assert crypto.decrypt_json(raw) == {'secret_key': 'S3SECRETVALUE', 'access_key': 'AKIAOLD'}
    assert crypto.decrypt_json(conns['cmod']['secrets']) == {'api_key': 'MODELKEY123'}
    assert conns['cmod']['config'] == {'url': 'http://m:8000', 'timeout_s': 30}
    assert conns['cdup']['name'] == 'my SERVER (2)' and conns['cdup']['secrets'] is None
    assert all(r['owner_id'] == admin['id'] for r in conns.values())

    # sessions: both kinds, owned by the admin, private, names unique per (owner, kind)
    ss = {r['id']: r for r in _all('SELECT id, kind, owner_id, name, visibility, tags, '
                                   'extract(epoch FROM created_at) AS created FROM app.sessions')}
    assert ss['d1']['name'] == 'Street' and ss['d2']['name'] == 'street (2)' and ss['d1']['tags'] == ['prod']
    assert float(ss['d1']['created']) == T0
    assert ss[PID]['kind'] == 'label' and ss[PID]['name'] == 'Cats' and ss[PID]['tags'] == ['pets']
    assert {r['visibility'] for r in ss.values()} == {'private'}
    assert {r['owner_id'] for r in ss.values()} == {admin['id']}
    vz = {r['session_id']: r for r in _all('SELECT * FROM app.viz_sessions')}
    assert vz['d1'] == {'session_id': 'd1', 'source_kind': 's3', 'path': None, 'connection_id': 'cs3', 'bucket': 'bkt',
                        'prefix': 'a/b', 'mode': 'stream', 'project_id': None, 'task_id': None, 'only_done': False}
    assert vz['d2']['project_id'] == PID and vz['d2']['task_id'] == 't1' and vz['d2']['only_done'] is True
    assert vz['d3']['project_id'] is None and vz['d3']['task_id'] == 'tx'
    assert vz['d4']['source_kind'] == 'local' and vz['d4']['path']

    lp = _all('SELECT * FROM app.label_projects')[0]
    assert lp == {'session_id': PID, 'description': 'desc', 'source': {'kind': 'upload'},
                  'export_default': {'format': 'yolo'}, 'n_media': 2, 'task_ids': ['t1'],
                  'model_connections': ['cmod'], 'review': True}

    # lbl rows: ids kept, project_id filled, JSON parsed, bools, users NULL, epoch floats
    assert _all('SELECT id, name, created_at, meta FROM lbl.projects') == [
        {'id': PID, 'name': 'Cats', 'created_at': T0, 'meta': {'source': {'kind': 'upload'}}}]
    t = _all('SELECT * FROM lbl.tasks')[0]
    assert t['config'] == {'multi': True} and t['project_id'] == PID and t['is_deleted'] is False
    cls = _all('SELECT id, project_id, task_id FROM lbl.label_classes ORDER BY id')
    assert cls == [{'id': 'c1', 'project_id': PID, 'task_id': 't1'}, {'id': 'c2', 'project_id': PID, 'task_id': 't1'}]
    assert [m['id'] for m in _all('SELECT id FROM lbl.media ORDER BY position')] == ['m0', 'm1']
    anns = {a['id']: a for a in _all('SELECT * FROM lbl.annotations')}
    assert len(anns) == 3 and anns['a2']['is_deleted'] is True and anns['a0']['created_by'] is None
    assert anns['a0']['geometry'] == {'x': .1, 'y': .2, 'w': .3, 'h': .4} and anns['a1']['created_at'] == T0 + 1
    mts = {r['media_id']: r for r in _all('SELECT * FROM lbl.media_task_status')}
    assert set(mts) == {'m0', 'm1'}                         # the orphan status row is skipped
    assert mts['m0']['project_id'] == PID and mts['m0']['committed_by'] is None
    assert mts['m0']['model_run_at'] == T0 + 8 and mts['m0']['review'] == 'manual' and mts['m0']['version'] == 3
    assert s['lbl_rows']['annotations'] == 3 and s['lbl_rows']['media_task_status'] == 2

    # llm + models
    p = _all('SELECT * FROM app.llm_providers')[0]
    assert p['name'] == 'GPT' and p['kind'] == 'openai' and 'api_key' not in p['config']
    assert p['config']['model'] == 'gpt-x' and p['config']['verified_at'] == T0
    assert b'sk-LLMSECRET' not in bytes(p['secrets']) and crypto.decrypt_json(p['secrets']) == {'api_key': 'sk-LLMSECRET'}
    cfg = _all('SELECT * FROM app.llm_user_config')[0]
    assert cfg['user_id'] == admin['id'] and cfg['active_provider_id'] == 'p1'
    assert cfg['features'] == {'assistant': True} and cfg['budget'] == {'max_input_tokens': 50000}
    ms = {m['id']: m for m in _all('SELECT * FROM app.models')}
    assert ms['mod1']['config']['api'] == {'preset': 'generic', 'url': 'http://x'}
    assert ms['mod1']['config']['created'] == '2024-01-01 10:00:00' and 'name' not in ms['mod1']['config']
    assert crypto.decrypt_json(ms['mod1']['secrets']) == {'api_key': 'MODELAPIKEY', 'headers': {'X-Token': 'HDRSECRET'}}
    assert b'HDRSECRET' not in bytes(ms['mod1']['secrets'])
    assert ms['mod2']['secrets'] is None and ms['mod2']['config']['path'] == '/models/w.pt'

    # prefs, tags, ui, curation
    assert _all('SELECT data FROM app.user_prefs')[0]['data'] == {'label': {'hotkeys': {'next': 'd'}, 'autosave': True}}
    tags = {(r['fingerprint'], r['tag']): r['sample_ids'] for r in _all('SELECT * FROM app.box_tags')}
    assert tags == {('deadbeef' * 2, 'good'): [1, 2, 3], ('deadbeef' * 2, 'bad'): [7], ('cafebabe' * 2, 'x'): [0]}
    usp = {r['session_id']: r['data'] for r in _all('SELECT * FROM app.user_session_prefs')}
    assert usp['d1'] == {'ui': {'point_size': 4, 'theme': 'dark'}, 'filter': 'cls == 1',
                         'filter_presets': [{'name': 'p', 'q': 'x'}]}           # curation's filter beats the old one
    assert 'd4' not in usp                                                      # no ui_settings.json there
    assert _all('SELECT session_id, data FROM app.dataset_state') == [
        {'session_id': 'd1', 'data': {'curation_ignored': {'dup': ['k1']}}}]

    st = _all("SELECT value FROM app.app_settings WHERE key = 'legacy_imported'")[0]['value']
    assert st['at'] > time.time() - 60 and st['summary']['connections'] == 3
    assert any('legacy import' in m for m in logs)


def test_second_run_is_a_noop(pg, ws):
    from dataviz import db
    from dataviz.db import legacy
    admin = make_user('admin', admin=True)
    legacy.import_legacy(ws, admin['id'], log=lambda m: None)
    before = _counts()
    assert legacy.import_legacy(ws, admin['id'], log=lambda m: None)['already'] is True
    assert _counts() == before
    # even with the flag gone, ids already present are left alone (no duplicates, no " (2)" copies)
    with db.begin() as c:
        c.execute(text("DELETE FROM app.app_settings WHERE key = 'legacy_imported'"))
    s = legacy.import_legacy(ws, admin['id'], log=lambda m: None)
    assert s['skipped'] == [] and s['connections'] == 0 and s['viz_sessions'] == 0 and s['label_sessions'] == 0
    assert _counts() == before
    assert sorted(r['name'] for r in _all("SELECT name FROM app.sessions WHERE kind = 'viz'")) == \
        ['Gone', 'Local', 'Street', 'street (2)']


def test_broken_files_are_skipped(pg, ws):
    from dataviz.db import legacy
    admin = make_user('admin', admin=True)
    (ws / 'llm.json').write_text('{not json')
    (ws / 'Street__deadbeef' / 'tags.json').write_text('[[[')
    (ws / 'labeling' / PID / 'annotations.db').write_bytes(b'this is not a database' * 100)
    logs = []
    s = legacy.import_legacy(ws, admin['id'], log=logs.append)
    assert any('llm.json' in m for m in logs) and any('tags.json' in m for m in logs)
    assert any('labeling session Cats' in m for m in logs)
    assert len(s['skipped']) == 3
    assert s['connections'] == 3 and s['models'] == 2 and s['llm_providers'] == 0
    assert _all('SELECT count(*) AS n FROM app.llm_providers')[0]['n'] == 0
    # the broken labeling session is skipped whole; the dataset pointing at it loses its link
    assert _all("SELECT count(*) AS n FROM app.sessions WHERE kind = 'label'")[0]['n'] == 0
    assert _all("SELECT project_id FROM app.viz_sessions WHERE session_id = 'd2'")[0]['project_id'] is None
    assert len(_all('SELECT * FROM app.box_tags')) == 1                         # the other workspace's tags
    assert _all("SELECT value FROM app.app_settings WHERE key = 'legacy_imported'")


def test_nothing_to_import(pg, tmp_path):
    from dataviz.db import legacy
    admin = make_user('admin', admin=True)
    s = legacy.import_legacy(tmp_path / 'empty', admin['id'], log=lambda m: None)
    assert s['skipped'] == [] and s['connections'] == 0
    assert legacy.import_legacy(tmp_path / 'empty', admin['id'], log=lambda m: None)['already'] is True
