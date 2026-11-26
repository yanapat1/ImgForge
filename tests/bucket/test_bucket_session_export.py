"""From a Visualize session (static/ts/views: Export dataset… / Send to Label…): the session is matched back to its
bucket folder (GET /api/bucket/session-folder/{did}), its labels (with the fixes saved in Visualize, they are in
the files) are exported in another format into a folder of any bucket (default <folder>/exports/<name>-<fmt>,
images linked), and a Label project is made on the same folder (images in place, the labels imported)."""

import os

import pytest

from conftest import scanned, wait_job
from test_bucket_routes import dc, new_bucket, upload_zip  # noqa: F401 - the fixture

pytestmark = pytest.mark.usefixtures('tmp_roots')


def _viz(c, bid, path=''):
    r = c.post('/api/bucket/session', json={'bucket': bid, 'path': path, 'label': False, 'name': 'Traffic viz'})
    assert r.status_code == 200, r.text
    return r.json()['visualize']['id']


def test_export_and_send_to_label_from_a_session(dc):  # noqa: F811 - the fixture
    _, c = dc
    b = new_bucket(c, name='traffic')
    upload_zip(c, b['id'])
    did = _viz(c, b['id'])
    r = c.get(f'/api/bucket/session-folder/{did}')
    assert r.status_code == 200, r.text
    info = r.json()
    assert info['bucket'] == b['id'] and info['path'] == '' and info['format'] == 'yolo-detect' and info['can_export']
    assert {'yolo', 'coco', 'voc'} <= {f['key'] for f in info['formats']}
    assert info['dest'] == {'bucket': b['id'], 'path': 'exports', 'name': 'traffic'}

    # a label fixed in Visualize is a file change: the export carries it
    root = c.get(f'/api/bucket/buckets/{b["id"]}').json()['root']
    with open(os.path.join(root, 'labels/train/im0.txt'), 'w') as f:
        f.write('1 0.5 0.5 0.4 0.4\n')
    r = c.post(f'/api/bucket/session-folder/{did}/export', json={'format': 'coco'})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out['path'] == 'exports/traffic-coco' and out['bucket'] == b['id']
    j = wait_job(c, out['job_id'], timeout=60)
    assert j['status'] == 'done', j
    assert j['result']['images_linked'] == 3
    names = {e['name'] for e in c.get(f'/api/bucket/buckets/{b["id"]}/ls', params={'path': 'exports/traffic-coco'}).json()['entries']}
    assert names, 'the dataset is in the bucket'
    # into another bucket, a named folder; an existing non-empty folder is refused
    other = new_bucket(c, name='exports')
    r = c.post(f'/api/bucket/session-folder/{did}/export', json={'format': 'voc', 'to_bucket': other['id'], 'to_path': 'voc1'})
    assert r.status_code == 200, r.text
    assert wait_job(c, r.json()['job_id'], timeout=60)['status'] == 'done'
    r = c.post(f'/api/bucket/session-folder/{did}/export', json={'format': 'coco', 'to_bucket': other['id'], 'to_path': 'voc1'})
    assert r.status_code == 400 and 'not empty' in r.text

    # Send to Label: the same folder, its labels imported
    r = c.post(f'/api/bucket/session-folder/{did}/label', json={'name': 'Traffic label'})
    assert r.status_code == 200, r.text
    lab = r.json()
    assert lab['target'] == 'label' and lab['url'] == f'?label={lab["id"]}'
    for k in ('job_id', 'import_job_id'):
        assert wait_job(c, lab[k], timeout=60)['status'] == 'done', k
    proj = c.get(f'/api/label/projects/{lab["id"]}').json()
    src = proj.get('source') or proj.get('project', {}).get('source')
    assert src['kind'] == 'local' and os.path.realpath(src['path']) == os.path.realpath(root), 'the same folder, in place'


def test_session_outside_buckets(dc, tmp_path):  # noqa: F811 - the fixture
    _, c = dc
    r = c.get('/api/bucket/session-folder/nope')
    assert r.status_code == 404


pytest_plugins = ['conftest_io']


def test_export_a_streamed_session_into_s3(client_as, tmp_path, s3_server, s3_bucket):
    from conftest import jpeg_bytes, make_user
    cfg = client_as.ctx.cfg
    cfg['storage']['data_root'] = str(tmp_path / 'dcdata')
    cfg['dataset']['workspace_root'] = str(tmp_path / 'ws')
    c = client_as(make_user('alice'))
    client, bucket = s3_bucket
    for i in range(2):
        client.put_object(Bucket=bucket, Key=f'ds/images/train/im{i}.jpg', Body=jpeg_bytes())
        client.put_object(Bucket=bucket, Key=f'ds/labels/train/im{i}.txt', Body=b'0 0.5 0.5 0.2 0.2\n')
    client.put_object(Bucket=bucket, Key='ds/data.yaml', Body=b'names: [car]\n')
    conn = c.post('/api/connections', json={**{k: v for k, v in s3_server.items() if k != 'id'}, 'kind': 's3'}).json()
    b = c.post('/api/bucket/buckets', json={'name': 'cloud', 'kind': 's3', 'connection_id': conn['id'],
                                          's3_bucket': bucket, 'prefix': ''}).json()
    wait_job(c, b['scan_job_id'])
    scanned(c, b['id'], 'ds')
    did = _viz(c, b['id'], 'ds')
    info = c.get(f'/api/bucket/session-folder/{did}').json()
    assert info['path'] == 'ds' and info['streamed'] and info['dest']['path'] == 'ds/exports'
    r = c.post(f'/api/bucket/session-folder/{did}/export', json={'format': 'coco'})
    assert r.status_code == 200, r.text
    j = wait_job(c, r.json()['job_id'], timeout=60)
    assert j['status'] == 'done', j
    keys = {o['Key'] for o in client.list_objects_v2(Bucket=bucket, Prefix='ds/exports/ds-coco/')['Contents']}
    assert 'ds/exports/ds-coco/images/train/im0.jpg' in keys, 'images copied inside S3'
    assert any(k.endswith('.json') for k in keys), 'the COCO labels are in S3'
    assert j['result']['uploaded']['uri'].startswith(f's3://{bucket}/ds/exports/ds-coco')
    staging = tmp_path / 'dcdata' / '.export-staging'
    assert not staging.exists() or not any(staging.iterdir()), 'the staging folder is removed'
