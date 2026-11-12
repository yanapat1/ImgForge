"""Chunked uploads (POST /api/dc/buckets/{bid}/upload/part, service.upload_part): a big file sent in parts lands
byte for byte under its folder path, archives are
unpacked, dot names / files already there are skipped before any bytes, the part file stays hidden and goes away,
chunks may come out of order, twice or in parallel, the status GET resumes after a reload, stale parts are swept,
and the bucket's video auto-extract fires when the file is whole."""

import io
import os
import time
import uuid
import zipfile

import pytest
from conftest import jpeg_bytes, wait_job
from test_bucket_routes import dc, new_bucket  # noqa: F401 - the fixture

pytestmark = pytest.mark.usefixtures('tmp_roots')


def part(c, bid, name, data, offset, total, uid, path=''):
    return c.post(f'/api/dc/buckets/{bid}/upload/part', content=data,
                  params={'path': path, 'name': name, 'id': uid, 'offset': offset, 'total': total},
                  headers={'Content-Type': 'application/octet-stream'})


def send_parts(c, bid, name, data, n=3, path=''):
    uid, step, out = uuid.uuid4().hex, -(-len(data) // n), None
    for off in range(0, len(data), step):
        r = part(c, bid, name, data[off:off + step], off, len(data), uid, path)
        assert r.status_code == 200, r.text
        out = r.json()
        assert bool(out.get('done')) == (off + step >= len(data))
    return out


def test_parts_land_byte_for_byte(dc):
    _, c = dc
    b = new_bucket(c, name='big')
    data = os.urandom(300_000)
    out = send_parts(c, b['id'], 'clip/sub/v.mp4', data, path='in')
    assert out['added'] == 1 and out['skipped'] == 0 and out['files'] == ['clip/sub/v.mp4']
    with open(os.path.join(b['root'], 'in', 'clip', 'sub', 'v.mp4'), 'rb') as f:
        assert f.read() == data
    assert not [n for n in os.listdir(os.path.join(b['root'], 'in')) if n.startswith('.upload-')]
    # the same name again is skipped at offset 0, before any bytes are kept
    r = part(c, b['id'], 'clip/sub/v.mp4', data[:10], 0, len(data), uuid.uuid4().hex, 'in')
    assert r.json() == {'added': 0, 'skipped': 1, 'files': [], 'done': True, 'retagged': None}
    assert not [n for n in os.listdir(os.path.join(b['root'], 'in')) if n.startswith('.upload-')]


def status(c, bid, name, total, uid, path=''):
    r = c.get(f'/api/dc/buckets/{bid}/upload/part', params={'path': path, 'name': name, 'id': uid, 'total': total})
    assert r.status_code == 200, r.text
    return r.json()


def test_out_of_order_duplicates_and_resume(dc):
    _, c = dc
    b = new_bucket(c, name='resume')
    data, uid = os.urandom(1000), uuid.uuid4().hex
    assert status(c, b['id'], 'a.bin', 1000, uid) == {'received': 0, 'ranges': []}
    assert part(c, b['id'], 'a.bin', data[600:], 600, 1000, uid).json() == {'received': 400, 'ranges': [[600, 1000]]}
    assert part(c, b['id'], 'a.bin', data[600:], 600, 1000, uid).json()['received'] == 400       # a repeat: no harm
    assert part(c, b['id'], 'a.bin', data[:300], 0, 1000, uid).json()['ranges'] == [[0, 300], [600, 1000]]
    ls = c.get(f'/api/dc/buckets/{b["id"]}/ls').json()
    assert 'upload' not in str(ls.get('entries', ls.get('items', [])))                           # parts stay hidden
    # a "reload": a new page asks what is held and sends only the gap
    assert status(c, b['id'], 'a.bin', 1000, uid)['ranges'] == [[0, 300], [600, 1000]]
    assert part(c, b['id'], 'a.bin', data[300:600], 300, 1000, uid).json()['added'] == 1
    with open(os.path.join(b['root'], 'a.bin'), 'rb') as f:
        assert f.read() == data
    assert not [n for n in os.listdir(b['root']) if n.startswith('.upload-')]
    # the file is there now: asking again (or a late chunk) skips it
    assert status(c, b['id'], 'a.bin', 1000, uid)['skipped'] == 1
    assert part(c, b['id'], 'a.bin', data[:10], 0, 1000, uid).json()['skipped'] == 1


def test_size_change_resets_the_part(dc):
    _, c = dc
    b = new_bucket(c, name='reset')
    uid = uuid.uuid4().hex
    part(c, b['id'], 'a.bin', b'x' * 10, 0, 100, uid)
    assert status(c, b['id'], 'a.bin', 50, uid) == {'received': 0, 'ranges': []}
    data = os.urandom(50)
    assert part(c, b['id'], 'a.bin', data, 0, 50, uid).json()['added'] == 1
    with open(os.path.join(b['root'], 'a.bin'), 'rb') as f:
        assert f.read() == data


def test_parallel_chunks_place_once(dc):
    from concurrent.futures import ThreadPoolExecutor
    _, c = dc
    b = new_bucket(c, name='parallel')
    data, uid, step = os.urandom(64 * 1024), uuid.uuid4().hex, 4096
    offs = list(range(0, len(data), step))[::-1]
    with ThreadPoolExecutor(8) as ex:
        outs = list(ex.map(lambda o: part(c, b['id'], 'p/v.bin', data[o:o + step], o, len(data), uid).json(), offs))
    assert sum(o.get('added', 0) for o in outs) == 1
    with open(os.path.join(b['root'], 'p', 'v.bin'), 'rb') as f:
        assert f.read() == data


def test_archive_in_parts_is_unpacked(dc):
    _, c = dc
    b = new_bucket(c, name='zipped')
    z = io.BytesIO()
    with zipfile.ZipFile(z, 'w') as zf:
        for i in range(3):
            zf.writestr(f'images/im{i}.jpg', jpeg_bytes())
    out = send_parts(c, b['id'], 'ds.zip', z.getvalue(), path='x')
    assert out['added'] == 3
    assert sorted(os.listdir(os.path.join(b['root'], 'x', 'images'))) == ['im0.jpg', 'im1.jpg', 'im2.jpg']
    assert not [n for n in os.listdir(os.path.join(b['root'], 'x')) if n.startswith('.')]


def test_refusals(dc):
    _, c = dc
    b = new_bucket(c, name='refuse')
    uid = uuid.uuid4().hex
    assert part(c, b['id'], 'a.bin', b'xx', 0, 2, 'nothex').status_code == 400
    assert part(c, b['id'], 'a.bin', b'xxx', 0, 2, uid).status_code == 400            # more than total
    assert part(c, b['id'], '../a.bin', b'xx', 0, 2, uid).status_code == 400
    assert part(c, b['id'], 'x', b'xx', 0, 2, uid, path='../up').status_code == 400
    r = part(c, b['id'], '.git/config', b'xx', 0, 2, uid)
    assert r.status_code == 200 and r.json()['skipped'] == 1
    assert not os.path.exists(os.path.join(b['root'], '.git'))


def test_stale_parts_are_swept(dc):
    _, c = dc
    b = new_bucket(c, name='stale')
    old = os.path.join(b['root'], f'.upload-{uuid.uuid4().hex}.part')
    with open(old, 'wb') as f:
        f.write(b'left over')
    side = old[:-len('.part')] + '.json'
    with open(side, 'w') as f:
        f.write('{}')
    for p in (old, side):
        os.utime(p, (time.time() - 2 * 86400,) * 2)
    send_parts(c, b['id'], 'n.bin', os.urandom(100))
    assert not os.path.exists(old) and not os.path.exists(side)


def test_auto_extract_on_chunked_video(dc):
    av = pytest.importorskip('av')  # noqa: F841
    from test_bucket_video import make_clip
    _, c = dc
    b = new_bucket(c, name='autoparts')
    c.post(f'/api/dc/buckets/{b["id"]}/video-auto', json={'opts': {'mode': 'count', 'n': 3, 'layout': 'flat'}})
    clip = make_clip(os.path.join(b['root'], '..', 'src.mp4'))
    with open(clip, 'rb') as f:
        data = f.read()
    out = send_parts(c, b['id'], 'a.mp4', data, n=4, path='in')
    jobs = out['frames']['jobs']
    assert [j['path'] for j in jobs] == ['in/a.mp4']
    for j in jobs:
        if j['job_id'] is not None:
            assert wait_job(c, j['job_id'])['status'] == 'done'
    assert len(os.listdir(os.path.join(b['root'], 'in', 'a-frames'))) == 4
