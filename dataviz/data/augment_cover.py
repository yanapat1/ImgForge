"""
Coverage: does a dataset, with an augmentation recipe, look like another set of images (another place or situation,
a new camera)? Three sets of image embeddings: train (this session), aug (train images run through the recipe) and
target (the new images).

    run(train, aug, target, aug_steps, train_stats, target_stats) -> the report the Augment tab draws

The measure is the visualizer's coverage (data/shift.py): a target image is *covered* when its nearest neighbour in
the reference set is no farther than train's own 95th-percentile nearest-neighbour distance. The threshold comes from
train alone and stays fixed, so adding augmented images cannot lower the bar: "after" is the share of target images
within that distance of train ∪ aug. Next to it: the Frechet distance (FID formula) and a domain classifier's AUC
(0.5 = it cannot tell the sets apart), each before and after; the augmented images that look like neither set (an
augmentation too strong to be real); which steps the rescued target images owe their cover to; and what the images
still not covered differ in (brightness, contrast, sharpness, colour, noise), with the steps that would close it.
Everything is in a 32-d PCA fit on train ∪ target, so one set's size does not shape the space.
"""

from collections import Counter

import numpy as np

from . import shift as shiftlib

DIMS, OFF_FACTOR, LAYOUT_MAX = 32, 1.5, 4000


def _l2(x):
    x = np.asarray(x, np.float32)
    return x / np.maximum(np.linalg.norm(x, axis=1, keepdims=True), 1e-12)


def project(train, aug, target, dims=DIMS):
    from sklearn.decomposition import PCA
    tr, au, tg = _l2(train), _l2(aug) if len(aug) else np.zeros((0, train.shape[1]), np.float32), _l2(target)
    fit = np.vstack([tr, tg])
    d = int(min(dims, fit.shape[1], len(fit) - 1))
    if d < 2:
        return tr, au, tg
    p = PCA(d, random_state=0).fit(fit)
    return p.transform(tr), p.transform(au) if len(au) else np.zeros((0, d)), p.transform(tg)


def threshold(z_train):
    """train's own p95 leave-one-out 1-NN distance: the bar a covered image must clear."""
    from sklearn.neighbors import NearestNeighbors
    d = NearestNeighbors(n_neighbors=2).fit(z_train).kneighbors(z_train, 2)[0][:, 1]
    return float(np.quantile(d, 0.95))


def _nn(ref, q):
    from sklearn.neighbors import NearestNeighbors
    d, i = NearestNeighbors(n_neighbors=1).fit(ref).kneighbors(q, 1)
    return d[:, 0], i[:, 0]


def _auc(a, b):
    if min(len(a), len(b)) < 5:
        return None
    auc, _, _ = shiftlib.domain_auc(np.vstack([a, b]), np.r_[np.zeros(len(a)), np.ones(len(b))])
    return None if auc is None else round(max(auc, 1 - auc), 4)          # which side is "1" does not matter


def layout(z, seed=0):
    """2-D positions for the scatter: UMAP when installed, else the first two principal axes."""
    if len(z) < 5:
        return np.zeros((len(z), 2))
    try:
        import umap
        return umap.UMAP(n_neighbors=min(15, len(z) - 1), min_dist=0.2, random_state=seed, n_jobs=1).fit_transform(z)
    except Exception:  # noqa: BLE001 - umap missing or failing on a tiny set: PCA is fine for a picture
        from sklearn.decomposition import PCA
        return PCA(2, random_state=seed).fit_transform(z)


# ---------------------------------------------------------------- image statistics: what the uncovered images differ in
def image_stats(rgb):
    """A few appearance numbers of one uint8 RGB image (a 224 px thumbnail is enough)."""
    import cv2
    f = rgb.astype(np.float32)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    lap = cv2.Laplacian(gray, cv2.CV_32F)
    resid = gray - cv2.GaussianBlur(gray, (0, 0), 1.0)
    r, b = float(f[..., 0].mean()), float(f[..., 2].mean())
    return {'brightness': float(gray.mean()), 'contrast': float(gray.std()), 'sharpness': float(np.log1p(lap.var())),
            'saturation': float(hsv[..., 1].mean()), 'warmth': (r - b) / 255.0, 'noise': float(np.median(np.abs(resid)) * 1.4826)}


STAT_LABEL = {'brightness': 'brightness', 'contrast': 'contrast', 'sharpness': 'sharpness', 'saturation': 'colour strength',
              'warmth': 'colour temperature', 'noise': 'noise'}


def gap(train_stats, target_stats):
    """[{key, text, delta, step}] for the appearance gaps between the uncovered target images and train, biggest first."""
    if not train_stats or not target_stats:
        return []
    med = lambda rows, k: float(np.median([s[k] for s in rows]))
    spread = lambda rows, k: float(np.std([s[k] for s in rows])) or 1e-6
    out = []
    for k in STAT_LABEL:
        a, b = med(train_stats, k), med(target_stats, k)
        z = (b - a) / spread(train_stats, k)
        if abs(z) < 0.6:
            continue
        step, text = _suggest(k, a, b)
        if step:
            out.append({'key': k, 'z': round(z, 2), 'train': round(a, 3), 'target': round(b, 3), 'text': text, 'step': step})
    return sorted(out, key=lambda x: -abs(x['z']))


def _suggest(k, a, b):
    """The catalog step (data/augment.py) that moves train toward b on `k`, and a sentence saying why."""
    if k == 'brightness':
        d = (b - a) / 255.0
        lo, hi = (round(min(-0.05, d * 1.3), 2), 0.05) if d < 0 else (-0.05, round(max(0.05, d * 1.3), 2))
        return ({'t': 'RandomBrightnessContrast', 'p': 0.5, 'args': {'brightness_limit': [max(-0.6, lo), min(0.6, hi)], 'contrast_limit': [-0.1, 0.1]}},
                f'The uncovered images are {"darker" if d < 0 else "brighter"} (mean {b:.0f} vs {a:.0f} of 255).')
    if k == 'contrast':
        r = b / max(a, 1e-6) - 1
        lim = [round(max(-0.6, min(-0.05, r * 1.3)), 2), 0.05] if r < 0 else [-0.05, round(min(0.6, max(0.05, r * 1.3)), 2)]
        return ({'t': 'RandomBrightnessContrast', 'p': 0.5, 'args': {'brightness_limit': [-0.05, 0.05], 'contrast_limit': lim}},
                f'They have {"less" if r < 0 else "more"} contrast ({abs(r) * 100:.0f}%).')
    if k == 'sharpness':
        if b < a:
            return ({'t': 'GaussianBlur', 'p': 0.4, 'args': {'sigma_limit': [0.5, 2.5]}}, 'They are softer / blurrier.')
        return ({'t': 'Sharpen', 'p': 0.3, 'args': {'alpha': [0.2, 0.5]}}, 'They are sharper.')
    if k == 'saturation':
        r = b / max(a, 1e-6) - 1
        if r < -0.6:
            return ({'t': 'ToGray', 'p': 0.3, 'args': {}}, 'They have almost no colour (grey or infrared).')
        lo, hi = (int(max(-100, r * 130)), 5) if r < 0 else (-5, int(min(100, r * 130)))
        return ({'t': 'HueSaturationValue', 'p': 0.5, 'args': {'hue_shift_limit': [-5, 5], 'sat_shift_limit': [lo, hi], 'val_shift_limit': [-5, 5]}},
                f'Their colours are {"weaker" if r < 0 else "stronger"} ({abs(r) * 100:.0f}%).')
    if k == 'warmth':
        return ({'t': 'PlanckianJitter', 'p': 0.5, 'args': {'mode': 'blackbody'}}, f'Their light is {"warmer" if b > a else "cooler"}.')
    if k == 'noise':
        if b > a:
            return ({'t': 'ISONoise', 'p': 0.4, 'args': {'color_shift': [0.01, 0.05], 'intensity': [0.2, 0.6]}}, 'They are noisier (grain, high ISO).')
        return (None, '')
    return (None, '')


# ---------------------------------------------------------------- the report
def run(train, aug, target, aug_steps=(), train_stats=None, target_stats=None, cap=200, seed=0):
    """train / aug / target: (n, d) embeddings; aug_steps[i]: the step names that ran on aug row i.
    -> the coverage report (plain JSON)."""
    if len(train) < 3 or len(target) < 1:
        raise ValueError('coverage needs at least 3 training images and 1 target image')
    zt, za, zx = project(np.asarray(train), np.asarray(aug), np.asarray(target))
    thr = threshold(zt)
    d0, i0 = _nn(zt, zx)
    if len(za):
        d1, i1 = _nn(za, zx)
        after = np.minimum(d0, d1)
        via_aug = d1 < d0
    else:
        d1, i1 = np.full(len(zx), np.inf), np.zeros(len(zx), int)
        after, via_aug = d0, np.zeros(len(zx), bool)
    cov0, cov1 = float((d0 <= thr).mean()), float((after <= thr).mean())
    rescued = np.nonzero((d0 > thr) & (after <= thr))[0]
    still = np.nonzero(after > thr)[0]
    still = still[np.argsort(-after[still], kind='stable')]
    # augmented images like neither set: too strong to be real
    off = None
    if len(za):
        dn, _ = _nn(np.vstack([zt, zx]), za)
        off = float((dn > OFF_FACTOR * thr).mean())
    credit = Counter()
    for t in rescued:
        for s in set(aug_steps[int(i1[t])] if len(aug_steps) > int(i1[t]) else ()):
            credit[s] += 1
    n_res = max(1, len(rescued))
    ref = np.vstack([zt, za]) if len(za) else zt
    report = {
        'n': {'train': len(zt), 'aug': len(za), 'target': len(zx)}, 'threshold': round(thr, 5),
        'coverage': {'before': round(cov0, 4), 'after': round(cov1, 4), 'gain': round(cov1 - cov0, 4)},
        'frechet': {'before': shiftlib.frechet(zt, zx), 'after': shiftlib.frechet(ref, zx)},
        'domain_auc': {'before': _auc(zt, zx), 'after': _auc(ref, zx)},
        'off_domain': None if off is None else round(off, 4),
        'rescued': len(rescued), 'uncovered': [int(x) for x in still[:cap]], 'n_uncovered': len(still),
        'credit': [{'t': t, 'rescued': c, 'share': round(c / n_res, 4)} for t, c in credit.most_common()],
        'nearest': [{'target': int(t), 'train': int(i0[t]), 'aug': int(i1[t]) if len(za) else None, 'via': 'aug' if via_aug[t] else 'train',
                     'd_before': round(float(d0[t]), 5), 'd_after': round(float(after[t]), 5)} for t in still[:cap]],
    }
    if train_stats is not None and target_stats is not None:
        report['gap'] = gap(train_stats, [target_stats[i] for i in still] if len(still) else [])
    report['verdict'] = verdict(report)
    # the picture: a sample of each set, laid out together
    rng = np.random.default_rng(seed)
    take = lambda n, k: np.sort(rng.choice(n, k, replace=False)) if n > k else np.arange(n)
    share = LAYOUT_MAX // 3
    it, ia, ix = take(len(zt), share), take(len(za), share), take(len(zx), share)
    xy = layout(np.vstack([zt[it], za[ia] if len(za) else np.zeros((0, zt.shape[1])), zx[ix]]), seed)
    a, b = len(it), len(it) + len(ia)
    covered = after[ix] <= thr
    report['map'] = {'train': {'i': it.tolist(), 'xy': np.round(xy[:a], 3).tolist()},
                     'aug': {'i': ia.tolist(), 'xy': np.round(xy[a:b], 3).tolist()},
                     'target': {'i': ix.tolist(), 'xy': np.round(xy[b:], 3).tolist(), 'covered': covered.tolist()}}
    return report


def verdict(r):
    c0, c1 = r['coverage']['before'], r['coverage']['after']
    pct = lambda v: f'{v * 100:.0f}%'
    head = (f'This recipe covers {pct(c1)} of the new images (without it: {pct(c0)}).' if r['n']['aug']
            else f'Your data covers {pct(c0)} of the new images.')
    if c1 >= 0.9:
        tail = ' That is good cover: a model trained this way should see few surprises there.'
    elif c1 - c0 >= 0.1:
        tail = ' The augmentation helps; the rest needs more of what is listed below, or real images from there.'
    elif r['n']['aug']:
        tail = ' The augmentation adds little: the new images differ in ways these steps do not make.'
    else:
        tail = ''
    off = r.get('off_domain')
    if off is not None and off >= 0.2:
        tail += f' {pct(off)} of the augmented images look like neither set: lower the strength or the chances.'
    gaps = r.get('gap') or []
    if gaps and c1 < 0.95:
        tail += ' Still not covered: ' + '; '.join(g['text'].rstrip('.') for g in gaps[:3]).lower() + '.'
    return head + tail
