"""
Image sources (upload / local / S3 / FTP / SFTP), the image cache, probe, export sinks (zip / local / S3 /
FTP / SFTP) and the connection kinds. Every remote kind imports 50 images and exports the 2-task layout:

    images/<file>, detect/labels/<stem>.txt, detect/classes.txt, detect/data.yaml, segment/..., manifest.json
"""

import json
import posixpath
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from conftest_io import N_IMAGES, jpeg_bytes, make_image_tree
from dataviz.data import connections, sources
from dataviz.labeling import media, sinks

pytest_plugins = ['conftest_io']             # fixtures: image_tree, s3_*, ftp_server, sftp_server


# ---------------------------------------------------------------- helpers

def upload_tree(local_root, put):
    """Copy a local image tree to a remote through put(rel, bytes)."""
    for p in sorted(Path(local_root).rglob('*')):
        if p.is_file():
            put(p.relative_to(local_root).as_posix(), p.read_bytes())


def check_source(src, names, cache_dir):
    lst = src.list_images()
    assert [r for r, _ in lst] == names                       # sorted, recursive, hidden + non-images skipped
    assert all(size > 0 for _, size in lst)
    info = {r: media.probe(src, r) for r in names}
    assert (info['img_001.jpg']['width'], info['img_001.jpg']['height']) == (40, 80)    # EXIF 6 swaps w / h
    assert info['img_001.jpg']['orientation'] == 6
    assert (info['img_003.jpg']['width'], info['img_003.jpg']['height']) == (64, 48)
    assert info['img_002.png']['mime'] == 'image/png'
    assert info['img_003.jpg']['mime'] == 'image/jpeg'
    assert info['img_003.jpg']['size_bytes'] == dict(lst)['img_003.jpg']
    head = src.read_head('img_003.jpg', 100)
    assert len(head) == 100 and head[:2] == b'\xff\xd8'
    cache = media.ImageCache(cache_dir, src)
    p = cache.path('sub/img_005.jpg')
    assert p.read_bytes() == src.read_bytes('sub/img_005.jpg')
    cache.prefetch(names[:5])
    th = cache.thumb('m1', 'img_001.jpg')
    with Image.open(th) as im:
        assert im.format == 'JPEG' and im.size == (40, 80)       # EXIF-oriented, never upscaled
    return cache


def export_two_tasks(cache, names, sink):
    sink.check_target(overwrite=False)
    for rel in names:
        sink.copy_file(f'images/{posixpath.basename(rel)}', cache.path(rel))
    for task in ('detect', 'segment'):
        for rel in names:
            stem = posixpath.splitext(posixpath.basename(rel))[0]
            sink.write(f'{task}/labels/{stem}.txt', '0 0.5 0.5 0.2 0.2\n' if task == 'detect' else '1 0.1 0.1 0.2 0.1 0.2 0.3\n')
        sink.write(f'{task}/classes.txt', 'แมว\ndog\n')
        sink.write(f'{task}/data.yaml', 'names:\n  0: แมว\n  1: dog\n')
    sink.write('manifest.json', json.dumps({'tasks': ['detect', 'segment']}))
    return sink.close()


def expected_tree(names):
    out = {f'images/{posixpath.basename(r)}' for r in names}
    for task in ('detect', 'segment'):
        out |= {f'{task}/labels/{posixpath.splitext(posixpath.basename(r))[0]}.txt' for r in names}
        out |= {f'{task}/classes.txt', f'{task}/data.yaml'}
    return out | {'manifest.json'}


def local_tree(root):
    root = Path(root)
    return {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}


# ---------------------------------------------------------------- local kinds

def test_local_source_probe_cache_thumb(image_tree, tmp_path):
    root, names = image_tree
    src = media.LocalImageSource(root)
    assert src.local_path('img_003.jpg') == root / 'img_003.jpg'
    cache = check_source(src, names, tmp_path / 'proj')
    assert cache.path('img_003.jpg') == root / 'img_003.jpg'        # local: served in place, never copied
    assert not (tmp_path / 'proj' / 'cache').exists()
    flat = media.LocalImageSource(root, recursive=False)
    assert [r for r, _ in flat.list_images()] == [n for n in names if '/' not in n]
    with pytest.raises(ValueError):
        src.local_path('../etc/passwd')


def test_upload_source_via_make_source(tmp_path):
    proj = tmp_path / 'p1'
    src = media.make_source({'kind': 'upload'}, proj, None, {})
    assert isinstance(src, media.UploadSource) and (proj / 'images').is_dir()
    (proj / 'images' / 'dropped').mkdir()
    (proj / 'images' / 'dropped' / 'x.jpg').write_bytes(jpeg_bytes())
    assert src.list_images()[0][0] == 'dropped/x.jpg'
    loc = media.make_source({'kind': 'local', 'path': str(proj / 'images'), 'recursive': False}, proj, None, {})
    assert loc.list_images() == []
    with pytest.raises(ValueError):
        media.make_source({'kind': 'nope'}, proj, None, {})


def test_preview_and_huge_draft(tmp_path):
    big = tmp_path / 'big'
    big.mkdir()
    Image.new('RGB', (3000, 1500), (10, 20, 30)).save(big / 'b.jpg', quality=80)
    src = media.LocalImageSource(big)
    cache = media.ImageCache(tmp_path / 'proj', src)
    pv = cache.preview('b.jpg', 1024)
    with Image.open(pv) as im:
        assert max(im.size) == 1024
    assert cache.preview('b.jpg', 4000) == big / 'b.jpg'               # already small enough
    assert media.scan_folder_images(big) == 1


def test_zip_sink(image_tree, tmp_path):
    root, names = image_tree
    cache = media.ImageCache(tmp_path / 'proj', media.LocalImageSource(root))
    sink = sinks.make_sink({'kind': 'zip'}, None, {}, tmp_path / 'exports' / 'run1')
    res = export_two_tasks(cache, names, sink)
    assert res['path'].endswith('run1/run1.zip') and res['files'] == len(expected_tree(names))
    with zipfile.ZipFile(res['path']) as zf:
        assert set(zf.namelist()) == expected_tree(names)
        assert zf.getinfo('images/img_003.jpg').compress_type == zipfile.ZIP_STORED
        assert zf.getinfo('detect/classes.txt').compress_type == zipfile.ZIP_DEFLATED
        assert zf.read('detect/classes.txt').decode() == 'แมว\ndog\n'
        assert zf.read('images/img_005.jpg') == (root / 'sub' / 'img_005.jpg').read_bytes()
    assert not list((tmp_path / 'exports' / 'run1').glob('*.part'))


def test_local_dir_sink(image_tree, tmp_path):
    root, names = image_tree
    cache = media.ImageCache(tmp_path / 'proj', media.LocalImageSource(root))
    out = tmp_path / 'out'
    res = export_two_tasks(cache, names, sinks.make_sink({'kind': 'local', 'path': str(out)}, None, {}, tmp_path))
    assert local_tree(out) == expected_tree(names) and res['files'] == len(expected_tree(names))
    with pytest.raises(ValueError, match='not empty'):
        sinks.LocalDirSink(out).check_target(overwrite=False)
    sinks.LocalDirSink(out).check_target(overwrite=True)
    assert not [p for p in out.rglob('*.part')]


# ---------------------------------------------------------------- S3

def test_s3_import_and_export(s3_server, s3_bucket, tmp_path):
    client, bucket = s3_bucket
    root = tmp_path / 'imgs'
    names = make_image_tree(root)
    upload_tree(root, lambda rel, data: client.put_object(Bucket=bucket, Key=f'data/set1/{rel}', Body=data))
    reg = SimpleNamespace(connection=lambda cid: s3_server)
    src = media.make_source({'kind': 's3', 'connection_id': 's3test', 'bucket': bucket, 'prefix': '/data/set1/'},
                            tmp_path / 'proj', reg, {})
    assert src.uri == f's3://{bucket}/data/set1'
    cache = check_source(src, names, tmp_path / 'proj')
    assert len(list((tmp_path / 'proj' / 'cache').glob('*.jpg'))) >= 1      # remote bytes cached
    sink = sinks.make_sink({'kind': 's3', 'connection_id': 's3test', 'bucket': bucket, 'prefix': 'out/run'}, reg, {}, tmp_path)
    res = export_two_tasks(cache, names, sink)
    keys = sources.list_objects(client, bucket, 'out/run/')
    assert set(keys) == expected_tree(names) and res['files'] == len(keys)
    assert client.get_object(Bucket=bucket, Key='out/run/detect/classes.txt')['Body'].read().decode() == 'แมว\ndog\n'
    with pytest.raises(ValueError, match='not empty'):
        sinks.make_sink({'kind': 's3', 'connection_id': 's3test', 'bucket': bucket, 'prefix': 'out/run'},
                        reg, {}, tmp_path).check_target(False)


def test_s3_sink_reports_errors(s3_server, tmp_path):
    sink = sinks.S3Sink(s3_server, 'no-such-bucket-xyz', 'p')
    sink.write('a.txt', b'x')
    with pytest.raises(Exception):
        sink.close()


# ---------------------------------------------------------------- FTP

def test_ftp_import_and_export(ftp_server, tmp_path):
    srv = ftp_server
    names = make_image_tree(srv['root'] / 'set1')
    reg = SimpleNamespace(connection=lambda cid: srv['conn'])
    src = media.make_source({'kind': 'ftp', 'connection_id': 'ftptest', 'path': '/set1'}, tmp_path / 'proj', reg, {})
    try:
        cache = check_source(src, names, tmp_path / 'proj')
        # NLST + SIZE fallback for servers without MLSD gives the same listing
        src._state['mlsd'] = False
        assert [r for r, _ in src.list_images()] == names
        assert src.pool._n <= 4
    finally:
        src.close()
    sink = sinks.make_sink({'kind': 'ftp', 'connection_id': 'ftptest', 'path': '/export/a'}, reg, {}, tmp_path)
    export_two_tasks(cache, names, sink)
    assert local_tree(srv['root'] / 'export' / 'a') == expected_tree(names)
    assert (srv['root'] / 'export/a/detect/classes.txt').read_text() == 'แมว\ndog\n'
    with pytest.raises(ValueError, match='not empty'):
        sinks.FTPSink(srv['conn'], '/export/a').check_target(False)
    sinks.FTPSink(srv['conn'], '/export/new').check_target(False)          # missing folder is fine


def test_ftp_browse_and_test(ftp_server):
    srv = ftp_server
    make_image_tree(srv['root'] / 'set1')
    res = media.browse_remote(srv['conn'], '/')
    assert res['path'] == '/' and res['parent'] is None
    assert res['folders'] == [{'name': 'set1', 'images': N_IMAGES - N_IMAGES // 5}]
    sub = media.browse_remote(srv['conn'], '/set1')
    assert sub['images'] == N_IMAGES - N_IMAGES // 5 and sub['folders'] == [{'name': 'sub', 'images': N_IMAGES // 5}]
    t = media.remote_test(srv['conn'])
    assert t['ok'] and t['folders'] == 1


# ---------------------------------------------------------------- SFTP

def test_sftp_import_and_export(sftp_server, tmp_path):
    srv = sftp_server
    names = make_image_tree(srv['root'] / 'set1')
    reg = SimpleNamespace(connection=lambda cid: srv['conn'])
    src = media.make_source({'kind': 'sftp', 'connection_id': 'sftptest', 'path': 'set1'}, tmp_path / 'proj', reg, {})
    try:
        cache = check_source(src, names, tmp_path / 'proj')
    finally:
        src.close()
    sink = sinks.make_sink({'kind': 'sftp', 'connection_id': 'sftptest', 'path': '/export/b'}, reg, {}, tmp_path)
    export_two_tasks(cache, names, sink)
    assert local_tree(srv['root'] / 'export' / 'b') == expected_tree(names)
    with pytest.raises(ValueError, match='not empty'):
        sinks.SFTPSink(srv['conn'], '/export/b').check_target(False)
    res = media.browse_remote(srv['conn'], '/set1')
    assert res['images'] == N_IMAGES - N_IMAGES // 5 and res['folders'][0]['name'] == 'sub'


def test_sftp_private_key(sftp_server):
    conn = dict(sftp_server['conn'], secret_key='', private_key=sftp_server['client_key_pem'])
    (sftp_server['root'] / 'k.jpg').write_bytes(jpeg_bytes())
    src = media.SFTPImageSource(conn, '/')
    try:
        assert [r for r, _ in src.list_images()] == ['k.jpg']
    finally:
        src.close()
    bad = dict(sftp_server['conn'], secret_key='wrong')
    with pytest.raises(Exception):
        media.remote_test(bad)


# ---------------------------------------------------------------- connections + routes

def test_connection_kinds(pg, tmp_path):
    reg = connections.Registry(tmp_path)
    s3 = reg.upsert_connection({'name': 'old', 'endpoint': 'http://x', 'access_key': 'AKIA123456', 'secret_key': 'sssss'})
    assert 'kind' not in s3                                       # old-style s3 entries stay as they were
    assert reg.public_connection(s3)['kind'] == 's3'
    sf = reg.upsert_connection({'kind': 'sftp', 'name': 'box', 'host': 'h', 'username': 'u', 'secret_key': 'pw',
                                'private_key': '-----BEGIN KEY-----', 'base_path': '/data'})
    assert sf['port'] == 22
    pub = reg.public_connection(sf)
    assert pub['has_private_key'] and pub['has_secret'] and 'private_key' not in pub and 'secret_key' not in pub
    again = reg.upsert_connection({'id': sf['id'], 'kind': 'sftp', 'host': 'h2', 'secret_key': '', 'private_key': ''})
    assert again['secret_key'] == 'pw' and again['private_key'].startswith('-----') and again['host'] == 'h2'
    db = reg.upsert_connection({'kind': 'postgres', 'host': 'db', 'port': '', 'database': 'd', 'username': 'u',
                                'secret_key': 'x', 'encrypt': 'true'})
    assert db['port'] == 5432 and db['table_prefix'] == 'lbl_' and db['encrypt'] is True
    assert reg.public_connection(db)['table_prefix'] == 'lbl_'
    with pytest.raises(ValueError):
        reg.merged_connection({'kind': 'gopher'})
    assert oct((tmp_path / 'connections.json').stat().st_mode & 0o777) == '0o600'
    # a labeling session that uses the connection blocks the delete
    from sqlalchemy import text
    from conftest import make_user
    from dataviz import db as appdb
    from dataviz.data import sessions
    owner = make_user('conn')
    for pid, name, src, ed in (('p1', 'Cats', {'kind': 'sftp', 'connection_id': sf['id']}, None),
                               ('p2', 'Dogs', {'kind': 'upload'}, {'kind': 'db', 'connection_id': db['id']})):
        sessions.create('label', owner['id'], name, sid=pid)
        with appdb.begin() as c:
            c.execute(text('INSERT INTO app.label_projects(session_id, source, export_default) '
                           'VALUES (:p, CAST(:s AS jsonb), CAST(:e AS jsonb))'),
                      {'p': pid, 's': json.dumps(src), 'e': json.dumps(ed) if ed else None})
    with pytest.raises(ValueError, match='Cats'):
        reg.delete_connection(sf['id'])
    with pytest.raises(ValueError, match='Dogs'):
        reg.delete_connection(db['id'])
    reg.delete_connection(s3['id'])
    # an S3 dataset cannot point at an FTP / DB connection
    with pytest.raises(ValueError):
        reg.upsert_dataset({'kind': 's3', 'connection_id': db['id'], 'bucket': 'b'})


def test_routes_browse_and_connections(tmp_path, ftp_server, image_tree):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from dataviz.server.routes import datasets
    reg = connections.Registry(tmp_path / 'ws')
    app = FastAPI()
    app.include_router(datasets.router(None, SimpleNamespace(datasets=reg, cfg={}, state=None)))
    c = TestClient(app)
    root, names = image_tree
    r = c.get('/api/fs/browse', params={'path': str(root), 'images': 1}).json()
    assert r['images'] == N_IMAGES - N_IMAGES // 5 and r['folders'] == [{'name': 'sub', 'yolo': False, 'images': N_IMAGES // 5}]
    r = c.get('/api/fs/browse', params={'path': str(root)}).json()
    assert 'images' not in r and 'images' not in r['folders'][0]               # unchanged without images=1
    make_image_tree(ftp_server['root'] / 'set1')
    saved = c.post('/api/connections', json={**ftp_server['conn'], 'id': None}).json()
    assert saved['kind'] == 'ftp' and 'secret_key' not in saved and saved['has_secret']
    assert [x['kind'] for x in c.get('/api/connections').json()['connections']] == ['ftp']
    t = c.post('/api/connections/test', json={'id': saved['id']}).json()
    assert t['ok'] and t['folders'] == 1
    b = c.post('/api/connections/browse', json={'connection': {'id': saved['id']}, 'path': '/set1'}).json()
    assert b['images'] == N_IMAGES - N_IMAGES // 5
    assert c.post('/api/connections', json={'kind': 'ftp'}).status_code == 400
    assert c.post('/api/connections', json={'name': 'x'}).json()['detail'] == 'endpoint URL is required'
