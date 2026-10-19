"""The role matrix on the labeling API (auth/access.py: viewer < labeler < owner, admins are owners, a stranger
gets 404, a global session gives everyone the viewer role), export runs visible only to who started them and
the project owner, per-user labeling page settings, and the same roles on the visualize session routes."""

import uuid

import pytest

from conftest import jpeg_bytes, make_user, wait_job

pytest.importorskip('dataviz.labeling.media')

TASKS = [{'name': 'Detect', 'task_type': 'object_detection', 'classes': ['cat', 'dog']}]


def box(cls):
    return {'id': uuid.uuid4().hex, 'class_id': cls, 'geometry_type': 'bbox', 'geometry': {'x': .1, 'y': .1, 'w': .2, 'h': .2}}


@pytest.fixture
def team(client_as, tmp_path):
    """A label session of `owner` with two images, shared with a viewer and a labeler; plus a stranger and an admin."""
    from dataviz.data import sessions
    client_as.ctx.cfg['storage']['allowed_roots'] = [str(tmp_path)]
    c = {k: client_as(make_user(k, admin=k == 'admin')) for k in ('owner', 'viewer', 'labeler', 'labeler2',
                                                                    'stranger', 'admin')}
    o = c['owner']
    out = o.post('/api/label/projects', json={'name': 'Pets', 'tasks': TASKS}).json()
    pid, det = out['project']['id'], out['project']['tasks'][0]
    files = [('files', (f'{n}.jpg', jpeg_bytes(40, 30), 'image/jpeg')) for n in 'ab']
    assert o.post(f'/api/label/projects/{pid}/upload', files=files).status_code == 200
    assert wait_job(o, o.post(f'/api/label/projects/{pid}/import', json={}).json()['job_id'])['status'] == 'done'
    sessions.set_sharing(pid, 'private', [{'user_id': c['viewer'].user['id'], 'role': 'viewer'},
                                          {'user_id': c['labeler'].user['id'], 'role': 'labeler'},
                                          {'user_id': c['labeler2'].user['id'], 'role': 'labeler'}], o.user['id'])
    mids = o.get(f'/api/label/projects/{pid}/media-ids').json()['ids']
    c.update(pid=pid, det=det, mids=mids)
    return c


def reads(c, pid, mid):
    base = f'/api/label/projects/{pid}'
    return [c.get(base), c.get(f'{base}/media'), c.get(f'{base}/media-ids'), c.get(f'{base}/image/{mid}'),
            c.get(f'{base}/thumb/{mid}'), c.get(f'{base}/annotations/{mid}'), c.get(f'{base}/class-stats')]


def label_writes(c, pid, det, mid):
    base = f'/api/label/projects/{pid}'
    return {'put': c.put(f'{base}/annotations/{mid}', json={'task_id': det['id'], 'annotations': [box(det['classes'][0]['id'])]}),
            'commit': c.post(f'{base}/commit/{mid}', json={'task_ids': [det['id']]}),
            'reopen': c.post(f'{base}/reopen/{mid}', json={'task_ids': [det['id']]}),
            'review': c.post(f'{base}/review', json={'task_ids': [det['id']], 'mode': 'bulk'}),
            'remove-model': c.post(f'{base}/remove-model-labels', json={'task_id': det['id']})}


def owner_writes(c, pid, det):
    base = f'/api/label/projects/{pid}'
    cid = det['classes'][1]['id']
    return {'patch': c.patch(base, json={'description': 'x'}),
            'add-task': c.post(f'{base}/tasks', json={'name': f'T{uuid.uuid4().hex[:4]}', 'task_type': 'classification',
                                                      'classes': ['a']}),
            'patch-task': c.patch(f"{base}/tasks/{det['id']}", json={'name': 'Detect'}),
            'add-class': c.post(f"{base}/tasks/{det['id']}/classes", json={'names': [f'c{uuid.uuid4().hex[:4]}']}),
            'patch-class': c.patch(f'{base}/classes/{cid}', json={'name': 'dog'}),
            'model': c.put(f"{base}/tasks/{det['id']}/model", json={'registry_id': 'nope', 'model_id': 'nope'}),
            'upload': c.post(f'{base}/upload', files=[('files', ('z.txt', b'x', 'text/plain'))]),
            'import': c.post(f'{base}/import', json={})}


def test_viewer_reads_but_cannot_label(team):
    v, pid, det, mid = team['viewer'], team['pid'], team['det'], team['mids'][0]
    assert all(r.status_code == 200 for r in reads(v, pid, mid)), [r.status_code for r in reads(v, pid, mid)]
    assert v.get(f'/api/label/projects/{pid}').json()['my_role'] == 'viewer'
    card = next(x for x in v.get('/api/label/projects').json()['projects'] if x['id'] == pid)
    assert card['my_role'] == 'viewer' and card['owner']['id'] == team['owner'].user['id']
    res = label_writes(v, pid, det, mid)
    assert {k: r.status_code for k, r in res.items()} == {k: 403 for k in res}
    assert res['put'].json()['detail'] == 'you can only view this session'
    assert v.post(f'/api/label/projects/{pid}/duplicate', json={}).status_code == 403
    assert v.post(f'/api/label/projects/{pid}/export', json={'target': {'kind': 'zip'}}).status_code == 403
    assert v.post(f'/api/llm/label/{pid}/accept', json={}).status_code == 403
    assert v.post(f'/api/label/projects/{pid}/import-annotations', json={}).status_code == 403


def test_labeler_labels_but_cannot_change_the_structure(team):
    lab, pid, det, mid = team['labeler'], team['pid'], team['det'], team['mids'][0]
    res = label_writes(lab, pid, det, mid)
    assert {k: r.status_code for k, r in res.items()} == {k: 200 for k in res}, {k: r.text for k, r in res.items()}
    res = owner_writes(lab, pid, det)
    assert {k: r.status_code for k, r in res.items()} == {k: 403 for k in res}
    assert res['patch'].json()['detail'] == 'only the owner of this session can do that'
    base = f'/api/label/projects/{pid}'
    assert lab.delete(base).status_code == 403
    assert lab.delete(f"{base}/tasks/{det['id']}").status_code == 403
    assert lab.post(f'{base}/classes/delete', json={'class_ids': [det['classes'][1]['id']]}).status_code == 403
    for kind in ('s3', 'ftp', 'db'):
        r = lab.post(f'{base}/export', json={'target': {'kind': kind, 'connection_id': 'x'}})
        assert r.status_code == 403 and r.json()['detail'] == 'only the owner can export to a connection'
    assert lab.post(f'{base}/export', json={'target': {'kind': 'local', 'path': '/tmp'}}).status_code == 403
    # a duplicate belongs to the labeler who made it
    j = wait_job(lab, lab.post(f'{base}/duplicate', json={'name': 'Mine'}).json()['job_id'])
    assert j['status'] == 'done', j
    assert lab.ctx.labeling.get(j['result']['id'])['owner_id'] == lab.user['id']
    assert lab.get(f"/api/label/projects/{j['result']['id']}").json()['my_role'] == 'owner'


def test_stranger_gets_404_owner_and_admin_everything(team):
    s, pid, det, mid = team['stranger'], team['pid'], team['det'], team['mids'][0]
    assert {r.status_code for r in reads(s, pid, mid)} == {404}
    assert {r.status_code for r in label_writes(s, pid, det, mid).values()} == {404}
    assert {r.status_code for r in owner_writes(s, pid, det).values()} == {404}
    assert s.delete(f'/api/label/projects/{pid}').status_code == 404
    assert pid not in {x['id'] for x in s.get('/api/label/projects').json()['projects']}
    for who, mid in zip(('owner', 'admin'), team['mids']):     # one image each: both PUT at base version 0
        c = team[who]
        assert all(r.status_code == 200 for r in reads(c, pid, mid))
        assert c.get(f'/api/label/projects/{pid}').json()['my_role'] == 'owner'
        res = label_writes(c, pid, det, mid)
        assert {r.status_code for r in res.values()} == {200}, {k: r.text for k, r in res.items()}
        res = owner_writes(c, pid, det)
        # past the guard: a missing model is a 400, a .txt upload is skipped, the rest succeed
        assert res['model'].status_code == 400 and res['upload'].json() == {'saved': 0, 'skipped': 1}
        assert all(r.status_code == 200 for k, r in res.items() if k != 'model'), {k: r.text for k, r in res.items()}
    assert team['admin'].delete(f'/api/label/projects/{pid}').json() == {'ok': True}


def test_global_session_gives_everyone_the_viewer_role(team):
    from dataviz.data import sessions
    s, pid, det, mid = team['stranger'], team['pid'], team['det'], team['mids'][0]
    sessions.set_sharing(pid, 'global', [{'user_id': team['labeler'].user['id'], 'role': 'labeler'}],
                         team['owner'].user['id'])
    assert all(r.status_code == 200 for r in reads(s, pid, mid))
    assert s.get(f'/api/label/projects/{pid}').json()['my_role'] == 'viewer'
    assert {r.status_code for r in label_writes(s, pid, det, mid).values()} == {403}
    assert pid in {x['id'] for x in s.get('/api/label/projects', params={'scope': 'global'}).json()['projects']}
    # a member keeps their (higher) role
    assert team['labeler'].get(f'/api/label/projects/{pid}').json()['my_role'] == 'labeler'


def test_export_runs_belong_to_who_started_them(team):
    lab, pid = team['labeler'], team['pid']
    run = lab.post(f'/api/label/projects/{pid}/export', json={'target': {'kind': 'zip'}}).json()
    assert wait_job(lab, run['job_id'])['status'] == 'done'
    st = lab.get(f"/api/label/exports/{run['run_id']}").json()
    assert st['status'] == 'done' and st['download']
    assert lab.get(st['download']).headers['content-type'] == 'application/zip'
    for who in ('owner', 'admin'):                     # the project's owner (and admins) see every run
        assert team[who].get(f"/api/label/exports/{run['run_id']}").status_code == 200
        assert team[who].get(st['download']).status_code == 200
    for who in ('labeler2', 'viewer', 'stranger'):
        assert team[who].get(f"/api/label/exports/{run['run_id']}").status_code == 404, who
        assert team[who].get(st['download']).status_code == 404, who
    # a labeler's zip leaves the owner's remembered export target alone
    assert team['owner'].get(f'/api/label/projects/{pid}').json()['export_default'] is None


def test_label_settings_are_per_user(team):
    o, lab = team['owner'], team['labeler']
    s = {'crosshair': False}
    assert o.put('/api/label/settings', json=s).json() == s
    assert o.get('/api/label/settings').json() == s and lab.get('/api/label/settings').json() == {}
    from dataviz.server.routes.me import get_prefs
    assert get_prefs(o.user['id'])['label'] == s           # = /api/me/prefs key 'label'


def test_visualize_session_roles(team, tmp_path):
    from dataviz.data import sessions
    o, v = team['owner'], team['viewer']
    for sub in ('images', 'labels'):
        (tmp_path / 'ds' / sub).mkdir(parents=True)
    ds = o.post('/api/datasets', json={'kind': 'local', 'path': str(tmp_path / 'ds'), 'name': 'DS'}).json()
    did = ds['id']
    sessions.set_sharing(did, 'private', [{'user_id': v.user['id'], 'role': 'labeler'}], o.user['id'])
    card = next(d for d in v.get('/api/datasets').json()['datasets'] if d['id'] == did)
    assert card['my_role'] == 'labeler' and card['members'][0]['role'] == 'labeler'
    assert v.get('/api/datasets', params={'scope': 'mine'}).json()['datasets'] == []
    assert v.put(f'/api/datasets/{did}/tags', json={'tags': ['a']}).status_code == 403
    assert v.delete(f'/api/datasets/{did}').status_code == 403
    assert v.post(f'/api/datasets/{did}/refresh').status_code == 403
    assert v.get(f'/api/cover/{did}/0').status_code == 404          # past the guard: no cover yet
    assert team['stranger'].get(f'/api/cover/{did}/0').json()['detail'].startswith('no visualize session')
    assert o.put(f'/api/datasets/{did}/tags', json={'tags': ['a']}).json()['tags'] == ['a']
    assert o.get('/api/datasets', params={'scope': 'nope'}).status_code == 400
    assert o.delete(f'/api/datasets/{did}').json() == {'ok': True}
