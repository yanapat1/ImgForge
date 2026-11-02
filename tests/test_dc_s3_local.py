"""Data Center: a streamed S3 link downloads only the folders picked (a read-only local copy). ls badges every entry
(fresh / outdated / new / gone / partial / stream only), a check against S3 counts new / changed / removed files,
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
    yield client_as(make_user('alice'))
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
    assert c.post(f'/api/dc/buckets/{bid}/ops', json={'ops': [{'op': 'mkdir', 'path': 'cats/x'}]}).status_code == 400

    # S3 changes: one replaced, one new, one deleted -> file badges at once, the check counts them
    client.put_object(Bucket=bucket, Key='cats/im0.jpg', Body=jpeg_bytes() + b'more')
    client.put_object(Bucket=bucket, Key='cats/im9.jpg', Body=jpeg_bytes())
    client.delete_object(Bucket=bucket, Key='cats/im2.jpg')
    from dataviz.datacenter import s3dir
    s3dir.invalidate(bid)
    cats = _ls(c, bid, 'cats')
    assert _states(cats) == {'im0.jpg': 'outdated', 'im1.jpg': 'fresh', 'im9.jpg': 'new', 'im2.jpg': 'gone'}
    j = wait_job(c, c.post(f'/api/dc/buckets/{bid}/local/check').json()['job_id'])
    assert j['result']['folders']['cats'] == {'state': 'outdated', 'new': 1, 'changed': 1, 'removed': 1}, j
    here = _ls(c, bid, 'cats')['here']
    assert here['state'] == 'outdated' and here['diff']['removed'] == 1
    assert c.get(f'/api/dc/buckets/{bid}').json()['local_outdated'] == 1

    # Update without mirror keeps the file S3 lost; with mirror it goes
    j = wait_job(c, c.post(f'/api/dc/buckets/{bid}/local', json={'paths': ['cats']}).json()['job_id'])
    assert (j['result']['downloaded'], j['result']['skipped'], j['result']['removed']) == (2, 1, 0), j
    assert _states(_ls(c, bid, 'cats'))['im2.jpg'] == 'gone'
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
