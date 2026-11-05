"""Data Center: a streamed S3 link downloads only the folders picked (a local copy, edited in place: a downloaded
folder lists from disk and takes mkdir / move / rename / delete / undo / text edits / uploads, S3 untouched; the
rest stays read-only). ls badges every entry (fresh / partial / stream only), a check against S3 counts new / changed / removed files,
Update fetches them and removes files S3 no longer has only with mirror, downloaded files are read from disk, and
with S3 unreachable only the downloaded folders are listed (offline) until a retry finds S3 again."""

import time

import pytest

from conftest import jpeg_bytes, wait_job
from conftest_io import free_port

pytestmark = pytest.mark.usefixtures('tmp_roots')
pytest_plugins = ['conftest_io']             # fixtures: s3_server, s3_bucket


@pytest.fixture
def dc(client_as, tmp_path):
    from conftest import make_user
    cfg = client_as.ctx.cfg
    cfg['storage']['data_root'] = str(tmp_path / 'dcdata')
    cfg['dataset']['workspace_root'] = str(tmp_path / 'ws')
    from dataviz.datacenter import s3dir, store
    s3dir._cache.clear()
    store._down.clear()
    u = make_user('alice')
    c = client_as(u)
    c.user = u
    yield c
    from dataviz.datacenter import service
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
    b = c.post('/api/dc/buckets', json={'name': 'zoo', 'kind': 's3', 'connection_id': conn['id'],
                                        's3_bucket': bucket, 'prefix': ''}).json()
    wait_job(c, b['scan_job_id'])
    return b['id'], client, bucket


def _ls(c, bid, path='', **kw):
    for _ in range(100):                                    # the listing fills in the background
        r = c.get(f'/api/dc/buckets/{bid}/ls', params={'path': path, **kw})
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
    r = c.post(f'/api/dc/buckets/{bid}/local', json={'paths': ['cats', 'dogs/im0.jpg', 'cats/im1.jpg']})
    assert r.status_code == 200, r.text
    assert r.json()['paths'] == ['cats', 'dogs/im0.jpg']                  # cats/im1.jpg is inside cats
    j = wait_job(c, r.json()['job_id'])
    assert j['status'] == 'done' and j['result']['downloaded'] == 4, j
    top = _ls(c, bid)
    assert _states(top) == {'cats': 'fresh', 'dogs': 'partial', 'birds': None, 'readme.txt': None}
    assert top['local_folders'] == 2
    assert _states(_ls(c, bid, 'dogs')) == {'im0.jpg': 'fresh', 'im1.jpg': None, 'im2.jpg': None}
    b = c.get(f'/api/dc/buckets/{bid}').json()
    assert b['local_folders'] == 2 and b['local_outdated'] == 0 and not b['writable']
    j = c.post(f'/api/dc/buckets/{bid}/ops', json={'ops': [{'op': 'mkdir', 'path': 'birds/x'}]}).json()
    assert not j['applied'] and 'read-only' in j['errors'][0]['error']      # birds is not downloaded

    # S3 changes: one replaced, one new, one deleted -> file badges at once, the check counts them
    client.put_object(Bucket=bucket, Key='cats/im0.jpg', Body=jpeg_bytes() + b'more')
    client.put_object(Bucket=bucket, Key='cats/im9.jpg', Body=jpeg_bytes())
    client.delete_object(Bucket=bucket, Key='cats/im2.jpg')
    from dataviz.datacenter import s3dir
    s3dir.invalidate(bid)
    cats = _ls(c, bid, 'cats')                                          # the copy, as it is on disk
    assert _states(cats) == {'im0.jpg': 'fresh', 'im1.jpg': 'fresh', 'im2.jpg': 'fresh'} and cats['writable']
    j = wait_job(c, c.post(f'/api/dc/buckets/{bid}/local/check').json()['job_id'])
    assert j['result']['folders']['cats'] == {'state': 'outdated', 'new': 1, 'changed': 1, 'removed': 1}, j
    here = _ls(c, bid, 'cats')['here']
    assert here['state'] == 'outdated' and here['diff']['removed'] == 1
    assert c.get(f'/api/dc/buckets/{bid}').json()['local_outdated'] == 1

    # Update without mirror keeps the file S3 lost; with mirror it goes
    j = wait_job(c, c.post(f'/api/dc/buckets/{bid}/local', json={'paths': ['cats']}).json()['job_id'])
    assert (j['result']['downloaded'], j['result']['skipped'], j['result']['removed']) == (2, 1, 0), j
    assert set(_states(_ls(c, bid, 'cats'))) == {'im0.jpg', 'im1.jpg', 'im2.jpg', 'im9.jpg'}
    j = wait_job(c, c.post(f'/api/dc/buckets/{bid}/local', json={'paths': ['cats'], 'mirror': True}).json()['job_id'])
    assert j['result']['removed'] == 1
    assert _states(_ls(c, bid, 'cats')) == {'im0.jpg': 'fresh', 'im1.jpg': 'fresh', 'im9.jpg': 'fresh'}

    # a downloaded file is read from disk: still readable once S3 lost it
    client.delete_object(Bucket=bucket, Key='cats/im1.jpg')
    r = c.get(f'/api/dc/buckets/{bid}/file', params={'path': 'cats/im1.jpg'})
    assert r.status_code == 200 and r.content == jpeg_bytes()
    assert c.get(f'/api/dc/buckets/{bid}/file', params={'path': 'cats/im1.jpg', 'w': 64}).status_code == 200

    # remove the local copy: streams again
    r = c.post(f'/api/dc/buckets/{bid}/local/remove', json={'paths': ['cats/im0.jpg']})
    assert r.status_code == 400 and 'part of the downloaded folder cats' in r.json()['detail']
    assert c.post(f'/api/dc/buckets/{bid}/local/remove', json={'paths': ['cats']}).json() == {'removed': ['cats']}
    assert _states(_ls(c, bid))['cats'] is None


def test_offline_lists_only_downloaded_folders_until_retry(dc, s3_server, s3_bucket, monkeypatch):
    c = dc
    bid, client, bucket = _link(c, s3_server, s3_bucket)
    wait_job(c, c.post(f'/api/dc/buckets/{bid}/local', json={'paths': ['birds']}).json()['job_id'])
    from dataviz.datacenter import s3dir, service
    reg = service._datasets()
    real = reg.connection
    dead = f'http://127.0.0.1:{free_port()}'
    monkeypatch.setattr(reg, 'connection', lambda *a, **k: {**real(*a, **k), 'endpoint': dead})
    s3dir.invalidate(bid)
    top = _ls(c, bid)
    assert top['offline'] and _states(top) == {'birds': 'fresh'} and 'unreachable' in top['error']
    assert _states(_ls(c, bid, 'birds')) == {'im0.jpg': 'fresh', 'im1.jpg': 'fresh', 'im2.jpg': 'fresh'}
    r = c.get(f'/api/dc/buckets/{bid}/ls', params={'path': 'cats'})
    assert r.status_code == 503 and 'only downloaded folders' in r.json()['detail']
    assert c.get(f'/api/dc/buckets/{bid}/file', params={'path': 'birds/im0.jpg', 'w': 64}).status_code == 200
    t0 = time.time()
    assert c.get(f'/api/dc/buckets/{bid}/file', params={'path': 'cats/im0.jpg'}).status_code == 503
    assert time.time() - t0 < 1                                         # the breaker answers at once
    monkeypatch.setattr(reg, 'connection', real)                        # S3 is back: a retry lists it again
    back = _ls(c, bid, retry=1)
    assert not back.get('offline') and _states(back) == {'cats': None, 'dogs': None, 'birds': 'fresh',
                                                         'readme.txt': None}


def _idle(prefix):
    from dataviz.datacenter import service
    for _ in range(200):
        if not service._busy(prefix):
            return
        time.sleep(0.05)
    raise TimeoutError(prefix)


def test_a_failed_check_or_scan_is_not_queued_again_by_every_ls(dc, s3_server, s3_bucket, monkeypatch):
    c = dc
    bid, *_ = _link(c, s3_server, s3_bucket)
    wait_job(c, c.post(f'/api/dc/buckets/{bid}/local', json={'paths': ['birds']}).json()['job_id'])
    from sqlalchemy import text
    from dataviz import db
    from dataviz.datacenter import service
    calls = {'compare': 0, 'scan': 0}

    def boom(name):
        def fn(*a, **k):
            calls[name] += 1
            raise RuntimeError('SlowDown')
        return fn
    monkeypatch.setattr(service, '_compare', boom('compare'))
    monkeypatch.setattr(service, '_scan', boom('scan'))
    with db.begin() as x:                                   # the check is due, cats was never scanned
        x.execute(text("UPDATE app.dc_s3_local SET checked_at = now() - interval '1 hour' WHERE bucket_id = :b"),
                  {'b': bid})
        x.execute(text("DELETE FROM app.dc_format_cache WHERE bucket_id = :b AND path = 'cats'"), {'b': bid})
    for _ in range(3):                                      # the page polls ls while a folder lists
        _ls(c, bid, 'cats')
        _idle(f'dc-check:{bid}')
        _idle(f'dc-scan:{bid}:')
    assert calls == {'compare': 1, 'scan': 1}
    monkeypatch.setattr(service, '_quiet', {})              # the backoff over: queued again
    _ls(c, bid, 'cats')
    _idle(f'dc-check:{bid}')
    _idle(f'dc-scan:{bid}:')
    assert calls == {'compare': 2, 'scan': 2}


def test_edit_inside_a_downloaded_folder_only(dc, s3_server, s3_bucket):
    c = dc
    bid, client, bucket = _link(c, s3_server, s3_bucket)
    wait_job(c, c.post(f'/api/dc/buckets/{bid}/local', json={'paths': ['cats']}).json()['job_id'])
    ops = lambda *o: c.post(f'/api/dc/buckets/{bid}/ops', json={'ops': list(o)})      # noqa: E731
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
    u = c.post(f'/api/dc/buckets/{bid}/undo', json={'undo_id': j['undo_id']})
    assert u.status_code == 200 and not u.json()['errors'], u.text
    assert set(_states(_ls(c, bid, 'cats'))) == {'im0.jpg', 'im1.jpg', 'im2.jpg'}
    w = c.put(f'/api/dc/buckets/{bid}/text', json={'path': 'cats/notes.txt', 'text': 'hi', 'create': True})
    assert w.status_code == 200, w.text
    t = c.get(f'/api/dc/buckets/{bid}/text', params={'path': 'cats/notes.txt'}).json()
    assert t['text'] == 'hi' and t['editable']
    assert not c.get(f'/api/dc/buckets/{bid}/text', params={'path': 'readme.txt'}).json()['editable']
    assert c.put(f'/api/dc/buckets/{bid}/text', json={'path': 'readme.txt', 'text': 'x'}).status_code == 400
    up = c.post(f'/api/dc/buckets/{bid}/upload', params={'path': 'cats'},
                files=[('files', ('new.jpg', jpeg_bytes(), 'image/jpeg'))])
    assert up.status_code == 200, up.text
    assert 'new.jpg' in _states(_ls(c, bid, 'cats'))
    assert c.post(f'/api/dc/buckets/{bid}/upload', params={'path': 'dogs'},
                  files=[('files', ('n.jpg', jpeg_bytes(), 'image/jpeg'))]).status_code == 400

    # the assistant's view of the open folder
    from dataviz.datacenter import service
    st = service.folder_state(c.user, bid, 'cats')
    assert st['editable'] and st['local'] in ('fresh', 'outdated') and st['downloaded_in'] == 'cats'
    st = service.folder_state(c.user, bid, 'dogs')
    assert not st['editable'] and st['local'] is None and st['location'] == f's3://{bucket}/dogs'
    from dataviz.llm.engine import roles
    txt = roles.data_scope(c.user, {'bucket': bid, 'path': 'cats'})
    assert "folder 'cats'" in txt and 'editable' in txt and 'Do NOT propose loading the whole bucket' in txt
    txt = roles.data_scope(c.user, {'bucket': bid, 'path': 'dogs'})
    assert f's3://{bucket}/dogs' in txt and 'download_s3_folders for just this folder' in txt and "'cats'" in txt
    from dataviz.llm.tools import datacenter as tdc
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
