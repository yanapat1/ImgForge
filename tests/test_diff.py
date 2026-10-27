"""Diff engine: known answers on tiny synthetic unions, update() == run(), and the 500k-box speed budget."""
import json
import os
import time

import numpy as np

from dataviz.data import diff as D

SLOW = 3 if os.environ.get('PYTEST_XDIST_WORKER') else 1
B0 = (0.1, 0.1, 0.3, 0.3)


def build(sides):
    """sides: [{inner name: (key, [(cls, (x1, y1, x2, y2)[, alive]), ...])}, ...] per member (0, 1, ...)."""
    images, img_origin, img_key = [], [], []
    origin, img_idx, xyxy, cls, alive = [], [], [], [], []
    for m, imgs in enumerate(sides):
        for name, (key, boxes) in imgs.items():
            i = len(images)
            images.append(f'm{m}/{name}')
            img_origin.append(m)
            img_key.append(key)
            for bx in boxes:
                origin.append(m)
                img_idx.append(i)
                cls.append(bx[0])
                xyxy.append(bx[1])
                alive.append(bx[2] if len(bx) > 2 else True)
    return D.DiffInput(origin=np.array(origin, np.uint8), img_idx=np.array(img_idx, np.int32),
                       xyxy=np.array(xyxy, np.float32).reshape(-1, 4), cls=np.array(cls, np.int32),
                       alive=np.array(alive, bool), images=images, img_origin=np.array(img_origin, np.uint8),
                       img_key=img_key)


def shifted(b, dx):
    return (b[0] + dx, b[1], b[2] + dx, b[3])


def test_name_key():
    assert D.name_key('a/Sub/X.JPG', 'rel') == 'sub/x.jpg'
    assert D.name_key('a/Sub/X.JPG', 'stem') == 'sub/x'
    assert D.name_key('a/Sub/X.JPG', 'basename') == 'x'


def test_identical_is_same():
    side = {'x.jpg': ('h1', [(0, B0), (1, (0.5, 0.5, 0.9, 0.9))]), 'empty.jpg': ('h2', [])}
    inp = build([side, side])
    r = D.run(inp, 0, 1)
    assert (r.status == D.BOX_SAME).all() and (r.img_status == D.IMG_SAME).all()
    assert r.pair.tolist() == [2, 3, 0, 1] and (r.img_how == 1).all()
    assert r.summary['images']['same'] == 2 and r.summary['boxes']['same'] == 2


def test_moved_reclass_added():
    a = {'x.jpg': ('h', [(0, B0), (1, (0.5, 0.5, 0.9, 0.9))])}
    b = {'x.jpg': ('h', [(0, shifted(B0, 0.02)), (2, (0.5, 0.5, 0.9, 0.9)), (0, (0.0, 0.6, 0.1, 0.7))])}
    r = D.run(build([a, b]), 0, 1)
    iou = float(r.iou[0])
    assert 0.75 < iou < 0.85
    assert r.status.tolist() == [D.BOX_MOVED, D.BOX_RECLASS, D.BOX_MOVED, D.BOX_RECLASS, D.BOX_UNMATCHED]
    assert r.img_status.tolist() == [D.IMG_LABELS_CHANGED] * 2
    s = r.summary
    assert s['boxes'] == {'same': 0, 'moved': 1, 'reclass': 1, 'removed': 0, 'added': 1,
                          'image_only_a': 0, 'image_only_b': 0}
    assert s['confusion'][1][2] == 1 and sum(map(sum, s['confusion'])) == 1
    c = s['classes']
    assert [x['cls'] for x in c] == [0, 1, 2]
    assert (c[0]['a'], c[0]['b'], c[0]['added']) == (1, 2, 1)
    assert (c[1]['reclass_out'], c[2]['reclass_in']) == (1, 1)


def test_reclass_moved_and_removed():
    a = {'x.jpg': ('h', [(0, B0), (1, (0.6, 0.6, 0.8, 0.8))])}
    b = {'x.jpg': ('h', [(3, shifted(B0, 0.02))])}
    r = D.run(build([a, b]), 0, 1)
    assert r.status.tolist() == [D.BOX_RECLASS_MOVED, D.BOX_UNMATCHED, D.BOX_RECLASS_MOVED]
    assert r.summary['boxes']['removed'] == 1 and r.summary['boxes']['reclass'] == 1


def test_only_a_image():
    a = {'x.jpg': ('h1', [(0, B0)]), 'gone.jpg': ('h9', [(0, B0), (1, B0)])}
    b = {'x.jpg': ('h1', [(0, B0)]), 'new.jpg': ('h8', [])}
    r = D.run(build([a, b]), 0, 1)
    assert r.img_status.tolist() == [D.IMG_SAME, D.IMG_ONLY_A, D.IMG_SAME, D.IMG_ONLY_B]
    assert r.status.tolist() == [D.BOX_SAME, D.BOX_IMAGE_ONLY, D.BOX_IMAGE_ONLY, D.BOX_SAME]
    s = r.summary
    assert s['images']['only_a'] == 1 and s['images']['only_b'] == 1 and s['boxes']['image_only_a'] == 2


def test_renamed_identical_file_matches_by_hash():
    a = {'old_name.jpg': ('h1', [(0, B0)])}
    b = {'dir/new_name.png': ('h1', [(0, B0)])}
    r = D.run(build([a, b]), 0, 1)
    assert r.img_status.tolist() == [D.IMG_SAME] * 2 and r.img_how.tolist() == [1, 1]
    assert r.img_pair.tolist() == [1, 0]


def test_same_name_different_pixels():
    a = {'x.jpg': ('h1', [(0, B0)]), 'y.jpg': (None, [])}
    b = {'x.png': ('h2', [(0, B0)]), 'y.jpg': (None, [])}      # stem mode: x.jpg ~ x.png
    r = D.run(build([a, b]), 0, 1)
    assert r.img_status.tolist() == [D.IMG_PIXELS_CHANGED] * 4 and r.img_how.tolist() == [2] * 4
    assert r.status.tolist() == [D.BOX_SAME, D.BOX_SAME]       # boxes still matched
    assert r.summary['images']['pixels_changed'] == 2
    r = D.run(build([a, b]), 0, 1, {'name_key': 'rel'})         # rel: x.jpg != x.png
    assert r.img_status.tolist() == [D.IMG_ONLY_A, D.IMG_PIXELS_CHANGED, D.IMG_ONLY_B, D.IMG_PIXELS_CHANGED]


def test_ambiguous_names_stay_unmatched():
    a = {'d1/x.jpg': ('h1', []), 'd2/x.jpg': ('h2', [])}
    b = {'d3/x.jpg': ('h3', [])}
    r = D.run(build([a, b]), 0, 1, {'name_key': 'basename'})
    assert r.img_status.tolist() == [D.IMG_ONLY_A, D.IMG_ONLY_A, D.IMG_ONLY_B]


def test_duplicate_hashes_pair_by_name_first():
    a = {'p.jpg': ('h', [(0, B0)]), 'q.jpg': ('h', [(1, B0)]), 'r.jpg': ('h', [])}
    b = {'z.jpg': ('h', [(0, B0)]), 'q.jpg': ('h', [(1, B0)])}
    r = D.run(build([a, b]), 0, 1)
    # q pairs with q by name, then p (first sorted leftover) with z; r is a duplicate left over
    assert r.img_pair.tolist() == [3, 4, -1, 0, 1]
    assert r.img_dup.tolist() == [True] * 5
    assert r.img_status.tolist() == [D.IMG_SAME, D.IMG_SAME, D.IMG_ONLY_A, D.IMG_SAME, D.IMG_SAME]
    assert r.summary['images']['dup_a'] == 3 and r.summary['images']['dup_b'] == 2


def test_hungarian_beats_greedy():
    """a1-b1 is the best single pair, but taking it leaves a2 without a partner (a2-b2 < 0.5)."""
    def full(x1, x2):
        return (x1, 0.0, x2, 1.0)

    A = [full(0.30, 0.70), full(0.36, 0.76), full(0.0, 0.1)]
    B = [full(0.32, 0.72), full(0.22, 0.62), full(0.0, 0.1)]
    iou = D._iou(np.array(A), np.array(B))
    assert iou[0, 0] > 0.9 and iou[0, 1] > 0.6 and iou[1, 0] > 0.8 and iou[1, 1] < 0.5
    same = np.ones((3, 3), bool)
    assert len(D._assign(iou, same, 0.5, greedy=True)[0]) == 2
    r, c = D._assign(iou, same, 0.5, greedy=False)
    assert sorted(zip(r.tolist(), c.tolist())) == [(0, 1), (1, 0), (2, 2)]
    res = D.run(build([{'x.jpg': ('h', [(0, b) for b in A])}, {'x.jpg': ('h', [(0, b) for b in B])}]), 0, 1)
    assert res.pair.tolist() == [4, 3, 5, 1, 0, 2]
    assert res.status.tolist() == [D.BOX_MOVED, D.BOX_MOVED, D.BOX_SAME] * 2


def test_deleted_boxes_are_na_and_not_counted():
    a = {'x.jpg': ('h', [(0, B0), (1, (0.5, 0.5, 0.9, 0.9), False)])}
    b = {'x.jpg': ('h', [(0, B0)])}
    r = D.run(build([a, b]), 0, 1)
    assert r.status.tolist() == [D.BOX_SAME, D.BOX_NA, D.BOX_SAME] and r.pair[1] == -1
    assert r.img_status.tolist() == [D.IMG_SAME] * 2           # the deleted box is not a label change
    assert r.summary['classes'][1]['a'] == 0 and r.summary['boxes']['removed'] == 0


def test_other_members_are_na():
    s = {'x.jpg': ('h', [(0, B0)])}
    r = D.run(build([s, s, s]), 0, 2)
    assert r.status.tolist() == [D.BOX_SAME, D.BOX_NA, D.BOX_SAME] and r.img_status.tolist() == [3, 0, 3]


def _random_union(seed, n_img=60):
    rng = np.random.default_rng(seed)
    a, b = {}, {}
    for i in range(n_img):
        boxes = [(int(rng.integers(0, 4)), tuple(np.r_[xy, xy + rng.uniform(0.05, 0.3, 2)]))
                 for xy in rng.uniform(0, 0.6, (int(rng.integers(0, 6)), 2))]
        kind = i % 6
        a[f'img{i}.jpg'] = (f'h{i}', boxes)
        if kind == 0:
            continue                                                   # only in A
        bb = list(boxes)
        if kind == 1 and bb:
            bb[0] = (bb[0][0], shifted(bb[0][1], 0.02))
        if kind == 2 and bb:
            bb[-1] = ((bb[-1][0] + 1) % 4, bb[-1][1])
        if kind == 3:
            bb.append((1, (0.7, 0.7, 0.8, 0.8)))
        b[f'img{i}.jpg'] = (f'h{i}' if kind != 4 else f'g{i}', bb[::-1])
    return build([a, b])


def _same(r1, r2):
    for f in ('img_status', 'img_pair', 'img_how', 'img_dup', 'status', 'pair', 'iou'):
        assert np.array_equal(getattr(r1, f), getattr(r2, f)), f
    assert r1.summary == r2.summary


def test_update_equals_full_run():
    inp = _random_union(0)
    res = D.run(inp, 0, 1)
    before = res.status.copy()
    # edit three images: move a box, change a class, delete a box (A side and B side)
    edited = []
    for m, i in ((0, 7), (1, 0), (0, 13)):
        img = [k for k, n in enumerate(inp.images) if n == f'm{m}/img{i}.jpg']
        if not img:
            continue
        bx = np.flatnonzero(inp.img_idx == img[0])
        if not len(bx):
            continue
        edited.append(img[0])
        inp.xyxy[bx[0]] += 0.03
        inp.cls[bx[-1]] = (inp.cls[bx[-1]] + 1) % 4
        if len(bx) > 2:
            inp.alive[bx[1]] = False
    assert edited
    changed = D.update(res, inp, edited)
    full = D.run(inp, 0, 1)
    _same(res, full)
    diff_ids = np.flatnonzero(before != full.status)
    assert changed == {int(k): int(full.status[k]) for k in diff_ids} and changed
    assert D.update(res, inp, []) == {}


def test_summary_is_json():
    r = D.run(_random_union(1), 0, 1)
    s = json.loads(json.dumps(r.summary))
    assert s == r.summary and len(s['confusion']) == len(s['classes'])
    assert s['images']['only_a'] == 10 and s['images']['pixels_changed'] == 10


def test_perf_500k_boxes():
    """500k boxes over 125k image pairs, 95% identical: under 2 s."""
    rng = np.random.default_rng(0)
    n_pair, per = 125_000, 2
    nb = n_pair * per                                          # boxes per side
    img_a = np.repeat(np.arange(n_pair), per)
    xy = rng.uniform(0, 0.7, (nb, 2)).astype(np.float32)
    xyxy_a = np.concatenate([xy, xy + rng.uniform(0.05, 0.3, (nb, 2)).astype(np.float32)], 1)
    cls_a = rng.integers(0, 10, nb).astype(np.int32)
    xyxy_b, cls_b = xyxy_a.copy(), cls_a.copy()
    changed = rng.random(n_pair) < 0.05
    cb = np.flatnonzero(np.repeat(changed, per))
    xyxy_b[cb[::2, None], [0, 2]] += 0.01
    cls_b[cb[1::2]] = (cls_b[cb[1::2]] + 1) % 10
    perm = rng.permutation(nb)                                 # B boxes in another order
    names = [f'img{i}.jpg' for i in range(n_pair)]
    inp = D.DiffInput(origin=np.r_[np.zeros(nb), np.ones(nb)].astype(np.uint8),
                      img_idx=np.r_[img_a, img_a[perm] + n_pair].astype(np.int32),
                      xyxy=np.r_[xyxy_a, xyxy_b[perm]], cls=np.r_[cls_a, cls_b[perm]],
                      alive=np.ones(2 * nb, bool), images=[f'a/{n}' for n in names] + [f'b/{n}' for n in names],
                      img_origin=np.r_[np.zeros(n_pair), np.ones(n_pair)].astype(np.uint8),
                      img_key=[f'k{i}' for i in range(n_pair)] * 2)
    took = float('inf')
    for _ in range(2):                                         # best of two (a busy machine can stall one run)
        t0 = time.perf_counter()
        r = D.run(inp, 0, 1)
        took = min(took, time.perf_counter() - t0)
    s = r.summary
    assert s['images']['same'] == n_pair - changed.sum() and s['images']['labels_changed'] == changed.sum()
    assert s['boxes']['moved'] + s['boxes']['reclass'] == len(cb)
    assert took < 2.0 * SLOW, f'diff of {2 * nb:,} boxes took {took:.2f}s'
    print(f'\ndiff of {2 * nb:,} boxes / {n_pair:,} image pairs: {took:.2f}s')
