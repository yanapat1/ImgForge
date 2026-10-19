import io
import uuid
import zipfile

import pytest
import yaml

from conftest import jpeg_bytes, make_jpeg, wait_job

pytest.importorskip('dataviz.labeling.media')
pytest.importorskip('dataviz.labeling.sinks')

TASKS = [{'name': 'Detect', 'task_type': 'object_detection', 'classes': [{'name': 'cat'}, {'name': 'dog'}]},
         {'name': 'Segment', 'task_type': 'segmentation', 'classes': ['house', 'grass']}]


def hid():
    return uuid.uuid4().hex


def create(client, source=None, tasks=TASKS):
    r = client.post('/api/label/projects', json={'name': 'Test', 'source': source or {'kind': 'upload'},
                                                 'tasks': tasks})
    assert r.status_code == 200, r.text
    return r.json()


def upload_and_import(client, pid):
    files = [('files', ('a.jpg', jpeg_bytes(64, 48), 'image/jpeg')),
             ('files', ('a.jpg', jpeg_bytes(32, 32), 'image/jpeg')),
             ('files', ('b.jpg', jpeg_bytes(20, 10), 'image/jpeg')),
             ('files', ('evil.jpg', jpeg_bytes(), 'image/jpeg')),
             ('files', ('notes.txt', b'hi', 'text/plain'))]
    rels = [('rel_paths', 'x/a.jpg'), ('rel_paths', 'y/a.jpg'), ('rel_paths', 'b.jpg'),
            ('rel_paths', '../../evil.jpg'), ('rel_paths', 'notes.txt')]
    r = client.post(f'/api/label/projects/{pid}/upload', files=files + [(k, (None, v)) for k, v in rels])
    assert r.status_code == 200, r.text
    assert r.json() == {'saved': 3, 'skipped': 2}
    job = wait_job(client, client.post(f'/api/label/projects/{pid}/import', json={}).json()['job_id'])
    assert job['status'] == 'done', job


def test_full_flow(client, tmp_path):
    out = create(client)
    assert out['job_id'] is None
    p = out['project']
    pid = p['id']
    det, seg = p['tasks']
    assert det['task_type'] == 'object_detection' and len(det['classes']) == 2
    upload_and_import(client, pid)

    ids = client.get(f'/api/label/projects/{pid}/media-ids').json()['ids']
    assert len(ids) == 3
    page = client.get(f'/api/label/projects/{pid}/media', params={'limit': 10}).json()
    assert page['total'] == 3
    names = [m['file_name'] for m in page['items']]
    assert names == ['b.jpg', 'x__a.jpg', 'y__a.jpg']                   # duplicates flattened, sorted by rel
    assert (page['items'][0]['width'], page['items'][0]['height']) == (20, 10)
    assert page['items'][0]['status'] == {det['id']: 'todo', seg['id']: 'todo'}

    img = client.get(f'/api/label/projects/{pid}/image/{ids[0]}')
    assert img.status_code == 200 and img.content[:2] == b'\xff\xd8' and 'max-age' in img.headers['cache-control']
    assert client.get(f'/api/label/projects/{pid}/thumb/{ids[0]}').status_code == 200
    assert client.get(f'/api/label/projects/{pid}/image/nope').status_code == 404

    cat = det['classes'][0]['id']
    a = {'id': hid(), 'class_id': cat, 'geometry_type': 'bbox', 'geometry': {'x': .1, 'y': .2, 'w': .3, 'h': .4}}
    put = client.put(f'/api/label/projects/{pid}/annotations/{ids[1]}',
                     json={'task_id': det['id'], 'base_version': 0, 'annotations': [a]})
    assert put.json() == {'version': 1, 'status': 'in_progress', 'n': 1}
    stale = client.put(f'/api/label/projects/{pid}/annotations/{ids[1]}',
                       json={'task_id': det['id'], 'base_version': 0, 'annotations': []})
    assert stale.status_code == 409
    body = stale.json()
    assert body['version'] == 1 and body['annotations'][0]['id'] == a['id'] and body['detail']
    bad = client.put(f'/api/label/projects/{pid}/annotations/{ids[1]}',
                     json={'task_id': det['id'], 'base_version': 1, 'annotations': [dict(a, class_id='x' * 32)]})
    assert bad.status_code == 400 and 'class' in bad.json()['detail']
    house = seg['classes'][0]['id']
    poly = {'id': hid(), 'class_id': house, 'geometry_type': 'polygon',
            'geometry': {'points': [[.1, .1], [.5, .1], [.3, .5]]}}
    client.put(f'/api/label/projects/{pid}/annotations/{ids[1]}',
               json={'task_id': seg['id'], 'base_version': 0, 'annotations': [poly]})
    got = client.get(f'/api/label/projects/{pid}/annotations/{ids[1]}').json()
    assert got['versions'] == {det['id']: 1, seg['id']: 1} and len(got['annotations']) == 2

    r = client.post(f'/api/label/projects/{pid}/commit/{ids[1]}', json={'task_ids': [det['id'], seg['id']]})
    assert r.json() == {'status': {det['id']: 'done', seg['id']: 'done'}}
    client.post(f'/api/label/projects/{pid}/commit/{ids[0]}', json={'task_ids': [det['id']]})   # labeled negative
    client.post(f'/api/label/projects/{pid}/skip/{ids[2]}', json={'task_ids': [det['id']]})

    cards = client.get('/api/label/projects').json()['projects']
    assert cards[0]['id'] == pid and cards[0]['n_media'] == 3 and cards[0]['cover'] == ids
    t0 = cards[0]['tasks'][0]
    assert (t0['done'], t0['skipped'], t0['todo'], t0['n_classes']) == (2, 1, 0, 2)

    ex = client.post(f'/api/label/projects/{pid}/export', json={'target': {'kind': 'zip'}}).json()
    job = wait_job(client, ex['job_id'])
    assert job['status'] == 'done', job
    st = client.get(f'/api/label/exports/{ex["run_id"]}').json()
    assert st['status'] == 'done' and st['download']
    z = client.get(st['download'])
    assert z.status_code == 200 and z.headers['content-type'] == 'application/zip'
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    n = set(zf.namelist())
    assert {'images/b.jpg', 'images/x__a.jpg', 'images/y__a.jpg', 'manifest.json',
            'detect/classes.txt', 'detect/data.yaml', 'detect/labels/b.txt', 'detect/labels/x__a.txt',
            'segment/labels/x__a.txt', 'segment/classes.txt', 'segment/data.yaml'} <= n
    assert 'detect/labels/y__a.txt' not in n                           # skipped + empty: no file
    assert zf.read('detect/labels/b.txt') == b''                        # done + empty: a labeled negative
    assert zf.read('detect/labels/x__a.txt') == b'0 0.250000 0.400000 0.300000 0.400000\n'
    assert zf.read('segment/labels/x__a.txt').startswith(b'0 0.100000 0.100000 ')
    assert zf.read('detect/classes.txt') == b'cat\ndog\n'
    assert yaml.safe_load(zf.read('segment/data.yaml'))['names'] == {0: 'house', 1: 'grass'}

    # local export: refuses a non-empty folder unless overwrite
    dst = tmp_path / 'out'
    dst.mkdir()
    (dst / 'keep.txt').write_text('x')
    ex = client.post(f'/api/label/projects/{pid}/export', json={'target': {'kind': 'local', 'path': str(dst)},
                                                                'task_ids': [det['id']], 'include_images': False})
    assert wait_job(client, ex.json()['job_id'])['status'] == 'error'
    ex = client.post(f'/api/label/projects/{pid}/export',
                     json={'target': {'kind': 'local', 'path': str(dst)}, 'task_ids': [det['id']],
                           'only_done': True, 'overwrite': True})
    assert wait_job(client, ex.json()['job_id'])['status'] == 'done'
    assert sorted(x.name for x in (dst / 'images').iterdir()) == ['b.jpg', 'x__a.jpg']
    assert not (dst / 'segment').exists()


def test_classes_tasks_crud(client):
    p = create(client)['project']
    pid, det = p['id'], p['tasks'][0]
    r = client.post(f'/api/label/projects/{pid}/tasks/{det["id"]}/classes', json={'names': ['horse', 'cat']})
    assert [c['name'] for c in r.json()['classes']] == ['horse']
    cat, dog = det['classes'][0]['id'], det['classes'][1]['id']
    r = client.patch(f'/api/label/projects/{pid}/classes/{cat}', json={'name': 'kitty', 'hotkey': 'ctrl+1'})
    assert r.json()['name'] == 'kitty' and r.json()['hotkey'] == 'ctrl+1'
    r = client.patch(f'/api/label/projects/{pid}/classes/{cat}', json={'hotkey': None})
    assert r.json()['hotkey'] is None and r.json()['name'] == 'kitty'
    assert client.patch(f'/api/label/projects/{pid}/classes/{cat}', json={'color': 'red'}).status_code == 400
    # class in use -> 409 until reassigned
    upload_and_import(client, pid)
    mid = client.get(f'/api/label/projects/{pid}/media-ids').json()['ids'][0]
    a = {'id': hid(), 'class_id': cat, 'geometry': {'x': .1, 'y': .1, 'w': .1, 'h': .1}}
    assert client.put(f'/api/label/projects/{pid}/annotations/{mid}',
                      json={'task_id': det['id'], 'annotations': [a]}).status_code == 200
    assert client.delete(f'/api/label/projects/{pid}/classes/{cat}').status_code == 409
    assert client.delete(f'/api/label/projects/{pid}/classes/{cat}', params={'reassign_to': dog}).status_code == 200
    t = client.post(f'/api/label/projects/{pid}/tasks', json={'name': 'Pose', 'task_type': 'keypoints',
                                                              'config': {'keypoints': ['l', 'r']},
                                                              'classes': ['person']}).json()
    assert t['config']['keypoints'] == ['l', 'r']
    assert client.post(f'/api/label/projects/{pid}/tasks', json={'name': 'X', 'task_type': 'nope'}).status_code == 400
    ids = [t['id'], *[x['id'] for x in p['tasks']]]
    assert client.post(f'/api/label/projects/{pid}/tasks/reorder', json={'ids': ids}).status_code == 200
    assert client.patch(f'/api/label/projects/{pid}/tasks/{t["id"]}', json={'name': 'Body'}).json()['name'] == 'Body'
    assert client.delete(f'/api/label/projects/{pid}/tasks/{t["id"]}').status_code == 200
    proj = client.get(f'/api/label/projects/{pid}').json()
    assert [x['name'] for x in proj['tasks']] == ['Detect', 'Segment']
    assert client.patch(f'/api/label/projects/{pid}', json={'name': 'Renamed'}).json()['name'] == 'Renamed'


def test_local_source_and_errors(client, tmp_path):
    src = tmp_path / 'pics'
    for i in range(4):
        make_jpeg(src / f'd{i % 2}' / f'{i}.jpg', 30 + i, 20)
    out = create(client, {'kind': 'local', 'path': str(src)})
    assert wait_job(client, out['job_id'])['status'] == 'done'
    pid = out['project']['id']
    assert client.get(f'/api/label/projects/{pid}').json()['n_media'] == 4
    # rescan finds nothing new
    j = wait_job(client, client.post(f'/api/label/projects/{pid}/import', json={}).json()['job_id'])
    assert j['status'] == 'done'
    assert client.get(f'/api/label/projects/{pid}/media').json()['total'] == 4
    # uploads are refused for a folder source, sources cannot be swapped once there are images
    assert client.post(f'/api/label/projects/{pid}/upload', files=[('files', ('a.jpg', b'x'))]).status_code == 400
    r = client.post(f'/api/label/projects/{pid}/import', json={'source': {'kind': 'local', 'path': str(tmp_path)}})
    assert r.status_code == 409
    # 4xx
    assert client.get('/api/label/projects/' + 'f' * 32).status_code == 404
    assert client.post('/api/label/projects', json={'name': '', 'tasks': TASKS}).status_code == 400
    assert client.post('/api/label/projects', json={'name': 'x', 'tasks': []}).status_code == 400
    dup = [{'name': 'D', 'task_type': 'object_detection', 'classes': ['a', 'A']}]
    assert client.post('/api/label/projects', json={'name': 'x', 'tasks': dup}).status_code == 400
    assert client.post('/api/label/projects', json={'name': 'x', 'tasks': TASKS,
                                                    'source': {'kind': 'local'}}).status_code == 400
    assert client.post(f'/api/label/projects/{pid}/export', json={'target': {'kind': 'nope'}}).status_code == 400
    assert client.get('/api/label/exports/' + '0' * 32).status_code == 404
    assert client.delete(f'/api/label/projects/{pid}', params={'delete_files': 1}).json() == {'ok': True}
    assert client.get(f'/api/label/projects/{pid}').status_code == 404
    assert src.is_dir()                                                  # a local source is never deleted


def test_colors_and_settings(client):
    c = client.get('/api/label/colors', params={'n': 10}).json()
    assert len(c['colors']) == 10 and c['min_de'][0] is None and c['min_de'][9] > 8
    assert client.get('/api/label/settings').json() == {}
    s = {'crosshair': False, 'hotkeys': {'commit': 'ctrl+s'}}
    assert client.put('/api/label/settings', json=s).json() == s
    assert client.get('/api/label/settings').json() == s
    assert client.put('/api/label/settings', json=[1]).status_code == 400


def test_safe_rel_and_names():
    from dataviz.server.routes.labeling import assign_names, safe_rel
    assert safe_rel('a/b.jpg') == 'a/b.jpg'
    assert safe_rel('C:\\x\\y.jpg') == 'x/y.jpg'
    assert safe_rel('/abs/p.jpg') == 'abs/p.jpg'
    assert safe_rel('../p.jpg') is None and safe_rel('.hidden/p.jpg') is None and safe_rel('') is None
    assert assign_names(['a/1.jpg', 'b/1.jpg', 'c/2.jpg'], {'2.jpg'}) == ['a__1.jpg', 'b__1.jpg', 'c__2.jpg']
    assert assign_names(['A.jpg'], {'a.jpg'}) == ['A_2.jpg']


def test_names_unique_by_stem():
    from dataviz.server.routes.labeling import assign_names
    # a.jpg + a.png would both export to a.txt
    assert assign_names(['a.jpg', 'a.png'], {}) == ['a.jpg', 'a_2.png']
    assert assign_names(['x/a.png'], {'A.jpg'}) == ['x__a.png']


def test_upload_never_replaces_a_different_file(client):
    pid = create(client)['project']['id']
    up = lambda data: client.post(f'/api/label/projects/{pid}/upload',  # noqa: E731
                                  files=[('files', ('a.jpg', data, 'image/jpeg')), ('rel_paths', (None, 'a.jpg'))]).json()
    first, other = jpeg_bytes(64, 48), jpeg_bytes(20, 10)
    assert up(first) == {'saved': 1, 'skipped': 0}
    assert up(first) == {'saved': 0, 'skipped': 1}                      # same file again
    assert up(other) == {'saved': 1, 'skipped': 0}                      # kept as a second image
    wait_job(client, client.post(f'/api/label/projects/{pid}/import', json={}).json()['job_id'])
    items = client.get(f'/api/label/projects/{pid}/media').json()['items']
    assert sorted((m['width'], m['height']) for m in items) == [(20, 10), (64, 48)]


def _export_zip(client, pid, **opts):
    run = client.post(f'/api/label/projects/{pid}/export', json={'target': {'kind': 'zip'}, **opts}).json()
    wait_job(client, run['job_id'])
    return zipfile.ZipFile(io.BytesIO(client.get(f"/api/label/exports/{run['run_id']}/download").content))


def test_export_without_images_skips_missing_files(client, tmp_path):
    src = tmp_path / 'pics'
    make_jpeg(src / 'a.jpg', 30, 20)
    make_jpeg(src / 'b.jpg', 30, 20)
    out = create(client, {'kind': 'local', 'path': str(src)})
    wait_job(client, out['job_id'])
    pid, det = out['project']['id'], out['project']['tasks'][0]
    for m in client.get(f'/api/label/projects/{pid}/media').json()['items']:
        a = {'id': hid(), 'task_id': det['id'], 'class_id': det['classes'][0]['id'], 'geometry_type': 'bbox',
             'geometry': {'x': .1, 'y': .1, 'w': .2, 'h': .2}}
        r = client.put(f"/api/label/projects/{pid}/annotations/{m['id']}",
                       json={'task_id': det['id'], 'base_version': 0, 'annotations': [a]})
        assert r.status_code == 200, r.text
    (src / 'b.jpg').unlink()
    z = _export_zip(client, pid, include_images=False)
    labels = sorted(n for n in z.namelist() if '/labels/' in n)
    assert labels == ['detect/labels/a.txt']
    import json
    assert json.loads(z.read('manifest.json'))['warnings']['images_missing'] == 1


def test_pose_uses_parent_box_of_another_task(client, tmp_path):
    src = tmp_path / 'pics'
    make_jpeg(src / 'a.jpg', 30, 20)
    tasks = [{'name': 'Detect', 'task_type': 'object_detection', 'classes': ['person']},
             {'name': 'Pose', 'task_type': 'keypoints', 'config': {'keypoints': ['l', 'r']}, 'classes': ['person']}]
    out = create(client, {'kind': 'local', 'path': str(src)}, tasks)
    wait_job(client, out['job_id'])
    pid, (det, pose) = out['project']['id'], out['project']['tasks']
    mid = client.get(f'/api/label/projects/{pid}/media-ids').json()['ids'][0]
    box = {'id': hid(), 'task_id': det['id'], 'class_id': det['classes'][0]['id'], 'geometry_type': 'bbox',
           'geometry': {'x': .1, 'y': .2, 'w': .4, 'h': .6}}
    kp = {'id': hid(), 'task_id': pose['id'], 'class_id': pose['classes'][0]['id'], 'geometry_type': 'keypoints',
          'geometry': {'points': [[.2, .3, 2], [.3, .4, 2]]}, 'parent_id': box['id']}
    for t, a in ((det, box), (pose, kp)):
        r = client.put(f'/api/label/projects/{pid}/annotations/{mid}',
                       json={'task_id': t['id'], 'base_version': 0, 'annotations': [a]})
        assert r.status_code == 200, r.text
    z = _export_zip(client, pid, include_images=False)
    slug = [n for n in z.namelist() if n.endswith('/labels/a.txt') and not n.startswith('detect')][0]
    line = z.read(slug).decode().split()
    assert line[:5] == ['0', '0.300000', '0.500000', '0.400000', '0.600000']   # the parent box, not the kp extent
    assert len(z.read(slug).decode().strip().splitlines()) == 1


# ---------------------------------------------------------------- v2: export class filter, duplicate, bulk delete
def _three_class_session(client, tmp_path):
    src = tmp_path / 'pics3'
    for n in ('a', 'b', 'c'):
        make_jpeg(src / f'{n}.jpg', 30, 20)
    tasks = [{'name': 'Detect', 'task_type': 'object_detection', 'classes': ['cat', 'dog', 'horse']}]
    out = create(client, {'kind': 'local', 'path': str(src)}, tasks)
    wait_job(client, out['job_id'])
    pid, det = out['project']['id'], out['project']['tasks'][0]
    cat, dog, horse = (c['id'] for c in det['classes'])
    ids = client.get(f'/api/label/projects/{pid}/media-ids').json()['ids']
    bx = lambda c, x=.1: {'id': hid(), 'class_id': c, 'geometry_type': 'bbox', 'geometry': {'x': x, 'y': .1, 'w': .2, 'h': .2}}  # noqa: E731
    for mid, rows in ((ids[0], [bx(cat), bx(dog, .4), bx(horse, .6)]), (ids[1], [bx(dog)]), (ids[2], [bx(dog)])):
        r = client.put(f'/api/label/projects/{pid}/annotations/{mid}',
                       json={'task_id': det['id'], 'base_version': 0, 'annotations': rows})
        assert r.status_code == 200, r.text
    client.post(f'/api/label/projects/{pid}/commit/{ids[1]}', json={'task_ids': [det['id']]})
    return pid, det, (cat, dog, horse), ids


def test_export_class_filter_renumbers(client, tmp_path):
    import json
    pid, det, (cat, dog, horse), ids = _three_class_session(client, tmp_path)
    z = _export_zip(client, pid, include_images=False, classes={det['id']: [horse, cat]})
    assert z.read('detect/classes.txt').decode() == 'cat\nhorse\n'
    assert yaml.safe_load(z.read('detect/data.yaml'))['names'] == {0: 'cat', 1: 'horse'}
    a = [line.split()[0] for line in z.read('detect/labels/a.txt').decode().splitlines()]
    assert a == ['0', '1']                                   # cat 0, horse 1 (dog's row left out)
    names = sorted(n for n in z.namelist() if '/labels/' in n)
    # b is done with only dog: an empty negative; c is in progress with only dog: no file
    assert names == ['detect/labels/a.txt', 'detect/labels/b.txt'] and z.read('detect/labels/b.txt') == b''
    man = json.loads(z.read('manifest.json'))
    assert man['excluded_classes'] == {'detect': ['dog']} and man['tasks'][det['id']]['n_annotations'] == 2
    ed = client.get(f'/api/label/projects/{pid}').json()['export_default']
    assert ed['classes'] == {det['id']: [horse, cat]} and ed['target'] == {'kind': 'zip'} and ed['kind'] == 'zip'
    assert ed['include_images'] is False and ed['only_done'] is False
    z = _export_zip(client, pid, include_images=False)        # no filter: every class, original ids
    assert z.read('detect/classes.txt').decode() == 'cat\ndog\nhorse\n'
    assert 'excluded_classes' not in json.loads(z.read('manifest.json')) or \
        json.loads(z.read('manifest.json'))['excluded_classes'] == {}


def test_class_stats_and_bulk_delete_routes(client, tmp_path):
    pid, det, (cat, dog, horse), ids = _three_class_session(client, tmp_path)
    base = f'/api/label/projects/{pid}'
    st = client.get(f'{base}/class-stats').json()['stats']
    assert st == {cat: {'n_annotations': 1, 'n_images': 1}, dog: {'n_annotations': 3, 'n_images': 3},
                  horse: {'n_annotations': 1, 'n_images': 1}}
    r = client.post(f'{base}/classes/delete', json={'class_ids': [dog, 'f' * 32]})
    assert r.status_code == 404 and client.get(f'{base}/class-stats').json()['stats'][dog]['n_annotations'] == 3
    assert client.post(f'{base}/classes/delete', json={'class_ids': []}).status_code == 400
    assert client.post(f'{base}/classes/delete', json={}).status_code == 422
    r = client.post(f'{base}/classes/delete', json={'class_ids': [dog, horse]})
    assert r.json() == {'deleted': 2, 'annotations': 4, 'images': 3}
    assert [c['name'] for c in client.get(base).json()['tasks'][0]['classes']] == ['cat']
    assert client.get(f'{base}/media').json()['items'][1]['n'][det['id']] == 0


def test_duplicate_route_copy_exports_the_same(client, tmp_path):
    out = create(client)
    pid, (det, seg) = out['project']['id'], out['project']['tasks']
    upload_and_import(client, pid)
    ids = client.get(f'/api/label/projects/{pid}/media-ids').json()['ids']
    for i, mid in enumerate(ids):
        a = {'id': hid(), 'class_id': det['classes'][i % 2]['id'], 'geometry_type': 'bbox',
             'geometry': {'x': .1 * i, 'y': .2, 'w': .3, 'h': .4}}
        client.put(f'/api/label/projects/{pid}/annotations/{mid}',
                   json={'task_id': det['id'], 'base_version': 0, 'annotations': [a]})
    client.post(f'/api/label/projects/{pid}/commit/{ids[0]}', json={'task_ids': [det['id'], seg['id']]})
    client.get(f'/api/label/projects/{pid}/thumb/{ids[0]}')
    j = wait_job(client, client.post(f'/api/label/projects/{pid}/duplicate', json={'name': 'Twin'}).json()['job_id'])
    assert j['status'] == 'done', j
    card = j['result']
    assert card['name'] == 'Twin' and card['id'] != pid and card['n_media'] == 3
    orig = [c for c in client.get('/api/label/projects').json()['projects'] if c['id'] == pid][0]
    strip = lambda ts: [{k: v for k, v in t.items() if k != 'id'} for t in ts]  # noqa: E731
    assert strip(card['tasks']) == strip(orig['tasks']) and not {t['id'] for t in card['tasks']} & {t['id'] for t in orig['tasks']}
    assert client.get(f"/api/label/projects/{card['id']}/thumb/{card['cover'][0]}").status_code == 200
    z1, z2 = _export_zip(client, pid), _export_zip(client, card['id'])
    files = lambda z: {n: z.read(n) for n in z.namelist() if n != 'manifest.json'}  # noqa: E731
    assert files(z1) == files(z2) and len(files(z1)) > 5
    j = wait_job(client, client.post(f'/api/label/projects/{pid}/duplicate').json()['job_id'])
    assert j['result']['name'] == 'Test (copy)'
    assert client.post('/api/label/projects/' + 'a' * 32 + '/duplicate', json={}).status_code == 404


def test_review_endpoint_flag_and_card(client):
    p = create(client)
    pid = p['project']['id']
    upload_and_import(client, pid)
    det = p['project']['tasks'][0]
    cls = det['classes'][0]['id']
    mids = client.get(f'/api/label/projects/{pid}/media-ids').json()['ids']
    for m in mids[:2]:
        r = client.put(f'/api/label/projects/{pid}/annotations/{m}', json={'task_id': det['id'], 'base_version': 0, 'annotations': [
            {'id': hid(), 'class_id': cls, 'geometry_type': 'bbox', 'geometry': {'x': .1, 'y': .1, 'w': .2, 'h': .2}}]})
        assert r.status_code == 200, r.text
        client.post(f'/api/label/projects/{pid}/commit/{m}', json={'task_ids': [det['id']]})
    # off by default; switch it on
    card = lambda: next(x for x in client.get('/api/label/projects').json()['projects'] if x['id'] == pid)  # noqa: E731
    assert card()['review'] is False
    assert client.patch(f'/api/label/projects/{pid}', json={'review': True}).json()['review'] is True
    assert client.patch(f'/api/label/projects/{pid}', json={'name': 'Renamed'}).json()['review'] is True   # kept
    r = client.post(f'/api/label/projects/{pid}/review', json={'task_ids': [det['id']], 'mode': 'manual', 'media_ids': [mids[0]]})
    assert r.json() == {'changed': {det['id']: 1}}
    r = client.post(f'/api/label/projects/{pid}/review', json={'task_ids': [det['id']], 'mode': 'bulk'})
    assert r.json() == {'changed': {det['id']: 1}}
    t = next(x for x in card()['tasks'] if x['id'] == det['id'])
    assert (t['done'], t['review_manual'], t['review_bulk']) == (2, 1, 1)
    page = client.get(f'/api/label/projects/{pid}/media', params={'task_id': det['id'], 'review': 'bulk'}).json()
    assert page['total'] == 1 and page['items'][0]['review'] == {det['id']: 'bulk'}
    assert client.post(f'/api/label/projects/{pid}/review', json={'task_ids': [det['id']], 'mode': 'nope'}).status_code == 400
    assert client.post(f'/api/label/projects/{pid}/review', json={'task_ids': ['x' * 32], 'mode': 'bulk'}).status_code == 404
    assert client.get(f'/api/label/projects/{pid}/media', params={'task_id': det['id'], 'review': 'nope'}).status_code == 400
