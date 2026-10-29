"""Data Center: an S3 link loaded into a local folder (mode local: editable, re-sync replaces same-name files and
keeps local-only ones, switch back to stream), zip export, the label-wizard source of a bucket folder, and
labeling a bucket with a model (in place and into a copy) through a fake model server."""

import io
import os
import zipfile

import pytest

from conftest import jpeg_bytes, wait_job
from test_dc_routes import new_bucket, upload_zip
from test_models import _serve, serving_app

pytest.importorskip('dataviz.labeling.media')
pytestmark = pytest.mark.usefixtures('tmp_roots')
pytest_plugins = ['conftest_io']             # fixtures: s3_server, s3_bucket


@pytest.fixture
def dc(client_as, tmp_path):
    from conftest import make_user
    cfg = client_as.ctx.cfg
    cfg['storage']['data_root'] = str(tmp_path / 'dcdata')
    cfg['dataset']['workspace_root'] = str(tmp_path / 'ws')
    return client_as, client_as(make_user('alice'))


@pytest.fixture(scope='module')
def serving():
    app = serving_app()
    server, url = _serve(app)
    yield url
    server.should_exit = True
    from dataviz.labeling import models
    models.close_clients()


def _s3_link(c, s3_server, s3_bucket, mode='stream'):
    client, bucket = s3_bucket
    for i in range(2):
        client.put_object(Bucket=bucket, Key=f'ds/images/im{i}.jpg', Body=jpeg_bytes())
        client.put_object(Bucket=bucket, Key=f'ds/labels/im{i}.txt', Body=b'0 0.5 0.5 0.2 0.2\n')
    client.put_object(Bucket=bucket, Key='ds/data.yaml', Body=b'names: [car]\n')
    conn = c.post('/api/connections', json={**{k: v for k, v in s3_server.items() if k != 'id'}, 'kind': 's3'})
    assert conn.status_code == 200, conn.text
    r = c.post('/api/dc/buckets', json={'name': 'cloud', 'kind': 's3', 'connection_id': conn.json()['id'],
                                        's3_bucket': bucket, 'prefix': 'ds', 'mode': mode})
    assert r.status_code == 200, r.text
    return r.json(), client, bucket


def test_s3_load_into_local_resync_and_stream(dc, s3_server, s3_bucket):
    _, c = dc
    b, client, bucket = _s3_link(c, s3_server, s3_bucket)
    bid = b['id']
    assert b['mode'] == 'stream' and not b['writable'] and not b['local_ready']
    r = c.post(f'/api/dc/buckets/{bid}/ops', json={'ops': [{'op': 'mkdir', 'path': 'x'}]})
    assert r.status_code == 400 and 'load into local' in r.json()['detail']
    assert c.get(f'/api/dc/buckets/{bid}/export').status_code == 400

    # stream -> local: a pull job, then the bucket is an editable local copy that still says s3
    r = c.patch(f'/api/dc/buckets/{bid}', json={'mode': 'local'}).json()
    assert r['mode'] == 'local' and r['job_id']
    j = wait_job(c, r['job_id'])
    assert j['status'] == 'done', j
    b = c.get(f'/api/dc/buckets/{bid}').json()
    assert b['kind'] == 's3' and b['local_ready'] and b['writable'] and b['synced_at']
    assert b['root'] != b['s3_root'] and os.path.isfile(os.path.join(b['root'], 'images', 'im0.jpg'))
    assert b['format']['format'] == 'yolo-detect'
    names = [e['name'] for e in c.get(f'/api/dc/buckets/{bid}/ls', params={'path': 'images'}).json()['entries']]
    assert names == ['im0.jpg', 'im1.jpg']

    # local edits: a local-only file, and a changed file that S3 also has
    c.post(f'/api/dc/buckets/{bid}/upload', params={'path': ''},
           files=[('files', ('notes.txt', b'mine', 'text/plain'))])
    with open(os.path.join(b['root'], 'data.yaml'), 'w') as f:
        f.write('names: [changed]\n')
    client.put_object(Bucket=bucket, Key='ds/images/im2.jpg', Body=jpeg_bytes())
    j = wait_job(c, c.post(f'/api/dc/buckets/{bid}/sync').json()['job_id'])
    assert j['status'] == 'done' and j['result']['downloaded'] == 6
    assert open(os.path.join(b['root'], 'data.yaml')).read() == 'names: [car]\n'       # replaced from S3
    assert open(os.path.join(b['root'], 'notes.txt')).read() == 'mine'                # kept
    assert os.path.isfile(os.path.join(b['root'], 'images', 'im2.jpg'))               # new in S3

    # export: a zip of the local copy
    z = zipfile.ZipFile(io.BytesIO(c.get(f'/api/dc/buckets/{bid}/export').content))
    assert {'data.yaml', 'notes.txt', 'images/im2.jpg', 'labels/im0.txt'} <= set(z.namelist())
    z = zipfile.ZipFile(io.BytesIO(c.get(f'/api/dc/buckets/{bid}/export', params={'path': 'labels'}).content))
    assert sorted(z.namelist()) == ['im0.txt', 'im1.txt']

    # back to stream: S3 again (read-only), the copy stays on disk
    b2 = c.patch(f'/api/dc/buckets/{bid}', json={'mode': 'stream'}).json()
    assert b2['mode'] == 'stream' and not b2['writable'] and b2['root'] == b2['s3_root']
    assert os.path.isfile(os.path.join(b['root'], 'notes.txt'))
    assert c.patch(f'/api/dc/buckets/{bid}', json={'mode': 'nope'}).status_code == 400
    assert c.post(f'/api/dc/buckets/{bid}/sync').status_code == 400

    # unlink + delete files removes only the local copy
    assert c.delete(f'/api/dc/buckets/{bid}', params={'delete_files': 1}).status_code == 200
    assert not os.path.exists(b['root'])


def test_mode_only_for_s3_and_create_local(dc, s3_server, s3_bucket):
    _, c = dc
    m = new_bucket(c, name='plain')
    r = c.patch(f'/api/dc/buckets/{m["id"]}', json={'mode': 'local'})
    assert r.status_code == 400 and 'only an S3 link' in r.json()['detail']
    b, _, _ = _s3_link(c, s3_server, s3_bucket, mode='local')
    assert b['mode'] == 'local' and b['job_id']
    assert wait_job(c, b['job_id'])['status'] == 'done'
    assert c.get(f'/api/dc/buckets/{b["id"]}').json()['writable']


def test_source_for_the_label_wizard(dc):
    _, c = dc
    b = new_bucket(c)
    upload_zip(c, b['id'])
    s = c.get(f'/api/dc/buckets/{b["id"]}/source').json()
    assert s['source'] == {'kind': 'local', 'path': b['root']} and s['classes'] == ['cat', 'dog']
    assert s['format']['format'] == 'yolo-detect'
    s = c.get(f'/api/dc/buckets/{b["id"]}/source', params={'path': 'images/train'}).json()
    assert s['source']['path'] == os.path.join(b['root'], 'images', 'train')
    assert c.get(f'/api/dc/buckets/{b["id"]}/source', params={'path': 'data.yaml'}).status_code == 400
    assert c.get(f'/api/dc/buckets/{b["id"]}/source', params={'path': '../x'}).status_code == 400


def _model_conn(c, url):
    r = c.post('/api/connections', json={'kind': 'model', 'name': 'yolo', 'url': url + '/'})
    assert r.status_code == 200, r.text
    return r.json()['id']


def test_autolabel_in_place_writes_yolo(dc, serving):
    _, c = dc
    b = new_bucket(c, name='raw')
    c.post(f'/api/dc/buckets/{b["id"]}/upload', params={'path': ''},
           files=[('files', (f'p{i}.jpg', jpeg_bytes(), 'image/jpeg')) for i in range(3)])
    assert c.get(f'/api/dc/buckets/{b["id"]}').json()['format']['format'] == 'images'
    cid = _model_conn(c, serving)
    body = {'bucket': b['id'], 'model': {'connection_id': cid, 'model_id': 'yolo/obd/fake-640', 'version': 'v1'},
            'target': 'inplace', 'conf': 0.3}
    r = c.post('/api/dc/autolabel', json=body)
    assert r.status_code == 200, r.text
    j = wait_job(c, r.json()['job_id'], timeout=60)
    assert j['status'] == 'done', j
    res = j['result']
    assert res['bucket'] == b['id'] and res['labeled']['n_images'] == 3 and res['written']['labels'] == 3
    assert res['written']['classes'] == ['cat', 'dog']                    # the session's classes = the model's
    p = c.get(f'/api/label/projects/{res["session"]["id"]}').json()
    assert [cl['name'] for cl in p['tasks'][0]['classes']] == ['cat', 'dog']
    # loose images were moved into images/ (an undoable change), labels/ mirrors it: a YOLO dataset now
    root = b['root']
    assert res['restructured']['moved'] == 3 and res['restructured']['undo_id']
    assert sorted(os.listdir(os.path.join(root, 'images'))) == ['p0.jpg', 'p1.jpg', 'p2.jpg']
    lines = open(os.path.join(root, 'labels', 'p0.txt')).read().split('\n')
    assert lines[0].startswith('0 ') and lines[1].startswith('1 ')
    assert 'nc: 2' in open(os.path.join(root, 'data.yaml')).read() and 'train: images' in open(os.path.join(root, 'data.yaml')).read()
    assert open(os.path.join(root, 'classes.txt')).read().split() == ['cat', 'dog']
    nb = c.get(f'/api/dc/buckets/{b["id"]}').json()
    assert nb['format']['format'] == 'yolo-detect' and nb['stats']['labels'] == 3
    # the session reads the moved images
    ms = c.get(f'/api/label/projects/{res["session"]["id"]}/media-ids').json()['ids']
    assert len(ms) == 3
    # the move can be undone like any file change
    u = c.post(f'/api/dc/buckets/{b["id"]}/undo', json={'undo_id': res['restructured']['undo_id']})
    assert u.status_code == 200 and os.path.isfile(os.path.join(root, 'p0.jpg'))


def test_autolabel_coco_and_voc_leave_loose_images_in_place(dc, serving):
    _, c = dc
    b = new_bucket(c, name='flat')
    c.post(f'/api/dc/buckets/{b["id"]}/upload', params={'path': ''},
           files=[('files', (f'p{i}.jpg', jpeg_bytes(), 'image/jpeg')) for i in range(2)])
    cid = _model_conn(c, serving)
    m = {'connection_id': cid, 'model_id': 'yolo/obd/fake-640', 'version': 'v1'}
    assert c.post('/api/dc/autolabel', json={'bucket': b['id'], 'model': m, 'formats': ['nope']}).status_code == 400
    r = c.post('/api/dc/autolabel', json={'bucket': b['id'], 'model': m, 'formats': ['coco', 'voc']})
    assert r.status_code == 200, r.text
    j = wait_job(c, r.json()['job_id'], timeout=60)
    assert j['status'] == 'done', j
    res, root = j['result'], b['root']
    assert set(res['written']['formats']) == {'coco', 'voc'} and not res.get('restructured')
    assert sorted(n for n in os.listdir(root) if n.endswith('.jpg')) == ['p0.jpg', 'p1.jpg']   # not moved: no YOLO
    import json
    d = json.load(open(os.path.join(root, 'annotations', 'instances_default.json')))
    assert [x['name'] for x in d['categories']] == ['cat', 'dog'] and len(d['images']) == 2
    assert sorted(x['file_name'] for x in d['images']) == ['p0.jpg', 'p1.jpg'] and len(d['annotations']) == 4
    assert sorted(os.listdir(os.path.join(root, 'Annotations'))) == ['p0.xml', 'p1.xml']
    assert not os.path.exists(os.path.join(root, 'labels'))
    fm = c.get('/api/label/formats', params={'task_type': 'object_detection', 'media_type': 'image'}).json()
    assert {'yolo', 'coco', 'voc'} <= {f['key'] for f in fm['formats']} and fm['default'] == ['yolo']


def test_convert_embeddings_folder(dc):
    import csv
    import numpy as np
    _, c = dc
    b = new_bucket(c, name='emb')
    meta = io.StringIO()
    csv.writer(meta).writerows([['id', 'label'], ['a', 'x'], ['b', 'y']])
    npy = io.BytesIO()
    np.save(npy, np.arange(6, dtype=np.float32).reshape(2, 3))
    c.post(f'/api/dc/buckets/{b["id"]}/upload', params={'path': ''},
           files=[('files', ('meta.csv', meta.getvalue().encode(), 'text/csv')),
                  ('files', ('clip.npy', npy.getvalue(), 'application/octet-stream'))])
    assert c.get(f'/api/dc/buckets/{b["id"]}').json()['format']['format'] == 'embeddings'
    r = c.post(f'/api/dc/buckets/{b["id"]}/convert', json={'path': '', 'to': 'csv'})
    assert r.status_code == 200, r.text
    if r.json()['job_id'] is not None:
        assert wait_job(c, r.json()['job_id'])['status'] == 'done'
    out = os.path.join(b['root'], r.json()['path'], 'embeddings.csv')
    assert open(out).read().splitlines()[:2] == ['id,label,clip_0,clip_1,clip_2', 'a,x,0.0,1.0,2.0']
    assert c.post(f'/api/dc/buckets/{b["id"]}/convert', json={'path': '', 'to': 'npy'}).status_code == 400
    assert c.post(f'/api/dc/buckets/{b["id"]}/convert', json={'path': '', 'to': 'xls'}).status_code == 400


def test_autolabel_into_a_copy(dc, serving):
    _, c = dc
    b = new_bucket(c, name='src')
    upload_zip(c, b['id'])
    before = sorted(os.listdir(os.path.join(b['root'], 'labels', 'train')))
    cid = _model_conn(c, serving)
    r = c.post('/api/dc/autolabel', json={'bucket': b['id'], 'target': 'copy', 'new_name': 'src-auto',
                                          'model': {'connection_id': cid, 'model_id': 'yolo/obd/fake-640',
                                                    'version': 'v1'}})
    assert r.status_code == 200, r.text
    j = wait_job(c, r.json()['job_id'], timeout=60)
    assert j['status'] == 'done', j
    nb = c.get(f'/api/dc/buckets/{r.json()["bucket"]}').json()
    assert nb['name'] == 'src-auto' and nb['kind'] == 'managed' and nb['id'] != b['id']
    assert j['result']['copied']['files'] == 7
    # images/train -> labels/train, model labels replace the copied ones; the source bucket is untouched
    txt = open(os.path.join(nb['root'], 'labels', 'train', 'im0.txt')).read().split('\n')
    assert len([x for x in txt if x]) == 2 and txt[0].startswith('0 ')
    assert 'train: images/train' in open(os.path.join(nb['root'], 'data.yaml')).read()
    assert nb['format']['format'] == 'yolo-detect'
    assert sorted(os.listdir(os.path.join(b['root'], 'labels', 'train'))) == before
    assert open(os.path.join(b['root'], 'data.yaml')).read() == 'names: [cat, dog]\n'
    # a taken name: 409 before any job starts
    r = c.post('/api/dc/autolabel', json={'bucket': b['id'], 'target': 'copy', 'new_name': 'src-auto',
                                          'model': {'connection_id': cid, 'model_id': 'yolo/obd/fake-640'}})
    assert r.status_code == 409


def test_autolabel_errors(dc, serving):
    _, c = dc
    b = new_bucket(c)
    assert c.post('/api/dc/autolabel', json={'bucket': b['id'], 'model': {}}).status_code == 400
    cid = _model_conn(c, serving)
    m = {'connection_id': cid, 'model_id': 'yolo/obd/fake-640'}
    assert c.post('/api/dc/autolabel', json={'bucket': b['id'], 'model': m, 'target': 'x'}).status_code == 400
    assert c.post('/api/dc/autolabel', json={'bucket': b['id'], 'model': m, 'conf': 3}).status_code == 400
    # the model is read in the job (it may have to load), so the POST returns at once and the job fails
    r = c.post('/api/dc/autolabel', json={'bucket': b['id'], 'model': {'connection_id': cid, 'model_id': 'nope'}})
    assert r.status_code == 200, r.text
    j = wait_job(c, r.json()['job_id'])
    assert j['status'] == 'error' and 'nope' in (j.get('error') or '')
    # a copy whose model is wrong leaves no empty bucket behind
    n = len(c.get('/api/dc/buckets').json())
    r = c.post('/api/dc/autolabel', json={'bucket': b['id'], 'target': 'copy', 'new_name': 'x-labeled',
                                          'model': {'connection_id': cid, 'model_id': 'nope'}})
    assert wait_job(c, r.json()['job_id'])['status'] == 'error'
    assert len(c.get('/api/dc/buckets').json()) == n


def test_new_label_session_from_a_managed_bucket(client_as, tmp_path_factory):
    """New label › Data Center: the source of a managed bucket lies in storage.data_root, which is not one of the
    allowed roots; POST /api/label/projects must accept it all the same."""
    from conftest import make_user
    cfg = client_as.ctx.cfg
    cfg['storage']['data_root'] = str(tmp_path_factory.mktemp('dcstore'))   # outside the test's allowed tmp_path
    c = client_as(make_user('carol'))
    b = new_bucket(c)
    upload_zip(c, b['id'])
    s = c.get(f'/api/dc/buckets/{b["id"]}/source', params={'path': 'images/train'}).json()
    r = c.post('/api/label/projects', json={'name': 'from dc', 'source': s['source'],
                                            'tasks': [{'name': 'Detect', 'task_type': 'object_detection',
                                                       'classes': s['classes']}]})
    assert r.status_code == 200, r.text
    assert wait_job(c, r.json()['job_id'])['status'] == 'done'
    assert c.get(f'/api/label/projects/{r.json()["project"]["id"]}').json()['n_media'] == 3
