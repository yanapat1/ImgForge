"""Copy / Paste between buckets (POST /api/bucket/copy, job bk-copy): a folder and a file of a managed bucket into
another bucket (bytes progress, a clashing name becomes "(copy)", undo removes the copies), into itself refused,
a read-only (streamed) destination refused, and a streamed S3 folder copied into a managed bucket."""

import pytest

from conftest import wait_job
from test_bucket_routes import dc, new_bucket, upload_zip  # noqa: F401 - the fixture

pytestmark = pytest.mark.usefixtures('tmp_roots')
pytest_plugins = ['conftest_io']


def _copy(c, **body):
    r = c.post('/api/bucket/copy', json=body)
    assert r.status_code == 200, r.text
    j = r.json()
    if j['job_id'] is None:
        return j['result']
    job = wait_job(c, j['job_id'])
    assert job['status'] == 'done', job
    assert job['key'].startswith('bk-copy:')
    return job['result']


def _names(c, bid, path=''):
    return {e['name'] for e in c.get(f'/api/bucket/buckets/{bid}/ls', params={'path': path}).json()['entries']}


def test_copy_between_buckets_and_undo(dc):  # noqa: F811 - the fixture
    _, c = dc
    a, b = new_bucket(c, 'src')['id'], new_bucket(c, 'dst')['id']
    upload_zip(c, a)
    c.post(f'/api/bucket/buckets/{b}/ops', json={'ops': [{'op': 'mkdir', 'path': 'in'}, {'op': 'mkdir', 'path': 'in/images'}]})
    res = _copy(c, from_bucket=a, paths=['images', 'data.yaml'], to_bucket=b, to_path='in')
    assert res['files'] == 4 and res['bytes'] > 0 and not res['cancelled']
    assert res['to'] == ['in/images (copy)', 'in/data.yaml'], 'a clashing name gets (copy)'
    assert _names(c, b, 'in/images (copy)/train') == {'im0.jpg', 'im1.jpg', 'im2.jpg'}
    assert c.get(f'/api/bucket/buckets/{b}/text', params={'path': 'in/data.yaml'}).json()['text'] == 'names: [cat, dog]\n'
    assert _names(c, a, 'images/train') == {'im0.jpg', 'im1.jpg', 'im2.jpg'}, 'the source stays'
    u = c.post(f'/api/bucket/buckets/{b}/undo', json={'undo_id': res['undo_id']}).json()
    assert u['restored'] == 2 and _names(c, b, 'in') == {'images'}
    # into itself: refused; a missing destination folder: 404
    r = c.post('/api/bucket/copy', json={'from_bucket': a, 'paths': ['images'], 'to_bucket': a, 'to_path': 'images/train'})
    assert r.status_code == 400 and 'into itself' in r.text
    r = c.post('/api/bucket/copy', json={'from_bucket': a, 'paths': ['images'], 'to_bucket': b, 'to_path': 'nope'})
    assert r.status_code == 404
    # the same bucket, another folder: fine
    res = _copy(c, from_bucket=a, paths=['labels'], to_bucket=a, to_path='images')
    assert res['to'] == ['images/labels'] and _names(c, a, 'images/labels/train') == {'im0.txt', 'im1.txt', 'im2.txt'}


def test_copy_from_streamed_s3(dc, s3_server, s3_bucket):  # noqa: F811 - the fixture
    from conftest import jpeg_bytes
    _, c = dc
    client, bucket = s3_bucket
    for i in range(3):
        client.put_object(Bucket=bucket, Key=f'cats/im{i}.jpg', Body=jpeg_bytes())
    conn = c.post('/api/connections', json={**{k: v for k, v in s3_server.items() if k != 'id'}, 'kind': 's3'}).json()
    s3 = c.post('/api/bucket/buckets', json={'name': 'zoo', 'kind': 's3', 'connection_id': conn['id'],
                                           's3_bucket': bucket, 'prefix': ''}).json()
    wait_job(c, s3['scan_job_id'])
    dst = new_bucket(c, 'mine')['id']
    res = _copy(c, from_bucket=s3['id'], paths=['cats'], to_bucket=dst, to_path='')
    assert res['files'] == 3 and _names(c, dst, 'cats') == {'im0.jpg', 'im1.jpg', 'im2.jpg'}
    # a streamed link is not a destination (outside a downloaded folder)
    r = c.post('/api/bucket/copy', json={'from_bucket': dst, 'paths': ['cats'], 'to_bucket': s3['id'], 'to_path': ''})
    assert r.status_code == 400 and 'read-only' in r.text
