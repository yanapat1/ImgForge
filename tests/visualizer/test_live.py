"""The live change feed (data/live.py, db/listen.py, GET /api/live): labeling edits reach an open visualize session
as patches (cls / deleted / restored / moved / new) without a re-index; bulk changes ask for a re-index."""

import asyncio
import threading
import time

import pytest
from sqlalchemy import text

from authutil import account, client, login
from conftest import make_user
from test_labeling_source import box, make_project, put
from vizutil import _Job, make_viz_app, make_yolo, viz_session

from dataviz import db
from dataviz.data import live
from dataviz.data.labeling_source import LabelingSource
from dataviz.labeling.projects import ProjectRegistry

pytestmark = pytest.mark.usefixtures('tmp_roots')


@pytest.fixture
def lab(pg, tmp_path):
    reg = ProjectRegistry(tmp_path / 'labeling')
    reg.owner_id = make_user('labeler')['id']
    return reg


def _anns(st, mid, tid):
    return [a for a in st.get_annotations(mid)['annotations'] if a['task_id'] == tid]


@pytest.fixture
def world(lab, tmp_path):
    from dataviz.state import State
    pid, st, tasks, mids = make_project(lab)
    det = tasks['object_detection']
    cat, dog = (c['id'] for c in det['classes'])
    put(st, mids['a.jpg'], det['id'], [box(cat, .1, .1, .2, .3), box(dog, .5, .4, .25, .25)])
    put(st, mids['c.jpg'], det['id'], [box(dog, .2, .2, .4, .4)])
    src = LabelingSource(lab, pid, det['id'])
    state = State(src, tmp_path / 'ws', {'none': None}, default_feature='none')
    state._index_job(_Job())
    return {'pid': pid, 'st': st, 'det': det, 'cat': cat, 'dog': dog, 'mids': mids, 'state': state, 'lab': lab}


def _sid(w, image, k):
    return w['state'].by_image[image][k]


def _edit(w, image, fn):
    """Change the live rows of one image: fn(rows) -> rows."""
    st, tid, mid = w['st'], w['det']['id'], w['mids'][image]
    put(st, mid, tid, fn(_anns(st, mid, tid)))


def test_trigger_seq_increases(world):
    st, tid = world['st'], world['det']['id']
    c0 = st.change_cursor(tid)
    assert c0 > 0
    _edit(world, 'a.jpg', lambda rows: [dict(rows[0], class_id=world['dog'])] + rows[1:])
    c1 = st.change_cursor(tid)
    assert c1 > c0
    ids, cur = st.changed_media(tid, c0)
    assert ids == [world['mids']['a.jpg']] and cur == c1
    with db.connect() as c:
        seqs = c.execute(text('SELECT change_seq FROM lbl.annotations WHERE project_id = :p ORDER BY change_seq'),
                         {'p': world['pid']}).scalars().all()
    assert all(s is not None for s in seqs) and len(set(seqs)) == len(seqs)


def test_listener_receives_notify(world):
    from dataviz.db import listen
    assert listen.wait_available()
    lst = listen.listener()
    loop = asyncio.new_event_loop()
    try:
        ev = lst.waiter(world['pid'], loop)
        _edit(world, 'a.jpg', lambda rows: [dict(rows[0], class_id=world['dog'])] + rows[1:])

        async def wait():
            await asyncio.wait_for(ev.wait(), 5)
        loop.run_until_complete(wait())
        assert ev.is_set()
    finally:
        loop.close()


def test_cls_delete_restore_move_new(world):
    state = world['state']
    feed = live.feed_for(state)
    assert feed.live and feed.pull() is None                     # nothing changed yet
    s0, s1 = _sid(world, 'a.jpg', 0), _sid(world, 'a.jpg', 1)
    assert state.labels[s0] == 0
    # class change
    _edit(world, 'a.jpg', lambda rows: [dict(r, class_id=world['dog']) if r['class_id'] == world['cat'] else r
                                        for r in rows])
    p = feed.pull()
    assert p['cls'] == {s0: 1} and p['gen'] == 1 and not p['deleted'] and not p['moved']
    assert state.labels[s0] == 1 and state.samples[s0]['cls_name'] == 'dog' and state.edit_seq >= 1
    # delete
    gone = None

    def drop(rows):
        nonlocal gone
        gone = rows[1]
        return rows[:1]
    _edit(world, 'a.jpg', drop)
    p = feed.pull()
    assert p['deleted'] == [s1] and state.deleted[s1] and s1 not in state.by_image['a.jpg']
    # restore (the same row back)
    _edit(world, 'a.jpg', lambda rows: rows + [{k: gone[k] for k in ('id', 'class_id', 'geometry_type', 'geometry')}])
    p = feed.pull()
    assert p['restored'] == [s1] and not state.deleted[s1] and s1 in state.by_image['a.jpg']
    # geometry
    _edit(world, 'a.jpg', lambda rows: [dict(r, geometry={'x': .3, 'y': .3, 'w': .2, 'h': .2}) if r['id'] == gone['id']
                                        else r for r in rows])
    p = feed.pull()
    assert [m['id'] for m in p['moved']] == [s1] and s1 in state.stale_ids
    smp = state.samples[s1]
    assert p['moved'][0]['bbox'] == smp['bbox'] == [round(.3 * smp['img_w']), round(.3 * smp['img_h']),
                                                     round(.5 * smp['img_w']), round(.5 * smp['img_h'])]
    # a new box
    _edit(world, 'c.jpg', lambda rows: rows + [box(world['cat'], .6, .6, .2, .2)])
    p = feed.pull()
    assert len(p['new']) == 1 and p['new'][0]['image'] == 'c.jpg' and p['new'][0]['cls'] == 0
    assert p['pending_new'] == 1 and feed.pending_new == 1
    assert feed.pull() is None                                   # no change: no patch
    assert [g for g, _ in feed.log] == [1, 2, 3, 4, 5]


def test_bulk_change_asks_for_reindex(world, monkeypatch):
    monkeypatch.setattr(live, 'LIMIT', 1)
    feed = live.feed_for(world['state'])
    _edit(world, 'a.jpg', lambda rows: rows[:1])
    _edit(world, 'c.jpg', lambda rows: [])
    assert feed.pull() is None and feed.reindex['n'] == 2
    out = asyncio.run(feed.poll(0, 0))
    assert out['reindex'] is True


def test_class_list_change_asks_for_reindex(world):
    feed = live.feed_for(world['state'])
    world['st'].update_class(world['cat'], name='kitten')
    _edit(world, 'a.jpg', lambda rows: rows[:1])
    feed.pull()
    assert feed.reindex and feed.reindex['reason'] == 'classes changed'


def test_long_poll_wakes_and_times_out(world):
    from dataviz.db import listen
    assert listen.wait_available()
    feed = live.feed_for(world['state'])

    async def run():
        start = await feed.poll(None)
        assert start == {'gen': 0, 'live': True, 'pending_new': 0}
        t = time.time()
        idle = await feed.poll(0, 0.6)                          # nothing happens: answers at `wait`
        assert 0.5 < time.time() - t < 2.5 and idle['patches'] == []
        await asyncio.sleep(0.3)                                # the pump is waiting on the NOTIFY now

        def write():
            time.sleep(0.2)
            _edit(world, 'a.jpg', lambda rows: [dict(r, class_id=world['dog']) for r in rows])
        th = threading.Thread(target=write)
        th.start()
        t = time.time()
        got = await feed.poll(0, 10)
        th.join()
        assert time.time() - t < 1.5 and got['gen'] == 1 and got['patches'][0]['cls']
        old = await feed.poll(-5, 0)
        assert old.get('resync') is True or old['patches']
        feed.stop()
    asyncio.run(run())


def test_api_live_false_on_local_session(pg, tmp_path):
    alice = account('alice')
    a = make_yolo(tmp_path / 'dsA')
    sid = viz_session(alice['id'], 'A', a)
    ctx, app = make_viz_app(tmp_path / 'ws')
    c = client(app)
    assert login(c, 'alice').status_code == 200
    r = c.get(f'/s/{sid}/api/live?since=0&wait=1')
    assert r.status_code == 200 and r.json() == {'live': False, 'gen': 0}
    assert r.headers['cache-control'] == 'no-store'
    assert c.get(f'/s/{sid}/api/meta').status_code == 200


def test_union_with_labeling_member(world, tmp_path):
    from dataviz.data.sources import LocalSource
    from dataviz.data.union import Member, UnionSource
    from dataviz.state import State
    b = make_yolo(tmp_path / 'dsB', classes=('dog', 'bird'), boxes=(('b1', '0 0.5 0.5 0.3 0.3\n'),))
    src = world['state'].source
    members = [Member('B', 'sid-b', 'B', LocalSource(b), 0), Member('L', 'sid-l', 'L', src, 1)]
    union = UnionSource({'id': 'u1', 'name': 'B + L'}, members, tmp_path / 'ws2')
    assert union.live
    state = State(union, None, {'none': None}, default_feature='none', workspace_root=tmp_path / 'ws2')
    state._index_job(_Job())
    assert state.classes == ['dog', 'bird', 'cat']               # B's classes first, the task's 'cat' after
    feed = live.feed_for(state)
    assert feed.live and not feed.reindex
    ids = state.by_image['L/a.jpg']
    cat_box = next(i for i in ids if state.samples[i]['cls_name'] == 'cat')
    assert cat_box >= 1                                          # after B's box: offset
    _edit(world, 'a.jpg', lambda rows: [dict(r, class_id=world['dog']) for r in rows])
    p = feed.pull()
    assert p['cls'] == {cat_box: 0}                              # the task's 'dog' = union class 0
    assert state.labels[cat_box] == 0
    _edit(world, 'c.jpg', lambda rows: rows + [box(world['cat'], .6, .6, .2, .2)])
    p = feed.pull()
    assert p['new'][0]['image'] == 'L/c.jpg' and p['new'][0]['cls'] == 2


def test_new_box_is_placed_near_its_neighbours(lab, tmp_path):
    """A new box gets a provisional dot: embedded with the shown feature, placed by its nearest projected boxes."""
    import numpy as np
    from dataviz.ml import features as feats
    from dataviz.state import State, projection_key
    pid, st, tasks, mids = make_project(lab)
    det = tasks['object_detection']
    cat, dog = (c['id'] for c in det['classes'])
    put(st, mids['a.jpg'], det['id'], [box(cat, .1, .1, .2, .3), box(dog, .5, .4, .25, .25)])
    put(st, mids['c.jpg'], det['id'], [box(dog, .2, .2, .4, .4)])
    from types import SimpleNamespace
    centers = np.linspace(0, 1, 16)

    def fn(paths, samples, progress):              # box centre, RBF-encoded: cosine tells positions apart
        out = []
        for smp in samples:
            x1, y1, x2, y2 = smp['bbox']
            cx, cy = (x1 + x2) / 2 / smp['img_w'], (y1 + y2) / 2 / smp['img_h']
            out.append(np.concatenate([np.exp(-(cx - centers) ** 2 / .005), np.exp(-(cy - centers) ** 2 / .005)]))
        return np.asarray(out, np.float32)
    reg = {'geom': SimpleNamespace(key='geom', label='pos', group='basic', note='', text_model=None, kind=None,
                                   metric='cosine', fn=fn)}
    state = State(LabelingSource(lab, pid, det['id']), tmp_path / 'ws', feats.FeatureRegistry(reg),
                  default_feature='geom')
    state._index_job(_Job())
    state.get_features('geom', _Job())
    key = projection_key('geom', 'pca', {}, None)
    xy = np.asarray([[0, 0], [10, 10], [20, 0]], np.float32)
    state._store_projection(key, {'id': np.arange(3), 'xy': xy, 'info': {}})
    feed = live.feed_for(state)
    feed.proj = key
    # same geometry as the dog box of a.jpg: lands on that box's dot
    put(st, mids['c.jpg'], det['id'], _anns(st, mids['c.jpg'], det['id']) + [box(cat, .5, .4, .25, .25)])
    p = feed.pull()
    assert len(p['new']) == 1
    feed._to_place = {b['ann']: b for b in p['new']}
    assert feed._place_job(_Job()) == 1 and feed._to_place == {}
    placed = feed.log[-1][1]['placed']
    assert len(placed) == 1 and placed[0]['proj'] == key and placed[0]['sim'] > .99
    assert abs(placed[0]['x'] - 10) < 1 and abs(placed[0]['y'] - 10) < 1
