"""
Label quality with cleanlab: k-fold out-of-fold class probabilities of every box, then cleanlab on them.

    oof_pred_probs   (N, C) probabilities from a classifier that never saw the box (StratifiedKFold)
    cleanlab_report  per box: label quality, label issue + rank, suggested class, OOD scores;
                     per dataset: confident joint, class ranking, overlapping classes, health score
    od_report        cleanlab.object_detection on a detector's predictions (Model tab): per image quality,
                     per label box bad-location / swapped-class scores, per prediction overlooked-box scores

Pure computation on arrays, like analysis.py: core/workers.py runs it in the heavy worker process. cleanlab and
sklearn are imported inside the functions, so a missing package only disables this option.
"""

import hashlib
import json

import numpy as np

from . import analysis

DEFAULTS = {
    'folds': 5,
    'classifier': 'logreg',            # logreg | knn | mlp
    'pca_dims': 256,                   # reduce wider features first (speed; logreg barely changes)
    'quality_method': 'self_confidence',   # self_confidence | normalized_margin | confidence_weighted_entropy
    'filter_by': 'prune_by_noise_rate',    # cleanlab.filter.find_label_issues filter_by
    'ood': True,                       # out-of-distribution scores (features + pred_probs)
    'min_class_count': 5,              # smaller classes are left out (cleanlab's per-class thresholds need examples)
    'max_iter': 300,
    'seed': 0,
}
CLASSIFIERS = ('logreg', 'knn', 'mlp')
PER_BOX = ('pred_probs', 'cl_quality', 'cl_issue', 'cl_rank', 'cl_suggested', 'cl_given_prob', 'cl_margin',
           'cl_ood_feat', 'cl_ood_prob', 'cl_scored')


def _noop(*_, **__):
    pass


def settings(cfg=None, **override):
    """DEFAULTS + config `analysis.label_quality` + per-request overrides (None values ignored), validated."""
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (cfg or {}).items() if k in DEFAULTS})
    out.update({k: v for k, v in override.items() if k in DEFAULTS and v is not None})
    out['folds'] = max(2, min(20, int(out['folds'])))
    out['pca_dims'] = max(2, int(out['pca_dims']))
    out['min_class_count'] = max(2, int(out['min_class_count']))
    if out['classifier'] not in CLASSIFIERS:
        raise ValueError(f'label_quality.classifier must be one of {CLASSIFIERS}, got {out["classifier"]!r}')
    return out


def settings_hash(s):
    """8 hex chars naming the cache file: a changed setting recomputes."""
    return hashlib.sha1(json.dumps(s, sort_keys=True).encode()).hexdigest()[:8]


def _classifier(kind, n, max_iter, seed):
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    if kind == 'knn':
        from sklearn.neighbors import KNeighborsClassifier
        return KNeighborsClassifier(n_neighbors=min(15, max(1, n - 1)), weights='distance')
    if kind == 'mlp':
        from sklearn.neural_network import MLPClassifier
        return make_pipeline(StandardScaler(), MLPClassifier((256,), early_stopping=n >= 200, max_iter=max_iter,
                                                             random_state=seed))
    from sklearn.linear_model import LogisticRegression
    # L2-normalized embeddings have tiny coordinates: standardize, or logreg underfits badly
    return make_pipeline(StandardScaler(), LogisticRegression(max_iter=max_iter))


def _reduce(emb, dims, seed):
    x = np.asarray(emb, np.float32)
    if x.shape[1] > dims and len(x) > dims:
        x, _ = analysis.pca_reduce(x, dims, seed=seed)
    return np.ascontiguousarray(x, np.float32)


def oof_pred_probs(emb, labels, folds=5, classifier='logreg', pca_dims=256, seed=0, max_iter=300, progress=_noop,
                   x=None):
    """Out-of-fold probabilities (N, C), C = labels.max() + 1, plus {accuracy, log_loss, folds}.

    Every box is predicted by a classifier trained on the other folds. Folds drop to the smallest class count
    (at least 2); a class with one box can't be held out, so its box gets the probabilities of a model that never
    saw that class (an honest 'nothing like it' answer)."""
    from sklearn.model_selection import StratifiedKFold
    labels = np.asarray(labels, np.int64)
    n = len(labels)
    n_cls = int(labels.max()) + 1 if n else 0
    probs = np.zeros((n, n_cls), np.float32)
    if n < 4 or len(np.unique(labels)) < 2:
        probs[np.arange(n), labels] = 1.0
        return probs, {'accuracy': None, 'log_loss': None, 'folds': 0}
    if x is None:
        progress(stage='label quality: reducing features')
        x = _reduce(emb, pca_dims, seed)
    counts = np.bincount(labels, minlength=n_cls)
    k = int(max(2, min(folds, counts[counts >= 2].min() if (counts >= 2).any() else 2)))
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')            # "least populated class has only 1 member" for one-box classes
        splits = list(StratifiedKFold(n_splits=k, shuffle=True, random_state=seed).split(x, labels))
        for i, (tr, te) in enumerate(splits):
            progress(i, k, f'label quality: training fold {i + 1}/{k} ({classifier})')
            if len(np.unique(labels[tr])) < 2:
                probs[te, labels[tr][0]] = 1.0
                continue
            clf = _classifier(classifier, len(tr), max_iter, seed).fit(x[tr], labels[tr])
            probs[np.ix_(te, clf.classes_)] = clf.predict_proba(x[te]).astype(np.float32)
    progress(k, k)
    given = np.clip(probs[np.arange(n), labels], 1e-7, 1.0)
    return probs, {'accuracy': float((probs.argmax(1) == labels).mean()), 'log_loss': float(-np.log(given).mean()),
                   'folds': k}


def _compact(labels, pred_probs):
    """cleanlab wants every class 0..K-1 present: map to the present classes (and renormalize their columns)."""
    present = np.unique(labels)
    remap = np.full(pred_probs.shape[1], -1, np.int64)
    remap[present] = np.arange(len(present))
    p = pred_probs[:, present].astype(np.float64)
    p = np.clip(p, 1e-6, None)
    p /= p.sum(1, keepdims=True)
    return present, remap[labels], p


def ood_from_knn(nbr_sim):
    """cleanlab's feature out-of-distribution score (OutOfDistribution: exp(-mean k-NN distance / its median)) from
    k-NN similarities computed already (analysis.knn, self excluded), as 1 − score: high = unlike the rest. The k-NN
    is the expensive part (sklearn's exact search took 2 of the 2.3 minutes at 50k boxes); the page has it anyway."""
    d = np.clip(1.0 - np.asarray(nbr_sim, np.float64), 0, None).mean(1)
    scale = max(float(np.median(d)) if len(d) else 0.0, 100 * np.finfo(np.float64).eps)
    return (1.0 - np.exp(-d / scale)).astype(np.float32)


def cleanlab_report(labels, pred_probs, emb=None, cfg=None, progress=_noop, class_names=None, x=None, nbr_sim=None):
    """Per box (dict of arrays, aligned with labels) + summary (JSON-able) from out-of-fold pred_probs.
    nbr_sim: (N, k) k-NN similarities of these boxes for the feature OOD score (None: computed from x / emb)."""
    from cleanlab import count, dataset, filter as cl_filter, rank
    s = settings(cfg)
    labels = np.asarray(labels, np.int64)
    n, n_cls = pred_probs.shape
    out = {
        'pred_probs': pred_probs.astype(np.float16),
        'cl_suggested': pred_probs.argmax(1).astype(np.int32) if n else np.zeros(0, np.int32),
        'cl_given_prob': pred_probs[np.arange(n), labels].astype(np.float32),
        'cl_quality': np.full(n, np.nan, np.float32), 'cl_issue': np.zeros(n, bool),
        'cl_rank': np.full(n, -1, np.int32), 'cl_margin': np.full(n, np.nan, np.float32),
        'cl_ood_feat': np.full(n, np.nan, np.float32), 'cl_ood_prob': np.full(n, np.nan, np.float32),
        'cl_scored': np.zeros(n, bool),
    }
    counts = np.bincount(labels, minlength=n_cls)
    summary = {'n': int(n), 'classes': int(n_cls), 'class_counts': counts.tolist(), 'settings': s}
    present = np.unique(labels)
    if n < 4 or len(present) < 2:
        summary['note'] = 'needs at least 2 classes and 4 boxes'
        return out, summary
    cls_ids, lab, p = _compact(labels, pred_probs)
    names = [class_names[c] if class_names and c < len(class_names) else str(c) for c in cls_ids]
    progress(stage='cleanlab: label quality scores')
    out['cl_quality'] = rank.get_label_quality_scores(lab, p, method=s['quality_method']).astype(np.float32)
    out['cl_margin'] = rank.get_label_quality_scores(lab, p, method='normalized_margin').astype(np.float32)
    out['cl_scored'] = counts[labels] >= 2
    progress(stage='cleanlab: finding label issues')
    cj = count.compute_confident_joint(lab, p)
    ranked = cl_filter.find_label_issues(lab, p, filter_by=s['filter_by'],
                                         return_indices_ranked_by='self_confidence', n_jobs=1)
    ranked = np.asarray(ranked, np.int64)
    ranked = ranked[out['cl_scored'][ranked]]
    out['cl_issue'][ranked] = True
    out['cl_rank'][ranked] = np.arange(len(ranked), dtype=np.int32)
    if s['ood']:
        from cleanlab.outlier import OutOfDistribution
        progress(stage='cleanlab: out-of-distribution scores')
        if nbr_sim is None and (emb is not None or x is not None):
            feats = x if x is not None else _reduce(emb, s['pca_dims'], s['seed'])
            feats = feats / np.maximum(np.linalg.norm(feats, axis=1, keepdims=True), 1e-12)
            _, nbr_sim = analysis.knn(np.ascontiguousarray(feats, np.float32), k=int(min(10, max(1, n - 1))))
        if nbr_sim is not None:
            out['cl_ood_feat'] = ood_from_knn(nbr_sim)
        out['cl_ood_prob'] = (1.0 - OutOfDistribution().fit_score(pred_probs=p, labels=lab, verbose=False)
                              ).astype(np.float32)
    progress(stage='cleanlab: class statistics')
    ranking = dataset.rank_classes_by_label_quality(lab, p, confident_joint=cj)
    overlap = dataset.find_overlapping_classes(lab, p, confident_joint=cj)
    joint = cj.astype(np.int64)
    summary.update({
        'class_ids': cls_ids.tolist(),
        'confident_joint': joint.tolist(),             # rows: given class, cols: cleanlab's estimate of the true one
        'health_score': float(dataset.overall_label_health_score(lab, p, confident_joint=cj, verbose=False)),
        'n_issues': int(len(ranked)),
        'class_ranking': [{'cls': int(cls_ids[int(r['Class Index'])]), 'name': names[int(r['Class Index'])],
                           'issues': int(r['Label Issues']), 'inverse_issues': int(r['Inverse Label Issues']),
                           'noise_rate': float(r['Label Noise']), 'quality': float(r['Label Quality Score'])}
                          for _, r in ranking.iterrows()],
        'overlapping_classes': [{'a': int(cls_ids[int(r['Class Index A'])]), 'b': int(cls_ids[int(r['Class Index B'])]),
                                 'a_name': names[int(r['Class Index A'])], 'b_name': names[int(r['Class Index B'])],
                                 'n': int(r['Num Overlapping Examples']), 'joint': float(r['Joint Probability'])}
                                for _, r in overlap.head(20).iterrows() if int(r['Num Overlapping Examples']) > 0],
        'imbalance': float(counts[present].min() / max(1, counts[present].max())),
    })
    return out, summary


def run(emb, labels, cfg=None, progress=_noop, class_names=None, keep=None, nbr_sim=None):
    """Everything for one feature space: out-of-fold probs, then cleanlab. keep: bool per box (None = all).
    Boxes left out (removed by a curation fix, or in a class under min_class_count) get NaN / False / -1.
    nbr_sim: (N, k) k-NN similarities of every box (State's scores) for the feature OOD score."""
    s = settings(cfg)
    labels = np.asarray(labels, np.int64)
    n_all = len(labels)
    n_cls = int(labels.max()) + 1 if n_all else 0
    counts = np.bincount(labels, minlength=n_cls)
    keep = (np.ones(n_all, bool) if keep is None else np.asarray(keep, bool)) & (counts[labels] >= s['min_class_count'])
    idx = np.flatnonzero(keep)
    e, lab = np.asarray(emb)[idx], labels[idx]
    progress(stage='label quality: reducing features')
    x = _reduce(e, s['pca_dims'], s['seed']) if len(e) else np.zeros((0, 1), np.float32)
    probs, fit = oof_pred_probs(e, lab, s['folds'], s['classifier'], s['pca_dims'], s['seed'], s['max_iter'],
                                progress, x=x)
    if probs.shape[1] < n_cls:
        probs = np.pad(probs, ((0, 0), (0, n_cls - probs.shape[1])))
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')            # cleanlab's "may not flag all issues in class k" notes
        per, summary = cleanlab_report(lab, probs, cfg=s, progress=progress, class_names=class_names, x=x,
                                       nbr_sim=None if nbr_sim is None else np.asarray(nbr_sim)[idx])
    small = [int(c) for c in np.flatnonzero((counts > 0) & (counts < s['min_class_count']))]
    summary.update({'oof_accuracy': fit['accuracy'], 'oof_log_loss': fit['log_loss'], 'folds_used': fit['folds'],
                    'class_counts': counts.tolist(), 'n_total': int(n_all), 'left_out_classes': small})
    full = {}
    for k, v in per.items():
        fill = False if v.dtype == bool else (-1 if np.issubdtype(v.dtype, np.integer) else np.nan)
        arr = np.full((n_all,) + v.shape[1:], fill, v.dtype)
        arr[idx] = v
        full[k] = arr
    return full, summary


# ---------------------------------------------------------------- object detection (detector predictions)

def od_inputs(gts, preds, n_cls):
    """cleanlab.object_detection inputs from, per image, gts = [(sample_id, cls, x1, y1, x2, y2)] and
    preds = [(pred_id, cls, score, x1, y1, x2, y2)] (pixels, dataset class ids).

    Returns (labels, predictions, pred_order): predictions[i] is a list of n_cls (M_k, 5) arrays [x1 y1 x2 y2 score];
    pred_order[i] lists the pred ids in the same flattened class order cleanlab scores them in."""
    labels, predictions, order = [], [], []
    for g, p in zip(gts, preds):
        labels.append({'bboxes': np.asarray([b[2:6] for b in g], np.float32).reshape(-1, 4),
                       'labels': np.asarray([b[1] for b in g], np.int64)})
        per_cls, ids = [], []
        for c in range(n_cls):
            mine = [q for q in p if q[1] == c]
            per_cls.append(np.asarray([[*q[3:7], q[2]] for q in mine], np.float32).reshape(-1, 5))
            ids += [q[0] for q in mine]
        predictions.append(per_cls)
        order.append(ids)
    return labels, predictions, order


def od_report(images, gts, preds, n_samples, n_cls, progress=_noop):
    """cleanlab object-detection scores (lower = worse, 0-1).

    Per label box (aligned with sample ids, NaN where not scored): cl_badloc (box drawn in the wrong place),
    cl_swap (box has the wrong class), cl_image_quality (its image's overall label quality).
    Summary: images worst first, and overlooked predictions (objects the labels likely miss) worst first."""
    from cleanlab.object_detection import rank as od_rank
    import warnings
    labels, predictions, order = od_inputs(gts, preds, n_cls)
    per = {'cl_badloc': np.full(n_samples, np.nan, np.float32), 'cl_swap': np.full(n_samples, np.nan, np.float32),
           'cl_image_quality': np.full(n_samples, np.nan, np.float32)}
    if not labels:
        return per, {'images': [], 'overlooked': []}
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        progress(stage='cleanlab: image label quality')
        img_q = od_rank.get_label_quality_scores(labels, predictions, verbose=False)
        progress(stage='cleanlab: badly located boxes')
        badloc = od_rank.compute_badloc_box_scores(labels=labels, predictions=predictions)
        progress(stage='cleanlab: swapped classes')
        swap = od_rank.compute_swap_box_scores(labels=labels, predictions=predictions)
        progress(stage='cleanlab: overlooked objects')
        overlooked = od_rank.compute_overlooked_box_scores(labels=labels, predictions=predictions)
    missed = []
    for i, name in enumerate(images):
        for j, b in enumerate(gts[i]):
            sid = int(b[0])
            per['cl_badloc'][sid] = badloc[i][j]
            per['cl_swap'][sid] = swap[i][j]
            per['cl_image_quality'][sid] = img_q[i]
        for pid, sc in zip(order[i], overlooked[i]):
            if np.isfinite(sc):
                missed.append({'image': name, 'pid': int(pid), 'quality': float(sc)})
    missed.sort(key=lambda m: m['quality'])
    worst = sorted(range(len(images)), key=lambda i: img_q[i])
    return per, {'images': [{'image': images[i], 'quality': float(img_q[i])} for i in worst],
                 'overlooked': missed}
