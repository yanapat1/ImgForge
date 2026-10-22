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
            for cache in (self._preds, self._ap):
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

    def ap(self, state, m, d, split):
        """Conf-independent AP stats, cached per (dataset, model, split)."""
        akey = (state.tag, m['id'], self.fingerprint(m), split, getattr(state, 'edit_seq', 0))
        with self._lock:
            ap = self._ap.get(akey)
        if ap is None:
            ap = evaluate.ap_stats(d, split)
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
        }

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

    def forget(self, mid):
        from . import hub
        hub.forget(mid)                                         # its API client on the labeling side too
        with self._lock:
            for cache in (self._preds, self._ap):
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
