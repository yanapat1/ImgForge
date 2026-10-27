"""Data Center HTTP API (/api/dc/*, datacenter/API.md) end to end against PostgreSQL: buckets, upload + format
auto tag, paging, file ops + undo, the retag when a change breaks the format, path escapes, tags, search,
owner-only visibility, the data.manage permission and Send to label."""

import io
import os
import time
import zipfile

import pytest
from sqlalchemy import text

from conftest import jpeg_bytes, make_jpeg, make_user, wait_job

pytestmark = pytest.mark.usefixtures('tmp_roots')


@pytest.fixture
def dc(client_as, tmp_path):
    """(as_user, admin client): every client shares one app whose managed data root lives in tmp_path."""
    cfg = client_as.ctx.cfg
    cfg['storage']['data_root'] = str(tmp_path / 'dcdata')
    cfg['dataset']['workspace_root'] = str(tmp_path / 'ws')
    return client_as, client_as(make_user('alice'))


def yolo_zip(n=3):
    b = io.BytesIO()
    with zipfile.ZipFile(b, 'w') as z:
        for i in range(n):
            z.writestr(f'images/train/im{i}.jpg', jpeg_bytes())
            z.writestr(f'labels/train/im{i}.txt', '0 0.5 0.5 0.2 0.2\n1 0.3 0.3 0.1 0.1\n')
        z.writestr('data.yaml', 'names: [cat, dog]\n')
        z.writestr('../evil.txt', 'x')                      # zip-slip: skipped
    return b.getvalue()


def new_bucket(c, name='cars', **kw):
    r = c.post('/api/dc/buckets', json={'name': name, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def upload_zip(c, bid, path=''):
    r = c.post(f'/api/dc/buckets/{bid}/upload', params={'path': path},
               files=[('files', ('ds.zip', yolo_zip(), 'application/zip'))])
    assert r.status_code == 200, r.text
    return r.json()


def auto(b):
    return [t['tag'] for t in b['tags'] if t['auto']]


def test_managed_bucket_upload_ls_ops_undo(dc, tmp_path):
    _, c = dc
    b = new_bucket(c, tags=['road'])
    assert b['kind'] == 'managed' and b['writable'] and auto(b) == ['format:empty']
    assert b['root'].startswith(str(tmp_path / 'dcdata'))
    up = upload_zip(c, b['id'])
    assert up['added'] == 7 and up['retagged'] == {'path': '', 'from': 'empty', 'to': 'yolo-detect',
                                                   'reason': up['retagged']['reason']}
    assert not (tmp_path / 'dcdata' / 'evil.txt').exists()
    b = c.get(f'/api/dc/buckets/{b["id"]}').json()
    assert auto(b) == ['format:yolo-detect'] and b['format']['classes'] == 2
    assert b['stats']['images'] == 3 and b['stats']['labels'] == 3 and b['stats']['size'] > 0

    # paging, sort, labels count on images
    ls = c.get(f'/api/dc/buckets/{b["id"]}/ls', params={'path': 'images/train', 'limit': 2}).json()
    assert ls['total'] == 3 and [e['name'] for e in ls['entries']] == ['im0.jpg', 'im1.jpg']
    assert ls['entries'][0]['kind'] == 'image' and ls['entries'][0]['labels'] == 2 and ls['writable']
    ls2 = c.get(f'/api/dc/buckets/{b["id"]}/ls', params={'path': 'images/train', 'offset': 2, 'limit': 2}).json()
    assert [e['name'] for e in ls2['entries']] == ['im2.jpg']
    root = c.get(f'/api/dc/buckets/{b["id"]}/ls').json()
    assert [e['name'] for e in root['entries']][:2] == ['images', 'labels'] and root['entries'][0]['items'] == 1
    assert root['format']['format'] == 'yolo-detect'
    st = c.get(f'/api/dc/buckets/{b["id"]}/stats').json()
    assert st['images'] == 3 and st['boxes'] == 6 and st['splits'] == {'train': 3}

    # thumbnails, raw files, text
    r = c.get(f'/api/dc/buckets/{b["id"]}/file', params={'path': 'images/train/im0.jpg', 'w': 32})
    assert r.status_code == 200 and r.headers['content-type'] == 'image/jpeg' and 'max-age=300' in r.headers['cache-control']
    assert c.get(f'/api/dc/buckets/{b["id"]}/file', params={'path': 'data.yaml'}).content == b'names: [cat, dog]\n'
    assert c.get(f'/api/dc/buckets/{b["id"]}/text', params={'path': 'data.yaml'}).json() == \
        {'text': 'names: [cat, dog]\n', 'truncated': False}

    # mkdir / copy / rename / move / delete, then undo all of it
    bid = b['id']
    r = c.post(f'/api/dc/buckets/{bid}/ops', json={'ops': [
        {'op': 'mkdir', 'path': 'extra'},
        {'op': 'copy', 'path': 'data.yaml', 'to': 'extra'},
        {'op': 'rename', 'path': 'extra/data.yaml', 'name': 'copy.yaml'},
        {'op': 'move', 'path': 'images/train/im2.jpg', 'to': 'extra'},
        {'op': 'delete', 'path': 'labels/train/im2.txt'},
        {'op': 'move', 'path': 'nope.jpg', 'to': 'extra'}]}).json()
    assert len(r['applied']) == 5 and len(r['errors']) == 1 and r['undo_id'] and r['retagged'] is None
    names = {e['name'] for e in c.get(f'/api/dc/buckets/{bid}/ls', params={'path': 'extra'}).json()['entries']}
    assert names == {'copy.yaml', 'im2.jpg'}
    assert c.get(f'/api/dc/buckets/{bid}/ls', params={'path': 'labels/train'}).json()['total'] == 2
    u = c.post(f'/api/dc/buckets/{bid}/undo', json={'undo_id': r['undo_id']}).json()
    assert u['restored'] == 5 and u['errors'] == []
    assert c.get(f'/api/dc/buckets/{bid}/ls', params={'path': 'labels/train'}).json()['total'] == 3
    assert c.get(f'/api/dc/buckets/{bid}/ls', params={'path': 'images/train'}).json()['total'] == 3
    assert c.get(f'/api/dc/buckets/{bid}/ls', params={'path': 'extra'}).status_code == 404
    assert c.post(f'/api/dc/buckets/{bid}/undo', json={'undo_id': r['undo_id']}).status_code == 404   # one shot


def test_retag_when_labels_move_out_and_undo(dc):
    _, c = dc
    b = new_bucket(c)
    upload_zip(c, b['id'])
    bid = b['id']
    r = c.post(f'/api/dc/buckets/{bid}/ops', json={'ops': [{'op': 'mkdir', 'path': 'backup'},
                                                           {'op': 'move', 'path': 'labels', 'to': 'backup'}]}).json()
    assert r['retagged']['from'] == 'yolo-detect' and r['retagged']['to'] == 'other'
    assert 'labels/ missing' in r['retagged']['reason']
    assert auto(c.get(f'/api/dc/buckets/{bid}').json()) == ['format:other']
    u = c.post(f'/api/dc/buckets/{bid}/undo', json={'undo_id': r['undo_id']}).json()
    assert u['retagged'] == {'path': '', 'from': 'other', 'to': 'yolo-detect', 'reason': u['retagged']['reason']}
    assert auto(c.get(f'/api/dc/buckets/{bid}').json()) == ['format:yolo-detect']


def test_path_escapes_and_local_links(dc, tmp_path):
    _, c = dc
    b = new_bucket(c)
    bid = b['id']
    for bad in ('../', '../../etc', '/etc', 'a/../../x'):
        r = c.get(f'/api/dc/buckets/{bid}/ls', params={'path': bad})
        assert r.status_code == 400, (bad, r.text)
        r = c.post(f'/api/dc/buckets/{bid}/ops', json={'ops': [{'op': 'mkdir', 'path': bad + '/x'}]}).json()
        assert r['applied'] == [] and len(r['errors']) == 1
    assert c.get(f'/api/dc/buckets/{bid}/file', params={'path': '../../secret.key'}).status_code == 400
    # a symlink out of the bucket is refused
    os.symlink('/etc', os.path.join(b['root'], 'out'))
    assert c.get(f'/api/dc/buckets/{bid}/ls', params={'path': 'out'}).status_code == 400

    # local links: inside the allowed roots only
    outside = '/proc' if os.path.isdir('/proc') else '/'
    r = c.post('/api/dc/buckets', json={'name': 'bad', 'kind': 'local', 'path': outside})
    assert r.status_code == 400 and 'allowed_roots' in r.json()['detail']
    folder = tmp_path / 'linked'
    make_jpeg(folder / 'a.jpg')
    lb = new_bucket(c, 'linked', kind='local', path=str(folder))
    assert lb['kind'] == 'local' and lb['root'] == str(folder.resolve()) and auto(lb) == ['format:images']
    # deleting a link never deletes its folder
    assert c.delete(f'/api/dc/buckets/{lb["id"]}', params={'delete_files': 1}).status_code == 400
    assert c.delete(f'/api/dc/buckets/{lb["id"]}').json()['ok'] and (folder / 'a.jpg').exists()


def test_tags_search_and_names(dc):
    _, c = dc
    b = new_bucket(c, 'Night drive', tags=['road'])
    upload_zip(c, b['id'])
    other = new_bucket(c, 'Empty one')
    assert c.post('/api/dc/buckets', json={'name': 'night DRIVE'}).status_code == 409
    bid = b['id']
    t = c.put(f'/api/dc/buckets/{bid}/tags', json={'path': '', 'add': ['night', 'Night'], 'remove': ['road']}).json()
    assert t == [{'tag': 'format:yolo-detect', 'auto': True}, {'tag': 'night', 'auto': False}]
    r = c.put(f'/api/dc/buckets/{bid}/tags', json={'path': '', 'remove': ['format:yolo-detect']})
    assert r.status_code == 400 and 'automatic' in r.json()['detail']
    assert c.put(f'/api/dc/buckets/{bid}/tags', json={'add': ['format:coco']}).status_code == 400
    t = c.put(f'/api/dc/buckets/{bid}/tags', json={'path': 'images/train/im1.jpg', 'add': ['blurry']}).json()
    assert t == [{'tag': 'blurry', 'auto': False}]
    tags = {x['tag']: x for x in c.get('/api/dc/tags').json()}
    assert tags['night']['count'] == 1 and tags['format:yolo-detect']['auto'] and 'format:empty' in tags

    # buckets by name / tag / format
    assert [x['id'] for x in c.get('/api/dc/buckets', params={'q': 'night'}).json()] == [bid]
    assert [x['id'] for x in c.get('/api/dc/buckets', params={'tag': 'NIGHT'}).json()] == [bid]
    assert [x['id'] for x in c.get('/api/dc/buckets', params={'format': 'empty'}).json()] == [other['id']]
    s = c.get('/api/dc/search', params={'format': 'yolo-detect'}).json()
    assert [x['id'] for x in s['buckets']] == [bid]
    # files by name, glob and tag
    s = c.get('/api/dc/search', params={'q': 'im1'}).json()
    assert {f['path'] for f in s['files']} == {'images/train/im1.jpg', 'labels/train/im1.txt'}
    s = c.get('/api/dc/search', params={'glob': '*.TXT', 'bucket': bid, 'path': 'labels'}).json()
    assert len(s['files']) == 3 and all(f['bucket'] == bid for f in s['files'])
    s = c.get('/api/dc/search', params={'tag': 'blurry'}).json()
    assert s['files'] == [{'bucket': bid, 'path': 'images/train/im1.jpg', 'name': 'im1.jpg', 'type': 'file',
                           'size': s['files'][0]['size']}]
    # a tag follows its file through a rename, and comes back with an undo of a delete
    r = c.post(f'/api/dc/buckets/{bid}/ops', json={'ops': [{'op': 'rename', 'path': 'images/train/im1.jpg',
                                                            'name': 'x1.jpg'}]}).json()
    assert c.get('/api/dc/search', params={'tag': 'blurry'}).json()['files'][0]['path'] == 'images/train/x1.jpg'
    r = c.post(f'/api/dc/buckets/{bid}/ops', json={'ops': [{'op': 'delete', 'path': 'images/train/x1.jpg'}]}).json()
    assert c.get('/api/dc/search', params={'tag': 'blurry'}).json()['files'] == []
    c.post(f'/api/dc/buckets/{bid}/undo', json={'undo_id': r['undo_id']})
    assert len(c.get('/api/dc/search', params={'tag': 'blurry'}).json()['files']) == 1

    sm = c.get('/api/dc/summary').json()
    assert sm['buckets'] == 2 and sm['images'] == 3 and sm['links'] == {'local': 0, 's3': 0}


def test_owner_only_and_permissions(dc):
    as_user, c = dc
    b = new_bucket(c)
    bob = as_user(make_user('bob'))
    for path in ('', '/ls', '/stats'):
        assert bob.get(f'/api/dc/buckets/{b["id"]}{path}').status_code == 404
    assert bob.post(f'/api/dc/buckets/{b["id"]}/ops', json={'ops': [{'op': 'mkdir', 'path': 'x'}]}).status_code == 404
    assert bob.get('/api/dc/buckets').json() == []
    admin = as_user(make_user('root', admin=True))
    assert [x['id'] for x in admin.get('/api/dc/buckets').json()] == [b['id']]

    from dataviz import db
    v = make_user('viewer')
    with db.begin() as conn:
        conn.execute(text("UPDATE app.users SET tier_id = 'viewer' WHERE id = :u"), {'u': v['id']})
    viewer = as_user({**v, 'tier_id': 'viewer'})
    r = viewer.post('/api/dc/buckets', json={'name': 'mine'})
    assert r.status_code == 403 and 'Viewer' in r.json()['detail']
    assert viewer.get('/api/dc/buckets').status_code == 200       # reading needs no permission


def test_send_to_label_and_visualize(dc):
    as_user, c = dc
    b = new_bucket(c)
    upload_zip(c, b['id'])
    r = c.post('/api/dc/send', json={'bucket': b['id'], 'paths': [''], 'target': 'label', 'task_type': 'detect'})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out['target'] == 'label' and out['url'] == f'?label={out["id"]}'
    if out.get('job_id'):
        assert wait_job(c, out['job_id'])['status'] == 'done'
    p = c.get(f'/api/label/projects/{out["id"]}').json()
    assert p['source']['kind'] == 'local' and p['source']['path'] == b['root']
    t0 = time.time()
    while p.get('n_media', 0) < 3 and time.time() - t0 < 10:
        time.sleep(0.05)
        p = c.get(f'/api/label/projects/{out["id"]}').json()
    assert p['n_media'] == 3

    v = c.post('/api/dc/send', json={'bucket': b['id'], 'paths': [''], 'target': 'visualize', 'name': 'Cars viz'})
    assert v.status_code == 200, v.text
    assert v.json()['url'] == f's/{v.json()["id"]}/'
    sess = {s['kind'] for s in c.get(f'/api/dc/buckets/{b["id"]}').json()['sessions']}
    assert sess == {'label', 'visualize'}
    # a folder the visualizer cannot read: a clear 400
    c.post(f'/api/dc/buckets/{b["id"]}/ops', json={'ops': [{'op': 'mkdir', 'path': 'docs'}]})
    c.post(f'/api/dc/buckets/{b["id"]}/upload', params={'path': 'docs'},
           files=[('files', ('a.docx', b'xx', 'application/octet-stream'))])
    r = c.post('/api/dc/send', json={'bucket': b['id'], 'paths': ['docs'], 'target': 'visualize'})
    assert r.status_code == 400 and 'visualizer opens' in r.json()['detail']
