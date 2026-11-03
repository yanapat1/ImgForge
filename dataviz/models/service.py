"""
What the routes call: saved models + their cached predictions and evaluations for the open dataset.

Memory caches are keyed by the dataset's State.tag, so opening another dataset never mixes results;
predictions on disk live in that dataset's workspace (predict.cache_path).
"""

import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np

from ..core import config as vconfig
from ..core import logging as vlog
from . import evaluate, predict, runners
from .splits import load_splits

SPLIT_CODE = {'train': 0, 'val': 1, 'test': 2}

log = vlog.get_logger('models')


class ModelService:
    def __init__(self, registry, cfg):
        self.reg = registry
        self.cfg = cfg.get('models', {})
        self.workdir = registry.store / '_converted'
        self._lock = threading.Lock()
        self._preds = OrderedDict()      # (tag, mid, fp) -> (preds, Data, unmatched)
        self._ap = OrderedDict()         # (tag, mid, fp, split) -> ap_stats
        self._splits = {}                # tag -> (splits, source)
        self._cleanlab = OrderedDict()   # (tag, mid, fp, edit_seq) -> cleanlab object-detection report
        self._imgcls = OrderedDict()     # (tag, mid, fp, conf) -> image_classes() (one point per image sessions)

    # ------------------------------------------------------------ lookups
    def model(self, mid):
        return self.reg.model(mid)

    def fingerprint(self, m):
        return self.reg.fingerprint(m)

    def splits(self, state):
        with self._lock:
            if state.tag not in self._splits:
                src = self.cfg.get('split_source')
                self._splits[state.tag] = load_splits(state.dataset_dir, vconfig.resolve(src) if src else None)
            return self._splits[state.tag]

    def drop_state(self, tag):
        """A State was unloaded (server/app.py Ctx.close_state): forget what is kept per its tag."""
        with self._lock:
            self._splits.pop(tag, None)
            for cache in (self._preds, self._ap, self._imgcls):
                for k in [k for k in cache if k[0] == tag]:
                    del cache[k]

    def is_predicted(self, state, m):
        if not state.ready or state.cache_dir is None:
            return False
        return predict.cache_path(state, m, self.fingerprint(m)).exists()

    def data(self, state, m):
        """(preds, evaluate.Data, unmatched class names) from memory / disk, None when not predicted yet."""
        fp = self.fingerprint(m)
        key = (state.tag, m['id'], fp, getattr(state, 'edit_seq', 0))    # a curation fix changes the ground truth
        with self._lock:
            if key in self._preds:
                self._preds.move_to_end(key)
                return self._preds[key]
        preds = predict.load(predict.cache_path(state, m, fp))
        if preds is None:
            return None
        pcls = predict.map_classes(preds['det'][:, 5], preds['names'], state.classes)
        _, unmatched = predict.mapping(preds['names'], state.classes)
        splits, _ = self.splits(state)
        dm = state.deleted_mask() if hasattr(state, 'deleted_mask') else None
        d = evaluate.Data(state.samples, len(state.classes), preds, pcls, splits, dm)
        with self._lock:
            self._preds[key] = (preds, d, unmatched)
            while len(self._preds) > 4:
                self._preds.popitem(last=False)
        return self._preds[key]

    # ------------------------------------------------------------ jobs
    def submit_predict(self, state, m):
        """`state` must be the real State (not the route proxy): a dataset switch mid-job must not redirect it."""
        fp = self.fingerprint(m)
        lane = 'io' if m['format'] == 'api' else 'main'
        bs = self.cfg.get('batch_size')

        def fn(job):
            out = predict.run(state, m, fp, self.workdir, job, batch_size=None if m['format'] == 'api' else bs)
            log.info(f'[bold]{vlog.esc(m["name"])}[/]: {len(out["det"]):,} detections on {len(out["images"]):,} images '
                     f'[dim]({out["ms"]:.1f} ms/img)[/]')
            return len(out['det'])
        return state.jobs.submit(f'preds:{state.tag}:{m["id"]}:{fp}', f'Predict · {m["name"]}', fn, lane=lane)

    def test(self, state, m):
        """Load the model and run it on one labelled image: timing, class mapping, a few detections."""
        import time
        fp = self.fingerprint(m)
        img_name = state.samples[0]['image'] if state.samples else sorted(state.source.list_images())[0]
        im = state.source.open_image(img_name).convert('RGB')
        with runners._lock:                                     # the one loaded runner is shared (models/hub.py)
            t0 = time.perf_counter()
            runner = runners.get_runner(m, fp, self.workdir)
            load_ms = (time.perf_counter() - t0) * 1000
            runner.predict([im])                                # warm-up (CUDA init, graph build)
            t0 = time.perf_counter()
            det = runner.predict([im])[0]
            ms = (time.perf_counter() - t0) * 1000
            names = list(runner.names or [])
        pcls = predict.map_classes(det[:, 5], names, state.classes) if len(det) else np.zeros(0, np.int64)
        _, unmatched = predict.mapping(names, state.classes)
        conf = float(m.get('conf') or self.cfg.get('default_conf', 0.25))
        keep = det[:, 4] >= conf
        top = [{'box': [round(float(v), 1) for v in d[:4]], 'score': round(float(d[4]), 3),
                'class_id': int(d[5]), 'model_class': names[int(d[5])] if int(d[5]) < len(names) else None,
                'dataset_class': state.classes[c] if c >= 0 else None}
               for d, c in zip(det[keep][:20], pcls[keep][:20])]
        gt = len(state.by_image.get(img_name, []))
        return {'ok': True, 'image': img_name, 'load_ms': round(load_ms), 'ms': round(ms, 1), 'n': int(keep.sum()),
                'n_labels': gt, 'conf': conf, 'names': names, 'unmatched': unmatched,
                'mapping': 'by name' if names else 'model class ids = dataset class ids', 'detections': top}

    # ------------------------------------------------------------ evaluation
    @staticmethod
    def pick_split(d, split):
        """The requested split, or 'all' when the dataset has no images in it."""
        avail = {s: int((d.img_split == s).sum()) for s in ('train', 'val', 'test')}
        if split not in ('all', 'train', 'val', 'test') or (split != 'all' and not avail.get(split)):
            split = 'all'
        return split, avail

    def ap(self, state, m, d, split, side=None, img_mask=None):
        """Conf-independent AP stats, cached per (dataset, model, split[, member of a combined dataset])."""
        akey = (state.tag, m['id'], self.fingerprint(m), split, getattr(state, 'edit_seq', 0), side)
        with self._lock:
            ap = self._ap.get(akey)
        if ap is None:
            ap = evaluate.ap_stats(d, split, img_mask)
            with self._lock:
                self._ap[akey] = ap
                while len(self._ap) > 32:
                    self._ap.popitem(last=False)
        return ap

    def score(self, state, m, conf=None, match_iou=None, split='val'):
        """Everything a view needs to score `m`: (preds, Data, unmatched, split, avail, ap, outcome, summary, conf,
        match_iou), or None when the model has not run on this dataset."""
        got = self.data(state, m)
        if got is None:
            return None
        preds, d, unmatched = got
        split, avail = self.pick_split(d, split)
        ap = self.ap(state, m, d, split)
        conf = float(conf if conf is not None else m.get('conf') or self.cfg.get('default_conf', 0.25))
        match_iou = float(match_iou or self.cfg.get('match_iou', 0.5))
        out = evaluate.outcome(d, conf, match_iou, split)
        summ = evaluate.summary(d, out, ap, split)
        return preds, d, unmatched, split, avail, ap, out, summ, conf, match_iou

    def evaluate(self, state, m, conf=None, match_iou=None, split='val', issues_min=0.5):
        got = self.score(state, m, conf, match_iou, split)
        if got is None:
            return None
        preds, d, unmatched, split, avail, ap, out, summ, conf, match_iou = got
        _, split_src = self.splits(state)
        pk = out['p_kind']
        fps = np.nonzero(pk >= 3)[0]
        fps = fps[np.argsort(-d.p_score[fps])][:500]
        gt_split = np.asarray([SPLIT_CODE.get(s, -1) for s in d.gt_split], np.int8)
        return {
            'model': self.reg.public(m), 'split': split, 'splits': avail, 'split_source': split_src,
            'conf': conf, 'match_iou': match_iou, 'ms': preds['ms'], 'model_names': preds['names'],
            'unmatched': unmatched, 'n_images': len(d.images),
            'ap': ap, **summ,
            'gt': {'status': out['status'], 'pred_cls': out['pred_cls'], 'pred_score': np.round(out['pred_score'], 3),
                   'iou': np.round(out['iou'], 3), 'pid': out['pid'], 'split': gt_split},
            'fps': [{'pid': int(p), 'score': round(float(d.p_score[p]), 3), 'pred_cls': int(d.p_cls[p]),
                     'kind': evaluate.PRED_KINDS[pk[p]], 'image': d.images[int(d.p_img[p])],
                     'gt': int(out['p_gt'][p])} for p in fps],
            'issues': evaluate.label_issues(d, out, min_score=max(issues_min, conf)),
            'by_origin': self.by_origin(state, m, d, out, split),
        }

    @staticmethod
    def image_origin(state, d):
        """Member index of each evaluated image of a combined dataset (names are '<alias>/<name>'), None otherwise."""
        mem = getattr(state, 'members', None) or []
        if not mem or getattr(state, 'origin', None) is None:
            return None
        at = {mm.get('alias'): k for k, mm in enumerate(mem)}
        return np.fromiter((at.get(n.split('/', 1)[0], -1) for n in d.images), np.int64, len(d.images))

    def by_origin(self, state, m, d, out, split):
        """A combined dataset: the same scores per member (A, B, ...) at this confidence, so the Model tab can show A
        and B side by side. [] for a plain dataset."""
        io = self.image_origin(state, d)
        if io is None:
            return []
        st, pk = out['status'], out['p_kind']
        g_org = np.asarray(state.origin, np.int64)[:d.n_gt]
        p_org = io[d.p_img] if len(d.p_img) else np.zeros(0, np.int64)
        res = []
        for k, mm in enumerate(state.members):
            ap = self.ap(state, m, d, split, side=k, img_mask=io == k)
            g, p = g_org == k, p_org == k
            tp = int(((st == 0) & g).sum())
            n_lbl = int(((st >= 0) & g).sum())
            n_pred = int(((pk >= 0) & p).sum())
            res.append({'alias': mm.get('alias'), 'name': mm.get('name'),
                        'images': int((d.image_mask(split) & (io == k)).sum()), 'labels': n_lbl, 'preds': n_pred,
                        'tp': tp, 'p': tp / n_pred if n_pred else 0.0, 'r': tp / n_lbl if n_lbl else 0.0,
                        'map50': ap['map50'], 'map': ap['map'], 'n_labels': ap['n_labels'], 'ap50': ap['ap50'],
                        'missed': int(((st == 3) & g).sum()), 'wrong_cls': int(((st == 1) & g).sum()),
                        'fp_bg': int(((pk == 4) & p).sum())})
        return res

    def cleanlab(self, state, m, min_score=0.05):
        """cleanlab.object_detection on this model's predictions (ml/label_quality.od_report): per box bad-location /
        swapped-class / image quality, plus overlooked objects. None when the model has not predicted yet."""
        from ..ml import label_quality
        got = self.data(state, m)
        if got is None:
            return None
        _, d, _ = got
        key = (state.tag, m['id'], self.fingerprint(m), getattr(state, 'edit_seq', 0))
        with self._lock:
            if key in self._cleanlab:
                return self._cleanlab[key]
        gts, preds = [], []
        for i in range(len(d.images)):
            gts.append([(int(sid), int(d.gt_cls[sid]), *map(float, d.gt_box[sid])) for sid in d.gt_by_img[i]])
            p = [int(q) for q in d.p_by_img[i] if d.p_cls[q] >= 0 and d.p_score[q] >= min_score]
            preds.append([(q, int(d.p_cls[q]), float(d.p_score[q]), *map(float, d.p_box[q])) for q in p])
        per, summary = label_quality.od_report(d.images, gts, preds, d.n_gt, d.nc)
        res = (per, summary)
        with self._lock:
            self._cleanlab[key] = res
            while len(self._cleanlab) > 4:
                self._cleanlab.popitem(last=False)
        return res

    def pred(self, state, m, pid):
        got = self.data(state, m)
        if got is None:
            raise KeyError('not predicted yet')
        preds, d, _ = got
        if pid < 0 or pid >= len(d.p_score):
            raise KeyError(f'no prediction {pid}')
        return {'image': d.images[int(d.p_img[pid])], 'box': [float(v) for v in d.p_box[pid]],
                'score': float(d.p_score[pid]), 'cls': int(d.p_cls[pid])}

    def image_preds(self, state, m, image, conf):
        """Detections drawn over one image (dataset class ids, -1 = class the dataset lacks)."""
        got = self.data(state, m)
        if got is None:
            return []
        preds, d, _ = got
        try:
            i = d.images.index(image)
        except ValueError:
            return []
        p = d.p_by_img[i]
        p = p[d.p_score[p] >= conf]
        return [(d.p_box[j].tolist(), float(d.p_score[j]), int(d.p_cls[j]), int(j)) for j in p]

    # ------------------------------------------------------------ one point per image: predicted classes
    def image_classes(self, state, m, conf=None):
        """A model's answer for every point of a one-point-per-image session (no labels to score against): per
        sample the top detection's class (an index into the MODEL's names, -1 = nothing at or above conf), its
        score and the number of detections, plus per-class totals. None when the model has not run here."""
        fp = self.fingerprint(m)
        conf = float(conf if conf is not None else m.get('conf') or self.cfg.get('default_conf', 0.25))
        key = (state.tag, m['id'], fp, round(conf, 4))
        with self._lock:
            if key in self._imgcls:
                self._imgcls.move_to_end(key)
                return self._imgcls[key]
        preds = predict.load(predict.cache_path(state, m, fp))
        if preds is None:
            return None
        det, img = preds['det'], preds['img'].astype(np.int64)
        keep = det[:, 4] >= conf
        det, img = det[keep], img[keep]
        cls = det[:, 5].astype(np.int64)
        names = [str(n) for n in preds['names']]
        top = int(cls.max()) if len(cls) else -1
        names += [f'class_{k}' for k in range(len(names), top + 1)]
        n_img = len(preds['images'])
        # the top detection per image: sort by (image, -score), the first row of each image
        o = np.lexsort((-det[:, 4], img))
        first = np.unique(img[o], return_index=True)[1] if len(o) else np.zeros(0, np.int64)
        best = o[first]
        top_cls = np.full(n_img, -1, np.int64)
        top_score = np.zeros(n_img, np.float32)
        top_cls[img[best]] = cls[best]
        top_score[img[best]] = det[best, 4]
        n_det = np.bincount(img, minlength=n_img)
        # preds are per image name, points per sample: one point per image, so map by name
        at = {n: i for i, n in enumerate(preds['images'])}
        pos = np.fromiter((at.get(s['image'], -1) for s in state.samples), np.int64, len(state.samples))
        ok = pos >= 0
        pc = np.full(len(pos), -1, np.int64)
        ps = np.zeros(len(pos), np.float32)
        nd = np.zeros(len(pos), np.int64)
        pc[ok], ps[ok], nd[ok] = top_cls[pos[ok]], top_score[pos[ok]], n_det[pos[ok]]
        nc = len(names)
        res = {'model': self.reg.public(m), 'conf': conf, 'names': names, 'ms': preds['ms'],
               'pred_cls': pc, 'pred_score': np.round(ps, 3), 'n_det': nd,
               'images_per_class': np.bincount(pc[pc >= 0], minlength=nc)[:nc].tolist(),
               'detections_per_class': np.bincount(cls, minlength=nc)[:nc].tolist() if len(cls) else [0] * nc,
               'no_detection': int((pc < 0).sum()), 'n_images': int(len(pc))}
        if getattr(state, 'origin', None) is not None and len(state.origin) == len(pc):
            res['by_origin'] = [{'alias': mem.get('alias'),
                                 'images_per_class': np.bincount(pc[(state.origin == k) & (pc >= 0)], minlength=nc)[:nc].tolist(),
                                 'no_detection': int(((state.origin == k) & (pc < 0)).sum()),
                                 'n_images': int((state.origin == k).sum())}
                                for k, mem in enumerate(state.members or [])]
        with self._lock:
            self._imgcls[key] = res
            while len(self._imgcls) > 8:
                self._imgcls.popitem(last=False)
        return res

    def image_agreement(self, state, ma, mb, conf=None):
        """Two models' top class per image side by side: the agreement table (rows: a's classes + 'nothing',
        columns: b's), the share of images where they agree (classes matched by name) and Cohen's kappa."""
        from ..ml import stats
        ra, rb = self.image_classes(state, ma, conf), self.image_classes(state, mb, conf)
        if ra is None or rb is None:
            return None
        na, nb = ra['names'] + ['(nothing)'], rb['names'] + ['(nothing)']
        ia = np.where(ra['pred_cls'] >= 0, ra['pred_cls'], len(na) - 1)
        ib = np.where(rb['pred_cls'] >= 0, rb['pred_cls'], len(nb) - 1)
        table = np.zeros((len(na), len(nb)), np.int64)
        np.add.at(table, (ia, ib), 1)
        # one shared category list (by normalised name) for agreement / kappa: the models' class orders may differ
        cats = list(dict.fromkeys(predict._norm(n) for n in na + nb))
        ca = np.asarray([cats.index(predict._norm(n)) for n in na])[ia]
        cb = np.asarray([cats.index(predict._norm(n)) for n in nb])[ib]
        sq = np.zeros((len(cats), len(cats)), np.int64)
        np.add.at(sq, (ca, cb), 1)
        k = stats.kappa(sq)
        return {'a': ra['model'], 'b': rb['model'], 'conf': ra['conf'], 'rows': na, 'cols': nb,
                'table': table.tolist(), 'agree': k['agree'], 'kappa': k['effect'], 'n': k['n_a'],
                'disagree_ids': np.nonzero(ca != cb)[0][:5000].tolist()}

    def forget(self, mid):
        from . import hub
        hub.forget(mid)                                         # its API client on the labeling side too
        with self._lock:
            for cache in (self._preds, self._ap, self._imgcls):
                for k in [k for k in cache if k[1] == mid]:
                    cache.pop(k)
        runners.release()

    def remove_upload(self, m):
        """Delete files we stored for an uploaded model (never files elsewhere)."""
        store = self.reg.store.resolve()
        for key in ('path', 'adapter'):
            p = m.get(key)
            if p and store in Path(p).resolve().parents:
                folder = Path(p).resolve()
                while folder.parent != store:
                    folder = folder.parent
                import shutil
                shutil.rmtree(folder, ignore_errors=True)
