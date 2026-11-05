"""Promote to labeling (data/promote.py, POST /s/<union>/api/promote): content dedup across members, checksum and
origins on the media, labels preferred by member priority, an existing-project rerun skips known content, and the
live visualize session opens from the union's workspace with no feature computation."""

import time

import pytest
from sqlalchemy import text

from authutil import account, client, login
from conftest import make_jpeg
from vizutil import _Job, geom_registry, union_session, viz_session

SAME = [(10, 200, 30), (200, 10, 30), (30, 10, 200)]          # the 3 images both datasets hold (same bytes)


def _yolo(root, classes, items):
    (root / 'labels').mkdir(parents=True)
    (root / 'classes.txt').write_text('\n'.join(classes) + '\n')
    for name, color, label in items:
        make_jpeg(root / 'images' / f'{name}.jpg', 64, 48, color=color)
        if label is not None:
            (root / 'labels' / f'{name}.txt').write_text(label)
    return root


def _app(root, calls):
    from dataviz.core import config as vconfig
    from dataviz.core.jobs import JobManager
    from dataviz.data import connections
    from dataviz.labeling.projects import ProjectRegistry
    from dataviz.ml import features as feats
    from dataviz.server.app import Ctx, create_app
    from dataviz.state import State
    cfg = vconfig.load_config(str(vconfig.DEFAULT_CONFIG))
    cfg['logging']['http'] = False
    reg = feats.FeatureRegistry(geom_registry(calls))
    ctx = Ctx(None, registry=reg, datasets=connections.Registry(root), cfg=cfg, jobs=JobManager(),
              labeling=ProjectRegistry(root / 'labeling'))

    def build(ds, **kw):
        src = ctx.datasets.source(ds, cfg['s3'], ctx.labeling)
        st = State(src, None, reg, default_feature='geom', jobs=ctx.jobs, cfg=cfg, workspace_root=root,
                   dataset_id=ds['id'])
        st._index_job(_Job())
        return st
    ctx.open_dataset = build
    return ctx, create_app(ctx)


def _wait(c, job_id):
    t0 = time.time()
    while time.time() - t0 < 60:
        j = c.get(f'/api/jobs/{job_id}').json()
        if j.get('status') in ('done', 'error'):
            assert j['status'] == 'done', j
            return j['result']
        time.sleep(0.05)
    raise TimeoutError(job_id)


@pytest.fixture
def world(pg, tmp_path, monkeypatch):
    a = _yolo(tmp_path / 'dsA', ('cat', 'dog'), [
        ('s1', SAME[0], '0 0.5 0.5 0.4 0.4\n'), ('s2', SAME[1], '1 0.3 0.3 0.2 0.2\n'), ('s3', SAME[2], None),
        ('a1', (90, 90, 90), '0 0.4 0.4 0.3 0.3\n')])
    b = _yolo(tmp_path / 'dsB', ('dog', 'bird'), [
        ('x1', SAME[0], '1 0.6 0.6 0.2 0.2\n'), ('x2', SAME[1], '0 0.3 0.3 0.2 0.2\n'),
        ('x3', SAME[2], '1 0.5 0.5 0.5 0.5\n'), ('b1', (140, 20, 90), '1 0.5 0.5 0.3 0.3\n')])
    alice = account('alice')
    A = viz_session(alice['id'], 'A', a)
    B = viz_session(alice['id'], 'B', b)
    U = union_session(alice['id'], 'A vs B', [(A, 'A'), (B, 'B')])
    calls = []
    ctx, app = _app(tmp_path / 'ws', calls)
    c = client(app)
    assert login(c, 'alice').status_code == 200
    return {'ctx': ctx, 'c': c, 'U': U, 'A': A, 'calls': calls, 'alice': alice, 'mp': monkeypatch}


def _media(pid):
    from dataviz import db
    with db.connect() as c:
        return {r.file_name: r for r in c.execute(text('SELECT file_name, checksum, meta, size_bytes, width '
                                                       'FROM lbl.media WHERE project_id = :p'), {'p': pid})}


def _anns(ctx, pid, tid):
    st = ctx.labeling.store(pid)
    out = {}
    for m, rows in st.iter_task_export(tid):
        out[m['file_name']] = [(r['class_id'], r['geometry']) for r in rows]
    return out


def test_promote_dedup_labels_and_live_session(world):
    ctx, c, U = world['ctx'], world['c'], world['U']
    ust = ctx.state_for(U)
    ust.get_features('geom', _Job())                                  # the union's features: computed once
    assert sorted(world["calls"]) == [3, 4]
    r = c.post(f'/s/{U}/api/promote', json={'name': 'Promoted', 'dedup': 'hash', 'labels': 'prefer'})
    assert r.status_code == 200, r.text
    res = _wait(c, r.json()['job_id'])
    assert res['n_media'] == 5 and res['n_skipped_dup'] == 3 and res['classes'] == ['cat', 'dog', 'bird']
    media = _media(res['project_id'])
    assert set(media) == {'A__s1.jpg', 'A__s2.jpg', 'A__s3.jpg', 'A__a1.jpg', 'B__b1.jpg'}
    s1 = media['A__s1.jpg']
    assert s1.checksum and len(s1.checksum) == 32 and s1.size_bytes > 0 and s1.width == 64
    assert s1.meta['origins'] == ['A', 'B'] and s1.meta['origin'] == 'A' and s1.meta['union_id'] == U
    assert sorted(s1.meta['origin_names']) == ['A/s1.jpg', 'B/x1.jpg']
    assert media['B__b1.jpg'].meta['origins'] == ['B']
    # prefer: A's boxes for the shared content; s3 (no box in A) takes B's
    pid, tid = res['project_id'], res['task_id']
    task = ctx.labeling.store(pid).task(tid)
    names = {cl['id']: cl['name'] for cl in task['classes']}
    anns = _anns(ctx, pid, tid)
    assert [names[k] for k, _ in anns['A__s1.jpg']] == ['cat']
    assert anns['A__s1.jpg'][0][1]['w'] == pytest.approx(0.4)
    assert [names[k] for k, _ in anns['A__s3.jpg']] == ['bird']
    assert res['n_annotations'] == 5
    # the live session: opens indexed from the union's workspace, every feature row transplanted
    vid = res['viz_session_id']
    assert vid
    world['calls'].clear()
    live = ctx.state_for(vid)
    assert live.ready and len(live.samples) == 5
    emb = live.get_features('geom', _Job())
    assert world['calls'] == [] and emb.shape == (5, 2)
    # a relabel in the labeling session: the re-index takes every image from the previous workspace
    from dataviz.data import indexer
    decoded = []
    real = indexer._index_one
    world['mp'].setattr(indexer, '_index_one', lambda src, label, *a, **k: (decoded.append(label),
                                                                             real(src, label, *a, **k))[1])
    st = ctx.labeling.store(pid)
    mid = {m['file_name']: m['id'] for m in st.media_for_import()}['A__a1.jpg']
    cur = st.get_annotations(mid)
    rows = [{**a, 'class_id': [k for k, n in names.items() if n == 'dog'][0]} for a in cur['annotations']]
    st.put_annotations(mid, tid, rows, cur['versions'][tid])
    ctx.evict(vid)
    relabeled = ctx.state_for(vid)
    assert relabeled.cache_dir != live.cache_dir and decoded == []
    assert [x['cls_name'] for x in relabeled.samples if x['image'] == 'A__a1.jpg'] == ['dog']
    world['calls'].clear()
    relabeled.get_features('geom', _Job())
    assert world['calls'] == []
    # existing-project rerun: every content is already there
    r = c.post(f'/s/{U}/api/promote', json={'target': 'existing', 'project_id': pid, 'task_id': tid,
                                            'create_viz': False})
    again = _wait(c, r.json()['job_id'])
    assert again['n_media'] == 0 and again['n_existing'] == 5 and len(_media(pid)) == 5


def test_promote_without_dedup_and_access(world):
    ctx, c, U = world['ctx'], world['c'], world['U']
    r = c.post(f'/s/{U}/api/promote', json={'name': 'All copies', 'dedup': 'none', 'labels': 'all',
                                            'create_viz': False})
    res = _wait(c, r.json()['job_id'])
    assert res['n_media'] == 8 and res['viz_session_id'] is None
    media = _media(res['project_id'])
    assert 'A__s1.jpg' in media and 'B__x1.jpg' in media
    assert media['A__s1.jpg'].checksum == media['B__x1.jpg'].checksum      # same bytes, kept twice
    assert c.post(f'/s/{U}/api/promote', json={'dedup': 'maybe'}).status_code == 400
    # not a combined dataset: 409; a viewer (not owner): 403
    assert c.post(f'/s/{world["A"]}/api/promote', json={}).status_code == 409
    from vizutil import share
    bob = account('bob')
    share(U, world['alice']['id'], **{bob['id']: 'viewer'})
    share(world['A'], world['alice']['id'], **{bob['id']: 'viewer'})
    from dataviz.data import sessions
    B = [p['id'] for p in ctx.datasets.dataset(U)['parts'] if p['alias'] == 'B'][0]
    sessions.set_sharing(B, 'private', [{'user_id': bob['id'], 'role': 'viewer'}], world['alice']['id'])
    cb = client(c.app)
    assert login(cb, 'bob').status_code == 200
    assert cb.post(f'/s/{U}/api/promote', json={}).status_code == 403


def test_promote_twice_takes_a_free_name(world):
    """The dialog pre-fills the same default name every time: a second promote gets '<name> (2)', not an error."""
    c, U = world['c'], world['U']
    first, second = (_wait(c, c.post(f'/s/{U}/api/promote', json={'name': 'Twice'}).json()['job_id'])
                     for _ in range(2))
    names = {world['ctx'].labeling.get(r['project_id'])['name'] for r in (first, second)}
    assert names == {'Twice', 'Twice (2)'}


@pytest.mark.parametrize('o', range(1, 9))
def test_orient_box_matches_exif_transpose(tmp_path, o):
    """orient_box() puts a box where PIL's exif_transpose puts its pixels, for every EXIF orientation."""
    from PIL import Image, ImageOps
    from dataviz.data.promote import orient_box
    W, H = 80, 50
    im = Image.new('RGB', (W, H), (0, 0, 0))
    box = {'x': .1, 'y': .2, 'w': .25, 'h': .3}
    x1, y1 = round(box['x'] * W), round(box['y'] * H)
    im.paste((255, 255, 255), (x1, y1, x1 + round(box['w'] * W), y1 + round(box['h'] * H)))
    ex = Image.Exif()
    ex[0x0112] = o
    im.save(tmp_path / 'x.jpg', exif=ex, quality=100)
    out = ImageOps.exif_transpose(Image.open(tmp_path / 'x.jpg')).convert('L')
    g = orient_box(box, o)
    ow, oh = out.size
    cx, cy = (g['x'] + g['w'] / 2) * ow, (g['y'] + g['h'] / 2) * oh
    assert out.getpixel((int(cx), int(cy))) > 200                      # the box centre is on the white patch
    assert out.getpixel((int((g['x'] + g['w'] + .1) * ow) % ow, int(cy))) < 60 or g['x'] + g['w'] + .1 > 1
