"""Video → frames (datacenter/video.py, service.extract_frames): every pick mode gives exactly the frames it promises
(30 and 60 fps, ranges, bursts, keyframes, scene, smart, near-duplicate skip), the layouts and frames.csv, the
poster thumbnail, and the HTTP flow end to end: estimate, the job, the hidden-then-renamed folder, the format
detection, cancel, the read-only S3 refusal, path escapes, the frame limit and the bucket's auto-extract."""

import csv
import io
import os
import shutil

import numpy as np
import pytest
from conftest import wait_job
from test_dc_routes import dc, new_bucket  # noqa: F401 - the fixture

av = pytest.importorskip('av')

from dataviz.datacenter import DCError
from dataviz.datacenter import video as V

pytestmark = pytest.mark.usefixtures('tmp_roots')


def make_clip(path, secs=3.0, fps=30, w=160, h=96, change_at=1.0, static=False, gop=15):
    """An h264 mp4: blue until `change_at`, then yellow, with a white square moving 4 px a frame (static: black)."""
    c = av.open(str(path), 'w')
    s = c.add_stream('libx264', rate=fps)
    s.width, s.height, s.pix_fmt = w, h, 'yuv420p'
    s.options = {'preset': 'ultrafast', 'g': str(gop), 'keyint_min': str(gop), 'sc_threshold': '0'}
    for i in range(round(secs * fps)):
        a = np.zeros((h, w, 3), np.uint8)
        if not static:
            a[:] = (200, 40, 40) if i / fps < change_at else (40, 200, 200)
            x = (i * 4) % w
            a[30:60, x:x + 12] = 255
        for p in s.encode(av.VideoFrame.from_ndarray(a, format='bgr24')):
            c.mux(p)
    for p in s.encode():
        c.mux(p)
    c.close()
    return str(path)


@pytest.fixture(scope='module')
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp('clips')
    return {'a': make_clip(d / 'a.mp4'), 'b60': make_clip(d / 'b60.mp4', secs=2, fps=60),
            'static': make_clip(d / 'static.mp4', static=True), 'long': make_clip(d / 'long.mp4', secs=40, gop=30)}


def frames(clip, tmp_path, **kw):
    o = V.options(kw)
    shutil.rmtree(tmp_path / 'out', ignore_errors=True)
    r = V.extract(clip, str(tmp_path / 'out'), 'v', o)
    return [x[1] for x in r['rows']], r, o


# ---------------------------------------------------------------------------------------------- picking
def test_probe(clips):
    i = V.probe(clips['a'])
    assert (i['w'], i['h'], i['fps'], i['frames'], i['duration'], i['codec'], i['decoder']) == \
        (160, 96, 30.0, 90, 3.0, 'h264', 'pyav')
    assert V.probe(clips['b60'])['fps'] == 60.0


@pytest.mark.parametrize('clip, kw, want', [
    ('a', {'mode': 'all'}, list(range(90))),
    ('a', {'mode': 'every', 'n': 10}, list(range(0, 90, 10))),
    ('a', {'fps': 15}, list(range(0, 90, 2))),                                        # 15 of 30 a second
    ('b60', {'fps': 15}, list(range(0, 120, 4))),                                     # 15 of 60 a second
    ('b60', {'mode': 'pattern', 'take': 15, 'pick': 'consecutive'},                   # the first 15 of each second
     list(range(15)) + list(range(60, 75))),
    ('a', {'mode': 'pattern', 'every_s': 2, 'window_s': 1, 'take': 15},               # 15 from 1 s, every 2 s
     list(range(0, 30, 2)) + list(range(60, 90, 2))),
    ('a', {'mode': 'pattern', 'every_s': 2, 'window_s': 1, 'take': 5, 'pick': 'consecutive'},
     [0, 1, 2, 3, 4, 60, 61, 62, 63, 64]),
    ('a', {'mode': 'count', 'n': 3}, [15, 45, 75]),
    ('a', {'fps': 10, 'start': 1, 'end': 2}, list(range(30, 60, 3))),
    ('a', {'fps': 0.5}, [0, 60]),
    ('a', {'mode': 'keyframes'}, [0, 15, 30, 45, 60, 75]),
    ('long', {'fps': 0.1}, [0, 300, 600, 900]),                                       # sparse: seeks over the gaps
])
def test_modes_pick_exactly(clips, tmp_path, clip, kw, want):
    got, _, o = frames(clips[clip], tmp_path, **kw)
    assert got == want
    est = V.estimate(V.probe(clips[clip]), o, V.keyframes(clips[clip]))
    assert est['frames'] == len(want) and not est['approx']
    assert sorted(os.listdir(tmp_path / 'out')) == sorted(f'v_{i:06d}.jpg' for i in want)


def test_scene_dedupe_smart(clips, tmp_path):
    got, _, _ = frames(clips['a'], tmp_path, mode='scene', threshold=0.2)
    assert got == [0, 30]                                                             # the cut at 1 s
    got, r, _ = frames(clips['static'], tmp_path, mode='all', dedupe=0.02)
    assert got == [0] and r['skipped_dupes'] == 89
    got, _, _ = frames(clips['a'], tmp_path, mode='smart', n=5)
    assert len(got) == 5 and any(28 <= i <= 33 for i in got) and got[-1] > 60    # catches the cut, covers the end
    got, _, _ = frames(clips['long'], tmp_path, mode='smart', n=6)
    assert len(got) == 6 and got[-1] > 900


def test_output_options_and_cap(clips, tmp_path):
    got, r, _ = frames(clips['a'], tmp_path, mode='all', max_frames=7, format='png', max_side=80)
    assert got == list(range(7)) and r['capped']
    import cv2
    im = cv2.imread(str(tmp_path / 'out' / 'v_000000.png'))
    assert im.shape[:2] == (48, 80)
    got, _, _ = frames(clips['a'], tmp_path, mode='count', n=2, format='webp')
    assert all(n.endswith('.webp') for n in os.listdir(tmp_path / 'out'))


@pytest.mark.parametrize('bad, msg', [
    ({'mode': 'nope'}, 'mode must be'), ({'fps': 0}, 'fps must be above 0'), ({'n': 0}, 'n must be at least 1'),
    ({'every_s': 1, 'window_s': 2}, 'window_s cannot be longer'), ({'start': 3, 'end': 2}, 'end must be after'),
    ({'format': 'gif'}, 'format must be'), ({'split': [0.5, 0.2]}, 'three ratios'), ({'speed': 2}, 'unknown option'),
    ({'take': 1.5}, 'whole number'),
])
def test_options_refused(bad, msg):
    with pytest.raises(DCError, match=msg):
        V.options(bad)


def test_split_assignment_and_layouts(clips, tmp_path):
    assert V.assign_splits(range(10), (0.8, 0.1, 0.1)).count('train') == 8
    s3 = V.assign_splits(range(3), (0.8, 0.1, 0.1))
    assert sorted(s3) == ['test', 'train', 'val']                                     # every split gets one
    o = V.options({'fps': 2, 'layout': 'yolo-split', 'block_s': 5})
    base = tmp_path / 'job'
    r = V.extract(clips['long'], V.image_folder(str(base), 'yolo-split', 'long'), 'long', o)
    lay = V.finish(str(base), 'yolo-split', [('vids/long.mp4', 'long', r['rows'], 40.0)], o)
    assert sum(lay['splits'].values()) == 80 and all(lay['splits'].values())
    with open(base / 'frames.csv') as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 80 and rows[0]['video'] == 'vids/long.mp4'
    per_block = {}
    for x in rows:                                                                    # one 5 s block, one split
        per_block.setdefault(int(float(x['time_s']) // 5), set()).add(x['split'])
        assert os.path.isfile(base / x['file']) and x['file'].startswith(f'images/{x["split"]}/')
    assert all(len(v) == 1 for v in per_block.values())
    assert (base / 'labels' / 'val').is_dir() and 'test: images/test' in (base / 'data.yaml').read_text()


def test_poster(clips):
    import cv2
    a = cv2.imdecode(np.frombuffer(V.frame_at(clips['a'], 2.0, 64), np.uint8), cv2.IMREAD_COLOR)
    assert a.shape[1] == 64 and a[5, 5, 1] > 150                                       # yellow: after the cut


def test_opencv_fallback(clips, tmp_path, monkeypatch):
    info = V.probe(clips['a'])
    monkeypatch.setitem(info, 'decoder', 'opencv')
    o = V.options({'mode': 'every', 'n': 30})
    r = V.extract(clips['a'], str(tmp_path / 'cv'), 'v', o, info)
    assert [x[1] for x in r['rows']] == [0, 30, 60] and r['decoder'] == 'opencv'
    with pytest.raises(DCError, match='keyframes-only'):
        V.extract(clips['a'], str(tmp_path / 'cv2'), 'v', V.options({'mode': 'keyframes'}), info)


def test_cancel_stops(clips, tmp_path):
    with pytest.raises(V.Cancelled):
        V.extract(clips['a'], str(tmp_path / 'c'), 'v', V.options({'mode': 'all'}), cancelled=lambda: True)


# ---------------------------------------------------------------------------------------------- HTTP + jobs
def _upload(c, bid, clip, path='', name=None):
    with open(clip, 'rb') as f:
        r = c.post(f'/api/dc/buckets/{bid}/upload', params={'path': path},
                   files=[('files', (name or os.path.basename(clip), f.read(), 'video/mp4'))])
    assert r.status_code == 200, r.text
    return r.json()


def _done(c, res):
    if res['job_id'] is not None:
        j = wait_job(c, res['job_id'])
        assert j['status'] == 'done', j
        return j['result']
    return res['result']


def test_extract_frames_end_to_end(dc, clips):
    _, c = dc
    b = new_bucket(c, name='vids')
    _upload(c, b['id'], clips['a'], 'raw')
    info = c.get(f'/api/dc/buckets/{b["id"]}/video', params={'path': 'raw/a.mp4'}).json()
    assert info['fps'] == 30.0 and info['frames'] == 90 and 'time_base' not in info
    poster = c.get(f'/api/dc/buckets/{b["id"]}/file', params={'path': 'raw/a.mp4', 'w': 64, 't': 0.5})
    assert poster.status_code == 200 and poster.headers['content-type'] == 'image/jpeg'

    opts = {'mode': 'pattern', 'every_s': 2, 'window_s': 1, 'take': 15}
    est = c.post('/api/dc/frames/estimate', json={'bucket': b['id'], 'paths': ['raw/a.mp4'], 'opts': opts}).json()
    assert est['frames'] == 30 and len(est['times']) == 8 and est['describe'] == '15 from 1 s every 2 s'
    r = c.post('/api/dc/frames', json={'bucket': b['id'], 'paths': ['raw/a.mp4'], 'opts': opts})
    assert r.status_code == 200, r.text
    assert r.json()['path'] == 'raw/a-frames' and r.json()['estimate']['frames'] == 30
    res = _done(c, r.json())
    assert res['frames'] == 30 and res['path'] == 'raw/a-frames'
    root = os.path.join(b['root'], 'raw', 'a-frames')
    assert len(os.listdir(os.path.join(root, 'images'))) == 30 and os.path.isdir(os.path.join(root, 'labels'))
    assert {'frames.csv', 'data.yaml'} <= set(os.listdir(root))
    assert not [n for n in os.listdir(os.path.join(b['root'], 'raw')) if n.startswith('.frames-')]
    ls = c.get(f'/api/dc/buckets/{b["id"]}/ls', params={'path': 'raw/a-frames'}).json()
    assert ls['format']['format'] == 'yolo-detect', ls['format']                     # ready to label

    # a second run: a new folder, never into the first one
    r2 = c.post('/api/dc/frames', json={'bucket': b['id'], 'paths': ['raw'], 'opts': {'mode': 'count', 'n': 4}})
    res2 = _done(c, r2.json())
    assert res2['path'] == 'raw/a-frames (copy)' and res2['frames'] == 4

    # a copy into a new bucket, flat
    r3 = c.post('/api/dc/frames', json={'bucket': b['id'], 'paths': ['raw/a.mp4'], 'target': 'copy',
                                        'new_name': 'a-cut', 'opts': {'fps': 1, 'layout': 'flat'}})
    res3 = _done(c, r3.json())
    nb = c.get(f'/api/dc/buckets/{res3["bucket"]}').json()
    assert nb['name'] == 'a-cut' and sorted(os.listdir(os.path.join(nb['root'], 'a-frames'))) == \
        ['a_000000.jpg', 'a_000030.jpg', 'a_000060.jpg', 'frames.csv']


def test_extract_frames_refusals(dc, clips):
    _, c = dc
    b = new_bucket(c, name='vids2')
    _upload(c, b['id'], clips['a'])
    post = lambda **kw: c.post('/api/dc/frames', json={'bucket': b['id'], 'paths': ['a.mp4'], **kw})
    assert post(paths=['../x.mp4']).status_code == 400
    assert post(paths=['nope.mp4']).status_code == 404
    assert post(opts={'mode': 'bogus'}).status_code == 400
    assert post(out='.hidden').status_code == 400
    c.post(f'/api/dc/buckets/{b["id"]}/ops', json={'ops': [{'op': 'mkdir', 'path': 'empty'}]})
    r = c.post('/api/dc/frames', json={'bucket': b['id'], 'paths': ['empty']})
    assert r.status_code == 400 and 'holds no video' in r.text


def test_nothing_matched_leaves_nothing(dc, clips):
    _, c = dc
    b = new_bucket(c, name='vids3')
    _upload(c, b['id'], clips['a'])
    r = c.post('/api/dc/frames', json={'bucket': b['id'], 'paths': ['a.mp4'], 'opts': {'fps': 1, 'start': 50}})
    j = wait_job(c, r.json()['job_id']) if r.json()['job_id'] else None
    assert j is None or j['status'] == 'error'
    assert sorted(os.listdir(b['root'])) == ['a.mp4']                                  # no folder, no .frames-*


def test_auto_extract_on_upload(dc, clips):
    _, c = dc
    b = new_bucket(c, name='auto')
    r = c.post(f'/api/dc/buckets/{b["id"]}/video-auto', json={'opts': {'mode': 'count', 'n': 3, 'layout': 'flat'}})
    assert r.status_code == 200 and r.json()['video_auto']['n'] == 3
    assert c.get(f'/api/dc/buckets/{b["id"]}').json()['video_auto']['mode'] == 'count'
    up = _upload(c, b['id'], clips['a'], 'in')
    jobs = up['frames']['jobs']
    assert [j['path'] for j in jobs] == ['in/a.mp4'] and not up['frames']['errors']
    for j in jobs:
        if j['job_id'] is not None:
            assert wait_job(c, j['job_id'])['status'] == 'done'
    assert sorted(os.listdir(os.path.join(b['root'], 'in', 'a-frames'))) == \
        ['a_000015.jpg', 'a_000045.jpg', 'a_000075.jpg', 'frames.csv']
    # the same file again adds nothing, so nothing starts; turning it off stops it
    assert 'frames' not in _upload(c, b['id'], clips['a'], 'in')
    assert c.post(f'/api/dc/buckets/{b["id"]}/video-auto', json={'opts': None}).json() == {'video_auto': None}
    assert 'frames' not in _upload(c, b['id'], clips['static'], 'y')


def test_frames_need_manage(dc, clips):
    as_user, c = dc
    from conftest import make_user
    b = new_bucket(c, name='mine')
    _upload(c, b['id'], clips['a'])
    other = as_user(make_user('bob'))
    r = other.post('/api/dc/frames', json={'bucket': b['id'], 'paths': ['a.mp4']})
    assert r.status_code in (403, 404)
    assert other.get(f'/api/dc/buckets/{b["id"]}/video', params={'path': 'a.mp4'}).status_code == 404


def test_s3_stream_needs_copy(dc, clips, monkeypatch):
    """A streamed S3 bucket is read-only: inplace is refused before anything is looked at."""
    from dataviz.datacenter import service
    _, c = dc
    b = new_bucket(c, name='fake')
    _upload(c, b['id'], clips['a'])
    real = service._row

    class S3Row:
        def __init__(self, r):
            self._r, self.kind, self.link, self.root, self.mode = r, 's3', 's3', r.root, 'stream'

        def __getattr__(self, k):
            return getattr(self._r, k)
    monkeypatch.setattr(service, '_row', lambda user, bid, conn=None: S3Row(real(user, bid, conn)))
    r = c.post('/api/dc/frames', json={'bucket': b['id'], 'paths': ['a.mp4']})
    assert r.status_code == 400 and 'streamed from S3' in r.text
    assert c.post(f'/api/dc/buckets/{b["id"]}/video-auto', json={'opts': {}}).status_code == 400
    assert c.get(f'/api/dc/buckets/{b["id"]}/file', params={'path': 'a.mp4', 'w': 64}).status_code == 415


def test_frames_csv_columns(clips, tmp_path):
    o = V.options({'mode': 'count', 'n': 2, 'layout': 'per-video'})
    base = tmp_path / 'pv'
    rows = []
    for rel, stem in zip(['x/a.mp4', 'y/a.mp4'], V.stems(['x/a.mp4', 'y/a.mp4'])):
        r = V.extract(clips['a'], V.image_folder(str(base), 'per-video', stem), stem, o)
        rows.append((rel, stem, r['rows'], 3.0))
    assert [s for _, s, _, _ in rows] == ['a', 'a-2']
    V.finish(str(base), 'per-video', rows, o)
    got = list(csv.reader(io.StringIO((base / 'frames.csv').read_text())))
    assert got[0] == ['file', 'video', 'frame', 'time_s', 'split']
    assert [g[0] for g in got[1:]] == ['a/a_000022.jpg', 'a/a_000067.jpg', 'a-2/a-2_000022.jpg', 'a-2/a-2_000067.jpg']
