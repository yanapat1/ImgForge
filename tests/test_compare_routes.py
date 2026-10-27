"""The Compare tab's API over a combined dataset with a known answer: one image identical in A and B (renamed in
B, matched by content), one with a moved + a reclassed box, one only in A, one only in B; binary columns; image
detail; edits keep the diff current; the routes refuse a plain session."""

import shutil

import numpy as np
import pytest

from authutil import account, client, login
from conftest import make_jpeg
from dataviz.data import diff
from dataviz.server.binary import unpack_columns
from vizutil import make_viz_app, make_yolo, union_session, viz_session


@pytest.fixture
def world(pg, tmp_path):
    a = make_yolo(tmp_path / 'dsA', classes=('cat', 'dog'), boxes=(
        ('same', '0 0.5 0.5 0.4 0.4\n'),
        ('edit', '0 0.3 0.3 0.2 0.2\n1 0.7 0.7 0.2 0.2\n'),
        ('onlya', '1 0.5 0.5 0.2 0.2\n')))
    b = make_yolo(tmp_path / 'dsB', classes=('cat', 'dog'), boxes=(
        ('renamed', '0 0.5 0.5 0.4 0.4\n'),
        ('edit', '0 0.32 0.3 0.2 0.2\n0 0.7 0.7 0.2 0.2\n'),
        ('onlyb', '0 0.5 0.5 0.3 0.3\n')))
    shutil.copy(a / 'images' / 'same.jpg', b / 'images' / 'renamed.jpg')       # same bytes, other name
    shutil.copy(a / 'images' / 'edit.jpg', b / 'images' / 'edit.jpg')
    make_jpeg(b / 'images' / 'onlyb.jpg', 64, 48, color=(200, 10, 200))          # make_yolo colours by position
    alice = account('alice')
    A = viz_session(alice['id'], 'A', a)
    B = viz_session(alice['id'], 'B', b)
    plain = viz_session(alice['id'], 'plain', a)
    U = union_session(alice['id'], 'A vs B', [(A, 'A'), (B, 'B')])
    ctx, app = make_viz_app(tmp_path / 'ws')
    c = client(app)
    assert login(c, 'alice').status_code == 200
    return {'c': c, 'U': U, 'plain': plain, 'ctx': ctx}


def test_known_answer(world):
    c, U = world['c'], world['U']
    r = c.post(f'/s/{U}/api/compare/run', json={'a': 'A', 'b': 'B'})
    assert r.status_code == 200, r.text
    body = r.json()
    sm = body['summary']
    assert sm['images'] == {'only_a': 1, 'only_b': 1, 'same': 1, 'labels_changed': 1, 'pixels_changed': 0,
                            'dup_a': 0, 'dup_b': 0}
    assert sm['boxes']['same'] == 1 and sm['boxes']['moved'] == 1 and sm['boxes']['reclass'] == 1
    assert sm['boxes']['image_only_a'] == 1 and sm['boxes']['image_only_b'] == 1
    key = body['key']
    res = c.get(f'/s/{U}/api/compare/result', params={'key': key}).json()
    assert res['image_lists']['only_a'] == ['A/onlya.jpg'] and res['image_lists']['only_b'] == ['B/onlyb.jpg']
    assert res['image_lists']['same'] == ['A/same.jpg']
    col = c.get(f'/s/{U}/api/compare/columns', params={'key': key})
    assert col.headers['content-type'] == 'application/octet-stream'
    meta, cols = unpack_columns(col.content)
    pts = c.get(f'/s/{U}/api/points').json()
    st = dict(zip(zip(pts['image'], range(len(pts['id']))), cols['status']))
    by = {}
    for (img, sid), s in st.items():
        by.setdefault(img, []).append(int(s))
    assert by['A/same.jpg'] == [diff.BOX_SAME] and by['B/renamed.jpg'] == [diff.BOX_SAME]
    assert sorted(by['A/edit.jpg']) == [diff.BOX_MOVED, diff.BOX_RECLASS]
    assert by['A/onlya.jpg'] == [diff.BOX_IMAGE_ONLY]
    assert c.get(f'/s/{U}/api/compare/columns', params={'key': key},
                 headers={'If-None-Match': col.headers['etag']}).status_code == 304
    d = c.get(f'/s/{U}/api/compare/image', params={'key': key, 'name': 'B/edit.jpg'}).json()
    assert d['a']['name'] == 'A/edit.jpg' and d['b']['name'] == 'B/edit.jpg' and len(d['b']['boxes']) == 2
    again = c.post(f'/s/{U}/api/compare/run', json={'a': 'A', 'b': 'B'}).json()
    assert again['key'] == key                                          # cached


def test_patch_keeps_diff_current(world):
    c, U, ctx = world['c'], world['U'], world['ctx']
    key = c.post(f'/s/{U}/api/compare/run', json={'a': 'A', 'b': 'B'}).json()['key']
    st = ctx.state_for(U)
    sid = st.by_image['B/edit.jpg'][1]                               # B's reclassed box: make it a dog again
    st.labels[sid] = 1
    st.samples[sid]['cls'] = 1
    out = st.on_boxes_patched(['B/edit.jpg'])
    assert out and list(out.values())[0]['summary']['boxes']['reclass'] == 0
    new = c.post(f'/s/{U}/api/compare/run', json={'a': 'A', 'b': 'B'}).json()
    assert new['key'] != key and new['summary']['boxes']['reclass'] == 0
    fresh = __import__('dataviz.data.compare', fromlist=['x']).CompareService(st).run('A', 'B', {})[1]
    assert np.array_equal(fresh.status, st._compare.cached('A', 'B', {})[1].status)


def test_errors(world):
    c = world['c']
    assert c.post(f'/s/{world["plain"]}/api/compare/run', json={'a': 'A', 'b': 'B'}).status_code == 409
    assert c.post(f'/s/{world["U"]}/api/compare/run', json={'a': 'A', 'b': 'A'}).status_code == 400
    assert c.post(f'/s/{world["U"]}/api/compare/run', json={'a': 'A', 'b': 'Z'}).status_code == 400
    s = c.get(f'/s/{world["U"]}/api/union/status').json()
    assert s['union'] and not s['stale'] and [m['alias'] for m in s['members']] == ['A', 'B']


def test_deleting_a_box_updates_the_diff(world):
    """A box deleted through the patch path (curation / live feed) is no longer SAME in the diff (review finding:
    patched() walked by_image, which no longer lists deleted boxes)."""
    from dataviz.data import patching
    c, U, ctx = world['c'], world['U'], world['ctx']
    before = c.post(f'/s/{U}/api/compare/run', json={'a': 'A', 'b': 'B'}).json()['summary']['boxes']
    st = ctx.state_for(U)
    sid = st.by_image['B/renamed.jpg'][0]                            # B's copy of the identical image
    patching.apply_patch(st, {'deleted': [sid]})
    after = c.post(f'/s/{U}/api/compare/run', json={'a': 'A', 'b': 'B'}).json()['summary']
    assert after['boxes']['same'] == before['same'] - 1 and after['boxes']['removed'] == before['removed'] + 1
    assert after['images']['labels_changed'] == 2
