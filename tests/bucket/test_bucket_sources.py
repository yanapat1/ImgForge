"""Bucket "From the web" (bucket/sources): Kaggle (Basic and Bearer auth, the redirect to storage without
the key, the competition-rules 403, a missing token), a direct link (tar.gz with a nested zip, ranged parallel
download, resume after a dropped connection, the SSRF guard), Hugging Face (tree + per-file download), Roboflow
(export polling), Zenodo (md5 check), OpenML (parquet + description.json), import into an existing folder,
Update from source, the token connections and their Test. No real network: every source talks to an
httpx.MockTransport, DNS is patched."""

import base64
import gzip
import hashlib
import io
import json
import os
import tarfile
import zipfile

import httpx
import pytest

from conftest import jpeg_bytes, wait_job
from test_bucket_routes import dc, new_bucket  # noqa: F401 - the Bucket page fixture

from dataviz.bucket import store
from dataviz.bucket.sources import fetch, huggingface

pytestmark = pytest.mark.usefixtures('tmp_roots')


def yolo_zip(wrap='ds/'):
    b = io.BytesIO()
    with zipfile.ZipFile(b, 'w') as z:
        for i in range(2):
            z.writestr(f'{wrap}images/train/im{i}.jpg', jpeg_bytes())
            z.writestr(f'{wrap}labels/train/im{i}.txt', '0 0.5 0.5 0.2 0.2\n')
        z.writestr(f'{wrap}data.yaml', 'names: [cat]\n')
    return b.getvalue()


class Web:
    """A fake internet: handlers by host + path; every request is recorded."""

    def __init__(self):
        self.routes = {}
        self.seen = []

    def on(self, host, path, fn):
        self.routes[(host, path)] = fn

    def __call__(self, request):
        self.seen.append(request)
        fn = self.routes.get((request.url.host, request.url.path))
        if fn is None:
            return httpx.Response(404, json={'message': f'no route {request.url}'})
        return fn(request)


@pytest.fixture
def web(monkeypatch):
    w = Web()
    monkeypatch.setattr(fetch, 'make_client', lambda **kw: httpx.Client(transport=httpx.MockTransport(w),
                                                                          follow_redirects=False))
    monkeypatch.setattr(fetch, 'resolve_host', lambda host: ['93.184.216.34'])
    monkeypatch.setattr(fetch, 'RETRY_SLEEP', 0)
    return w


def token(c, kind, **kw):
    r = c.post('/api/connections', json={'kind': kind, 'name': kind, **kw})
    assert r.status_code == 200, r.text
    return r.json()['id']


def run(c, body, ok=True):
    r = c.post('/api/bucket/import', json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    j = wait_job(c, out['job_id'], timeout=60) if out['job_id'] is not None else {'status': 'done', 'result': out['result']}
    if ok:
        assert j['status'] == 'done', j
    return out, j


def files_of(root):
    return sorted(os.path.relpath(os.path.join(d, f), root) for d, _, fs in os.walk(root) for f in fs
                  if '.trash' not in d and not f.startswith('.'))


# ---------------------------------------------------------------- Kaggle
def test_kaggle_dataset_basic_auth_redirect_and_unzip(dc, web):
    _, c = dc
    token(c, 'kaggle', username='alice', api_key='k123')
    auth = {}

    def dl(req):
        auth['kaggle'] = req.headers.get('authorization')
        assert req.url.params.get('datasetVersionNumber') == '2'
        return httpx.Response(302, headers={'location': 'https://storage.example.com/signed/ds.zip?sig=1'})

    def storage(req):
        auth['storage'] = req.headers.get('authorization')
        return httpx.Response(200, content=yolo_zip(), headers={'content-type': 'application/zip'})
    web.on('www.kaggle.com', '/api/v1/datasets/download/owner/my-ds', dl)
    web.on('storage.example.com', '/signed/ds.zip', storage)
    out, j = run(c, {'source': 'kaggle', 'ref': 'https://www.kaggle.com/datasets/owner/my-ds/versions/2'})
    assert auth['kaggle'] == 'Basic ' + base64.b64encode(b'alice:k123').decode()
    assert auth['storage'] is None                                  # the key never leaves kaggle.com
    b = c.get(f'/api/bucket/buckets/{out["bucket"]}').json()
    assert b['name'] == 'my-ds' and b['format']['format'] == 'yolo-detect'
    assert b['source']['kind'] == 'kaggle' and b['source']['ref'] == 'owner/my-ds/versions/2' and b['source']['imported_at']
    assert files_of(b['root']) == ['data.yaml', 'images/train/im0.jpg', 'images/train/im1.jpg',
                                   'labels/train/im0.txt', 'labels/train/im1.txt']    # wrapper ds/ stripped, zip gone
    assert j['result']['archives'] == 1 and j['result']['added'] == 5


def test_kaggle_errors_and_bearer_token(dc, web):
    _, c = dc
    r = c.post('/api/bucket/import', json={'source': 'kaggle', 'ref': 'owner/ds'})
    assert r.status_code == 400 and 'token' in r.json()['detail']
    assert c.post('/api/bucket/import', json={'source': 'kaggle', 'ref': 'not a ref!'}).status_code == 400
    assert c.post('/api/bucket/import', json={'source': 'nope', 'ref': 'x'}).status_code == 400
    cid = token(c, 'kaggle', api_key='KGAT_abc')
    seen = {}
    web.on('www.kaggle.com', '/api/v1/competitions/data/download-all/titanic',
           lambda req: seen.update(a=req.headers.get('authorization')) or httpx.Response(403, json={'message': 'rules'}))
    n = len(c.get('/api/bucket/buckets').json())
    _, j = run(c, {'source': 'kaggle', 'ref': 'competitions/titanic', 'connection_id': cid}, ok=False)
    assert seen['a'] == 'Bearer KGAT_abc'
    assert j['status'] == 'error' and 'accept the competition rules' in j['error']
    assert len(c.get('/api/bucket/buckets').json()) == n                 # the new bucket went with the failure


def test_kaggle_inspect(dc, web):
    _, c = dc
    token(c, 'kaggle', username='alice', api_key='k')
    web.on('www.kaggle.com', '/api/v1/datasets/view/owner/ds', lambda r: httpx.Response(200, json={
        'title': 'My data', 'totalBytes': 1234, 'licenseName': 'CC0', 'currentVersionNumber': 3,
        'versions': [{'versionNumber': 3}, {'versionNumber': 2}]}))
    web.on('www.kaggle.com', '/api/v1/datasets/list/owner/ds',
           lambda r: httpx.Response(200, json={'datasetFiles': [{'name': 'a.csv'}, {'name': 'b.csv'}]}))
    r = c.post('/api/bucket/sources/inspect', json={'source': 'kaggle', 'ref': 'owner/ds'})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d['title'] == 'My data' and d['size'] == 1234 and d['license'] == 'CC0' and d['files'] == 2
    assert d['name'] == 'ds' and [v['version'] for v in d['versions']] == [3, 2]
    src = c.get('/api/bucket/sources').json()['sources']
    kg = next(s for s in src if s['key'] == 'kaggle')
    assert kg['token'] == 'required' and len(kg['connections']) == 1
    assert {s['key'] for s in src} == {'kaggle', 'huggingface', 'roboflow', 'url', 'zenodo', 'openml'}


# ---------------------------------------------------------------- direct link
def test_url_tarball_with_nested_zip_and_ssrf(dc, web):
    _, c = dc
    tb = io.BytesIO()
    with tarfile.open(fileobj=tb, mode='w:gz') as t:
        for name, data in (('pack/readme.md', b'# hi'), ('pack/train.zip', yolo_zip('train/'))):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
        ln = tarfile.TarInfo('pack/link')
        ln.type, ln.linkname = tarfile.SYMTYPE, '/etc/passwd'
        t.addfile(ln)
    web.on('files.example.com', '/d/pack.tar.gz',
           lambda r: httpx.Response(200, content=tb.getvalue(), headers={'content-disposition': 'attachment; filename="pack.tar.gz"'}))
    out, j = run(c, {'source': 'url', 'ref': 'https://files.example.com/d/pack.tar.gz', 'opts': {'nested': True}})
    root = c.get(f'/api/bucket/buckets/{out["bucket"]}').json()['root']
    got = files_of(root)
    assert 'readme.md' in got and 'train/images/train/im0.jpg' in got and 'train/data.yaml' in got
    assert 'link' not in got and not any(g.endswith('.zip') for g in got)
    assert j['result']['added'] >= 6
    # private targets are refused, before and after DNS
    for bad in ('http://127.0.0.1/x.zip', 'http://10.0.0.5/x.zip', 'http://[::1]/x', 'file:///etc/passwd'):
        r = c.post('/api/bucket/import', json={'source': 'url', 'ref': bad})
        assert r.status_code == 400, bad
    fetch.resolve_host  # the fixture's fake DNS says public; a host that resolves private:
    import dataviz.bucket.sources.fetch as f
    real = f.resolve_host
    f.resolve_host = lambda host: ['192.168.1.9']
    try:
        r = c.post('/api/bucket/import', json={'source': 'url', 'ref': 'https://intranet.example.com/a.zip'})
        assert r.status_code == 400 and 'private' in r.json()['detail']
    finally:
        f.resolve_host = real


def test_url_parallel_ranges_and_resume(dc, web, monkeypatch, tmp_path):
    _, c = dc
    data = os.urandom(3 * 1024 * 1024 + 17)
    monkeypatch.setattr(fetch, 'PARALLEL_MIN', 1024 * 1024)
    ranges = []

    def big(req):
        h = {'accept-ranges': 'bytes', 'content-type': 'application/octet-stream'}
        rg = req.headers.get('range')
        if not rg:
            return httpx.Response(200, content=data, headers={**h, 'content-length': str(len(data))})
        a, b = rg.removeprefix('bytes=').split('-')
        a, b = int(a), int(b) if b else len(data) - 1
        ranges.append((a, b))
        return httpx.Response(206, content=data[a:b + 1], headers={**h, 'content-range': f'bytes {a}-{b}/{len(data)}'})
    web.on('files.example.com', '/blob.bin', big)
    out, _ = run(c, {'source': 'url', 'ref': 'https://files.example.com/blob.bin'})
    root = c.get(f'/api/bucket/buckets/{out["bucket"]}').json()['root']
    assert open(os.path.join(root, 'blob.bin'), 'rb').read() == data
    assert len(ranges) == fetch.PARTS                                # split over parallel ranged requests

    # a connection that drops half way is resumed with Range
    calls = []

    class Drop(httpx.SyncByteStream):
        def __iter__(self):
            yield data[:1000]
            raise httpx.ReadError('connection reset')

    def flaky(req):
        calls.append(req.headers.get('range'))
        h = {'accept-ranges': 'bytes'}
        if len(calls) == 1:
            return httpx.Response(200, stream=Drop(), headers={**h, 'content-length': str(len(data))})
        a = int(req.headers['range'].removeprefix('bytes=').split('-')[0])
        return httpx.Response(206, content=data[a:], headers={**h, 'content-range': f'bytes {a}-{len(data) - 1}/{len(data)}'})
    monkeypatch.setattr(fetch, 'PARALLEL_MIN', 1 << 40)
    web.on('files.example.com', '/flaky.bin', flaky)
    got = fetch.download('https://files.example.com/flaky.bin', str(tmp_path / 'dl'))
    assert open(got['path'], 'rb').read() == data and calls[1] == 'bytes=1000-'


# ---------------------------------------------------------------- Hugging Face, Roboflow, Zenodo, OpenML
def test_huggingface_tree_and_files(dc, web, monkeypatch):
    _, c = dc

    class F:
        def __init__(self, path, size):
            self.path, self.size = path, size

    class Api:
        def list_repo_tree(self, repo, **kw):
            assert repo == 'user/set' and kw['revision'] == 'v2'
            return [F('data/train.csv', 6), F('data/test.csv', 4), F('README.md', 3), F('.gitattributes', 1)]

        def dataset_info(self, repo, revision=None):
            return type('I', (), {'sha': 'abc', 'card_data': {'license': 'mit'}, 'tags': []})()

    def download(repo_id, filename, local_dir, **kw):
        p = os.path.join(local_dir, filename)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, 'w').write('x' * 3)
        return p
    monkeypatch.setattr(huggingface, 'hf_api', lambda token=None: Api())
    monkeypatch.setattr(huggingface, 'hf_download', download)
    d = c.post('/api/bucket/sources/inspect', json={'source': 'huggingface', 'ref': 'https://huggingface.co/datasets/user/set/tree/v2'}).json()
    assert d['files'] == 3 and d['license'] == 'mit' and d['size'] == 13
    out, _ = run(c, {'source': 'huggingface', 'ref': 'user/set@v2', 'opts': {'patterns': 'data/*.csv'}})
    root = c.get(f'/api/bucket/buckets/{out["bucket"]}').json()['root']
    assert files_of(root) == ['data/test.csv', 'data/train.csv']


def test_roboflow_polls_until_the_export_exists(dc, web, monkeypatch):
    _, c = dc
    from dataviz.bucket.sources import roboflow
    monkeypatch.setattr(roboflow, 'POLL', 0)
    token(c, 'roboflow', api_key='rf')
    n = {'k': 0}

    def export(req):
        assert req.url.params['api_key'] == 'rf'
        n['k'] += 1
        if n['k'] < 3:
            return httpx.Response(200, json={'progress': n['k'] / 3})
        return httpx.Response(200, json={'export': {'link': 'https://storage.roboflow.example/e.zip'}})
    web.on('api.roboflow.com', '/ws/cars/4/yolov8', export)
    web.on('storage.roboflow.example', '/e.zip', lambda r: httpx.Response(200, content=yolo_zip('cars-4/')))
    out, _ = run(c, {'source': 'roboflow', 'ref': 'https://universe.roboflow.com/ws/cars/dataset/4'})
    assert n['k'] == 3
    b = c.get(f'/api/bucket/buckets/{out["bucket"]}').json()
    assert b['format']['format'] == 'yolo-detect' and b['name'] == 'cars-v4'


def test_zenodo_md5_and_openml(dc, web):
    _, c = dc
    good = yolo_zip('z/')
    rec = {'metadata': {'title': 'Z', 'license': {'id': 'cc-by-4.0'}},
           'files': [{'key': 'z.zip', 'size': len(good), 'checksum': 'md5:' + hashlib.md5(good).hexdigest(),
                      'links': {'self': 'https://zenodo.org/api/records/42/files/z.zip/content'}}]}
    web.on('zenodo.org', '/api/records/42', lambda r: httpx.Response(200, json=rec))
    web.on('zenodo.org', '/api/records/42/files/z.zip/content', lambda r: httpx.Response(200, content=good))
    out, _ = run(c, {'source': 'zenodo', 'ref': 'https://doi.org/10.5281/zenodo.42'})
    assert c.get(f'/api/bucket/buckets/{out["bucket"]}').json()['format']['format'] == 'yolo-detect'
    rec['files'][0]['checksum'] = 'md5:' + '0' * 32
    _, j = run(c, {'source': 'zenodo', 'ref': '42', 'name': 'z-bad'}, ok=False)
    assert j['status'] == 'error' and 'damaged' in j['error']

    web.on('www.openml.org', '/api/v1/json/data/61', lambda r: httpx.Response(200, json={'data_set_description': {
        'id': '61', 'name': 'iris', 'version': '1', 'licence': 'Public', 'default_target_attribute': 'class',
        'parquet_url': 'https://data.openml.example/iris.pq', 'url': 'https://www.openml.org/data/v1/download/61'}}))
    web.on('data.openml.example', '/iris.pq', lambda r: httpx.Response(200, content=b'PAR1fakePAR1'))
    out, _ = run(c, {'source': 'openml', 'ref': 'https://www.openml.org/d/61'})
    root = c.get(f'/api/bucket/buckets/{out["bucket"]}').json()['root']
    assert files_of(root) == ['description.json', 'iris.parquet']
    assert json.load(open(os.path.join(root, 'description.json')))['default_target_attribute'] == 'class'


# ---------------------------------------------------------------- into a folder, Update from source, tokens
def test_import_into_a_folder_and_reimport(dc, web):
    _, c = dc
    b = new_bucket(c, name='mine')
    v = {'n': 1}
    web.on('files.example.com', '/notes.txt', lambda r: httpx.Response(200, content=f'v{v["n"]}'.encode()))
    out, _ = run(c, {'source': 'url', 'ref': 'https://files.example.com/notes.txt', 'bucket': b['id'], 'path': 'docs'})
    assert out['bucket'] == b['id']
    p = os.path.join(b['root'], 'docs', 'notes.txt')
    assert open(p).read() == 'v1'
    v['n'] = 2
    r = c.post(f'/api/bucket/buckets/{b["id"]}/reimport')
    assert r.status_code == 200, r.text
    assert wait_job(c, r.json()['job_id'])['status'] == 'done'
    assert open(p).read() == 'v2'                                     # newer file replaces the older one
    other = new_bucket(c, name='plain')
    assert c.post(f'/api/bucket/buckets/{other["id"]}/reimport').status_code == 400


def test_token_connections_are_masked_and_tested(dc, web):
    _, c = dc
    assert c.post('/api/connections', json={'kind': 'kaggle', 'name': 'k'}).status_code == 400   # no key
    cid = token(c, 'kaggle', username='alice', api_key='secret-key-123')
    pub = next(x for x in c.get('/api/connections').json()['connections'] if x['id'] == cid)
    assert pub['kind'] == 'kaggle' and pub['has_secret'] and 'secret-key-123' not in json.dumps(pub)
    web.on('www.kaggle.com', '/api/v1/datasets/list', lambda r: httpx.Response(200, json=[]))
    r = c.post('/api/connections/test', json={'id': cid, 'kind': 'kaggle'})
    assert r.status_code == 200 and r.json()['ok'], r.text
    web.on('www.kaggle.com', '/api/v1/datasets/list', lambda r: httpx.Response(401, json={'message': 'bad'}))
    r = c.post('/api/connections/test', json={'id': cid, 'kind': 'kaggle'})
    assert r.status_code == 400 and 'refused the token' in r.json()['detail']


# ---------------------------------------------------------------- extraction (store.extract_into)
def test_extract_into_is_safe_and_cancellable(tmp_path):
    root = tmp_path / 'b'
    root.mkdir()
    allowed = [tmp_path.resolve()]
    z = tmp_path / 'evil.zip'
    with zipfile.ZipFile(z, 'w') as zz:
        zz.writestr('../out.txt', 'x')
        zz.writestr('/abs.txt', 'x')
        zz.writestr('.hidden/a.txt', 'x')
        zz.writestr('__MACOSX/a', 'x')
        zz.writestr('ok/a.txt', 'a')
        zz.writestr('ok/b.txt', 'b')
    r = store.extract_into(str(z), str(root), str(root), allowed)
    assert r['added'] == 2 and sorted(os.listdir(root)) == ['a.txt', 'b.txt']      # wrapper ok/ stripped
    assert not (tmp_path / 'out.txt').exists()
    # into a folder that is not empty nothing is stripped; the default skips files already there, overwrite replaces
    (root / 'ok').mkdir()
    (root / 'ok' / 'a.txt').write_text('old')
    r = store.extract_into(str(z), str(root), str(root), allowed)
    assert r['skipped'] == 1 and r['added'] == 1 and (root / 'ok' / 'a.txt').read_text() == 'old'
    r = store.extract_into(str(z), str(root), str(root), allowed, overwrite=True)
    assert r['replaced'] == 2 and (root / 'ok' / 'a.txt').read_text() == 'a'
    import shutil
    shutil.rmtree(root / 'ok')
    for n in ('a.txt', 'b.txt'):
        (root / n).unlink()
    # tar: devices and links are skipped
    t = tmp_path / 'x.tar'
    with tarfile.open(t, 'w') as tf:
        ti = tarfile.TarInfo('dev')
        ti.type = tarfile.CHRTYPE
        tf.addfile(ti)
        ti = tarfile.TarInfo('f.txt')
        ti.size = 2
        tf.addfile(ti, io.BytesIO(b'hi'))
        ti = tarfile.TarInfo('../up.txt')
        ti.size = 2
        tf.addfile(ti, io.BytesIO(b'no'))
    assert store.extract_into(str(t), str(root), str(root), allowed)['added'] == 1
    assert sorted(os.listdir(root)) == ['f.txt']
    # a single .gz
    g = tmp_path / 'data.csv.gz'
    g.write_bytes(gzip.compress(b'a,b\n'))
    store.extract_into(str(g), str(root), str(root), allowed)
    assert (root / 'data.csv').read_text() == 'a,b\n'
    # cancel
    big = tmp_path / 'many.zip'
    with zipfile.ZipFile(big, 'w') as zz:
        for i in range(600):
            zz.writestr(f'm/{i}.txt', 'x')
    dest = root / 'many'
    dest.mkdir()
    with pytest.raises(store.Cancelled):
        store.extract_into(str(big), str(dest), str(root), allowed, cancelled=lambda: True)
    with pytest.raises(store.BucketError, match='not an archive'):
        store.extract_into(str(root / 'f.txt'), str(root), str(root), allowed)


def test_upload_unpacks_a_tarball(dc):
    _, c = dc
    b = new_bucket(c, name='tarred')
    tb = io.BytesIO()
    with tarfile.open(fileobj=tb, mode='w:gz') as t:
        for name in ('images/a.jpg', 'images/b.jpg'):
            data = jpeg_bytes()
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    r = c.post(f'/api/bucket/buckets/{b["id"]}/upload', params={'path': ''},
               files=[('files', ('imgs.tar.gz', tb.getvalue(), 'application/gzip')),
                      ('files', ('keep.csv.gz', gzip.compress(b'x'), 'application/gzip'))])
    assert r.status_code == 200, r.text
    assert files_of(b['root']) == ['images/a.jpg', 'images/b.jpg', 'keep.csv.gz']
