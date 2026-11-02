"""
Domain shift between two members A (what a model was trained on) and B (new camera / new lighting) of a combined
dataset, from their image-condition tables (data/imgstats.py). Pure numpy + scikit-learn; the Compare tab's
"Image conditions" section (data/compare.py CompareService.shift) and the assistant's compare_shift tool read it.

Per metric (A vs B):
    median / p5 / p95 per side, delta and delta %, effect = delta of medians / robust scale (IQR / 1.349, on log10
    for log-scale metrics), KS (two-sample, exact on the samples), PSI (10 bins at A's deciles), W1 / scale,
    out_b = share of B outside A's p1..p99 (A's own share is ~2%: the excess is what A never showed a model).
    score = max(PSI / PSI_REF, |effect|, excess / OUT_REF)  ->  ok < 0.35 <= minor < 0.75 <= moderate < 1.5 <= major
Separability: a logistic regression told to say A or B from the pixel metrics (light, sharpness, noise, colour), 5-fold
    cross-validated ROC AUC: 0.5 = the two look alike, 1.0 = every image gives its side away. Its out-of-fold P(B) ranks
    "the most B-like images" (the ones to label first). The same on a feature (embedding) = the appearance AUC.
Model fit (model_fit): a model's cached predictions scored on A's images and on B's separately (mAP50 when there are
    labels), plus label-free signals: mean top confidence per image, images with nothing found, confidence vs metric.
Verdict: fits / check / shifted, the drivers in words, rule-based recommendations and an Ultralytics hyp snippet.
"""

import math

import numpy as np

from . import imgstats

PSI_REF = 0.25          # the usual "significant shift" PSI
OUT_REF = 0.15          # 15 % of B outside A's p1..p99 beyond A's own share
SEVERITY = [(0.35, 'ok'), (0.75, 'minor'), (1.5, 'moderate'), (math.inf, 'major')]
SEV_RANK = {'ok': 0, 'minor': 1, 'moderate': 2, 'major': 3, 'n/a': -1}
PIXEL_GROUPS = ('light', 'sharpness', 'noise', 'colour')
NBINS = 30
SCATTER_MAX = 3000
LOW_N = 30
HEADLINE = ('brightness', 'contrast', 'dark_clip', 'bright_clip', 'sharpness', 'noise', 'saturation', 'cast_a', 'cast_b')


def severity(score):
    for thr, name in SEVERITY:
        if score < thr:
            return name
    return 'major'


def _t(v, log):
    return np.log10(np.clip(v, 1e-6, None)) if log else v


def ks_2samp(x, y):
    """Two-sample Kolmogorov-Smirnov statistic (= scipy.stats.ks_2samp(x, y).statistic), O((n+m) log)."""
    x, y = np.sort(np.asarray(x, np.float64)), np.sort(np.asarray(y, np.float64))
    if not len(x) or not len(y):
        return 0.0
    allv = np.concatenate([x, y])
    return float(np.abs(np.searchsorted(x, allv, 'right') / len(x) - np.searchsorted(y, allv, 'right') / len(y)).max())


def psi(a, b, bins=10, eps=1e-4):
    """Population stability index of b against a, bins at a's quantiles (ties collapse; a constant a = 2 bins)."""
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64)
    if not len(a) or not len(b):
        return 0.0
    edges = np.unique(np.quantile(a, np.linspace(0, 1, bins + 1)[1:-1]))
    ia = np.searchsorted(edges, a, 'right')
    ib = np.searchsorted(edges, b, 'right')
    k = len(edges) + 1
    pa = np.maximum(np.bincount(ia, minlength=k) / len(a), eps)
    pb = np.maximum(np.bincount(ib, minlength=k) / len(b), eps)
    return float(((pb - pa) * np.log(pb / pa)).sum())


def _edges(a, b, log, bins=NBINS):
    v = np.concatenate([a, b])
    v = v[np.isfinite(v)]
    if log:
        v = v[v > 0]
    if not len(v):
        return None
    lo, hi = np.quantile(v, [0.005, 0.995]) if len(v) > 50 else (v.min(), v.max())
    if hi <= lo:
        lo, hi = (lo * 0.9, lo * 1.1) if lo > 0 else (lo - 1, lo + 1)
    return np.geomspace(lo, hi, bins + 1) if log and lo > 0 else np.linspace(lo, hi, bins + 1)


def _hist(v, edges):
    if not len(v):
        return np.zeros(len(edges) - 1)
    h = np.histogram(np.clip(v, edges[0], edges[-1]), edges)[0]
    return h / max(1, len(v))


def _q(v):
    if not len(v):
        return None
    p5, p50, p95 = np.quantile(v, [0.05, 0.5, 0.95])
    return {'med': float(p50), 'p5': float(p5), 'p95': float(p95), 'mean': float(v.mean())}


# the smallest change worth a whole "effect" unit, per metric (in its own units; log metrics in log10): below it a
# tight dataset's tiny spread would call a 2 % change major
MIN_SCALE = {'brightness': 4.0, 'contrast': 3.0, 'dark_clip': 0.5, 'bright_clip': 0.5, 'dyn_range': 6.0, 'entropy': 0.1,
             'edge_density': 0.3, 'r_mean': 4.0, 'g_mean': 4.0, 'b_mean': 4.0, 'saturation': 6.0, 'colorfulness': 4.0,
             'cast_a': 1.5, 'cast_b': 1.5, 'wb_rg': 0.04, 'wb_bg': 0.04, 'aspect': 0.02, 'jpeg_q': 3.0, 'fnumber': 0.3}
ABSOLUTE = ('cast_a', 'cast_b')                     # centred on 0: a change in units, never a percentage / ratio


def compare_metric(a, b, log=False, key=''):
    """The statistics of one metric (NaNs dropped). None when a side has no value."""
    a, b = a[np.isfinite(a)], b[np.isfinite(b)]
    if not len(a) or not len(b):
        return None
    ta, tb = _t(a, log), _t(b, log)
    qa = np.quantile(ta, [0.25, 0.5, 0.75])
    qb = np.quantile(tb, [0.25, 0.5, 0.75])
    span = max(abs(qa[1]), abs(qb[1]))
    scale = max((qa[2] - qa[0]) / 1.349, (qb[2] - qb[0]) / 1.349, 0.05 * span if not log else 0.02,
                MIN_SCALE.get(key, 0.0), 1e-9)
    effect = float((qb[1] - qa[1]) / scale)
    p1, p99 = np.quantile(a, [0.01, 0.99])
    out_a = float(((a < p1) | (a > p99)).mean())
    out_b = float(((b < p1) | (b > p99)).mean())
    excess = max(0.0, out_b - out_a)
    u = np.linspace(0.01, 0.99, 99)
    w1 = float(np.abs(np.quantile(ta, u) - np.quantile(tb, u)).mean() / scale)
    p = psi(a, b)
    score = max(p / PSI_REF, abs(effect), excess / OUT_REF)
    ma, mb = float(np.median(a)), float(np.median(b))
    rel = key not in ABSOLUTE and ma > 1e-9 and mb >= 0
    return {'a': _q(a), 'b': _q(b), 'delta': mb - ma,
            'delta_pct': (mb - ma) / ma * 100 if rel else None,
            'ratio': mb / ma if rel else None,
            'effect': effect, 'ks': ks_2samp(a, b), 'psi': p, 'w1': w1, 'out_a': out_a, 'out_b': out_b,
            'band': [float(p1), float(p99)], 'score': float(score), 'severity': severity(score),
            'n_a': len(a), 'n_b': len(b), 'low_n': bool(min(len(a), len(b)) < LOW_N)}


def domain_auc(X, y, folds=5, seed=0):
    """(cross-validated ROC AUC, out-of-fold P(class 1), |coef| per column of a model fit on everything) of a
    balanced logistic regression telling y=0 from y=1 by X (columns robust-standardised, NaN -> 0 + a missing flag).
    (None, None, None) when a side has fewer than `folds` rows."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import StratifiedKFold
    y = np.asarray(y, np.int8)
    n1, n0 = int(y.sum()), int(len(y) - y.sum())
    k = min(folds, n0, n1)
    if k < 2 or X.shape[1] == 0:
        return None, None, None
    X = np.asarray(X, np.float64)
    med = np.nanmedian(np.where(np.isfinite(X), X, np.nan), axis=0)
    med = np.where(np.isfinite(med), med, 0.0)
    q75, q25 = np.nanpercentile(X, 75, axis=0), np.nanpercentile(X, 25, axis=0)
    sc = np.where(np.isfinite(q75 - q25) & ((q75 - q25) > 1e-9), (q75 - q25) / 1.349, 1.0)
    miss = ~np.isfinite(X)
    Z = np.where(miss, 0.0, (X - med) / sc)
    Z = np.clip(Z, -8, 8)
    flags = miss[:, miss.any(0) & ~miss.all(0)]
    if flags.shape[1]:
        Z = np.hstack([Z, flags.astype(np.float64)])
    p = np.zeros(len(y))
    cv = StratifiedKFold(n_splits=k, shuffle=True, random_state=seed)
    mk = lambda: LogisticRegression(C=1.0, class_weight='balanced', max_iter=400)
    for tr, te in cv.split(Z, y):
        p[te] = mk().fit(Z[tr], y[tr]).predict_proba(Z[te])[:, 1]
    auc = float(roc_auc_score(y, p))
    coef = np.abs(mk().fit(Z, y).coef_[0][:X.shape[1]])
    return auc, p, coef


def auc_words(auc):
    if auc is None:
        return 'not enough images'
    return ('the two look alike' if auc < 0.6 else 'slightly different' if auc < 0.75 else
            'clearly different' if auc < 0.9 else 'separate domains')


# ---------------------------------------------------------------- recommendations

def _ratio(by, k):
    r = by[k].get('ratio')
    return 1.0 if r is None else float(r)


def _pct(x):
    return f'{abs(x):.0f}%'


def _round_up(x, step=0.05):
    return round(math.ceil(x / step) * step, 2)


def recommendations(by, cams, sep):
    """[{key, severity, text, aug?}] + the Ultralytics hyp values they suggest."""
    out = []
    hyp = {'hsv_h': 0.015, 'hsv_s': 0.7, 'hsv_v': 0.4}
    extra = []

    def sev(k):
        m = by.get(k)
        return m['severity'] if m else 'n/a'

    def bad(k, lvl='minor'):
        return SEV_RANK[sev(k)] >= SEV_RANK[lvl]

    def add(key, text, aug=None):
        out.append({'key': key, 'severity': sev(key) if key in by else 'minor', 'text': text, **({'aug': aug} if aug else {})})

    if bad('brightness'):
        r = _ratio(by, 'brightness')
        need = abs(r - 1) + 0.1
        word = 'darker' if r < 1 else 'brighter'
        if need > hyp['hsv_v']:
            hyp['hsv_v'] = min(0.9, _round_up(need))
        add('brightness', f'B is {_pct((r - 1) * 100)} {word} than A (median luma {by["brightness"]["a"]["med"]:.0f} → '
            f'{by["brightness"]["b"]["med"]:.0f}). Train with brightness augmentation hsv_v ≥ {hyp["hsv_v"]} or fix the '
            f'exposure / lighting of the new setup.', {'hsv_v': hyp['hsv_v']})
    if bad('contrast') or bad('dyn_range'):
        k = 'contrast' if SEV_RANK[sev('contrast')] >= SEV_RANK[sev('dyn_range')] else 'dyn_range'
        r = _ratio(by, k)
        add(k, f'Contrast differs: B has {_pct((r - 1) * 100)} {"less" if r < 1 else "more"} tonal spread. Add '
            f'RandomBrightnessContrast (contrast_limit ≈ {min(0.5, _round_up(abs(r - 1) + 0.05))}) or CLAHE.',
            {'albumentations': f'RandomBrightnessContrast(contrast_limit={min(0.5, _round_up(abs(r - 1) + 0.05))}, p=0.5)'})
    if bad('dark_clip') and by['dark_clip']['delta'] > 0:
        add('dark_clip', f'B crushes more shadows ({by["dark_clip"]["b"]["med"]:.1f}% vs {by["dark_clip"]["a"]["med"]:.1f}% '
            'of pixels black): objects in the dark lose detail no augmentation brings back. Raise exposure / gain or add light.')
    if bad('bright_clip') and by['bright_clip']['delta'] > 0:
        add('bright_clip', f'B blows out more highlights ({by["bright_clip"]["b"]["med"]:.1f}% vs '
            f'{by["bright_clip"]["a"]["med"]:.1f}%): reduce exposure or glare; label examples of washed-out objects.')
    if bad('saturation') or bad('colorfulness'):
        k = max((x for x in ('saturation', 'colorfulness') if x in by), key=lambda x: by[x]['score'])
        r = _ratio(by, k)
        if abs(r - 1) + 0.1 > hyp['hsv_s']:
            hyp['hsv_s'] = min(0.9, _round_up(abs(r - 1) + 0.1))
        add(k, f'Colour differs: B is {_pct((r - 1) * 100)} {"duller" if r < 1 else "more colourful"} ({k}). '
            f'Keep hsv_s ≥ {hyp["hsv_s"]}.', {'hsv_s': hyp['hsv_s']})
    tint = [k for k in ('cast_a', 'cast_b', 'wb_rg', 'wb_bg') if bad(k)]
    if tint:
        da = by['cast_a']['delta'] if 'cast_a' in by else 0
        db = by['cast_b']['delta'] if 'cast_b' in by else 0
        words = [w for w, on in ((('more magenta' if da > 0 else 'greener'), abs(da) >= 1.5),
                                 (('yellower / warmer' if db > 0 else 'bluer / cooler'), abs(db) >= 1.5)) if on]
        hyp['hsv_h'] = max(hyp['hsv_h'], min(0.05, _round_up(0.015 + (abs(da) + abs(db)) / 600, 0.005)))
        add(tint[0], f'White balance / colour cast differs{": B is " + " and ".join(words) if words else ""}. Fix the '
            f'camera white balance, or train with hue augmentation hsv_h ≈ {hyp["hsv_h"]} and ColorJitter / '
            'RGBShift.', {'hsv_h': hyp['hsv_h'], 'albumentations': 'RGBShift(10, 10, 10, p=0.3)'})
    if bad('sharpness') or bad('tenengrad'):
        k = 'sharpness' if bad('sharpness') else 'tenengrad'
        r = _ratio(by, k)
        if r < 1:
            add(k, f'B is blurrier (sharpness {_pct((1 - r) * 100)} lower): check focus, motion blur (shutter speed) or '
                'lens. Add Blur / MotionBlur augmentation so A-trained features survive soft edges.',
                {'albumentations': 'OneOf([Blur(blur_limit=5), MotionBlur(blur_limit=7)], p=0.3)'})
            extra.append('blur')
        else:
            add(k, f'B is sharper (+{_pct((r - 1) * 100)}): usually harmless; textures the model never saw may still '
                'fool it — check the model rows below.')
    if bad('noise') and by['noise']['delta'] > 0:
        s = by['noise']['b']['med']
        add('noise', f'B is noisier (σ {by["noise"]["a"]["med"]:.1f} → {s:.1f}; high ISO / low light / a smaller sensor). '
            f'Add GaussNoise (std ≈ {s:.0f}/255) or denoise B before inference.',
            {'albumentations': f'GaussNoise(std_range=(0.0, {min(0.2, max(0.01, s / 255)):.3f}), p=0.3)'})
        extra.append('noise')
    if bad('megapixels') or bad('width') or bad('height'):
        k = next(x for x in ('megapixels', 'width', 'height') if bad(x))
        ma = by['width']['a']['med'] if 'width' in by else 0
        mb = by['width']['b']['med'] if 'width' in by else 0
        add(k, f'Resolution changed ({ma:.0f} → {mb:.0f} px wide): objects are {"smaller" if mb < ma else "larger"} '
            'in pixels at the same training imgsz. Re-check imgsz, and the box-size histograms above.')
    if bad('aspect'):
        add('aspect', f'Aspect ratio changed ({by["aspect"]["a"]["med"]:.2f} → {by["aspect"]["b"]["med"]:.2f}): '
            'letterbox padding and the field of view differ; validate on B with the same imgsz / rect setting.')
    if bad('jpeg_q') and by['jpeg_q']['delta'] < 0:
        q = by['jpeg_q']['b']['p5']
        add('jpeg_q', f'B is compressed harder (JPEG quality {by["jpeg_q"]["a"]["med"]:.0f} → {by["jpeg_q"]["b"]["med"]:.0f}). '
            f'Add ImageCompression (quality down to {max(10, q - 5):.0f}).',
            {'albumentations': f'ImageCompression(quality_range=({max(10, int(q - 5))}, 100), p=0.3)'})
    if cams and cams.get('new_in_b'):
        names = ', '.join(c for c, _ in cams['new_in_b'][:4])
        out.append({'key': 'camera', 'severity': 'moderate', 'text': f'B has cameras A never had: {names}. '
                    'A different sensor / lens / ISP changes colours and noise beyond what augmentation covers.'})
    auc = sep.get('auc')
    if auc is not None and auc >= 0.75:
        out.append({'key': 'label_b', 'severity': 'major' if auc >= 0.9 else 'moderate',
                    'text': 'Label a sample of B — start with "Most B-like images" (the ones the shift detector is surest '
                            'about) — fine-tune on A + B, and validate on B, not on A.'})
    return out, hyp


def hyp_yaml(hyp, recs):
    lines = ['# Ultralytics train hyperparameters suggested by the Image conditions check',
             f'hsv_h: {hyp["hsv_h"]}  # hue (default 0.015)',
             f'hsv_s: {hyp["hsv_s"]}  # saturation (default 0.7)',
             f'hsv_v: {hyp["hsv_v"]}  # brightness (default 0.4)']
    alb = [r['aug']['albumentations'] for r in recs if r.get('aug', {}).get('albumentations')]
    if alb:
        lines.append('# Albumentations (ultralytics applies these when albumentations is installed / via augmentations=):')
        lines += [f'#   {a}' for a in alb]
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------- the report

def _tally(vals):
    out = {}
    for v in vals:
        out[v or 'unknown'] = out.get(v or 'unknown', 0) + 1
    return sorted(out.items(), key=lambda kv: -kv[1])


def _js(pa, pb):
    keys = set(pa) | set(pb)
    if not keys:
        return 0.0
    a = np.array([pa.get(k, 0) for k in keys], np.float64)
    b = np.array([pb.get(k, 0) for k in keys], np.float64)
    a, b = a / max(a.sum(), 1), b / max(b.sum(), 1)
    m = 0.5 * (a + b)
    kl = lambda p: float(np.sum(np.where(p > 0, p * np.log2(np.where(p > 0, p, 1) / np.where(m > 0, m, 1)), 0)))
    return math.sqrt(max(0.0, 0.5 * kl(a) + 0.5 * kl(b)))


def _sub(n, k, seed):
    if n <= k:
        return np.arange(n)
    return np.sort(np.random.default_rng(seed).choice(n, k, replace=False))


def _driver_text(key, m):
    r = m.get('ratio')
    lbl = {'brightness': ('darker', 'brighter'), 'contrast': ('lower contrast', 'higher contrast'),
           'sharpness': ('blurrier', 'sharper'), 'tenengrad': ('softer edges', 'stronger edges'),
           'noise': ('cleaner', 'noisier'), 'saturation': ('duller colours', 'more saturated'),
           'colorfulness': ('less colourful', 'more colourful'), 'megapixels': ('lower resolution', 'higher resolution'),
           'dark_clip': ('fewer crushed shadows', 'more crushed shadows'),
           'bright_clip': ('fewer blown highlights', 'more blown highlights'),
           'jpeg_q': ('more compressed', 'less compressed')}.get(key)
    if lbl and r is not None:
        w = lbl[0] if r < 1 else lbl[1]
        amt = (f'×{r:.1f}' if r >= 2 else f'÷{1 / r:.1f}' if r > 0.01 else '÷100+') if r >= 2 or r <= 0.5 \
            else _pct((r - 1) * 100)
        return f'{w} ({amt})'
    if key == 'cast_a':
        return 'more magenta' if m['delta'] > 0 else 'greener'
    if key == 'cast_b':
        return 'yellower / warmer' if m['delta'] > 0 else 'bluer / cooler'
    info = {k: lb for k, _, lb, _, _ in imgstats.METRICS}
    return f'{info.get(key, key)} {"+" if m["delta"] > 0 else "−"}{abs(m["delta"]):.2g}'


def build(ta, tb, a_alias='A', b_alias='B', totals=None):
    """(report dict, per-image arrays for the image lists) of tables ta (A) and tb (B)."""
    info = imgstats.metrics_info()
    metrics, by = [], {}
    for j, mi in enumerate(info):
        st = compare_metric(ta.vec[:, j], tb.vec[:, j], mi['log'], mi['key'])
        row = {**mi, 'available': st is not None}
        if st is not None:
            e = _edges(ta.vec[:, j][np.isfinite(ta.vec[:, j])], tb.vec[:, j][np.isfinite(tb.vec[:, j])], mi['log'])
            if e is not None:
                row['edges'] = np.round(e, 5).tolist()
                row['ha'] = np.round(_hist(ta.vec[:, j][np.isfinite(ta.vec[:, j])], e), 5).tolist()
                row['hb'] = np.round(_hist(tb.vec[:, j][np.isfinite(tb.vec[:, j])], e), 5).tolist()
            row.update(st)
            by[mi['key']] = st
        else:
            row['severity'] = 'n/a'
            row['n_a'] = int(np.isfinite(ta.vec[:, j]).sum())
            row['n_b'] = int(np.isfinite(tb.vec[:, j]).sum())
        metrics.append(row)

    groups = {}
    for row in metrics:
        g = groups.setdefault(row['group'], {'worst': 'ok', 'n': 0})
        if row['available']:
            g['n'] += 1
            if SEV_RANK[row['severity']] > SEV_RANK[g['worst']]:
                g['worst'] = row['severity']

    # separability on the pixel metrics (capture facts like the resolution would give the side away trivially)
    cols = [j for j, mi in enumerate(info) if mi['group'] in PIXEL_GROUPS and metrics[j]['available']]
    X = np.vstack([ta.vec[:, cols], tb.vec[:, cols]]) if cols else np.zeros((0, 0))
    for c, j in enumerate(cols):
        if info[j]['log']:
            X[:, c] = _t(X[:, c], True)
    y = np.r_[np.zeros(len(ta.names), np.int8), np.ones(len(tb.names), np.int8)]
    auc, p_b, coef = domain_auc(X, y) if cols and len(y) else (None, None, None)
    drivers = []
    if coef is not None:
        order = np.argsort(-coef)[:5]
        tot = float(coef.sum()) or 1.0
        drivers = [{'key': info[cols[i]]['key'], 'label': info[cols[i]]['label'], 'weight': round(float(coef[i] / tot), 3)}
                   for i in order if coef[i] > 0]
    sep = {'auc': auc, 'words': auc_words(auc), 'drivers': drivers, 'appearance': None}

    # curves: mean per-image histograms; scatter samples
    H = imgstats.NHIST
    curves = {}
    for nm, sl in (('luma', slice(0, H)), ('r', slice(H, 2 * H)), ('g', slice(2 * H, 3 * H)), ('b', slice(3 * H, 4 * H))):
        curves[nm] = {'a': np.round(ta.hist[:, sl].mean(0), 5).tolist() if len(ta.names) else [],
                      'b': np.round(tb.hist[:, sl].mean(0), 5).tolist() if len(tb.names) else []}
    sc = {}
    for side, t, seed in (('a', ta, 1), ('b', tb, 2)):
        ix = _sub(len(t.names), SCATTER_MAX, seed)
        sc[side] = {k: np.round(t.vec[ix, imgstats.COL[k]], 3).tolist()
                    for k in ('brightness', 'contrast', 'sharpness', 'noise', 'saturation', 'cast_b')}
        sc[side]['i'] = ix.tolist()

    ca, cb = dict(_tally(ta.camera)), dict(_tally(tb.camera))
    cams = {'a': list(ca.items()), 'b': list(cb.items()),
            'new_in_b': [(k, v) for k, v in cb.items() if k not in ca and k != 'unknown'],
            'js': round(_js(ca, cb), 3)}
    fa, fb = dict(_tally(ta.fmt)), dict(_tally(tb.fmt))
    formats = {'a': list(fa.items()), 'b': list(fb.items())}

    recs, hyp = recommendations(by, cams, sep)
    report = {'a': a_alias, 'b': b_alias,
              'n': {'a': len(ta.names), 'b': len(tb.names), **(totals or {}),
                    'measured': ta.measured + tb.measured, 'failed': ta.failed + tb.failed},
              'metrics': metrics, 'groups': groups, 'curves': curves, 'scatter': sc, 'cameras': cams,
              'formats': formats, 'separability': sep, 'recommendations': recs, 'hyp': hyp,
              'hyp_yaml': hyp_yaml(hyp, recs), 'models': []}
    report['verdict'] = verdict(report)
    arrays = {'names_a': ta.names, 'names_b': tb.names, 'vec_a': ta.vec, 'vec_b': tb.vec,
              'p_b': p_b[len(ta.names):] if p_b is not None else None}
    return report, arrays


def verdict(report):
    """{level: fits | check | shifted, score 0..100, title, text, drivers[]} from the metrics, AUCs and models."""
    px = [m for m in report['metrics'] if m['available'] and m['group'] in PIXEL_GROUPS]
    cap = [m for m in report['metrics'] if m['available'] and m['group'] == 'capture']
    sep = report['separability']
    auc, app = sep.get('auc'), (sep.get('appearance') or {}).get('auc')
    n_major = sum(m['severity'] == 'major' for m in px)
    n_mod = sum(m['severity'] == 'moderate' for m in px)
    cap_major = [m for m in cap if m['severity'] == 'major' and m['key'] in ('megapixels', 'width', 'height', 'aspect', 'jpeg_q')]
    drop = None
    for md in report.get('models') or []:
        d = md.get('drop')
        if d is not None and (drop is None or d > drop):
            drop = d
    top = max([m['score'] for m in px] or [0])
    parts = [min(1.0, top / 3.0)]
    if auc is not None:
        parts.append(max(0.0, (auc - 0.5) * 2))
    if app is not None:
        parts.append(max(0.0, (app - 0.5) * 2))
    if drop is not None:
        parts.append(min(1.0, max(0.0, drop) * 4))
    score = round(100 * max(parts))
    if n_major or (auc or 0) >= 0.9 or (app or 0) >= 0.95 or (drop or 0) >= 0.15:
        level = 'shifted'
    elif n_mod or cap_major or (auc or 0) >= 0.75 or (app or 0) >= 0.85 or (drop or 0) >= 0.05 or report['cameras']['new_in_b']:
        level = 'check'
    else:
        level = 'fits'
    ranked = sorted((m for m in px if m['severity'] not in ('ok',)), key=lambda m: -m['score'])
    head = [m for m in ranked if m['key'] in HEADLINE]          # channel means / edge counts echo these: said once
    drivers = [_driver_text(m['key'], m) for m in (head or ranked)[:3]]
    a, b = report['a'], report['b']
    title = {'fits': f'{b} looks like {a}: a model trained on {a} should fit',
             'check': f'{b} differs from {a} in places: validate the model on {b}',
             'shifted': f'{b} is a different domain: expect a model trained on {a} to drop'}[level]
    text = (f'{b} is ' + ', '.join(drivers) + f' compared with {a}.') if drivers else \
        'No light, sharpness, noise or colour metric moved beyond normal variation.'
    if auc is not None:
        text += f' Shift detector AUC {auc:.2f} ({auc_words(auc)}).'
    if cap_major:
        text += ' Capture changed: ' + ', '.join(m['label'] for m in cap_major) + '.'
    if drop is not None:
        text += f' Model confidence on {b} {"drops" if drop > 0 else "changes"} by {abs(drop) * 100:.0f}%.'
    low = min(report['n']['a'], report['n']['b']) < LOW_N
    return {'level': level, 'score': score, 'title': title, 'text': text + (' (few images: indicative only)' if low else ''),
            'drivers': drivers, 'n_major': n_major, 'n_moderate': n_mod}


# ---------------------------------------------------------------- model fit

def model_fit(d, side_of_img, a, b, conf, ap_fn, by_name_vec=None, metric_keys=None, bins=8):
    """One model's fit on A vs B. d: evaluate.Data; side_of_img: int8 per d.images (a / b index or -1);
    ap_fn(mask) -> ap_stats dict; by_name_vec: {image name: metric vector} of the measured images (conf vs metric)."""
    n_img = len(d.images)
    top = np.zeros(n_img, np.float32)
    nhit = np.zeros(n_img, np.int32)
    for i, p in enumerate(d.p_by_img):
        if len(p):
            s = d.p_score[p]
            top[i] = float(s.max())
            nhit[i] = int((s >= conf).sum())
    out = {}
    hist_bins = np.linspace(0, 1, 21)
    for key, side in (('a', a), ('b', b)):
        m = side_of_img == side
        k = int(m.sum())
        if not k:
            out[key] = None
            continue
        n_lab = int(sum(len(d.gt_by_img[i]) for i in np.nonzero(m)[0]))
        row = {'images': k, 'labels': n_lab, 'mean_top': float(top[m].mean()), 'none_found': float((nhit[m] == 0).mean()),
               'per_image': float(nhit[m].mean())}
        sel = np.isin(d.p_img, np.nonzero(m)[0])
        sc = d.p_score[sel]
        row['conf_hist'] = np.round(np.histogram(sc[sc >= 0.05], hist_bins)[0] / max(1, int((sc >= 0.05).sum())), 4).tolist()
        if n_lab:
            ap = ap_fn(m)
            ci = min(100, max(0, round(conf * 100)))
            row.update({'map50': ap['map50'], 'map': ap['map'], 'p': ap['p_conf_all'][ci], 'r': ap['r_conf_all'][ci],
                        'best_f1': ap['best_f1']})
        out[key] = row
    res = {'a': out.get('a'), 'b': out.get('b'), 'conf': conf, 'hist_bins': hist_bins.tolist()}
    if out.get('a') and out.get('b'):
        ra, rb = out['a'], out['b']
        res['drop'] = (ra['mean_top'] - rb['mean_top']) / max(ra['mean_top'], 1e-6)
        if 'map50' in ra and 'map50' in rb:
            res['map_drop'] = ra['map50'] - rb['map50']
            res['drop'] = max(res['drop'], res['map_drop'] / max(ra['map50'], 1e-6))
    if by_name_vec and metric_keys:
        idx = {n: i for i, n in enumerate(d.images)}
        rows = [(idx[n], v) for n, v in by_name_vec.items() if n in idx]
        if len(rows) >= bins * 2:
            ii = np.array([r[0] for r in rows])
            V = np.stack([r[1] for r in rows])
            sides = side_of_img[ii]
            cv = {}
            for key in metric_keys:
                j = imgstats.COL[key]
                v = V[:, j]
                ok = np.isfinite(v)
                if ok.sum() < bins * 2:
                    continue
                edges = np.unique(np.quantile(v[ok], np.linspace(0, 1, bins + 1)))
                if len(edges) < 3:
                    continue
                bi = np.clip(np.searchsorted(edges, v, 'right') - 1, 0, len(edges) - 2)
                curve = {'x': [float((edges[t] + edges[t + 1]) / 2) for t in range(len(edges) - 1)]}
                for key2, side in (('a', a), ('b', b)):
                    ys, ns = [], []
                    for t in range(len(edges) - 1):
                        sm = ok & (bi == t) & (sides == side)
                        ns.append(int(sm.sum()))
                        ys.append(float(top[ii[sm]].mean()) if sm.any() else None)
                    curve[key2], curve[f'n_{key2}'] = ys, ns
                cv[key] = curve
            res['conf_vs'] = cv
    return res


# ---------------------------------------------------------------- "as B would shoot it" preview

def simulate(rgb, a_med, b_med, strength=1.0, img_vec=None):
    """`rgb` (an A image, uint8) moved toward B's conditions by the A -> B change of the medians: luma gain and
    contrast, saturation gain, Lab tint shift, a Gaussian blur fitted to the sharpness ratio, added luma noise.
    a_med / b_med: {metric: median}. Returns (uint8 image, applied params)."""
    import cv2
    s = float(min(1.5, max(0.0, strength)))
    out = rgb.astype(np.float32)
    prm = {}

    def ratio(k, lo=0.2, hi=5.0):
        ma, mb = a_med.get(k), b_med.get(k)
        if ma is None or mb is None or not math.isfinite(ma) or not math.isfinite(mb) or ma <= 1e-6:
            return 1.0
        return float(min(hi, max(lo, mb / ma))) ** s

    g = ratio('brightness')
    c = ratio('contrast')
    y = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    m = float(y.mean())
    out = (out - m) * c + m * g
    prm.update(brightness_gain=round(g, 3), contrast_gain=round(c, 3))
    out = np.clip(out, 0, 255)
    sg = ratio('saturation')
    da = (b_med.get('cast_a', 0) - a_med.get('cast_a', 0)) * s
    db = (b_med.get('cast_b', 0) - a_med.get('cast_b', 0)) * s
    if abs(sg - 1) > 0.01 or abs(da) > 0.2 or abs(db) > 0.2:
        u8 = out.astype(np.uint8)
        if abs(sg - 1) > 0.01:
            hsv = cv2.cvtColor(u8, cv2.COLOR_RGB2HSV).astype(np.float32)
            hsv[..., 1] = np.clip(hsv[..., 1] * sg, 0, 255)
            u8 = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB)
        if abs(da) > 0.2 or abs(db) > 0.2:
            lab = cv2.cvtColor(u8, cv2.COLOR_RGB2LAB).astype(np.float32)
            lab[..., 1] += da
            lab[..., 2] += db
            u8 = cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2RGB)
        out = u8.astype(np.float32)
    prm.update(saturation_gain=round(sg, 3), tint_a=round(da, 2), tint_b=round(db, 2))
    sr = ratio('sharpness', 0.01, 100)
    sigma = 0.0
    if sr < 0.95:
        h0, w0 = rgb.shape[:2]
        k = -(-max(h0, w0) // imgstats.WORK)
        small = cv2.resize(out.astype(np.uint8), (max(1, w0 // k), max(1, h0 // k)), interpolation=cv2.INTER_AREA)
        g0 = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
        v0 = float(cv2.Laplacian(g0, cv2.CV_32F).var())
        target = v0 * sr
        lo, hi = 0.0, 8.0
        for _ in range(10):                     # sharpness falls monotonically with the blur sigma
            mid = (lo + hi) / 2
            v = float(cv2.Laplacian(cv2.GaussianBlur(g0, (0, 0), max(mid, 0.05)), cv2.CV_32F).var())
            lo, hi = (mid, hi) if v > target else (lo, mid)
        sigma = (lo + hi) / 2 * k              # in the full image's pixels
        if sigma > 0.1:
            out = cv2.GaussianBlur(out, (0, 0), sigma)
    prm['blur_sigma_px'] = round(sigma, 2)
    na, nb = a_med.get('noise'), b_med.get('noise')
    add = 0.0
    if na is not None and nb is not None and math.isfinite(na) and math.isfinite(nb) and nb > na:
        add = math.sqrt(max(0.0, nb * nb - na * na)) * s
        if add > 0.05:
            h0, w0 = out.shape[:2]
            n = np.random.default_rng(0).normal(0.0, add, (h0, w0, 1)).astype(np.float32)
            out = out + n
    prm['noise_sigma'] = round(add, 2)
    return np.clip(out, 0, 255).astype(np.uint8), prm
