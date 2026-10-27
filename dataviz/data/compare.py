"""
The Compare tab of a combined dataset (data/union.py): data/diff.py over the open State, cached, kept current
while boxes are edited, plus the "quick shared map" (one member's saved view, the others placed by neighbours).

    inputs     DiffInput built once per index from the State's samples + the union images.json (every image,
               boxless ones too); patched in place for the images an edit / the live feed touched
    results    per (a, b, params): the DiffResult in memory (LRU 4) and in <ws>/compare/<key>.npz + .json, the key
               hashing the fingerprint, the pair, the params and the current labels (labels_tag), so a reload after
               edits never serves an old diff
    patched()  State.on_boxes_patched(images) -> diff.update() on every result in memory (milliseconds), the
               changed statuses kept for the live feed / the next /api/compare poll
"""

import hashlib
import json
import threading
from collections import OrderedDict

import numpy as np

from . import diff, imagehash

MAX_MEM = 4


def _pkey(params):
    return json.dumps({k: params[k] for k in sorted(params)}, separators=(',', ':'))


class CompareService:
    def __init__(self, state):
        self.state = state
        self.lock = threading.RLock()
        self._inp = None                 # (fingerprint, DiffInput, image index by name)
        self.results = OrderedDict()     # (a, b, pkey) -> {'key', 'res'}
        self.changes = {}                # (a, b, pkey) -> {sid: status} since the client last asked

    # ------------------------------------------------------------ inputs
    def aliases(self):
        return [m['alias'] for m in self.state.members]

    def member_index(self, alias):
        try:
            return self.aliases().index(alias)
        except ValueError:
            raise KeyError(f'no member {alias!r} in this combined dataset') from None

    def inputs(self):
        st = self.state
        with self.lock:
            if self._inp is not None and self._inp[0] == st.fingerprint:
                return self._inp[1], self._inp[2]
            imgs = (imagehash.load(st.cache_dir) or {}).get('images') or {}
            names = sorted(set(imgs) | set(st.by_image))
            where = {n: i for i, n in enumerate(names)}
            alias_at = {a: i for i, a in enumerate(self.aliases())}
            full = bool(imgs) and all(e.get('h') for e in imgs.values())     # one key kind for every image
            keys = []
            for n in names:
                e = imgs.get(n)
                keys.append(None if not e else ('h:' + e['h']) if full else ('q:' + e['q']) if e.get('q') else None)
            img_origin = np.array([alias_at.get(n.split('/', 1)[0], 255) for n in names], np.uint8)
            s = st.samples
            n = len(s)
            xyxy = np.zeros((n, 4), np.float32)
            img_idx = np.zeros(n, np.int32)
            for i, r in enumerate(s):
                w, h = max(1, r['img_w']), max(1, r['img_h'])
                b = r['bbox']
                xyxy[i] = (b[0] / w, b[1] / h, b[2] / w, b[3] / h)
                img_idx[i] = where[r['image']]
            dm = st.deleted_mask()
            inp = diff.DiffInput(origin=st.origin.copy(), img_idx=img_idx, xyxy=xyxy,
                                 cls=st.labels.astype(np.int32), alive=~dm if dm is not None else np.ones(n, bool),
                                 images=names, img_origin=img_origin, img_key=keys)
            # every box of each image, deleted ones included (by_image drops deleted boxes: patched() must not)
            order = np.argsort(img_idx, kind='stable')
            cuts = np.searchsorted(img_idx[order], np.arange(len(names) + 1))
            self._boxes_of = (order, cuts)
            self._inp = (st.fingerprint, inp, where)
            return inp, where

    def labels_tag(self):
        """Hash of what the diff reads that edits change: classes, deletions, box corners."""
        inp, _ = self.inputs()
        h = hashlib.sha1(inp.cls.tobytes())
        h.update(np.packbits(inp.alive).tobytes())
        h.update(inp.xyxy.tobytes())
        return h.hexdigest()[:16]

    # ------------------------------------------------------------ results
    def params(self, params):
        p = dict(diff.DEFAULTS)
        for k, v in (params or {}).items():
            if k in ('iou_match', 'iou_same'):
                p[k] = min(1.0, max(0.05, float(v)))
            elif k == 'name_key' and v in ('rel', 'stem', 'basename'):
                p[k] = v
        if p['iou_same'] < p['iou_match']:
            p['iou_same'] = p['iou_match']
        return p

    def key(self, a, b, params):
        st = self.state
        seed = f'{st.fingerprint}|{a}|{b}|{_pkey(params)}|{self.labels_tag()}'
        return hashlib.sha1(seed.encode()).hexdigest()[:20]

    def _dir(self):
        d = self.state.cache_dir / 'compare'
        d.mkdir(exist_ok=True)
        return d

    def cached(self, a, b, params):
        """(key, DiffResult) when this pair is computed for the current labels (memory, then disk), else (key, None)."""
        params = self.params(params)
        ia, ib = self.member_index(a), self.member_index(b)
        mk = (ia, ib, _pkey(params))
        key = self.key(ia, ib, params)
        with self.lock:
            hit = self.results.get(mk)
            if hit is not None and hit['key'] == key:
                self.results.move_to_end(mk)
                return key, hit['res']
        path = self._dir() / f'{key}.npz'
        if path.exists():
            try:
                res = self._load(path, ia, ib, params)
            except (OSError, ValueError, KeyError):
                res = None
            if res is not None:
                self._remember(mk, key, res)
                return key, res
        return key, None

    def run(self, a, b, params, job=None):
        if a == b:
            raise ValueError('pick two different members to compare')
        params = self.params(params)
        with self.lock:                # key, diff and save see the same inputs (patched() takes this lock too)
            key, res = self.cached(a, b, params)
            if res is not None:
                return key, res
            if job is not None:
                job.progress(stage=f'comparing {a} with {b}')
            inp, _ = self.inputs()
            ia, ib = self.member_index(a), self.member_index(b)
            res = diff.run(inp, ia, ib, params)
            res.summary.update(a_alias=a, b_alias=b)
            self._save(key, res)
            self._remember((ia, ib, _pkey(params)), key, res)
            return key, res

    def by_key(self, key):
        with self.lock:
            for v in self.results.values():
                if v['key'] == key:
                    return v['res']
        path = self.state.cache_dir / 'compare' / f'{key}.npz'
        if path.exists():
            meta = json.loads((path.with_suffix('.json')).read_text())
            return self._load(path, meta['a'], meta['b'], meta['params'])
        raise KeyError(f'no comparison {key!r}: run it again')

    def _remember(self, mk, key, res):
        with self.lock:
            self.results[mk] = {'key': key, 'res': res}
            self.results.move_to_end(mk)
            while len(self.results) > MAX_MEM:
                self.results.popitem(last=False)

    def _save(self, key, res):
        d = self._dir()
        np.savez(d / f'{key}.npz', img_status=res.img_status, img_pair=res.img_pair, img_how=res.img_how,
                 img_dup=res.img_dup, status=res.status, pair=res.pair, iou=res.iou)
        (d / f'{key}.json').write_text(json.dumps({'a': res.a, 'b': res.b, 'params': res.params,
                                                   'summary': res.summary}))

    def _load(self, path, a, b, params):
        z = np.load(path)
        meta = json.loads(path.with_suffix('.json').read_text())
        return diff.DiffResult(a=a, b=b, params=params, img_status=z['img_status'], img_pair=z['img_pair'],
                               img_how=z['img_how'], img_dup=z['img_dup'], status=z['status'].copy(),
                               pair=z['pair'].copy(), iou=z['iou'].copy(), summary=meta['summary'])

    # ------------------------------------------------------------ edits
    def patched(self, images):
        """Boxes of `images` changed (class, corners, deleted): patch the inputs, update every diff in memory."""
        st = self.state
        with self.lock:
            if self._inp is None or self._inp[0] != st.fingerprint:
                return {}
            inp, where = self._inp[1], self._inp[2]
            idx = []
            dm = st.deleted_mask()
            order, cuts = self._boxes_of
            for name in images:
                i = where.get(name)
                if i is None:
                    continue
                idx.append(i)
                for sid in order[cuts[i]:cuts[i + 1]].tolist():
                    r = st.samples[sid]
                    w, h = max(1, r['img_w']), max(1, r['img_h'])
                    b = r['bbox']
                    inp.xyxy[sid] = (b[0] / w, b[1] / h, b[2] / w, b[3] / h)
                    inp.cls[sid] = int(st.labels[sid])
                    inp.alive[sid] = not (dm is not None and dm[sid])
            if not idx:
                return {}
            out = {}
            tag = None
            for mk, v in list(self.results.items()):
                ch = diff.update(v['res'], inp, idx)
                if tag is None:
                    tag = self.labels_tag()
                v['key'] = hashlib.sha1(f'{st.fingerprint}|{mk[0]}|{mk[1]}|{mk[2]}|{tag}'.encode()).hexdigest()[:20]
                self._save(v['key'], v['res'])
                acc = self.changes.setdefault(mk, {})
                acc.update(ch)
                out[v['key']] = {'status': {int(k): int(s) for k, s in ch.items()}, 'summary': v['res'].summary}
            return out

    # ------------------------------------------------------------ answers
    def image_lists(self, res, cap=50_000):
        inp, _ = self.inputs()
        names = inp.images
        lists, trunc = {}, {}
        sel = {
            'only_a': res.img_status == diff.IMG_ONLY_A,
            'only_b': res.img_status == diff.IMG_ONLY_B,
            'same': (res.img_status == diff.IMG_SAME) & (inp.img_origin == res.a),
            'labels_changed': (res.img_status == diff.IMG_LABELS_CHANGED) & (inp.img_origin == res.a),
            'pixels_changed': (res.img_status == diff.IMG_PIXELS_CHANGED) & (inp.img_origin == res.a),
            'dup': res.img_dup & ((inp.img_origin == res.a) | (inp.img_origin == res.b)),
        }
        for k, m in sel.items():
            ids = np.nonzero(m)[0]
            if len(ids) > cap:
                trunc[k] = int(len(ids))
                ids = ids[:cap]
            lists[k] = [names[i] for i in ids]
        return lists, trunc

    def image_detail(self, res, name):
        """Both sides of one image of the pair: its status, its partner and every box with status / partner."""
        inp, where = self.inputs()
        i = where.get(name)
        if i is None:
            raise KeyError(f'no image {name!r}')
        j = int(res.img_pair[i])
        st = self.state

        def side(k):
            if k < 0:
                return None
            nm = inp.images[k]
            ids = st.by_image.get(nm, [])
            s0 = st.samples[ids[0]] if ids else None
            return {'name': nm, 'origin': int(inp.img_origin[k]),
                    'img_w': s0['img_w'] if s0 else None, 'img_h': s0['img_h'] if s0 else None,
                    'boxes': [{'sid': int(sid), 'cls': int(inp.cls[sid]),
                               'xyxy': [round(float(v), 5) for v in inp.xyxy[sid]],
                               'status': int(res.status[sid]), 'pair': int(res.pair[sid]),
                               'iou': round(float(res.iou[sid]), 3)}
                              for sid in ids if inp.alive[sid]]}
        a_side, b_side = (i, j) if int(inp.img_origin[i]) == res.a else (j, i)
        return {'status': int(res.img_status[i]), 'how': int(res.img_how[i]), 'dup': bool(res.img_dup[i]),
                'a': side(a_side), 'b': side(b_side)}

    # ------------------------------------------------------------ quick shared map
    def quick_map(self, ref, feature, job=None):
        """A shared map without a new joint projection: member `ref` keeps the positions of its own saved view
        of `feature` (the newest; computed in its own session), every other box is placed at the inverse-distance
        mean of its 10 nearest `ref` boxes in feature space. Also the cosine similarity of each box to its nearest
        `ref` box (novelty: low = unlike anything in ref). ValueError when ref has no saved view of that feature."""
        from ..ml import features as feats
        st = self.state
        ir = self.member_index(ref)
        blk = st.members[ir]
        ws = st.workspace_root / blk['ws']
        try:
            entries = json.loads((ws / 'projections' / 'index.json').read_text())
        except (OSError, ValueError):
            entries = {}
        cands = sorted((e for e in entries.values() if e.get('feature') == feature and not e.get('classes')),
                       key=lambda e: e.get('last_used') or e.get('created') or '', reverse=True)
        proj = None
        for e in cands:
            h = hashlib.sha1(f'{e["key"]}|{blk["fp"]}'.encode()).hexdigest()[:16]
            p = ws / 'projections' / f'{h}.npz'
            if p.exists():
                z = np.load(p)
                if len(z['id']) == blk['n']:
                    proj = (z['id'], z['xy'], e)
                    break
        if proj is None:
            raise ValueError(f'member {ref} has no saved map of this feature yet: open it once, or use the joint map')
        emb = st.get_features(feature, job)
        X = feats.l2_normalize(np.asarray(emb, np.float32))
        off, n = blk['offset'], blk['n']
        ref_ids = off + proj[0].astype(np.int64)
        ref_xy = proj[1].astype(np.float32)
        N = len(st.samples)
        xy = np.zeros((N, 2), np.float32)
        sim = np.ones(N, np.float32)
        xy[ref_ids] = ref_xy
        others = np.setdiff1d(np.arange(N), ref_ids)
        if len(others):
            if job is not None:
                job.progress(stage=f'placing {len(others):,} boxes on {ref}’s map')
            rng = np.random.default_rng(0)
            fit = ref_ids if len(ref_ids) <= 50_000 else np.sort(rng.choice(ref_ids, 50_000, replace=False))
            fit_xy = xy[fit]
            R = X[fit]
            k = min(10, len(fit))
            for a0 in range(0, len(others), 4096):                  # chunked cosine k-NN against the reference
                q = others[a0:a0 + 4096]
                S = X[q] @ R.T
                top = np.argpartition(-S, k - 1, axis=1)[:, :k]
                s = np.take_along_axis(S, top, 1)
                w = np.exp((s - s.max(1, keepdims=True)) / 0.02)   # relative to the best match: sharp at any dim
                xy[q] = (fit_xy[top] * w[..., None]).sum(1) / w.sum(1, keepdims=True)
                sim[q] = s.max(1)
        info = {'method': 'quick', 'ref': ref, 'feature': feature, 'from': proj[2].get('key'), 'n_ref': int(n)}
        return {'id': np.arange(N), 'xy': xy, 'info': info, 'novel_sim': sim}


def service(state):
    """The State's CompareService (created on first use)."""
    svc = getattr(state, '_compare', None)
    if svc is None:
        svc = state._compare = CompareService(state)
    return svc
