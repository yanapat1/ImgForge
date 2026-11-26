"""Bucket: a streamed S3 link downloads only the folders picked (a local copy, edited in place: a downloaded
folder lists from disk and takes mkdir / move / rename / delete / undo / text edits / uploads, S3 untouched; the
rest stays read-only). ls badges every entry (fresh / partial / stream only), a check against S3 counts new / changed / removed files,
Update fetches them and removes files S3 no longer has only with mirror, downloaded files are read from disk, and
with S3 unreachable only the downloaded folders are listed (offline) until a retry finds S3 again."""

import time

import pytest

from conftest import jpeg_bytes, scanned, wait_job
from conftest_io import free_port

pytestmark = pytest.mark.usefixtures('tmp_roots')
pytest_plugins = ['conftest_io']             # fixtures: s3_server, s3_bucket


@pytest.fixture
def dc(client_as, tmp_path):
    from conftest import make_user
    cfg = client_as.ctx.cfg
    cfg['storage']['data_root'] = str(tmp_path / 'dcdata')
    cfg['dataset']['workspace_root'] = str(tmp_path / 'ws')
    from dataviz.bucket import s3dir, store
    s3dir._cache.clear()
    store._down.clear()
    u = make_user('alice')
    c = client_as(u)
    c.user = u
    yield c
    from dataviz.bucket import service
    for _ in range(200):                     # background checks (a listing, an edit queues one) end before the next test
        if not service._busy('dc-'):
            break
        time.sleep(0.05)
    s3dir._cache.clear()
    store._down.clear()


def _link(c, s3_server, s3_bucket):
    client, bucket = s3_bucket
    for d in ('cats', 'dogs', 'birds'):
        for i in range(3):
            client.put_object(Bucket=bucket, Key=f'{d}/im{i}.jpg', Body=jpeg_bytes())
    client.put_object(Bucket=bucket, Key='readme.txt', Body=b'hello')
    conn = c.post('/api/connections', json={**{k: v for k, v in s3_server.items() if k != 'id'}, 'kind': 's3'}).json()
    b = c.post('/api/bucket/buckets', json={'name': 'zoo', 'kind': 's3', 'connection_id': conn['id'],
                                        's3_bucket': bucket, 'prefix': ''}).json()
    wait_job(c, b['scan_job_id'])
    return b['id'], client, bucket


def _ls(c, bid, path='', **kw):
    for _ in range(100):                                    # the listing fills in the background
        r = c.get(f'/api/bucket/buckets/{bid}/ls', params={'path': path, **kw})
        assert r.status_code == 200, r.text
        j = r.json()
        if not j.get('partial'):
            return j
        time.sleep(0.05)
    raise TimeoutError('listing never completed')


def _states(listing):
    return {e['name']: e.get('local') for e in listing['entries']}


def test_download_folders_badges_update_and_mirror(dc, s3_server, s3_bucket):
    c = dc
    bid, client, bucket = _link(c, s3_server, s3_bucket)
    assert _states(_ls(c, bid)) == {'cats': None, 'dogs': None, 'birds': None, 'readme.txt': None}
    r = c.post(f'/api/bucket/buckets/{bid}/local', json={'paths': ['cats', 'dogs/im0.jpg', 'cats/im1.jpg']})
    assert r.status_code == 200, r.text
    assert r.json()['paths'] == ['cats', 'dogs/im0.jpg']                  # cats/im1.jpg is inside cats
    j = wait_job(c, r.json()['job_id'])
    assert j['status'] == 'done' and j['result']['downloaded'] == 4, j
    top = _ls(c, bid)
    assert _states(top) == {'cats': 'fresh', 'dogs': 'partial', 'birds': None, 'readme.txt': None}
    assert top['local_folders'] == 2
    assert _states(_ls(c, bid, 'dogs')) == {'im0.jpg': 'fresh', 'im1.jpg': None, 'im2.jpg': None}
    b = c.get(f'/api/bucket/buckets/{bid}').json()
    assert b['local_folders'] == 2 and b['local_outdated'] == 0 and not b['writable']
    j = c.post(f'/api/bucket/buckets/{bid}/ops', json={'ops': [{'op': 'mkdir', 'path': 'birds/x'}]}).json()
    assert not j['applied'] and 'read-only' in j['errors'][0]['error']      # birds is not downloaded

    # S3 changes: one replaced, one new, one deleted -> file badges at once, the check counts them
    client.put_object(Bucket=bucket, Key='cats/im0.jpg', Body=jpeg_bytes() + b'more')
    client.put_object(Bucket=bucket, Key='cats/im9.jpg', Body=jpeg_bytes())
    client.delete_object(Bucket=bucket, Key='cats/im2.jpg')
    from dataviz.bucket import s3dir
    s3dir.invalidate(bid)
    cats = _ls(c, bid, 'cats')                                          # the copy, as it is on disk
    assert _states(cats) == {'im0.jpg': 'fresh', 'im1.jpg': 'fresh', 'im2.jpg': 'fresh'} and cats['writable']
    j = wait_job(c, c.post(f'/api/bucket/buckets/{bid}/local/check').json()['job_id'])
    assert j['result']['folders']['cats'] == {'state': 'outdated', 'new': 1, 'changed': 1, 'removed': 1}, j
    here = _ls(c, bid, 'cats')['here']
    assert here['state'] == 'outdated' and here['diff']['removed'] == 1
    assert c.get(f'/api/bucket/buckets/{bid}').json()['local_outdated'] == 1

    # Update without mirror keeps the file S3 lost; with mirror it goes
    j = wait_job(c, c.post(f'/api/bucket/buckets/{bid}/local', json={'paths': ['cats']}).json()['job_id'])
    assert (j['result']['downloaded'], j['result']['skipped'], j['result']['removed']) == (2, 1, 0), j
    assert set(_states(_ls(c, bid, 'cats'))) == {'im0.jpg', 'im1.jpg', 'im2.jpg', 'im9.jpg'}
    j = wait_job(c, c.post(f'/api/bucket/buckets/{bid}/local', json={'paths': ['cats'], 'mirror': True}).json()['job_id'])
    assert j['result']['removed'] == 1
    assert _states(_ls(c, bid, 'cats')) == {'im0.jpg': 'fresh', 'im1.jpg': 'fresh', 'im9.jpg': 'fresh'}

    # a downloaded file is read from disk: still readable once S3 lost it
    client.delete_object(Bucket=bucket, Key='cats/im1.jpg')
    r = c.get(f'/api/bucket/buckets/{bid}/file', params={'path': 'cats/im1.jpg'})
    assert r.status_code == 200 and r.content == jpeg_bytes()
    assert c.get(f'/api/bucket/buckets/{bid}/file', params={'path': 'cats/im1.jpg', 'w': 64}).status_code == 200

    # remove the local copy: streams again
    r = c.post(f'/api/bucket/buckets/{bid}/local/remove', json={'paths': ['cats/im0.jpg']})
    assert r.status_code == 400 and 'part of the downloaded folder cats' in r.json()['detail']
    assert c.post(f'/api/bucket/buckets/{bid}/local/remove', json={'paths': ['cats']}).json() == {'removed': ['cats']}
    assert _states(_ls(c, bid))['cats'] is None


def test_offline_lists_only_downloaded_folders_until_retry(dc, s3_server, s3_bucket, monkeypatch):
    c = dc
    bid, client, bucket = _link(c, s3_server, s3_bucket)
    wait_job(c, c.post(f'/api/bucket/buckets/{bid}/local', json={'paths': ['birds']}).json()['job_id'])
    from dataviz.bucket import s3dir, service
    reg = service._datasets()
    real = reg.connection
    dead = f'http://127.0.0.1:{free_port()}'
    monkeypatch.setattr(reg, 'connection', lambda *a, **k: {**real(*a, **k), 'endpoint': dead})
    s3dir.invalidate(bid)
    top = _ls(c, bid)
    assert top['offline'] and _states(top) == {'birds': 'fresh'} and 'unreachable' in top['error']
    assert _states(_ls(c, bid, 'birds')) == {'im0.jpg': 'fresh', 'im1.jpg': 'fresh', 'im2.jpg': 'fresh'}
    r = c.get(f'/api/bucket/buckets/{bid}/ls', params={'path': 'cats'})
    assert r.status_code == 503 and 'only downloaded folders' in r.json()['detail']
    assert c.get(f'/api/bucket/buckets/{bid}/file', params={'path': 'birds/im0.jpg', 'w': 64}).status_code == 200
    t0 = time.time()
    assert c.get(f'/api/bucket/buckets/{bid}/file', params={'path': 'cats/im0.jpg'}).status_code == 503
    assert time.time() - t0 < 1                                         # the breaker answers at once
    monkeypatch.setattr(reg, 'connection', real)                        # S3 is back: a retry lists it again
    back = _ls(c, bid, retry=1)
    assert not back.get('offline') and _states(back) == {'cats': None, 'dogs': None, 'birds': 'fresh',
                                                         'readme.txt': None}


def _idle(prefix):
    from dataviz.bucket import service
    for _ in range(200):
        if not service._busy(prefix):
            return
        time.sleep(0.05)
    raise TimeoutError(prefix)


def test_a_failed_check_or_scan_is_not_queued_again_by_every_ls(dc, s3_server, s3_bucket, monkeypatch):
    c = dc
    bid, *_ = _link(c, s3_server, s3_bucket)
    wait_job(c, c.post(f'/api/bucket/buckets/{bid}/local', json={'paths': ['birds']}).json()['job_id'])
    from sqlalchemy import text
    from dataviz import db
    from dataviz.bucket import service
    calls = {'compare': 0, 'scan': 0}

    def boom(name):
        def fn(*a, **k):
            calls[name] += 1
            raise RuntimeError('SlowDown')
        return fn
    monkeypatch.setattr(service, '_compare', boom('compare'))
    monkeypatch.setattr(service, '_scan', boom('scan'))
    with db.begin() as x:                                   # the check is due, cats was never scanned
        x.execute(text("UPDATE app.bucket_s3_local SET checked_at = now() - interval '1 hour' WHERE bucket_id = :b"),
                  {'b': bid})
        x.execute(text("DELETE FROM app.bucket_format_cache WHERE bucket_id = :b AND path = 'cats'"), {'b': bid})
    for _ in range(3):                                      # the page polls ls while a folder lists
        _ls(c, bid, 'cats')
        _idle(f'bk-check:{bid}')
        _idle(f'bk-scan:{bid}:')
    assert calls == {'compare': 1, 'scan': 1}
    monkeypatch.setattr(service, '_quiet', {})              # the backoff over: queued again
    _ls(c, bid, 'cats')
    _idle(f'bk-check:{bid}')
    _idle(f'bk-scan:{bid}:')
    assert calls == {'compare': 2, 'scan': 2}


def test_edit_inside_a_downloaded_folder_only(dc, s3_server, s3_bucket):
    c = dc
    bid, client, bucket = _link(c, s3_server, s3_bucket)
    wait_job(c, c.post(f'/api/bucket/buckets/{bid}/local', json={'paths': ['cats']}).json()['job_id'])
    ops = lambda *o: c.post(f'/api/bucket/buckets/{bid}/ops', json={'ops': list(o)})      # noqa: E731
    r = ops({'op': 'mkdir', 'path': 'cats/sub'}, {'op': 'move', 'path': 'cats/im0.jpg', 'to': 'cats/sub'},
            {'op': 'rename', 'path': 'cats/im1.jpg', 'name': 'one.jpg'}, {'op': 'delete', 'path': 'cats/im2.jpg'},
            {'op': 'move', 'path': 'cats/one.jpg', 'to': 'dogs'}, {'op': 'delete', 'path': 'cats'},
            {'op': 'delete', 'path': 'dogs/im0.jpg'})
    assert r.status_code == 200, r.text
    j = r.json()
    assert [a['op'] for a in j['applied']] == ['mkdir', 'move', 'rename', 'delete'], j
    errs = [e['error'] for e in j['errors']]
    assert 'streamed from S3 and read-only: download that folder first' in errs[0]        # into a stream-only folder
    assert 'is a downloaded folder itself' in errs[1] and 'streamed from S3' in errs[2]
    cats = _ls(c, bid, 'cats')
    assert cats['writable'] and set(_states(cats)) == {'sub', 'one.jpg'}
    assert set(_states(_ls(c, bid, 'cats/sub'))) == {'im0.jpg'}
    keys = {o['Key'] for o in client.list_objects_v2(Bucket=bucket, Prefix='cats/')['Contents']}
    assert keys == {'cats/im0.jpg', 'cats/im1.jpg', 'cats/im2.jpg'}                      # S3 is never written
    assert cats['location'].endswith('/cats') and cats['s3_url'] == f's3://{bucket}/cats'
    assert _ls(c, bid)['location'] == f's3://{bucket}' and not _ls(c, bid)['writable']

    # undo puts the copy back; text edits and uploads land in the copy; the rest stays read-only
    u = c.post(f'/api/bucket/buckets/{bid}/undo', json={'undo_id': j['undo_id']})
    assert u.status_code == 200 and not u.json()['errors'], u.text
    assert set(_states(_ls(c, bid, 'cats'))) == {'im0.jpg', 'im1.jpg', 'im2.jpg'}
    w = c.put(f'/api/bucket/buckets/{bid}/text', json={'path': 'cats/notes.txt', 'text': 'hi', 'create': True})
    assert w.status_code == 200, w.text
    t = c.get(f'/api/bucket/buckets/{bid}/text', params={'path': 'cats/notes.txt'}).json()
    assert t['text'] == 'hi' and t['editable']
    assert not c.get(f'/api/bucket/buckets/{bid}/text', params={'path': 'readme.txt'}).json()['editable']
    assert c.put(f'/api/bucket/buckets/{bid}/text', json={'path': 'readme.txt', 'text': 'x'}).status_code == 400
    up = c.post(f'/api/bucket/buckets/{bid}/upload', params={'path': 'cats'},
                files=[('files', ('new.jpg', jpeg_bytes(), 'image/jpeg'))])
    assert up.status_code == 200, up.text
    assert 'new.jpg' in _states(_ls(c, bid, 'cats'))
    assert c.post(f'/api/bucket/buckets/{bid}/upload', params={'path': 'dogs'},
                  files=[('files', ('n.jpg', jpeg_bytes(), 'image/jpeg'))]).status_code == 400

    # the assistant's view of the open folder
    from dataviz.bucket import service
    st = service.folder_state(c.user, bid, 'cats')
    assert st['editable'] and st['local'] in ('fresh', 'outdated') and st['downloaded_in'] == 'cats'
    st = service.folder_state(c.user, bid, 'dogs')
    assert not st['editable'] and st['local'] is None and st['location'] == f's3://{bucket}/dogs'
    from dataviz.llm.engine import roles
    txt = roles.bucket_scope(c.user, {'bucket': bid, 'path': 'cats'})
    assert "folder 'cats'" in txt and 'editable' in txt and 'Do NOT propose loading the whole bucket' in txt
    txt = roles.bucket_scope(c.user, {'bucket': bid, 'path': 'dogs'})
    assert f's3://{bucket}/dogs' in txt and 'download_s3_folders for just this folder' in txt and "'cats'" in txt
    from dataviz.llm.tools import bucket as tdc
    change = type('T', (), {'s3_ok': False})()                       # a changing tool that is not S3-aware
    tdc._writable({'kind': 's3', 'writable': False, 'local_folders': 1}, change)      # the service decides per path
    with pytest.raises(Exception, match='download_s3_folders'):
        tdc._writable({'kind': 's3', 'writable': False, 'name': 'zoo'}, change)


def test_reload_lists_s3_again(dc, s3_server, s3_bucket):
    c = dc
    bid, client, bucket = _link(c, s3_server, s3_bucket)
    assert 'new' not in {e['name'] for e in _ls(c, bid)['entries']}
    client.put_object(Bucket=bucket, Key='new/a.jpg', Body=jpeg_bytes())
    assert 'new' not in {e['name'] for e in _ls(c, bid)['entries']}      # the listing is kept for a minute
    assert 'new' in {e['name'] for e in _ls(c, bid, refresh=1)['entries']}


def _compare(c, bid, path='', verify=False, **kw):
    r = c.post(f'/api/bucket/buckets/{bid}/compare', json={'path': path, 'verify': verify})
    assert r.status_code == 200, r.text
    j = wait_job(c, r.json()['job_id'])
    assert j['status'] == 'done', j
    g = c.get(f'/api/bucket/buckets/{bid}/compare', params={'path': path, 'limit': 1000, **kw})
    assert g.status_code == 200, g.text
    return g.json()


def test_compare_the_local_copy_with_the_cloud(dc, s3_server, s3_bucket):
    """Every file of a downloaded folder against S3: what the user added, deleted or edited here, and what S3 added,
    changed or removed since the download; verify compares the bytes (MD5 = a single-part ETag); paging + filters."""
    import os
    c = dc
    bid, client, bucket = _link(c, s3_server, s3_bucket)
    assert c.get(f'/api/bucket/buckets/{bid}/compare', params={'path': 'cats'}).json()['ready'] is False
    r = c.post(f'/api/bucket/buckets/{bid}/compare', json={'path': 'cats'})
    assert r.status_code == 400 and 'nothing of' in r.json()['detail']          # nothing downloaded yet
    wait_job(c, c.post(f'/api/bucket/buckets/{bid}/local', json={'paths': ['cats']}).json()['job_id'])
    same = _compare(c, bid, 'cats')
    assert same['counts']['same'] == 3 and same['total'] == 3

    time.sleep(3.2)                                     # past the download's end (and the clock slack)
    root = next(x for x in c.get('/api/bucket/buckets').json() if x['id'] == bid)
    local = os.path.join(c.ctx.cfg['storage']['data_root'], '.s3')
    copy = next(os.path.join(local, d) for d in os.listdir(local))
    with open(os.path.join(copy, 'cats', 'im0.jpg'), 'ab') as f:         # edited here
        f.write(b'x')
    os.remove(os.path.join(copy, 'cats', 'im1.jpg'))                     # deleted here
    with open(os.path.join(copy, 'cats', 'mine.txt'), 'w') as f:         # added here
        f.write('note')
    client.put_object(Bucket=bucket, Key='cats/im2.jpg', Body=jpeg_bytes() + b'v2')   # changed in S3
    client.put_object(Bucket=bucket, Key='cats/new.jpg', Body=jpeg_bytes())           # new in S3
    got = _compare(c, bid, 'cats')
    st = {x['path']: x['status'] for x in got['rows']}
    assert st == {'cats/im0.jpg': 'edited_local', 'cats/im1.jpg': 'deleted_local', 'cats/mine.txt': 'added_local',
                  'cats/im2.jpg': 'changed_cloud', 'cats/new.jpg': 'new_cloud'}, st
    assert got['counts']['same'] == 0 and got['total'] == 5 and got['complete'] and root['kind'] == 's3'
    # rows come by status (what the user did first), then path; filters and paging
    assert [x['status'] for x in got['rows']][:3] == ['added_local', 'deleted_local', 'edited_local']
    f = c.get(f'/api/bucket/buckets/{bid}/compare', params={'path': 'cats', 'status': 'new_cloud,changed_cloud'}).json()
    assert [x['path'] for x in f['rows']] == ['cats/new.jpg', 'cats/im2.jpg'] and f['matched'] == 2
    p = c.get(f'/api/bucket/buckets/{bid}/compare', params={'path': 'cats', 'offset': 4, 'limit': 1}).json()
    assert p['matched'] == 5 and len(p['rows']) == 1 and p['offset'] == 4
    assert c.get(f'/api/bucket/buckets/{bid}/compare', params={'path': 'cats', 'q': 'mine'}).json()['matched'] == 1
    assert c.get(f'/api/bucket/buckets/{bid}/compare', params={'path': 'cats', 'status': 'nope'}).status_code == 400

    # an S3 deletion after the download; a file re-saved with the same bytes reads as same with verify
    client.delete_object(Bucket=bucket, Key='cats/new.jpg')
    upd = wait_job(c, c.post(f'/api/bucket/buckets/{bid}/local', json={'paths': ['cats']}).json()['job_id'])   # update
    assert upd['result']['kept'] == 1 and upd['result']['conflicts'] == []     # the local edit stays (S3 did not change it)
    time.sleep(3.2)
    client.delete_object(Bucket=bucket, Key='cats/im2.jpg')
    os.utime(os.path.join(copy, 'cats', 'im1.jpg'))                       # touched, same bytes
    got = _compare(c, bid, 'cats', verify=True)
    st = {x['path']: (x['status'], x.get('verified')) for x in got['rows']}
    assert st['cats/im2.jpg'][0] == 'removed_cloud' and st['cats/im1.jpg'] == ('same', 'match'), st
    assert st['cats/im0.jpg'][0] == 'edited_local'                           # the update kept the local edit
    assert got['verify'] is True
    # the ◆ assistant reads the same comparison (read-only tool); refresh compares again
    from dataviz.llm import tools
    out, line = tools.run(c.user, 'compare_s3_local', {'bucket': bid, 'path': 'cats', 'status': ['removed_cloud']})
    assert [f['path'] for f in out['files']] == ['cats/im2.jpg'] and out['counts']['removed_cloud'] == 1
    assert 'in S3' in line and tools.TOOLS['compare_s3_local'].kind == 'read'
    out, _ = tools.run(c.user, 'compare_s3_local', {'bucket': 'zoo', 'path': 'cats', 'refresh': True})
    assert out['total'] == got['total'] and not out['verify']


def test_compare_needs_an_s3_link(dc, tmp_path):
    c = dc
    (tmp_path / 'm').mkdir()
    b = c.post('/api/bucket/buckets', json={'name': 'm', 'kind': 'managed'}).json()
    r = c.post(f"/api/bucket/buckets/{b['id']}/compare", json={'path': ''})
    assert r.status_code == 400 and 'S3 link' in r.json()['detail']


def test_reformat_a_streamed_folder_keeps_the_images_in_s3(dc, s3_server, s3_bucket):
    """Re-format of a read-only S3 folder: labels + images.txt + source.json in "<bucket> · formats", nothing written
    to S3; the new folder's session check says "images in S3" and Visualize opens it (local labels, streamed images)."""
    c = dc
    client, bucket = s3_bucket
    for i in range(2):
        client.put_object(Bucket=bucket, Key=f'cars/images/im{i}.jpg', Body=jpeg_bytes())
        client.put_object(Bucket=bucket, Key=f'cars/labels/im{i}.txt', Body=b'0 0.5 0.5 0.2 0.2\n')
    client.put_object(Bucket=bucket, Key='cars/data.yaml', Body=b'names:\n  0: car\n')
    before = sorted(o['Key'] for o in client.list_objects_v2(Bucket=bucket)['Contents'])
    conn = c.post('/api/connections', json={**{k: v for k, v in s3_server.items() if k != 'id'}, 'kind': 's3'}).json()
    b = c.post('/api/bucket/buckets', json={'name': 'road', 'kind': 's3', 'connection_id': conn['id'],
                                        's3_bucket': bucket, 'prefix': ''}).json()
    wait_job(c, b['scan_job_id'])
    _ls(c, b['id'], 'cars')
    r = c.post('/api/bucket/reformat', json={'bucket': b['id'], 'path': 'cars', 'to': 'yolo'})
    assert r.status_code == 409 and 'still reading cars from S3' in r.json()['detail']    # not listed yet
    assert scanned(c, b['id'], 'cars')['format'] == 'yolo-detect'
    r = c.post('/api/bucket/reformat', json={'bucket': b['id'], 'path': 'cars', 'to': 'yolo'})
    assert r.status_code == 200, r.text
    out = r.json()
    j = wait_job(c, out['job_id'], timeout=60)
    assert j['status'] == 'done' and j['result']['streamed'] and j['result']['images_linked'] == 0, j
    assert sorted(o['Key'] for o in client.list_objects_v2(Bucket=bucket)['Contents']) == before, 'S3 untouched'
    chk = c.get(f'/api/bucket/buckets/{out["bucket"]}/session-check', params={'path': out['path']}).json()
    assert chk['format'] == 's3-labels' and chk['can'] == {'visualize': True, 'label': True, 'linked': True}, chk
    assert chk['label_import'] == 'imported'
    s = c.post('/api/bucket/session', json={'bucket': out['bucket'], 'path': out['path'], 'label': False})
    assert s.status_code == 200, s.text
    ds = c.ctx.datasets.dataset(s.json()['visualize']['id'])
    assert ds['kind'] == 's3' and ds['labels_dir'].endswith(out['path']) and ds['prefix'] == 'cars'
    src = c.ctx.datasets.source(ds)
    assert src.list_labels() == ['im0.txt', 'im1.txt'] and src.list_images() == ['im0.jpg', 'im1.jpg']
    assert src.label_writable                            # its fixes go to these local labels
    # both, linked: the Label session streams the images from S3 and imports the local labels
    s = c.post('/api/bucket/session', json={'bucket': out['bucket'], 'path': out['path'], 'name': 'cars linked'})
    assert s.status_code == 200, s.text
    lab = s.json()['label']
    assert s.json()['linked'] and lab.get('import_job_id'), s.json()
    wait_job(c, lab['job_id'], timeout=60)
    imp = wait_job(c, lab['import_job_id'], timeout=60)
    assert imp['status'] == 'done' and imp['result']['images'] == 2 and imp['result']['annotations'] == 2, imp
    entry = c.ctx.labeling.get(lab['id'])
    assert entry['source']['kind'] == 's3' and entry['source']['prefix'] == 'cars'
    assert sorted(o['Key'] for o in client.list_objects_v2(Bucket=bucket)['Contents']) == before, 'S3 untouched'


def _yolo_in_s3(client, bucket, folder='cars', n=2):
    for i in range(n):
        client.put_object(Bucket=bucket, Key=f'{folder}/images/im{i}.jpg', Body=jpeg_bytes())
        client.put_object(Bucket=bucket, Key=f'{folder}/labels/im{i}.txt', Body=b'0 0.5 0.5 0.2 0.2\n')
    client.put_object(Bucket=bucket, Key=f'{folder}/data.yaml', Body=b'names:\n  0: car\n  1: bus\n')


def test_a_streamed_visualize_session_fixes_labels_locally_and_uploads_them(dc, s3_server, s3_bucket):
    """Visualize alone on a streamed YOLO folder: its labels/ (+ data.yaml) is downloaded as a downloaded folder and the
    session reads its labels there, so a label fix never writes S3: it is marked in Bucket, kept by a later download,
    and Upload to S3 sends it (a conflict when S3 changed it too). The class names come from data.yaml in S3."""
    c = dc
    client, bucket = s3_bucket
    _yolo_in_s3(client, bucket)
    conn = c.post('/api/connections', json={**{k: v for k, v in s3_server.items() if k != 'id'}, 'kind': 's3'}).json()
    b = c.post('/api/bucket/buckets', json={'name': 'road', 'kind': 's3', 'connection_id': conn['id'],
                                        's3_bucket': bucket, 'prefix': ''}).json()
    wait_job(c, b['scan_job_id'])
    bid = b['id']
    chk = scanned(c, bid, 'cars')
    assert chk['format'] == 'yolo-detect' and chk['streamed'] and chk['classes'] == ['car', 'bus'], chk
    r = c.post('/api/bucket/session', json={'bucket': bid, 'path': 'cars', 'label': False, 'name': 'Cars viz'})
    assert r.status_code == 200, r.text
    viz = r.json()['visualize']
    assert wait_job(c, viz['job_id'])['status'] == 'done', 'the labels were downloaded'
    ds = c.ctx.datasets.dataset(viz['id'])
    assert ds['kind'] == 's3' and ds['prefix'] == 'cars' and ds['labels_dir'].endswith('cars')
    src = c.ctx.datasets.source(ds)
    assert src.label_writable and src.list_labels() == ['im0.txt', 'im1.txt'] and src.read_classes() == ['car', 'bus']
    assert _states(_ls(c, bid, 'cars'))['labels'] == 'fresh'

    from dataviz.bucket import changes
    from dataviz.image import sources
    changes.install()
    old = src.read_label_bytes('im0.txt')
    src.write_label('im0.txt', '1 0.5 0.5 0.3 0.3\n', sources.sha1_bytes(old))      # a fix made in Visualize
    assert client.get_object(Bucket=bucket, Key='cars/labels/im0.txt')['Body'].read() == old, 'S3 untouched'
    changes.flush()
    marks = {e['name']: e.get('changed') for e in _ls(c, bid, 'cars/labels')['entries']}
    assert marks['im0.txt'] and not marks['im1.txt'], marks
    # a download again keeps the fix
    j = wait_job(c, c.post(f'/api/bucket/buckets/{bid}/local', json={'paths': ['cars/labels']}).json()['job_id'])
    assert j['result']['kept'] == 1 and src.read_label('im0.txt').startswith('1 '), j['result']
    # Upload to S3 sends it
    r = c.post(f'/api/bucket/buckets/{bid}/push', json={'paths': ['cars/labels']})
    assert r.status_code == 200, r.text
    res = wait_job(c, r.json()['job_id'])['result']
    assert res['uploaded'] == 1 and not res['conflicts'], res
    assert client.get_object(Bucket=bucket, Key='cars/labels/im0.txt')['Body'].read().startswith(b'1 ')


def test_a_downloaded_folder_makes_a_local_session_and_plain_streams_are_read_only(dc, s3_server, s3_bucket):
    c = dc
    client, bucket = s3_bucket
    _yolo_in_s3(client, bucket)
    conn = c.post('/api/connections', json={**{k: v for k, v in s3_server.items() if k != 'id'}, 'kind': 's3'}).json()
    b = c.post('/api/bucket/buckets', json={'name': 'road', 'kind': 's3', 'connection_id': conn['id'],
                                        's3_bucket': bucket, 'prefix': ''}).json()
    wait_job(c, b['scan_job_id'])
    bid = b['id']
    wait_job(c, c.post(f'/api/bucket/buckets/{bid}/local', json={'paths': ['cars']}).json()['job_id'])
    chk = scanned(c, bid, 'cars')
    assert not chk['streamed'], 'a downloaded folder is read from its local copy'
    r = c.post('/api/bucket/session', json={'bucket': bid, 'path': 'cars', 'label': False})
    assert r.status_code == 200, r.text
    ds = c.ctx.datasets.dataset(r.json()['visualize']['id'])
    assert ds['kind'] == 'local' and ds['path'].endswith('cars') and 'job_id' not in r.json()['visualize']
    # an S3 source without local labels never writes S3
    from dataviz.image import sources
    s3 = sources.S3Source(c.ctx.datasets.connection(conn['id'], owner_id=c.user['id']), bucket, 'cars')
    assert not s3.label_writable
    with pytest.raises(ValueError, match='not kept locally'):
        s3.write_label('im0.txt', '0 0.5 0.5 0.1 0.1\n')
