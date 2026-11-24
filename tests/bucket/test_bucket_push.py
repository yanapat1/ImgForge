"""Upload to S3 (POST …/push, job bk-push) from the local copy of a streamed S3 link: an edited and a new file go up to
the same keys, an unchanged one is skipped, a file S3 changed since the download is a conflict (skipped, listed)
until it is sent again with overwrite; the uploaded files read as fresh again and lose their change marks; a path
outside the downloaded folders and a bucket not linked to S3 are refused."""

import time

import pytest

from conftest import jpeg_bytes, wait_job
from test_bucket_s3_local import _link, _ls, dc  # noqa: F401 - the fixtures

pytestmark = pytest.mark.usefixtures('tmp_roots')
pytest_plugins = ['conftest_io']


def _push(c, bid, **body):
    r = c.post(f'/api/bucket/buckets/{bid}/push', json=body)
    assert r.status_code == 200, r.text
    j = wait_job(c, r.json()['job_id'])
    assert j['status'] == 'done', j
    assert j['key'].startswith(f'bk-push:{bid}:')
    return j['result']


def test_push_edits_new_files_and_conflicts(dc, s3_server, s3_bucket):  # noqa: F811 - the fixtures
    c = dc
    bid, client, bucket = _link(c, s3_server, s3_bucket)
    j = wait_job(c, c.post(f'/api/bucket/buckets/{bid}/local', json={'paths': ['cats']}).json()['job_id'])
    assert j['status'] == 'done'
    time.sleep(1.1)                                     # S3 times are whole seconds: a later write is a later time
    # here: one edited (a text write), one new file; in S3: another one replaced by someone else
    r = c.put(f'/api/bucket/buckets/{bid}/text', json={'path': 'cats/notes.txt', 'text': 'hello\n', 'create': True})
    assert r.status_code == 200, r.text
    r = c.post(f'/api/bucket/buckets/{bid}/upload', params={'path': 'cats'},
               files=[('files', ('new.jpg', jpeg_bytes() + b'new', 'image/jpeg'))])
    assert r.status_code == 200, r.text
    client.put_object(Bucket=bucket, Key='cats/im0.jpg', Body=jpeg_bytes() + b'theirs')
    # and the local im0 edited too: both sides changed -> a conflict
    from dataviz.bucket import service
    row = service._row(c.user, bid)
    with open(f'{row.local_root}/cats/im0.jpg', 'ab') as f:
        f.write(b'mine')
    assert _ls(c, bid)['changes'] >= 2
    res = _push(c, bid, paths=['cats'])
    assert res['uploaded'] == 2 and res['unchanged'] == 2 and [x['path'] for x in res['conflicts']] == ['cats/im0.jpg']
    assert client.get_object(Bucket=bucket, Key='cats/notes.txt')['Body'].read() == b'hello\n'
    assert client.get_object(Bucket=bucket, Key='cats/new.jpg')['Body'].read().endswith(b'new')
    assert client.get_object(Bucket=bucket, Key='cats/im0.jpg')['Body'].read().endswith(b'theirs'), 'not overwritten'
    files = {e['name']: e.get('changed') for e in _ls(c, bid, 'cats')['entries']}
    assert files['notes.txt'] is None and files['new.jpg'] is None, 'uploaded: no mark'
    # again: nothing left but the conflict; with overwrite it goes
    res = _push(c, bid, paths=['cats'])
    assert res['uploaded'] == 0 and len(res['conflicts']) == 1
    res = _push(c, bid, paths=['cats/im0.jpg'], overwrite=['cats/im0.jpg'])
    assert res['uploaded'] == 1 and not res['conflicts']
    assert client.get_object(Bucket=bucket, Key='cats/im0.jpg')['Body'].read().endswith(b'mine')
    # outside the downloaded folders: refused
    r = c.post(f'/api/bucket/buckets/{bid}/push', json={'paths': ['dogs']})
    assert r.status_code == 400 and 'download' in r.text.lower()


def test_push_needs_an_s3_link(dc):  # noqa: F811 - the fixture
    c = dc
    b = c.post('/api/bucket/buckets', json={'name': 'plain'}).json()
    r = c.post(f'/api/bucket/buckets/{b["id"]}/push', json={'paths': ['']})
    assert r.status_code == 400 and 'Export to' in r.text
