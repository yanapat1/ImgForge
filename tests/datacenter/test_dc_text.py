"""Data Center text editing (GET / PUT /api/dc/buckets/{bid}/text): read metadata, save + undo, the 409 when the
file changed since it was read, newline style / BOM / mode kept, new files (and the `create` op), the dot-file
allowlist (.env shown but read only with data.manage, internal dot folders hidden), the new kinds, a read-only S3
link refused, and a data.yaml edit that re-tags the bucket."""

import os
import stat

import pytest
from sqlalchemy import text

from conftest import make_user
from test_dc_routes import new_bucket, upload_zip

pytestmark = pytest.mark.usefixtures('tmp_roots')
pytest_plugins = ['conftest_io']             # fixtures: s3_server, s3_bucket


@pytest.fixture
def dc(client_as, tmp_path):
    cfg = client_as.ctx.cfg
    cfg['storage']['data_root'] = str(tmp_path / 'dcdata')
    cfg['dataset']['workspace_root'] = str(tmp_path / 'ws')
    return client_as, client_as(make_user('alice'))


def _put(c, bid, path, body, **kw):
    return c.put(f'/api/dc/buckets/{bid}/text', json={'path': path, 'text': body, **kw})


def _get(c, bid, path, **kw):
    return c.get(f'/api/dc/buckets/{bid}/text', params={'path': path, **kw})


def test_read_save_undo_conflict(dc):
    _, c = dc
    b = new_bucket(c)
    root = b['root']
    with open(os.path.join(root, 'notes.txt'), 'w') as f:
        f.write('hello\nworld\n')
    os.chmod(os.path.join(root, 'notes.txt'), 0o640)
    r = _get(c, b['id'], 'notes.txt').json()
    assert r['text'] == 'hello\nworld\n' and r['editable'] and not r['truncated'] and 'reason' not in r
    assert r['size'] == 12 and r['encoding'] == 'utf-8' and r['newline'] == '\n'
    assert isinstance(r['mtime_ns'], str) and int(r['mtime_ns']) == os.stat(os.path.join(root, 'notes.txt')).st_mtime_ns

    s = _put(c, b['id'], 'notes.txt', 'hello\nthere\n', expect_mtime_ns=r['mtime_ns'])
    assert s.status_code == 200, s.text
    out = s.json()
    assert out['size'] == 12 and out['undo_id'] and out['path'] == 'notes.txt' and not out['created']
    assert open(os.path.join(root, 'notes.txt')).read() == 'hello\nthere\n'
    assert stat.S_IMODE(os.stat(os.path.join(root, 'notes.txt')).st_mode) == 0o640
    assert not [n for n in os.listdir(root) if '.part' in n]

    # someone else changed it since: 409 with the stale mtime, None overwrites
    stale = _put(c, b['id'], 'notes.txt', 'mine\n', expect_mtime_ns=r['mtime_ns'])
    assert stale.status_code == 409 and 'changed' in stale.json()['detail']
    assert open(os.path.join(root, 'notes.txt')).read() == 'hello\nthere\n'

    u = c.post(f'/api/dc/buckets/{b["id"]}/undo', json={'undo_id': out['undo_id']})
    assert u.status_code == 200 and u.json()['restored'] == 1 and not u.json()['errors'], u.text
    assert open(os.path.join(root, 'notes.txt')).read() == 'hello\nworld\n'

    # a truncated read is not editable
    t = _get(c, b['id'], 'notes.txt', limit=4).json()
    assert t['truncated'] and not t['editable'] and t['text'] == 'hell' and 'first' in t['reason']


def test_newline_bom_and_encoding(dc):
    _, c = dc
    b = new_bucket(c)
    p = os.path.join(b['root'], 'win.txt')
    with open(p, 'wb') as f:
        f.write(b'\xef\xbb\xbfa\r\nb\r\n')
    r = _get(c, b['id'], 'win.txt').json()
    assert r['text'] == 'a\nb\n' and r['newline'] == '\r\n' and r['encoding'] == 'utf-8-sig' and r['editable']
    assert _put(c, b['id'], 'win.txt', 'a\nb\nc\n', expect_mtime_ns=r['mtime_ns']).status_code == 200
    assert open(p, 'rb').read() == b'\xef\xbb\xbfa\r\nb\r\nc\r\n'

    with open(os.path.join(b['root'], 'latin.txt'), 'wb') as f:
        f.write('café\n'.encode('latin-1'))
    r = _get(c, b['id'], 'latin.txt').json()
    assert not r['editable'] and 'UTF-8' in r['reason']
    assert _put(c, b['id'], 'latin.txt', 'x').status_code == 415

    with open(os.path.join(b['root'], 'bin.dat'), 'wb') as f:
        f.write(b'\x00\x01\x02')
    assert _get(c, b['id'], 'bin.dat').status_code == 400


def test_create_new_file_and_create_op(dc):
    _, c = dc
    b = new_bucket(c)
    s = _put(c, b['id'], 'cfg/new.json', '{}', create=True)
    assert s.status_code == 404                                  # no folder cfg/
    s = _put(c, b['id'], 'new.json', '{"a": 1}\n', create=True)
    assert s.status_code == 200 and s.json()['created']
    assert _put(c, b['id'], 'new.json', '{}', create=True).status_code == 409
    assert c.post(f'/api/dc/buckets/{b["id"]}/undo', json={'undo_id': s.json()['undo_id']}).status_code == 200
    assert not os.path.exists(os.path.join(b['root'], 'new.json'))
    # a missing file is a 404 without create
    assert _put(c, b['id'], 'missing.txt', 'x').status_code == 404

    r = c.post(f'/api/dc/buckets/{b["id"]}/ops', json={'ops': [{'op': 'create', 'path': 'empty.md'},
                                                               {'op': 'create', 'path': '.hidden'}]}).json()
    assert r['applied'] == [{'op': 'create', 'path': 'empty.md'}] and len(r['errors']) == 1
    assert os.path.getsize(os.path.join(b['root'], 'empty.md')) == 0
    ls = c.get(f'/api/dc/buckets/{b["id"]}/ls').json()
    assert [(e['name'], e['kind']) for e in ls['entries']] == [('empty.md', 'text')]
    c.post(f'/api/dc/buckets/{b["id"]}/undo', json={'undo_id': r['undo_id']})
    assert not os.path.exists(os.path.join(b['root'], 'empty.md'))


def test_dotfiles_kinds_and_secret_reads(dc):
    as_user, c = dc
    b = new_bucket(c)
    root = b['root']
    for n in ('.env', '.env.local', '.gitignore', '.DS_Store', 'a.parquet', 'run.py', 'Dockerfile', 'clip.mp4',
              'song.wav', 'readme.md', 'x.txt.part-0123abcd', '.upload-123.zip'):
        with open(os.path.join(root, n), 'w') as f:
            f.write('SECRET=1\n' if n.startswith('.env') else 'x\n')
    for d in ('.git', '.cache', '.s3'):
        os.makedirs(os.path.join(root, d, 'sub'))
        with open(os.path.join(root, d, 'sub', 'f.txt'), 'w') as f:
            f.write('x')
    ls = c.get(f'/api/dc/buckets/{b["id"]}/ls').json()
    kinds = {e['name']: e['kind'] for e in ls['entries']}
    assert kinds == {'.env': 'code', '.env.local': 'code', '.gitignore': 'text', 'a.parquet': 'table',
                     'run.py': 'code', 'Dockerfile': 'code', 'clip.mp4': 'video', 'song.wav': 'audio',
                     'readme.md': 'text'}
    # the search walk sees .env but none of the internal dot folders
    found = c.get('/api/dc/search', params={'q': 'f.txt', 'bucket': b['id']}).json()['files']
    assert found == []
    found = c.get('/api/dc/search', params={'q': '.env', 'bucket': b['id']}).json()['files']
    assert sorted(f['name'] for f in found) == ['.env', '.env.local']

    # rename may produce an allowlisted dot name, never a hidden one
    r = c.post(f'/api/dc/buckets/{b["id"]}/ops', json={'ops': [{'op': 'rename', 'path': 'readme.md',
                                                               'name': '.secret'}]}).json()
    assert r['errors'] and not r['applied']
    r = c.post(f'/api/dc/buckets/{b["id"]}/ops', json={'ops': [{'op': 'rename', 'path': 'readme.md',
                                                               'name': '.env.prod'}]}).json()
    assert r['applied'] and not r['errors']

    assert _get(c, b['id'], '.env').json()['text'] == 'SECRET=1\n'

    # a tier without data.manage reads ordinary files of its own buckets, never .env
    from dataviz import db
    v = make_user('viewer')
    with db.begin() as conn:
        conn.execute(text("UPDATE app.users SET tier_id = 'viewer' WHERE id = :u"), {'u': v['id']})
        conn.execute(text('UPDATE app.dc_buckets SET owner_id = :u WHERE id = :b'), {'u': v['id'], 'b': b['id']})
    viewer = as_user({**v, 'tier_id': 'viewer'})
    assert _get(viewer, b['id'], 'run.py').status_code == 200
    ro = _get(viewer, b['id'], 'run.py').json()
    assert not ro['editable'] and 'tier' in ro['reason']
    assert _get(viewer, b['id'], '.env').status_code == 403
    assert _get(viewer, b['id'], '.env.local').status_code == 403
    assert viewer.get(f'/api/dc/buckets/{b["id"]}/file', params={'path': '.env'}).status_code == 403
    assert _put(viewer, b['id'], 'run.py', 'y\n').status_code == 403


def test_data_yaml_edit_retags_and_undo(dc):
    _, c = dc
    b = new_bucket(c)
    upload_zip(c, b['id'])
    r = _get(c, b['id'], 'data.yaml').json()
    assert r['editable']
    s = _put(c, b['id'], 'data.yaml', 'names: [cat, dog, bird]\n', expect_mtime_ns=r['mtime_ns']).json()
    b2 = c.get(f'/api/dc/buckets/{b["id"]}').json()
    assert b2['format']['classes'] == 3
    # a broken data.yaml: still saved, the format check runs again
    r = _get(c, b['id'], 'data.yaml').json()
    s = _put(c, b['id'], 'data.yaml', 'nonsense\n', expect_mtime_ns=r['mtime_ns']).json()
    assert 'retagged' in s
    u = c.post(f'/api/dc/buckets/{b["id"]}/undo', json={'undo_id': s['undo_id']}).json()
    assert u['restored'] == 1
    assert open(os.path.join(b['root'], 'data.yaml')).read() == 'names: [cat, dog, bird]\n'



def test_s3_stream_text_read_only(dc, s3_server, s3_bucket):
    _, c = dc
    client, bucket = s3_bucket
    client.put_object(Bucket=bucket, Key='t/data.yaml', Body=b'names: [car]\n')
    client.put_object(Bucket=bucket, Key='t/.env', Body=b'K=V\n')
    client.put_object(Bucket=bucket, Key='t/.git/config', Body=b'x\n')
    conn = c.post('/api/connections', json={**{k: v for k, v in s3_server.items() if k != 'id'}, 'kind': 's3'})
    b = c.post('/api/dc/buckets', json={'name': 'cloud', 'kind': 's3', 'connection_id': conn.json()['id'],
                                        's3_bucket': bucket, 'prefix': 't'}).json()
    r = _get(c, b['id'], 'data.yaml').json()
    assert r['text'] == 'names: [car]\n' and not r['editable'] and 'read-only' in r['reason']
    assert r['mtime_ns'] is None
    s = _put(c, b['id'], 'data.yaml', 'names: [bus]\n')
    assert s.status_code == 400 and 'read-only' in s.json()['detail']
    names = [e['name'] for e in c.get(f'/api/dc/buckets/{b["id"]}/ls').json()['entries']]
    assert '.env' in names and '.git' not in names
