"""
The Evaluation tab: compare every model that ran on the open dataset, explain one model's results with the
dataset (class balance, train/val gap, sessions, box sizes, localization, calibration, kNN agreement), and
turn it all into a short list of plain-language findings, each with a click-through action.

Pure numpy on top of evaluate.py and ModelService's caches; ~0.1 s per model on ~10k boxes.
"""

from pathlib import Path

import numpy as np

from ..data.indexer import parse_session_and_frame
from . import evaluate

SMALL, MEDIUM = 32 * 32, 96 * 96          # COCO size buckets (pixels²)
MAX_IDS = 3000                            # ids sent per clickable chart cell


def _size_mb(m):
    p = Path(m.get('path') or '')
    if not m.get('path') or not p.exists():
        return None
    if p.is_file():
        return round(p.stat().st_size / 1e6, 1)
    return round(sum(f.stat().st_size for f in p.rglob('*') if f.is_file()) / 1e6, 1)


def _f1(p, r):
    return 2 * p * r / (p + r) if p + r else 0.0


def _ids(mask, limit=MAX_IDS):
    return [int(i) for i in np.nonzero(mask)[0][:limit]]


def _scored(svc, state, m, split, conf_mode, conf, match_iou):
    """svc.score() at the model's best-F1 conf (conf_mode='best') or at `conf`."""
    got = svc.data(state, m)
    if got is None:
        return None
    d = got[1]
    split, _ = svc.pick_split(d, split)
    if conf_mode == 'best':
        conf = svc.ap(state, m, d, split)['best_conf']
    return svc.score(state, m, conf, match_iou, split)


# ---------------------------------------------------------------- compare models
def leaderboard(svc, state, models, split='val', conf_mode='best', conf=None, match_iou=None):
    rows, ap_rows, not_run = [], [], []
    used_split = split
    for m in models:
        got = _scored(svc, state, m, split, conf_mode, conf, match_iou)
        if got is None:
            not_run.append(svc.reg.public(m))
            continue
        preds, d, unmatched, used_split, avail, ap, out, summ, c, iou = got
        t = summ['totals']
        rows.append({
            'id': m['id'], 'name': m['name'], 'family': m['family'], 'format': m['format'],
            'map50': ap['map50'], 'map': ap['map'], 'p': t['p'], 'r': t['r'], 'f1': _f1(t['p'], t['r']),
            'conf': c, 'best_conf': ap['best_conf'], 'ms': preds['ms'], 'size_mb': _size_mb(m),
            'preds': t['preds'], 'tp': t['tp'], 'labels': t['labels'], 'images': t['images'],
            'missed': t['missed'] + t['loc'], 'wrong_cls': t['wrong_cls'], 'fp_bg': t['fp_bg'], 'fp_dup': t['fp_dup'],
            'unmatched': unmatched,
        })
        ap_rows.append([a if n else None for a, n in zip(ap['ap50'], ap['n_labels'])])
    nc = len(state.classes)
    winners = []
    for c in range(nc):
        vals = [(r[c], i) for i, r in enumerate(ap_rows) if r[c] is not None]
        winners.append(rows[max(vals)[1]]['id'] if vals else None)
    best = max(rows, key=lambda r: r['map']) if rows else None
    fastest = None
    if best:
        near = [r for r in rows if r['map'] >= best['map'] - 0.01 and r['ms']]
        fastest = min(near, key=lambda r: r['ms']) if near else None
    return {'rows': rows, 'per_class': {'classes': state.classes, 'models': [r['id'] for r in rows], 'ap50': ap_rows,
                                        'winner': winners},
            'best': best['id'] if best else None, 'fastest_near_best': fastest['id'] if fastest else None,
            'not_run': not_run, 'split': used_split}


def compare(svc, state, ma, mb, split='val', conf_mode='best', conf=None, match_iou=None, limit=MAX_IDS):
    """Model A vs model B, label by label: which labels only one of them finds, which both miss, and the false
    positives each makes per class. Label indices are sample ids in both, so the outcomes line up directly.
    None when either model has not run on this dataset."""
    ga = _scored(svc, state, ma, split, conf_mode, conf, match_iou)
    gb = _scored(svc, state, mb, split, conf_mode, conf, match_iou)
    if ga is None or gb is None:
        return None
    (_, da, _, split_a, _, ap_a, out_a, summ_a, conf_a, iou), (_, db, _, _, _, ap_b, out_b, _, conf_b, _) = ga, gb
    sa, sb = out_a['status'], out_b['status']
    both = (sa >= 0) & (sb >= 0)          # labels in the split on images both models ran on
    fa, fb = both & (sa == 0), both & (sb == 0)
    only_a, only_b, miss = fa & ~fb, fb & ~fa, both & ~fa & ~fb
    nc = len(state.classes)
    cls = da.gt_cls

    def per_cls(mask):
        return np.bincount(cls[mask], minlength=nc).tolist()

    def fp_cls(d, out):
        p = np.nonzero(out['p_kind'] >= 3)[0]             # fp_dup + fp_bg, as in the leaderboard
        return np.bincount(d.p_cls[p], minlength=nc).tolist()

    def lost(mask, st):
        """How the other model failed on these labels: counts of wrong class / bad box / missed."""
        return {evaluate.STATUS[k]: int(((st == k) & mask).sum()) for k in (1, 2, 3)}

    ap50 = lambda ap: [a if n else None for a, n in zip(ap['ap50'], ap['n_labels'])]
    return {
        'a': svc.reg.public(ma), 'b': svc.reg.public(mb), 'split': split_a, 'conf_mode': conf_mode,
        'conf_a': conf_a, 'conf_b': conf_b, 'match_iou': iou,
        'labels': int(both.sum()),
        'counts': {'tp_a': int(fa.sum()), 'tp_b': int(fb.sum()), 'only_a': int(only_a.sum()),
                   'only_b': int(only_b.sum()), 'both_miss': int(miss.sum())},
        'ids': {'only_a': _ids(only_a, limit), 'only_b': _ids(only_b, limit), 'both_miss': _ids(miss, limit)},
        'b_lost': lost(only_a, sb), 'a_lost': lost(only_b, sa),
        'per_class': {'classes': state.classes, 'labels': per_cls(both), 'tp_a': per_cls(fa), 'tp_b': per_cls(fb),
                      'only_a': per_cls(only_a), 'only_b': per_cls(only_b), 'both_miss': per_cls(miss),
                      'fp_a': fp_cls(da, out_a), 'fp_b': fp_cls(db, out_b),
                      'ap50_a': ap50(ap_a), 'ap50_b': ap50(ap_b)},
        'map_a': ap_a['map'], 'map_b': ap_b['map'], 'map50_a': ap_a['map50'], 'map50_b': ap_b['map50'],
    }


# ---------------------------------------------------------------- one model vs the dataset
def _knn_scores(state):
    for key in [state.default_feature, *state.scores.keys()]:
        sc = state.scores.get(key)
        if sc is None and state.ready and state.feature_cached(key):
            try:
                sc = state.get_scores(key)
            except Exception:  # noqa: BLE001 - optional
                sc = None
        if sc is not None:
            return key, sc
    return None, None


def dataset_insights(svc, state, m, split='val', conf_mode='best', conf=None, match_iou=None):
    got = _scored(svc, state, m, split, conf_mode, conf, match_iou)
    if got is None:
        return None
    preds, d, unmatched, split, avail, ap, out, summ, conf, match_iou = got
    nc = len(state.classes)
    st, iou = out['status'], out['iou']
    scored = st >= 0
    gcls = d.gt_cls
    samples = state.samples

    # class balance: what the model learned from (train labels) vs how well it does here
    train_mask = d.gt_split == 'train'
    has_train = bool(train_mask.any())
    n_train = np.bincount(gcls[train_mask if has_train else np.ones(len(gcls), bool)], minlength=nc)[:nc]
    present = [c for c in range(nc) if ap['n_labels'][c]]
    x = np.log10(np.maximum(n_train[present], 1))
    y = np.asarray([ap['ap50'][c] for c in present])
    corr = float(np.corrcoef(x, y)[0, 1]) if len(present) > 2 and x.std() > 0 and y.std() > 0 else None
    balance = {'train_labels': n_train.tolist(), 'from_train_split': has_train, 'corr_log_count_ap50': corr}

    # train vs val gap (overfitting): both conf-independent, cached
    gap = None
    if avail.get('train') and avail.get('val'):
        a_tr, a_va = svc.ap(state, m, d, 'train'), svc.ap(state, m, d, 'val')
        gap = {'train_ap50': a_tr['ap50'], 'val_ap50': a_va['ap50'], 'train_ap': a_tr['ap'], 'val_ap': a_va['ap'],
               'n_val': a_va['n_labels'], 'map50': [a_tr['map50'], a_va['map50']], 'map': [a_tr['map'], a_va['map']]}

    # per session (scene / recording): labels, found, missed, wrong class, false positives
    img_sess = np.asarray([parse_session_and_frame(n)[0] for n in d.images])
    g_sess = np.asarray([s['session'] for s in samples]) if samples else np.zeros(0, str)
    pk = out['p_kind']
    fp_mask = pk >= 3
    sessions = []
    for name in np.unique(img_sess[d.image_mask(split)]):
        gm = scored & (g_sess == name)
        n = int(gm.sum())
        tp = int((gm & (st == 0)).sum())
        fp = int((fp_mask & (img_sess[d.p_img] == name)).sum())
        n_pred = tp + int((gm & (st == 1)).sum()) + fp
        sessions.append({'session': str(name), 'labels': n, 'tp': tp, 'missed': int((gm & (st >= 2)).sum()),
                         'wrong_cls': int((gm & (st == 1)).sum()), 'fp': fp,
                         'images': int(((img_sess == name) & d.image_mask(split)).sum()),
                         'recall': tp / n if n else None, 'precision': tp / n_pred if n_pred else None,
                         'ids': _ids(gm & (st > 0))})
    sessions.sort(key=lambda s: (s['recall'] if s['recall'] is not None else 1, -s['labels']))

    # worst images: most mistakes (labels not found right + false positives)
    img_err = np.zeros(len(d.images), np.int64)
    img_lbl = np.zeros(len(d.images), np.int64)
    img_fp = np.bincount(d.p_img[fp_mask], minlength=len(d.images))
    for i, ids in enumerate(d.gt_by_img):
        if ids:
            s_ = st[ids]
            img_err[i] = int((s_ > 0).sum())
            img_lbl[i] = int((s_ >= 0).sum())
    total = img_err + img_fp
    total[~d.image_mask(split)] = -1
    worst = []
    for i in np.argsort(-total, kind='stable')[:24]:
        if total[i] <= 0:
            break
        fps = np.nonzero(fp_mask & (d.p_img == i))[0]
        worst.append({'image': d.images[i], 'labels': int(img_lbl[i]), 'errors': int(total[i]), 'fp': int(img_fp[i]),
                      'missed': int(img_err[i]), 'sid': int(d.gt_by_img[i][0]) if d.gt_by_img[i] else None,
                      'pid': int(fps[0]) if len(fps) else None,
                      'focus': next((int(s) for s in d.gt_by_img[i] if st[s] > 0), None)})

    # localization quality of the boxes it found right
    tp_mask = st == 0
    loc = []
    for c in range(nc):
        v = iou[tp_mask & (gcls == c)]
        loc.append({'cls': c, 'n': int(len(v)), 'mean': float(v.mean()) if len(v) else None,
                    'p10': float(np.percentile(v, 10)) if len(v) else None,
                    'loose': float((v < 0.75).mean()) if len(v) else None})
    all_iou = iou[tp_mask]
    loc_hist = np.histogram(all_iou, np.linspace(0.5, 1, 26))[0].tolist() if len(all_iou) else []

    # calibration: does a 0.8 score mean 80% of those predictions are right?
    tp_h, fp_h = np.asarray(ap['hist_tp'], float), np.asarray(ap['hist_fp'], float)
    nb = tp_h + fp_h
    mids = (np.asarray(ap['hist_bins'][:-1]) + np.asarray(ap['hist_bins'][1:])) / 2
    prec = np.where(nb > 0, tp_h / np.maximum(nb, 1), np.nan)
    keep = nb > 0
    ece = float(np.sum(nb[keep] / nb[keep].sum() * np.abs(prec[keep] - mids[keep]))) if keep.any() else None
    # over/under-confident above the threshold that matters
    hi = keep & (mids >= 0.3)
    bias = float(np.sum(nb[hi] * (mids[hi] - prec[hi])) / nb[hi].sum()) if hi.any() else None
    calib = {'conf': mids.tolist(), 'precision': [None if np.isnan(v) else float(v) for v in prec],
             'count': nb.astype(int).tolist(), 'ece': ece, 'bias': bias}

    # box size: COCO small / medium / large
    area = np.asarray([s['area_px'] for s in samples], np.float64) if samples else np.zeros(0)
    sizes = []
    for name, lo, hi_ in (('small', 0, SMALL), ('medium', SMALL, MEDIUM), ('large', MEDIUM, np.inf)):
        gm = scored & (area >= lo) & (area < hi_)
        n = int(gm.sum())
        sizes.append({'size': name, 'labels': n, 'tp': int((gm & tp_mask).sum()),
                      'recall': float((gm & tp_mask).sum() / n) if n else None, 'ids': _ids(gm & (st > 0))})

    # model vs kNN neighbours: two independent signals that a label is wrong
    key, sc = _knn_scores(state)
    agree = None
    if sc is not None:
        knn_bad = (np.asarray(sc['suggested']) != gcls) & (np.asarray(sc['suspicion']) >= 0.5)
        m_bad = st > 0
        cells = {'both': scored & m_bad & knn_bad, 'model_only': scored & m_bad & ~knn_bad,
                 'knn_only': scored & ~m_bad & knn_bad, 'neither': scored & ~m_bad & ~knn_bad}
        agree = {'feature': key, **{k: {'n': int(v.sum()), 'ids': _ids(v)} for k, v in cells.items()},
                 'wrong_cls_both': _ids(scored & (st == 1) & knn_bad & (np.asarray(sc['suggested']) == out['pred_cls']))}

    return {'model': svc.reg.public(m), 'split': split, 'splits': avail, 'conf': conf, 'match_iou': match_iou,
            'ap': {k: ap[k] for k in ('ap50', 'ap', 'map50', 'map', 'n_labels', 'best_conf', 'best_f1')},
            'per_class': summ['per_class'], 'totals': summ['totals'], 'unmatched': unmatched,
            'balance': balance, 'gap': gap, 'sessions': sessions, 'worst': worst,
            'localization': {'per_class': loc, 'hist': loc_hist, 'mean': float(all_iou.mean()) if len(all_iou) else None},
            'calibration': calib, 'sizes': sizes, 'knn': agree}


# ---------------------------------------------------------------- plain-language findings
def findings(lb, ins, classes, split_source, curation=None):
    """Ranked findings: severity 3 = act on it, 2 = worth a look, 1 = good to know. curation: the open dataset's
    scanned data.curation.Curation (its split_leak items), or None."""
    out = []

    def add(sev, title, detail, action=None, tag=''):
        out.append({'severity': sev, 'title': title, 'detail': detail, 'action': action, 'tag': tag})

    rows = {r['id']: r for r in lb['rows']}
    if not split_source or (ins and not ins['splits'].get('val')):
        add(3, 'No train/val split found', 'Every image is scored, including the ones the model trained on, so the '
            'numbers are too optimistic. Set models.split_source in config.yaml.', None, 'data')
    leaks = [it for it in curation.all_items() if it['type'] == 'split_leak'] if curation is not None else []
    if leaks:
        imgs = {it['image'] for it in leaks}
        ids = sorted({b for it in leaks for b in it['box_ids']})
        add(3, f'{len(imgs)} held-out image{"s" if len(imgs) > 1 else ""} leak from train',
            'Copies or near-copies of training images are scored as val / test, so the numbers are too optimistic. '
            'Move them to one split (Curation › Splits lists the pairs).',
            {'kind': 'select', 'ids': ids[:MAX_IDS], 'title': 'train / val leakage'}, 'data')
    if len(rows) > 1 and lb['best']:
        b = rows[lb['best']]
        f = rows.get(lb['fastest_near_best'])
        add(1, f'Best model: {b["name"]}', f'mAP50-95 {b["map"]:.3f} · mAP50 {b["map50"]:.3f} · {b["ms"]:.1f} ms/image.',
            {'kind': 'model', 'mid': b['id']}, 'compare')
        if f and f['id'] != b['id']:
            add(2, f'{f["name"]} is nearly as good and faster',
                f'{f["map"]:.3f} vs {b["map"]:.3f} mAP50-95 (−{(b["map"] - f["map"]) * 100:.1f} pt) at {f["ms"]:.1f} vs '
                f'{b["ms"]:.1f} ms/image ({b["ms"] / max(f["ms"], 1e-6):.1f}× faster).', {'kind': 'model', 'mid': f['id']}, 'compare')
        other = {}
        ids = lb['per_class']['models']
        bi = ids.index(b['id'])
        for c, w in enumerate(lb['per_class']['winner']):
            if w and w != b['id']:
                wa, ba = lb['per_class']['ap50'][ids.index(w)][c], lb['per_class']['ap50'][bi][c]
                if ba is not None and wa - ba >= 0.01:          # a real margin, not 0.995 vs 0.994
                    other.setdefault(w, []).append(f'{classes[c]} (+{(wa - ba) * 100:.1f})')
        for mid, cl in other.items():
            if cl:
                add(1, f'{rows[mid]["name"]} beats the best model on {len(cl)} class{"es" if len(cl) > 1 else ""}', ', '.join(cl[:6]) +
                    ('…' if len(cl) > 6 else '') + ': worth checking if those classes matter most.', {'kind': 'model', 'mid': mid}, 'compare')
    if not ins:
        return sorted(out, key=lambda f: -f['severity'])

    mid = ins['model']['id']
    pc = [c for c in ins['per_class'] if c['n']]
    if pc:
        weak = min(pc, key=lambda c: (c['ap50'] if c['ap50'] is not None else 1))
        n_bad = weak['missed'] + weak['loc'] + weak['wrong_cls']
        add(3 if (weak['ap50'] or 0) < 0.9 else 2, f'Weakest class: {classes[weak["cls"]]}',
            f'AP50 {weak["ap50"]:.3f}, recall {(weak["r"] or 0) * 100:.1f}% — {n_bad} of {weak["n"]} labels not found right '
            f'({weak["wrong_cls"]} called another class, {weak["missed"]} missed).', {'kind': 'model', 'mid': mid, 'cls': weak['cls']}, 'class')
        for c in pc:
            if c is not weak and c['r'] is not None and c['r'] < 0.9 and c['n'] >= 10:
                add(2, f'{classes[c["cls"]]}: recall {c["r"] * 100:.1f}%', f'{c["missed"] + c["loc"] + c["wrong_cls"]} of {c["n"]} '
                    'labels not found right.', {'kind': 'model', 'mid': mid, 'cls': c['cls']}, 'class')
        for c in pc:
            fp = c['fp_bg'] + c['fp_dup']
            if c['n'] >= 10 and fp >= max(5, 0.15 * c['n']):
                dup = c['fp_dup'] / fp if fp else 0
                add(2, f'{classes[c["cls"]]}: {fp} false positives', (f'{dup * 100:.0f}% are a second box on an object already '
                    'found: try a lower NMS IoU.' if dup >= 0.5 else f'{c["fp_bg"]} where nothing is labelled: check for '
                    'missing labels before blaming the model.'), {'kind': 'model', 'mid': mid, 'cls': c['cls']}, 'class')
    knn = ins.get('knn')
    if knn and knn['both']['n']:
        add(3, f'{knn["both"]["n"]} labels both the model and their neighbours disagree with',
            'Two independent signals say these boxes look wrong: the best place to start relabelling.',
            {'kind': 'select', 'ids': knn['both']['ids'], 'title': 'model + kNN both disagree with the label'}, 'labels')
    gap = ins.get('gap')
    if gap:
        g = gap['map'][0] - gap['map'][1]
        if g > 0.05:
            add(2, f'Overfitting: train mAP50-95 {gap["map"][0]:.3f} vs val {gap["map"][1]:.3f}',
                'The model does clearly better on images it trained on: more varied data or stronger augmentation would help.', None, 'data')
        cls_gap = [(gap['train_ap50'][c] - gap['val_ap50'][c], c) for c in range(len(classes)) if gap['n_val'][c] >= 5]
        cls_gap.sort(reverse=True)
        for gv, c in cls_gap[:3]:
            if gv > 0.05:
                add(2, f'{classes[c]}: {gv * 100:.1f} pt better on train than val', 'This class does not generalise to new images yet.',
                    {'kind': 'model', 'mid': mid, 'cls': c}, 'data')
    bal = ins['balance']
    n_tr = bal['train_labels']
    if pc:
        med = float(np.median([n_tr[c['cls']] for c in pc]))
        for c in pc:
            if n_tr[c['cls']] < 0.3 * med and (c['ap50'] or 0) < 0.95:
                add(2, f'{classes[c["cls"]]}: few training labels ({n_tr[c["cls"]]})', f'{n_tr[c["cls"]]} vs a median of {med:.0f} '
                    f'per class and AP50 {c["ap50"]:.3f}: more examples of this class should help most.', {'kind': 'model', 'mid': mid, 'cls': c['cls']}, 'data')
    ss = [s for s in ins['sessions'] if s['labels'] >= 20 and s['recall'] is not None]
    if len(ss) > 1:
        worst = ss[0]
        avg = sum(s['tp'] for s in ss) / max(1, sum(s['labels'] for s in ss))
        if worst['recall'] < avg - 0.03:
            add(2, f'Weakest session: {worst["session"]}', f'recall {worst["recall"] * 100:.1f}% vs {avg * 100:.1f}% overall '
                f'({worst["missed"] + worst["wrong_cls"]} labels not found right in {worst["images"]} images): a different scene, '
                'camera or lighting?', {'kind': 'select', 'ids': worst['ids'], 'title': f'session {worst["session"]}: model mistakes'}, 'data')
    sz = {s['size']: s for s in ins['sizes']}
    if sz['small']['labels'] >= 10 and sz['large']['recall'] and sz['small']['recall'] is not None \
            and sz['small']['recall'] < sz['large']['recall'] - 0.05:
        add(2, f'Small boxes: recall {sz["small"]["recall"] * 100:.1f}%', f'vs {sz["large"]["recall"] * 100:.1f}% for large '
            f'boxes ({sz["small"]["labels"]} small labels): a larger input size or tiling would help.',
            {'kind': 'select', 'ids': sz['small']['ids'], 'title': 'small boxes the model got wrong'}, 'data')
    cal = ins['calibration']
    if cal['bias'] is not None and abs(cal['bias']) > 0.1:
        add(1, 'Over-confident scores' if cal['bias'] > 0 else 'Under-confident scores',
            f'Above 0.3, predictions are right {abs(cal["bias"]) * 100:.0f} pt {"less" if cal["bias"] > 0 else "more"} often than '
            'their score says: pick the threshold from the F1 curve, not the score.', None, 'model')
    loc = ins['localization']
    if loc['mean'] is not None and loc['mean'] < 0.8:
        add(1, f'Loose boxes: mean IoU {loc["mean"]:.2f}', 'Found objects are boxed loosely; mAP50-95 suffers. Check label box '
            'consistency.', None, 'model')
    return sorted(out, key=lambda f: -f['severity'])
