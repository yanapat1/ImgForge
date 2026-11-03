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
    stress     _stress_plan / stress_run / stress_cached: a model on member A's images under graded light, blur,
               noise, colour, JPEG and resolution changes (data/stress.py), B placed on each axis by the shift report
    shift()    "Image conditions": data/imgstats.py tables of A's and B's images (a sample, cached in each member's
               own workspace) -> data/shift.py report (+ the appearance AUC of a computed feature, + the fit of
               every model that already predicted this dataset); in memory (LRU 4) by shift_key()
"""

import hashlib
import json
import threading
import warnings
from collections import OrderedDict

import numpy as np

from . import diff, imagehash, imgstats
from . import shift as shiftlib

MAX_MEM = 4
SHIFT_SAMPLE = 3000              # images measured per side by default (0 = all)
APPEARANCE_MAX = 4000            # boxes per side the appearance AUC reads


def _pkey(params):
    return json.dumps({k: params[k] for k in sorted(params)}, separators=(',', ':'))


class CompareService:
    def __init__(self, state):
        self.state = state
        self.lock = threading.RLock()
        self._inp = None                 # (fingerprint, DiffInput, image index by name)
        self.results = OrderedDict()     # (a, b, pkey) -> {'key', 'res'}
        self.changes = {}                # (a, b, pkey) -> {sid: status} since the client last asked
        self.shifts = OrderedDict()      # shift key -> {'report', 'arrays', 'a', 'b'}
        self.stresses = OrderedDict()    # stress key -> result (data/stress.py)

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


    # ------------------------------------------------------------ image conditions (domain shift)
    def shift_key(self, a, b, sample, feature, models_sig=''):
        st = self.state
        seed = f'{st.fingerprint}|{a}|{b}|{int(sample or 0)}|{feature or ""}|{models_sig}|{getattr(st, "edit_seq", 0)}'
        return 'sh' + hashlib.sha1(seed.encode()).hexdigest()[:18]

    def shift_cached(self, key):
        with self.lock:
            hit = self.shifts.get(key)
            if hit is not None:
                self.shifts.move_to_end(key)
            return hit

    def shift_by_key(self, key):
        hit = self.shift_cached(key)
        if hit is None:
            raise KeyError(f'no image-conditions report {key!r}: run it again')
        return hit

    def _member_ws(self, ia):
        st = self.state
        blk = st.members[ia]
        root = getattr(st, 'workspace_root', None)
        ws = root / blk['ws'] if root is not None and blk.get('ws') else None
        if ws is None or not ws.is_dir():
            ws = st.cache_dir / 'compare' / f'imgstats-{blk["alias"]}'
            ws.mkdir(parents=True, exist_ok=True)
        return ws

    def shift_table(self, alias, sample, job=None):
        """imgstats.Table of member `alias` (union names '<alias>/<inner>'), measuring what its cache lacks."""
        inp, _ = self.inputs()
        ia = self.member_index(alias)
        pre = f'{alias}/'
        inner = [nm[len(pre):] for nm, o in zip(inp.images, inp.img_origin.tolist()) if o == ia and nm.startswith(pre)]
        pick = imgstats.sample(inner, sample)
        src = self.state.source
        sig = getattr(src, 'image_sig', None)
        prog = None
        if job is not None:
            prog = lambda d, t, stage: job.progress(d, t, f'{stage} · {alias}')
        t = imgstats.ensure(self._member_ws(ia), pick, lambda n: src.read_image_bytes(pre + n),
                            (lambda n: sig(pre + n)) if sig else None, progress=prog,
                            cancelled=getattr(job, 'cancelled', None))
        t.names = [pre + n for n in t.names]
        return t, len(inner)

    def tests(self, res, feature=None, job=None):
        """Statistical tests for the Compare panels of diff result `res` (ml/stats.py), cached per (result, feature):
            classes        chi-square of homogeneity of A's vs B's class mix + Cramér's V, per class two-proportion
                           z with BH q (aligned with summary['classes'])
            class_changes  Bowker symmetry of the matched boxes' class table (summary['confusion'])
            box_stats      per box statistic (area, aspect, brightness, blur) KS + Mann-Whitney, Cliff's delta, W1
            embedding      MMD permutation test on `feature` (PCA-32, <= 1500 per side), None when not computed
        Every box of A and B that is alive counts (the filter bar does not change a test)."""
        from ..ml import stats
        st = self.state
        ck = (res.a, res.b, _pkey(res.params), self.labels_tag(), feature or '')   # an edit changes labels_tag
        with self.lock:
            memo = getattr(self, '_tests', None)
            if memo is None:
                memo = self._tests = OrderedDict()
            if ck in memo:
                return memo[ck]
        inp, _ = self.inputs()
        ia, ib = res.a, res.b
        summ = res.summary
        rows = summ.get('classes') or []
        out = {'a': ia, 'b': ib, 'classes': stats.contingency([r['a'] for r in rows], [r['b'] for r in rows]),
               'class_changes': stats.bowker(summ.get('confusion') or [[0]])}
        sa = inp.alive & (inp.origin == ia)
        sb = inp.alive & (inp.origin == ib)
        image_unit = getattr(st.source, 'unit', None) == 'image'
        cols = {}
        for col in ('area', 'aspect', 'brightness', 'blur'):
            if image_unit and col in ('area', 'aspect'):
                continue
            v = np.fromiter((x.get(col, np.nan) for x in st.samples), np.float64, len(st.samples))
            cols[col] = stats.two_sample(v[sa], v[sb])
        out['box_stats'] = cols
        out['embedding'] = None
        if feature and (feature in st.features or st.feature_cached(feature)):
            X = np.asarray(st.get_features(feature, job), np.float32)
            rng = np.random.default_rng(stats.SEED)
            pick = lambda m: rng.permutation(np.nonzero(m)[0])[:1500]  # noqa: E731
            xa, xb = pick(sa), pick(sb)
            if len(xa) >= 5 and len(xb) >= 5:
                Z = np.vstack([X[xa], X[xb]]).astype(np.float64)
                Z -= Z.mean(0)
                # PCA-32 of the subsample (a 3000 x D SVD): the kernel sees the main directions, not noise dims
                u, sv, _ = np.linalg.svd(Z, full_matrices=False)
                Z = u[:, :32] * sv[:32]
                out['embedding'] = {**stats.mmd(Z[:len(xa)], Z[len(xa):]), 'feature': feature}
        with self.lock:
            memo[ck] = out
            while len(memo) > 8:
                memo.popitem(last=False)
        return out

    def appearance_auc(self, feature, ia, ib, job=None):
        """What a model sees, on a computed feature (PCA-32), None when it is not computed:
            auc        cross-validated AUC of telling A's boxes from B's
            coverage   share of B's boxes whose nearest A box is as close as A's boxes are to each other (the p95 of
                       A's own leave-one-out 1-NN distance): the part of B that A has shown a model something like
            frechet    Frechet distance of the two Gaussians (FID-style, on the same 32 dims)
            uncovered  B image names holding the boxes farthest outside A (for the image grid), farthest first"""
        st = self.state
        if not feature or not (feature in st.features or st.feature_cached(feature)):
            return None
        from ..ml import features as feats
        X = np.asarray(st.get_features(feature, job), np.float32)
        inp, _ = self.inputs()
        rng = np.random.default_rng(0)
        ids = []
        for side in (ia, ib):
            s = np.nonzero((inp.origin == side) & inp.alive)[0]
            if len(s) > APPEARANCE_MAX:
                s = np.sort(rng.choice(s, APPEARANCE_MAX, replace=False))
            ids.append(s)
        if min(len(ids[0]), len(ids[1])) < 5:
            return None
        Z = feats.l2_normalize(X[np.r_[ids[0], ids[1]]])
        k = min(32, Z.shape[1], Z.shape[0] - 1)
        if k >= 2 and Z.shape[1] > k:
            from sklearn.decomposition import PCA
            Z = PCA(n_components=k, svd_solver='randomized', random_state=0).fit_transform(Z)
        y = np.r_[np.zeros(len(ids[0]), np.int8), np.ones(len(ids[1]), np.int8)]
        auc, _, _ = shiftlib.domain_auc(Z, y)
        if auc is None:
            return None
        za, zb = Z[:len(ids[0])], Z[len(ids[0]):]
        cov, unc = shiftlib.coverage(za, zb)
        names = [inp.images[inp.img_idx[ids[1][i]]] for i in unc]
        seen, uncovered = set(), []
        for nm in names:
            if nm not in seen:
                seen.add(nm)
                uncovered.append(nm)
        return {'feature': feature, 'auc': auc, 'words': shiftlib.auc_words(auc), 'n_a': len(ids[0]),
                'n_b': len(ids[1]), 'coverage': cov, 'frechet': shiftlib.frechet(za, zb), '_uncovered': uncovered}

    def shift(self, a, b, sample=SHIFT_SAMPLE, feature=None, models=(), key=None, job=None):
        """The image-conditions report of A vs B (data/shift.py). models: [(model dict, evaluate.Data, conf)] of
        models that already predicted this dataset. Memoised under `key` (shift_key)."""
        if a == b:
            raise ValueError('pick two different members to compare')
        ia, ib = self.member_index(a), self.member_index(b)
        key = key or self.shift_key(a, b, sample, feature)
        hit = self.shift_cached(key)
        if hit is not None:
            return key, hit
        ta, na = self.shift_table(a, sample, job)
        tb, nb = self.shift_table(b, sample, job)
        if job is not None:
            job.progress(stage='comparing image conditions')
        report, arrays = shiftlib.build(ta, tb, a, b, totals={'total_a': na, 'total_b': nb, 'sample': int(sample or 0)})
        try:
            app = self.appearance_auc(feature, ia, ib, job)
        except Exception:              # noqa: BLE001 - the feature is extra evidence, never a failure of the report
            app = None
        uncovered = app.pop('_uncovered', []) if app else []
        report['separability']['appearance'] = app
        if models:
            from ..models import evaluate
            vecs = {**dict(zip(ta.names, ta.vec)), **dict(zip(tb.names, tb.vec))}
            px = [m for m in report['metrics'] if m['available'] and m['group'] in shiftlib.PIXEL_GROUPS]
            keys = [m['key'] for m in sorted(px, key=lambda m: -m['score'])[:6]]
            for k in ('brightness', 'contrast', 'sharpness', 'noise'):
                if k not in keys and any(m['key'] == k for m in px):
                    keys.append(k)
            for m, d, conf in models:
                side = np.array([ia if nm.startswith(f'{a}/') else ib if nm.startswith(f'{b}/') else -1
                                 for nm in d.images], np.int16)
                fit = shiftlib.model_fit(d, side, ia, ib, conf, lambda mask, d=d: evaluate.ap_stats(d, 'all', mask),
                                         vecs, keys)
                report['models'].append({'id': m['id'], 'name': m['name'], **fit})
        report['verdict'] = shiftlib.verdict(report)
        report['key'] = key
        meds = {}
        for side, t in (('a', ta), ('b', tb)):
            with warnings.catch_warnings():               # an all-NaN column (no EXIF): no median, no warning
                warnings.simplefilter('ignore', RuntimeWarning)
                med = np.nanmedian(t.vec, axis=0) if len(t.names) else np.full(len(imgstats.KEYS), np.nan)
            meds[side] = {k: float(v) for k, v in zip(imgstats.KEYS, med) if np.isfinite(v)}
        arrays['uncovered_b'] = uncovered
        entry = {'report': report, 'arrays': arrays, 'a': a, 'b': b, 'med': meds}
        with self.lock:
            self.shifts[key] = entry
            while len(self.shifts) > MAX_MEM:
                self.shifts.popitem(last=False)
        return key, entry

    @staticmethod
    def shift_images(entry, which, metric=None, side='b', cap=2000):
        """Image names of a report: outside (B outside A's p1..p99 of `metric`, farthest first), low / high (side's
        images by `metric`), most_b (B images the shift detector is surest are B)."""
        ar = entry['arrays']
        names = ar['names_b'] if side == 'b' else ar['names_a']
        vec = ar['vec_b'] if side == 'b' else ar['vec_a']
        if which == 'uncovered':
            return list(ar.get('uncovered_b') or [])[:cap]
        if which == 'most_b':
            p = ar.get('p_b')
            if p is None or not len(p):
                return []
            return [ar['names_b'][i] for i in np.argsort(-p)[:cap]]
        if metric not in imgstats.COL:
            raise KeyError(f'unknown metric {metric!r}')
        v = vec[:, imgstats.COL[metric]]
        ok = np.isfinite(v)
        if which in ('low', 'high'):
            ix = np.nonzero(ok)[0]
            ix = ix[np.argsort(v[ix] if which == 'low' else -v[ix], kind='stable')]
            return [names[i] for i in ix[:cap]]
        if which == 'outside':
            row = next((m for m in entry['report']['metrics'] if m['key'] == metric), None)
            if not row or not row.get('available'):
                return []
            lo, hi = row['band']
            dist = np.where(v < lo, lo - v, np.where(v > hi, v - hi, 0.0))
            ix = np.nonzero(ok & (dist > 0))[0]
            ix = ix[np.argsort(-dist[ix], kind='stable')]
            return [names[i] for i in ix[:cap]]
        raise KeyError(f'unknown list {which!r}')

    def shift_preview(self, entry, name, strength=1.0, max_side=1024, mode='as_b'):
        """(JPEG bytes, params) of image `name`: mode as_b = an A image moved from A's to B's median conditions
        (shift.simulate); correct = a B image through the correction LUT that matches its curves to A's."""
        import io

        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(self.state.source.read_image_bytes(name))))
        im = im.convert('RGB')
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side), Image.BILINEAR)
        if mode == 'correct':
            cor = entry['report'].get('correction')
            if not cor:
                raise ValueError('this report has no correction (no colour curves on one side)')
            out, prm = shiftlib.apply_lut(np.asarray(im), cor['lut']), {'lut': True, 'residual': cor['residual']}
        else:
            out, prm = shiftlib.simulate(np.asarray(im), entry['med']['a'], entry['med']['b'], strength)
        buf = io.BytesIO()
        Image.fromarray(out).save(buf, 'JPEG', quality=90)
        return buf.getvalue(), prm


# ------------------------------------------------------------ stress test (data/stress.py)
def _stress_plan(svc, member, m, fp, n, axes, conf):
    """(key, names, labelled) of a stress run: `n` images of `member` (the labelled ones when it has labels), the
    same deterministic sample every time."""
    from . import stress
    inp, _ = svc.inputs()
    ia = svc.member_index(member)
    st = svc.state
    mine = [nm for nm, o in zip(inp.images, inp.img_origin.tolist()) if o == ia]
    lab = [nm for nm in mine if st.by_image.get(nm)]
    labelled = bool(lab)
    names = imgstats.sample(lab if labelled else mine, max(1, min(int(n), stress.SAMPLE_MAX)))
    axes = sorted(a for a in axes if a in stress.AXES) or sorted(stress.AXES)
    key = stress.key_of(m['id'], fp, names, axes, conf, getattr(st, 'edit_seq', 0), labelled)
    return key, names, labelled, axes


def stress_cached(svc, key):
    from . import stress
    with svc.lock:
        hit = svc.stresses.get(key)
        if hit is not None:
            svc.stresses.move_to_end(key)
            return hit
    st = svc.state
    hit = stress.load(st.cache_dir, key) if getattr(st, 'cache_dir', None) else None
    if hit is not None:
        with svc.lock:
            svc.stresses[key] = hit
    return hit


def stress_run(svc, key, member, m, fp, workdir, names, labelled, axes, conf, job=None):
    from . import stress
    res = stress.run(svc.state, m, fp, workdir, names, axes, conf, job, labelled=labelled)
    res.update(key=key, member=member)
    if getattr(svc.state, 'cache_dir', None):
        stress.save(svc.state.cache_dir, key, res)
    with svc.lock:
        svc.stresses[key] = res
        while len(svc.stresses) > MAX_MEM:
            svc.stresses.popitem(last=False)
    return res


def service(state):
    """The State's CompareService (created on first use)."""
    svc = getattr(state, '_compare', None)
    if svc is None:
        svc = state._compare = CompareService(state)
    return svc
