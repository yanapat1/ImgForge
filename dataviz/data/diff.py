"""
Diff engine: what changed between two members (a, b) of a union, image by image and box by box. Pure numpy/scipy:
the input is flat arrays (DiffInput), the output flat status arrays (DiffResult) plus a JSON summary.

Image matching (O(n), dicts):
    1. images with the same content key (hash) are the same picture. Inside one hash group (copies on a side)
       pair equal name keys first, then the rest in sorted-name order; every copy of a hash seen >1 times on its
       side is flagged img_dup. Hash pairs end SAME or LABELS_CHANGED (decided by the boxes).
    2. still-unpaired images whose name key is unique on both sides (among unpaired ones) -> PIXELS_CHANGED.
    3. the rest: ONLY_A / ONLY_B, and their boxes IMAGE_ONLY.
    name_key modes: rel (inner path), stem (inner path without extension), basename (file name without extension).

Box matching, per paired image:
    fast path   a per-box uint64 hash of (cls, round(xyxy*1e4)), aggregated per image as (count, sum, xor) with
                reduceat. Equal triples -> every box SAME, paired by sorted hash, fully vectorised (most pairs).
    slow path   the other pairs: IoU matrix, score = iou + 0.5*same class, linear_sum_assignment (greedy when
                nA*nB > 1e6); matches need iou >= iou_match; iou >= iou_same counts as not moved.
                SAME / MOVED / RECLASS / RECLASS_MOVED, unmatched alive boxes UNMATCHED (removed on A, added on B).
    Deleted boxes (alive False) and boxes of other members are BOX_NA and ignored everywhere.

update() reruns the same code path for the image pairs touching some images (after an edit) and refreshes the
summary with bincounts, so it equals a full run().
"""

import posixpath
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linear_sum_assignment

IMG_ONLY_A, IMG_ONLY_B, IMG_SAME, IMG_LABELS_CHANGED, IMG_PIXELS_CHANGED = 1, 2, 3, 4, 5   # 0 = not in this pair
BOX_NA, BOX_SAME, BOX_MOVED, BOX_RECLASS, BOX_RECLASS_MOVED, BOX_UNMATCHED, BOX_IMAGE_ONLY = 0, 1, 2, 3, 4, 5, 6
DEFAULTS = {'iou_match': 0.5, 'iou_same': 0.98, 'name_key': 'stem'}   # name_key: rel | stem | basename
HOW_NONE, HOW_HASH, HOW_NAME = 0, 1, 2
GREEDY_ABOVE = 1_000_000                     # nA*nB above this: greedy instead of Hungarian


@dataclass
class DiffInput:
    origin: np.ndarray      # uint8[N] member index of each box (sample)
    img_idx: np.ndarray     # int32[N] index into `images` of the box's image
    xyxy: np.ndarray        # float32[N,4] normalized 0..1 box corners
    cls: np.ndarray         # int32[N] (already unified class ids)
    alive: np.ndarray       # bool[N] (False = deleted box -> BOX_NA, ignored)
    images: list            # union image names '<alias>/<inner name>' (ALL images incl. ones with no boxes)
    img_origin: np.ndarray  # uint8[n_img]
    img_key: list           # content-hash key per image or None (None -> can only match by name)


@dataclass
class DiffResult:
    a: int
    b: int
    params: dict
    img_status: np.ndarray  # uint8[n_img]
    img_pair: np.ndarray    # int32[n_img] matched image index on the other side, -1
    img_how: np.ndarray     # uint8[n_img] 0 none, 1 hash, 2 name
    img_dup: np.ndarray     # bool[n_img] image whose hash appears >1 times on its side
    status: np.ndarray      # uint8[N]
    pair: np.ndarray        # int32[N] matched box id on the other side, -1
    iou: np.ndarray         # float16[N] IoU with pair (0 when none)
    summary: dict = field(default_factory=dict)


def name_key(name: str, mode: str) -> str:
    """Matching key of a union image name: the inner name (after the first '/'), lowercased."""
    inner = name.replace('\\', '/').split('/', 1)[-1].lower()
    if mode == 'rel':
        return inner
    if mode == 'basename':
        inner = posixpath.basename(inner)
    elif mode != 'stem':
        raise ValueError(f'unknown name_key mode {mode!r}')
    return posixpath.splitext(inner)[0]


# ---------------------------------------------------------------- images

def _match_images(inp, a, b, mode):
    n = len(inp.images)
    org = np.asarray(inp.img_origin)
    pair = np.full(n, -1, np.int32)
    how = np.zeros(n, np.uint8)
    dup = np.zeros(n, bool)
    side = {a: np.flatnonzero(org == a).tolist(), b: np.flatnonzero(org == b).tolist()}
    keys = {i: name_key(inp.images[i], mode) for i in side[a] + side[b]}

    def link(i, j, h):
        pair[i], pair[j] = j, i
        how[i] = how[j] = h

    groups = {}                                            # hash -> ([A images], [B images])
    for s, lst in ((0, side[a]), (1, side[b])):
        for i in lst:
            k = inp.img_key[i]
            if k is not None:
                groups.setdefault(k, ([], []))[s].append(i)
    for ga, gb in groups.values():
        if len(ga) > 1:
            dup[ga] = True
        if len(gb) > 1:
            dup[gb] = True
        if not ga or not gb:
            continue
        if len(ga) == 1 and len(gb) == 1:
            link(ga[0], gb[0], HOW_HASH)
            continue
        by_name = {}
        for j in gb:
            by_name.setdefault(keys[j], []).append(j)
        rest_a, used = [], set()
        for i in sorted(ga, key=lambda i: inp.images[i]):
            cand = by_name.get(keys[i])
            if cand:
                j = cand.pop(0)
                used.add(j)
                link(i, j, HOW_HASH)
            else:
                rest_a.append(i)
        rest_b = sorted((j for j in gb if j not in used), key=lambda j: inp.images[j])
        for i, j in zip(rest_a, rest_b):
            link(i, j, HOW_HASH)

    # unpaired images with a name key unique on both sides
    cnt_a, cnt_b = {}, {}
    for cnt, lst in ((cnt_a, side[a]), (cnt_b, side[b])):
        for i in lst:
            if pair[i] < 0:
                cnt.setdefault(keys[i], []).append(i)
    for k, la in cnt_a.items():
        lb = cnt_b.get(k)
        if len(la) == 1 and lb is not None and len(lb) == 1:
            link(la[0], lb[0], HOW_NAME)

    status = np.zeros(n, np.uint8)
    ia, ib = np.array(side[a], np.int64), np.array(side[b], np.int64)
    status[ia] = IMG_ONLY_A
    status[ib] = IMG_ONLY_B
    status[how == HOW_NAME] = IMG_PIXELS_CHANGED
    status[how == HOW_HASH] = IMG_SAME                     # refined by the boxes
    return status, pair, how, dup


# ---------------------------------------------------------------- boxes

_M1, _M2, _GOLD = np.uint64(0xBF58476D1CE4E5B9), np.uint64(0x94D049BB133111EB), np.uint64(0x9E3779B97F4A7C15)


def _mix(z):
    """splitmix64 finaliser, uint64 wrap-around."""
    z = z + _GOLD
    z = (z ^ (z >> np.uint64(30))) * _M1
    z = (z ^ (z >> np.uint64(27))) * _M2
    return z ^ (z >> np.uint64(31))


def _box_hash(cls, xyxy):
    q = np.rint(np.asarray(xyxy, np.float64) * 1e4).astype(np.int64).view(np.uint64)
    h = _mix(np.asarray(cls, np.int64).view(np.uint64))
    for j in range(4):
        h = _mix(h ^ q[:, j])
    return h


def _iou(A, B):
    """IoU matrix (nA, nB) of xyxy boxes."""
    A, B = np.asarray(A, np.float64), np.asarray(B, np.float64)
    iw = np.minimum(A[:, None, 2], B[None, :, 2]) - np.maximum(A[:, None, 0], B[None, :, 0])
    ih = np.minimum(A[:, None, 3], B[None, :, 3]) - np.maximum(A[:, None, 1], B[None, :, 1])
    inter = np.clip(iw, 0, None) * np.clip(ih, 0, None)
    area_a = (A[:, 2] - A[:, 0]) * (A[:, 3] - A[:, 1])
    area_b = (B[:, 2] - B[:, 0]) * (B[:, 3] - B[:, 1])
    union = area_a[:, None] + area_b[None, :] - inter
    return np.where(union > 0, inter / np.where(union > 0, union, 1), 0.0)


def _assign(iou, same, iou_match, greedy=None):
    """Rows/cols of the matched pairs (iou >= iou_match), maximising iou + 0.5*same class."""
    ok = iou >= iou_match
    if not ok.any():
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    score = iou + 0.5 * same
    if greedy is None:
        greedy = iou.size > GREEDY_ABOVE
    if greedy:
        r, c = np.nonzero(ok)
        o = np.argsort(-score[r, c], kind='stable')
        ur, uc, rr, cc = set(), set(), [], []
        for i, j in zip(r[o].tolist(), c[o].tolist()):
            if i not in ur and j not in uc:
                ur.add(i)
                uc.add(j)
                rr.append(i)
                cc.append(j)
        return np.array(rr, np.int64), np.array(cc, np.int64)
    r, c = linear_sum_assignment(np.where(ok, -score, 1e6))
    keep = ok[r, c]
    return r[keep], c[keep]


def _match_boxes(res, inp, boxes, pa, pb):
    """Box statuses for the eligible `boxes` (alive, of a or b) and the image pairs (pa[i] on A, pb[i] on B).
    Boxes of unpaired images -> IMAGE_ONLY. Returns bool per pair: every box on both sides SAME."""
    p = res.params
    n_img = len(inp.images)
    img = inp.img_idx[boxes].astype(np.int64)
    h = _box_hash(inp.cls[boxes], inp.xyxy[boxes])
    o = np.lexsort((h, img))
    boxes, img, h = boxes[o], img[o], h[o]
    cnt = np.bincount(img, minlength=n_img)
    start = np.zeros(n_img + 1, np.int64)
    np.cumsum(cnt, out=start[1:])
    hsum, hxor = np.zeros(n_img, np.uint64), np.zeros(n_img, np.uint64)
    nz = cnt > 0
    if len(boxes):                                          # reduceat over non-empty segments only
        seg = start[:-1][nz]
        hsum[nz] = np.add.reduceat(h, seg)
        hxor[nz] = np.bitwise_xor.reduceat(h, seg)

    unpaired = res.img_pair[img] < 0
    res.status[boxes[unpaired]] = BOX_IMAGE_ONLY

    eq = (cnt[pa] == cnt[pb]) & (hsum[pa] == hsum[pb]) & (hxor[pa] == hxor[pb])
    ok = eq.copy()
    # fast path: identical box sets, pair k-th sorted hash with k-th
    ea, eb = pa[eq], pb[eq]
    k = cnt[ea]
    if k.sum():
        within = np.arange(k.sum()) - np.repeat(np.cumsum(k) - k, k)
        ba = boxes[np.repeat(start[ea], k) + within]
        bb = boxes[np.repeat(start[eb], k) + within]
        res.status[ba] = res.status[bb] = BOX_SAME
        res.pair[ba], res.pair[bb] = bb, ba
        res.iou[ba] = res.iou[bb] = 1.0

    # slow path
    im, isame = p['iou_match'], p['iou_same']
    for t in np.flatnonzero(~eq).tolist():
        ia, ib = pa[t], pb[t]
        A, B = boxes[start[ia]:start[ia + 1]], boxes[start[ib]:start[ib + 1]]
        res.status[A] = BOX_UNMATCHED
        res.status[B] = BOX_UNMATCHED
        if not len(A) or not len(B):
            continue
        iou = _iou(inp.xyxy[A], inp.xyxy[B])
        same = inp.cls[A][:, None] == inp.cls[B][None, :]
        r, c = _assign(iou, same, im)
        if not len(r):
            continue
        ma, mb, v, s = A[r], B[c], iou[r, c], same[r, c]
        hi = v >= isame
        st = np.where(s, np.where(hi, BOX_SAME, BOX_MOVED), np.where(hi, BOX_RECLASS, BOX_RECLASS_MOVED))
        res.status[ma] = res.status[mb] = st
        res.pair[ma], res.pair[mb] = mb, ma
        res.iou[ma] = res.iou[mb] = v
        ok[t] = len(r) == len(A) == len(B) and bool((st == BOX_SAME).all())
    return ok


def _eligible(inp, a, b):
    o = np.asarray(inp.origin)
    return np.asarray(inp.alive, bool) & ((o == a) | (o == b))


def _set_image_status(res, pa, pb, ok):
    hashed = res.img_how[pa] == HOW_HASH
    st = np.where(ok, IMG_SAME, IMG_LABELS_CHANGED).astype(np.uint8)
    res.img_status[pa[hashed]] = st[hashed]
    res.img_status[pb[hashed]] = st[hashed]


def run(inp: DiffInput, a: int, b: int, params: dict | None = None) -> DiffResult:
    p = {**DEFAULTS, **(params or {})}
    st, pair, how, dup = _match_images(inp, a, b, p['name_key'])
    N = len(inp.origin)
    res = DiffResult(a=a, b=b, params=p, img_status=st, img_pair=pair, img_how=how, img_dup=dup,
                     status=np.zeros(N, np.uint8), pair=np.full(N, -1, np.int32), iou=np.zeros(N, np.float16))
    org = np.asarray(inp.img_origin)
    pa = np.flatnonzero((org == a) & (pair >= 0))
    pb = pair[pa].astype(np.int64)
    ok = _match_boxes(res, inp, np.flatnonzero(_eligible(inp, a, b)), pa, pb)
    _set_image_status(res, pa, pb, ok)
    res.summary = summarize(res, inp)
    return res


def update(res: DiffResult, inp: DiffInput, images) -> dict:
    """Recompute the image pairs touching `images` (same N and image list; alive/cls/xyxy may have changed).
    Returns {box id: new status} of the boxes whose status changed; refreshes res.summary."""
    a, b = res.a, res.b
    org = np.asarray(inp.img_origin)
    imgs = np.unique(np.asarray(list(images), np.int64))
    imgs = imgs[(org[imgs] == a) | (org[imgs] == b)] if len(imgs) else imgs
    if not len(imgs):
        return {}
    partner = res.img_pair[imgs]
    touched = np.unique(np.concatenate([imgs, partner[partner >= 0]]))
    in_touched = np.zeros(len(inp.images), bool)
    in_touched[touched] = True
    bx = np.flatnonzero(in_touched[inp.img_idx])
    old = res.status[bx].copy()
    res.status[bx], res.pair[bx], res.iou[bx] = BOX_NA, -1, 0
    pa = touched[(org[touched] == a) & (res.img_pair[touched] >= 0)]
    pb = res.img_pair[pa].astype(np.int64)
    ok = _match_boxes(res, inp, bx[_eligible(inp, a, b)[bx]], pa, pb)
    _set_image_status(res, pa, pb, ok)
    res.summary = summarize(res, inp)
    ch = res.status[bx] != old
    return dict(zip(bx[ch].tolist(), res.status[bx][ch].tolist()))


# ---------------------------------------------------------------- summary

def summarize(res, inp) -> dict:
    a, b = res.a, res.b
    io, st = np.asarray(inp.img_origin), res.img_status
    on_a, on_b = io == a, io == b
    bo, bs = np.asarray(inp.origin), res.status
    alive = np.asarray(inp.alive, bool)
    sa, sb = alive & (bo == a), alive & (bo == b)
    recl = (bs == BOX_RECLASS) | (bs == BOX_RECLASS_MOVED)
    cls = np.asarray(inp.cls, np.int64)
    n_cls = int(cls.max()) + 1 if len(cls) else 0

    def cc(m):
        return np.bincount(cls[m], minlength=n_cls)

    ra = np.flatnonzero(recl & sa)
    rb = res.pair[ra].astype(np.int64)
    conf = np.zeros((n_cls, n_cls), np.int64)
    np.add.at(conf, (cls[ra], cls[rb]), 1)
    per = dict(a=cc(sa), b=cc(sb), added=cc(sb & (bs == BOX_UNMATCHED)), removed=cc(sa & (bs == BOX_UNMATCHED)),
               reclass_in=np.bincount(cls[rb], minlength=n_cls), reclass_out=np.bincount(cls[ra], minlength=n_cls))
    per = {k: v.tolist() for k, v in per.items()}
    def n(m):
        return int(np.count_nonzero(m))

    return {
        'a': int(a), 'b': int(b), 'params': dict(res.params),
        'images': {'only_a': n(st == IMG_ONLY_A), 'only_b': n(st == IMG_ONLY_B), 'same': n(on_a & (st == IMG_SAME)),
                   'labels_changed': n(on_a & (st == IMG_LABELS_CHANGED)),
                   'pixels_changed': n(on_a & (st == IMG_PIXELS_CHANGED)),
                   'dup_a': n(on_a & res.img_dup), 'dup_b': n(on_b & res.img_dup)},
        'boxes': {'same': n(sa & (bs == BOX_SAME)), 'moved': n(sa & (bs == BOX_MOVED)), 'reclass': n(sa & recl),
                  'removed': n(sa & (bs == BOX_UNMATCHED)), 'added': n(sb & (bs == BOX_UNMATCHED)),
                  'image_only_a': n(sa & (bs == BOX_IMAGE_ONLY)), 'image_only_b': n(sb & (bs == BOX_IMAGE_ONLY))},
        'classes': [{'cls': c, **{k: v[c] for k, v in per.items()}} for c in range(n_cls)],
        'confusion': conf.tolist(),
    }
