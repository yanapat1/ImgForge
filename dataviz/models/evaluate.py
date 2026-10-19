"""
Compare cached predictions with the dataset labels (pure numpy, ~10k boxes in well under a second).

Two passes:
  ap_stats()   conf-independent: class-aware matching of every prediction at IoU .50:.95 -> AP50, AP50-95,
               PR curves, P/R/F1 vs confidence, TP/FP score histograms (cached per model × split)
  outcome()    at the chosen conf + match IoU: what happened to each label box and each prediction

Label box (per sample id) status codes, also used by the page to colour the plot:
    0 tp         found with the right class
    1 wrong_cls  found (IoU >= match) but the model says another class
    2 loc        right class, badly placed (0.1 <= IoU < match)
    3 missed     nothing found
   -1            not scored (image outside the chosen split)
Prediction kinds: tp, wrong_cls, fp_loc, fp_dup (second box on an object), fp_bg (no object there).
"""

import numpy as np

STATUS = ['tp', 'wrong_cls', 'loc', 'missed']
PRED_KINDS = ['tp', 'wrong_cls', 'fp_loc', 'fp_dup', 'fp_bg']
IOUS = np.linspace(0.5, 0.95, 10)
CONF_GRID = np.linspace(0, 1, 101)


def box_iou(a, b):
    if not len(a) or not len(b):
        return np.zeros((len(a), len(b)), np.float32)
    lt = np.maximum(a[:, None, :2], b[None, :, :2])
    rb = np.minimum(a[:, None, 2:4], b[None, :, 2:4])
    inter = np.clip(rb - lt, 0, None).prod(-1)
    aa = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    ab = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return inter / np.maximum(aa[:, None] + ab[None, :] - inter, 1e-9)


class Data:
    """Labels + predictions grouped per image, in dataset class ids."""

    def __init__(self, samples, n_classes, preds, pred_cls, splits, deleted=None):
        """deleted: bool per sample (boxes a curation fix removed): not ground truth, never scored."""
        self.nc = n_classes
        self.images = preds['images']
        img_of = {n: i for i, n in enumerate(self.images)}
        self.gt_by_img = [[] for _ in self.images]
        for s in samples:
            i = img_of.get(s['image'])
            if i is not None and (deleted is None or not deleted[s['id']]):
                self.gt_by_img[i].append(s['id'])
        self.gt_box = np.asarray([s['bbox'] for s in samples], np.float32).reshape(-1, 4)
        self.gt_cls = np.asarray([s['cls'] for s in samples], np.int64)
        self.n_gt = len(samples)
        det = preds['det']
        self.p_img = preds['img'].astype(np.int64)
        self.p_box = det[:, :4]
        self.p_score = det[:, 4]
        self.p_cls = pred_cls
        order = np.argsort(self.p_img, kind='stable')
        bounds = np.searchsorted(self.p_img[order], np.arange(len(self.images) + 1))
        self.p_by_img = [order[bounds[i]:bounds[i + 1]] for i in range(len(self.images))]
        sp = [splits.get(n, 'unknown') for n in self.images]
        self.img_split = np.asarray(sp)
        self.gt_split = np.full(self.n_gt, 'unknown', dtype=object)
        for i, ids in enumerate(self.gt_by_img):
            self.gt_split[ids] = sp[i]

    def image_mask(self, split):
        if split in (None, '', 'all'):
            return np.ones(len(self.images), bool)
        return self.img_split == split


# ---------------------------------------------------------------- conf-independent: AP / PR / F1
def _match_multi(iou, same):
    """ultralytics-style: for each IoU threshold, greedy one-to-one matches of same-class pairs by IoU.
    Returns (n_pred, 10) bool tp."""
    tp = np.zeros((iou.shape[1], len(IOUS)), bool)
    for t, thr in enumerate(IOUS):
        x = np.nonzero((iou >= thr) & same)
        if not len(x[0]):
            continue
        m = np.stack([x[0], x[1], iou[x]], 1)
        if len(m) > 1:
            m = m[m[:, 2].argsort()[::-1]]
            m = m[np.unique(m[:, 1], return_index=True)[1]]
            m = m[np.unique(m[:, 0], return_index=True)[1]]
        tp[m[:, 1].astype(int), t] = True
    return tp


def _ap(recall, precision):
    mrec = np.concatenate(([0.0], recall, [1.0]))
    mpre = np.flip(np.maximum.accumulate(np.flip(np.concatenate(([1.0], precision, [0.0])))))
    x = np.linspace(0, 1, 101)
    return float(np.trapezoid(np.interp(x, mrec, mpre), x)), np.interp(x, mrec, mpre)


def ap_stats(data, split):
    imask = data.image_mask(split)
    tps, confs, pcls, tcls = [], [], [], []
    for i in np.nonzero(imask)[0]:
        g = data.gt_by_img[i]
        p = data.p_by_img[i]
        p = p[data.p_cls[p] >= 0]
        tcls.append(data.gt_cls[g])
        if not len(p):
            continue
        iou = box_iou(data.gt_box[g], data.p_box[p])
        same = data.gt_cls[g][:, None] == data.p_cls[p][None, :]
        tps.append(_match_multi(iou, same) if len(g) else np.zeros((len(p), len(IOUS)), bool))
        confs.append(data.p_score[p])
        pcls.append(data.p_cls[p])
    tp = np.concatenate(tps) if tps else np.zeros((0, len(IOUS)), bool)
    conf = np.concatenate(confs) if confs else np.zeros(0)
    pc = np.concatenate(pcls) if pcls else np.zeros(0, np.int64)
    tc = np.concatenate(tcls) if tcls else np.zeros(0, np.int64)
    order = np.argsort(-conf, kind='stable')
    tp, conf, pc = tp[order], conf[order], pc[order]

    nc = data.nc
    ap = np.zeros((nc, len(IOUS)))
    n_lbl = np.bincount(tc, minlength=nc)[:nc]
    pr_curves = np.zeros((nc, 101))
    p_conf = np.zeros((nc, len(CONF_GRID)))
    r_conf = np.zeros((nc, len(CONF_GRID)))
    for c in range(nc):
        sel = pc == c
        n_p = int(sel.sum())
        if not n_p or not n_lbl[c]:
            continue
        tpc = tp[sel].cumsum(0)
        fpc = (1 - tp[sel]).cumsum(0)
        recall = tpc / (n_lbl[c] + 1e-16)
        precision = tpc / (tpc + fpc)
        # P/R as a function of the conf threshold (scores are descending: interpolate on -conf)
        r_conf[c] = np.interp(-CONF_GRID, -conf[sel], recall[:, 0], left=0)
        p_conf[c] = np.interp(-CONF_GRID, -conf[sel], precision[:, 0], left=1)
        for t in range(len(IOUS)):
            ap[c, t], curve = _ap(recall[:, t], precision[:, t])
            if t == 0:
                pr_curves[c] = curve
    present = n_lbl > 0
    f1 = 2 * p_conf * r_conf / (p_conf + r_conf + 1e-16)
    f1_all = f1[present].mean(0) if present.any() else np.zeros(len(CONF_GRID))
    best = int(f1_all.argmax())
    tp50 = tp[:, 0]
    bins = np.linspace(0, 1, 21)
    return {
        'n_labels': n_lbl.tolist(),
        'ap50': ap[:, 0].tolist(), 'ap': ap.mean(1).tolist(),
        'map50': float(ap[present, 0].mean()) if present.any() else 0.0,
        'map': float(ap[present].mean()) if present.any() else 0.0,
        'pr': pr_curves.tolist(),
        'pr_all': pr_curves[present].mean(0).tolist() if present.any() else [],
        'conf_grid': CONF_GRID.tolist(),
        'p_conf': p_conf.tolist(), 'r_conf': r_conf.tolist(), 'f1_conf': f1.tolist(),
        'p_conf_all': (p_conf[present].mean(0) if present.any() else np.zeros(101)).tolist(),
        'r_conf_all': (r_conf[present].mean(0) if present.any() else np.zeros(101)).tolist(),
        'f1_conf_all': f1_all.tolist(),
        'best_conf': float(CONF_GRID[best]), 'best_f1': float(f1_all[best]),
        'hist_bins': bins.tolist(),
        'hist_tp': np.histogram(conf[tp50], bins)[0].tolist(),
        'hist_fp': np.histogram(conf[~tp50], bins)[0].tolist(),
    }


# ---------------------------------------------------------------- at a conf threshold: what happened to each box
def outcome(data, conf, match_iou, split):
    n_gt, n_p = data.n_gt, len(data.p_score)
    status = np.full(n_gt, -1, np.int8)
    g_pcls = np.full(n_gt, -1, np.int64)
    g_score = np.zeros(n_gt, np.float32)
    g_iou = np.zeros(n_gt, np.float32)
    g_pid = np.full(n_gt, -1, np.int64)
    p_kind = np.full(n_p, -1, np.int8)                  # index into PRED_KINDS; -1 = below conf / outside split
    p_gt = np.full(n_p, -1, np.int64)
    imask = data.image_mask(split)
    for i in np.nonzero(imask)[0]:
        g = np.asarray(data.gt_by_img[i], np.int64)
        p = data.p_by_img[i]
        p = p[(data.p_score[p] >= conf) & (data.p_cls[p] >= 0)]      # classes the dataset lacks are not scored
        p = p[np.argsort(-data.p_score[p], kind='stable')]
        status[g] = 3
        if not len(p):
            continue
        if not len(g):
            p_kind[p] = 4
            continue
        iou = box_iou(data.gt_box[g], data.p_box[p])          # (G, P)
        gc, pc = data.gt_cls[g], data.p_cls[p]
        g_used = np.zeros(len(g), bool)
        p_used = np.zeros(len(p), bool)
        # 1. right class, IoU >= match: greedy by score
        for j in range(len(p)):
            cand = np.where(~g_used & (gc == pc[j]) & (iou[:, j] >= match_iou), iou[:, j], -1)
            k = int(cand.argmax())
            if cand[k] >= 0:
                g_used[k] = p_used[j] = True
                status[g[k]], g_pcls[g[k]], g_score[g[k]], g_iou[g[k]], g_pid[g[k]] = 0, pc[j], data.p_score[p[j]], iou[k, j], p[j]
                p_kind[p[j]], p_gt[p[j]] = 0, g[k]
        # 2. another class on an unmatched label: wrong class
        for j in np.nonzero(~p_used)[0]:
            cand = np.where(~g_used & (iou[:, j] >= match_iou), iou[:, j], -1)
            k = int(cand.argmax())
            if cand[k] >= 0:
                g_used[k] = p_used[j] = True
                status[g[k]], g_pcls[g[k]], g_score[g[k]], g_iou[g[k]], g_pid[g[k]] = 1, pc[j], data.p_score[p[j]], iou[k, j], p[j]
                p_kind[p[j]], p_gt[p[j]] = 1, g[k]
        # 3. right class but badly placed
        for j in np.nonzero(~p_used)[0]:
            cand = np.where(~g_used & (gc == pc[j]) & (iou[:, j] >= 0.1), iou[:, j], -1)
            k = int(cand.argmax())
            if cand[k] >= 0:
                g_used[k] = p_used[j] = True
                status[g[k]], g_pcls[g[k]], g_score[g[k]], g_iou[g[k]], g_pid[g[k]] = 2, pc[j], data.p_score[p[j]], iou[k, j], p[j]
                p_kind[p[j]], p_gt[p[j]] = 2, g[k]
        # 4. leftovers: a second box on a found object, or nothing there
        for j in np.nonzero(~p_used)[0]:
            k = int(iou[:, j].argmax())
            if iou[k, j] >= match_iou:
                p_kind[p[j]], p_gt[p[j]] = 3, g[k]
            else:
                p_kind[p[j]] = 4
    return {'status': status, 'pred_cls': g_pcls, 'pred_score': g_score, 'iou': g_iou, 'pid': g_pid,
            'p_kind': p_kind, 'p_gt': p_gt}


def summary(data, out, ap, split):
    """Per-class table + confusion matrix at the chosen conf."""
    nc = data.nc
    st, pk = out['status'], out['p_kind']
    scored = st >= 0
    gt_cls = data.gt_cls
    tp = np.bincount(gt_cls[st == 0], minlength=nc)[:nc]
    n_lbl = np.bincount(gt_cls[scored], minlength=nc)[:nc]
    pm = pk >= 0
    pcls = data.p_cls
    n_pred = np.bincount(pcls[pm], minlength=nc)[:nc]
    fp = n_pred - np.bincount(pcls[pk == 0], minlength=nc)[:nc]
    per = []
    for c in range(nc):
        per.append({'cls': c, 'n': int(n_lbl[c]), 'tp': int(tp[c]),
                    'p': float(tp[c] / n_pred[c]) if n_pred[c] else None,
                    'r': float(tp[c] / n_lbl[c]) if n_lbl[c] else None,
                    'ap50': ap['ap50'][c] if n_lbl[c] else None, 'ap': ap['ap'][c] if n_lbl[c] else None,
                    'wrong_cls': int(((st == 1) & (gt_cls == c)).sum()), 'loc': int(((st == 2) & (gt_cls == c)).sum()),
                    'missed': int(((st == 3) & (gt_cls == c)).sum()),
                    'fp_bg': int(((pk == 4) & (pcls == c)).sum()), 'fp_dup': int(((pk == 3) & (pcls == c)).sum())})
    # confusion: rows = true class (+ background), cols = predicted class (+ nothing found)
    cm = np.zeros((nc + 1, nc + 1), np.int64)
    sel = (st == 0) | ((st == 1) & (out['pred_cls'] >= 0))
    np.add.at(cm, (gt_cls[sel], out['pred_cls'][sel]), 1)
    sel = st >= 2
    np.add.at(cm, (gt_cls[sel], nc), 1)
    sel = pk >= 3
    np.add.at(cm, (nc, pcls[sel]), 1)
    tot_tp, tot_lbl, tot_pred = int(tp.sum()), int(n_lbl.sum()), int(n_pred.sum())
    n_img = int(data.image_mask(split).sum())
    return {
        'per_class': per, 'confusion': cm.tolist(),
        'totals': {'labels': tot_lbl, 'preds': tot_pred, 'tp': tot_tp, 'images': n_img,
                   'p': tot_tp / tot_pred if tot_pred else 0.0, 'r': tot_tp / tot_lbl if tot_lbl else 0.0,
                   'wrong_cls': int((st == 1).sum()), 'loc': int((st == 2).sum()), 'missed': int((st == 3).sum()),
                   'fp_bg': int((pk == 4).sum()), 'fp_dup': int((pk == 3).sum()), 'fp_loc': int((pk == 2).sum())},
    }


def label_issues(data, out, min_score=0.5, limit=300):
    """Places where the model and the label disagree confidently: likely label errors, best first."""
    items = []
    st, sc = out['status'], out['pred_score']
    img_of = {}
    for i, ids in enumerate(data.gt_by_img):
        for s in ids:
            img_of[s] = i
    for sid in np.nonzero((st == 1) & (sc >= min_score))[0]:
        items.append({'kind': 'wrong_cls', 'sid': int(sid), 'pid': int(out['pid'][sid]), 'score': float(sc[sid]),
                      'cls': int(data.gt_cls[sid]), 'pred_cls': int(out['pred_cls'][sid]),
                      'image': data.images[img_of[int(sid)]]})
    for pid in np.nonzero((out['p_kind'] == 4) & (data.p_score >= min_score) & (data.p_cls >= 0))[0]:
        items.append({'kind': 'fp_bg', 'pid': int(pid), 'score': float(data.p_score[pid]),
                      'pred_cls': int(data.p_cls[pid]), 'image': data.images[int(data.p_img[pid])]})
    # labels nothing overlaps at all, even far below the threshold: maybe a stray / wrong box
    for sid in np.nonzero(st == 3)[0]:
        items.append({'kind': 'missed', 'sid': int(sid), 'score': 0.0, 'cls': int(data.gt_cls[sid]),
                      'image': data.images[img_of[int(sid)]]})
    missed = [it for it in items if it['kind'] == 'missed']
    others = sorted([it for it in items if it['kind'] != 'missed'], key=lambda it: -it['score'])
    # rank missed labels by how little the model saw there (max score of any overlapping prediction)
    for it in missed:
        p = data.p_by_img[img_of[it['sid']]]
        if len(p):
            iou = box_iou(data.gt_box[[it['sid']]], data.p_box[p])[0]
            near = iou >= 0.3
            it['score'] = float(data.p_score[p][near].max()) if near.any() else 0.0
    missed.sort(key=lambda it: it['score'])
    return (others + missed)[:limit]
