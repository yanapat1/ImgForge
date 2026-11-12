"""
Domain shift between two members A (what a model was trained on) and B (new camera / new lighting) of a combined
dataset, from their image-condition tables (image/imgstats.py). Pure numpy + scikit-learn; the Compare tab's
"Image conditions" section (image/compare.py CompareService.shift) and the assistant's compare_shift tool read it.

Per metric (A vs B):
    median / p5 / p95 per side, delta and delta %, effect = delta of medians / robust scale (IQR / 1.349, on log10
    for log-scale metrics), KS (two-sample, exact on the samples), PSI (10 bins at A's deciles), W1 / scale,
    out_b = share of B outside A's p1..p99 (A's own share is ~2%: the excess is what A never showed a model),
    ci = 95 % bootstrap interval of the median delta, p = KS p-value, q = Benjamini-Hochberg over the metrics (evidence
    only: with thousands of images everything is "significant", severity stays on the effect size).
    score = max(PSI / PSI_REF, |effect|, excess / OUT_REF)  ->  ok < 0.35 <= minor < 0.75 <= moderate < 1.5 <= major
Separability: a logistic regression told to say A or B from the pixel metrics (light, sharpness, noise, colour), 5-fold
    cross-validated ROC AUC: 0.5 = the two look alike, 1.0 = every image gives its side away. Its out-of-fold P(B) ranks
    "the most B-like images" (the ones to label first). The same on a feature (embedding) = the appearance AUC.
Model fit (model_fit): a model's cached predictions scored on A's images and on B's separately (mAP50 when there are
    labels), plus label-free signals: mean top confidence per image, images with nothing found, confidence vs metric.
Light layout: the mean 4x4 luma grid of A and B (light map), the exposure mix (dark / dim / normal / bright images).
Correction: a per-channel LUT matching B's tone / colour curves to A's (fix B at inference instead of retraining).
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
HEADLINE = ('brightness', 'contrast', 'dark_clip', 'bright_clip', 'sharpness', 'noise', 'saturation', 'cast_a', 'cast_b',
            'vignetting', 'uniformity', 'cct', 'haze', 'blur_aniso', 'chroma_noise')
BOOT = 200              # bootstrap resamples for the median-delta interval
BOOT_MAX = 3000         # rows per side the bootstrap draws from
EXPOSURE = [('dark', 60), ('dim', 100), ('normal', 160), ('bright', math.inf)]   # by mean luma
LOW_CONTRAST = 25.0     # luma std below this = a flat image


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


def ks_pvalue(d, n, m):
    """Asymptotic two-sided p-value of a two-sample KS statistic d (Stephens' small-sample correction)."""
    if d <= 0 or not n or not m:
        return 1.0
    en = math.sqrt(n * m / (n + m))
    lam = (en + 0.12 + 0.11 / en) * d
    if lam < 0.2:
        return 1.0
    k = np.arange(1, 101)
    return float(min(1.0, max(0.0, 2.0 * np.sum((-1.0) ** (k - 1) * np.exp(-2.0 * k * k * lam * lam)))))


def bh(p):
    """Benjamini-Hochberg adjusted q-values of the p-values `p` (same order)."""
    p = np.asarray(p, np.float64)
    m = len(p)
    if not m:
        return p
    o = np.argsort(p)
    q = p[o] * m / np.arange(1, m + 1)
    q = np.minimum.accumulate(q[::-1])[::-1]
    out = np.empty(m)
    out[o] = np.minimum(q, 1.0)
    return out


def boot_ci(a, b, reps=BOOT, cap=BOOT_MAX, seed=0):
    """95 % bootstrap interval of median(b) - median(a), vectorised (one index matrix per side)."""
    rng = np.random.default_rng(seed)
    a = a if len(a) <= cap else a[rng.choice(len(a), cap, replace=False)]
    b = b if len(b) <= cap else b[rng.choice(len(b), cap, replace=False)]
    ma = np.median(a[rng.integers(0, len(a), (reps, len(a)))], axis=1)
    mb = np.median(b[rng.integers(0, len(b), (reps, len(b)))], axis=1)
    lo, hi = np.quantile(mb - ma, [0.025, 0.975])
    return [float(lo), float(hi)]


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
             'cast_a': 1.5, 'cast_b': 1.5, 'wb_rg': 0.04, 'wb_bg': 0.04, 'aspect': 0.02, 'jpeg_q': 3.0, 'fnumber': 0.3,
             'midtone': 4.0, 'local_contrast': 1.0, 'vignetting': 0.05, 'uniformity': 3.0, 'light_dx': 4.0,
             'light_dy': 4.0, 'haze': 6.0, 'cct': 300.0, 'blur_aniso': 0.06, 'chroma_noise': 0.5, 'blockiness': 0.08,
             'hour': 1.0}
ABSOLUTE = ('cast_a', 'cast_b', 'light_dx', 'light_dy', 'hour')   # centred / cyclic: a change in units, never a %


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
    ks = ks_2samp(a, b)
    return {'a': _q(a), 'b': _q(b), 'delta': mb - ma, 'ci': boot_ci(a, b), 'p': ks_pvalue(ks, len(a), len(b)),
            'delta_pct': (mb - ma) / ma * 100 if rel else None,
            'ratio': mb / ma if rel else None,
            'effect': effect, 'ks': ks, 'psi': p, 'w1': w1, 'out_a': out_a, 'out_b': out_b,
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


def coverage(za, zb, cap=200):
    """(share of zb's rows within A's own p95 1-NN distance of their nearest za row, indices of the uncovered zb rows
    farthest first, at most `cap`). None, [] when a side has fewer than 3 rows."""
    from sklearn.neighbors import NearestNeighbors
    if len(za) < 3 or not len(zb):
        return None, []
    nn = NearestNeighbors(n_neighbors=2).fit(za)
    da = nn.kneighbors(za, 2, return_distance=True)[0][:, 1]           # leave-one-out: the 2nd is the nearest other
    thr = float(np.quantile(da, 0.95))
    db = nn.kneighbors(zb, 1, return_distance=True)[0][:, 0]
    out = db > thr
    ix = np.nonzero(out)[0]
    return round(float(1.0 - out.mean()), 4), ix[np.argsort(-db[ix], kind='stable')][:cap].tolist()


def frechet(za, zb):
    """Frechet distance between Gaussians fit to za and zb (the FID formula), None with too few rows."""
    from scipy import linalg
    if len(za) < 3 or len(zb) < 3:
        return None
    ma, mb = za.mean(0), zb.mean(0)
    ca, cb = np.atleast_2d(np.cov(za, rowvar=False)), np.atleast_2d(np.cov(zb, rowvar=False))
    cs = linalg.sqrtm(ca @ cb)
    if np.iscomplexobj(cs):
        cs = cs.real
    return round(float(max(0.0, ((ma - mb) ** 2).sum() + np.trace(ca + cb - 2.0 * cs))), 4)


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
    if bad('midtone') and not bad('brightness'):
        r = _ratio(by, 'midtone')
        add('midtone', f'Midtones moved ({by["midtone"]["a"]["med"]:.0f} → {by["midtone"]["b"]["med"]:.0f}) while the '
            f'average brightness did not: a different tone curve / gamma. Add RandomGamma.',
            {'albumentations': f'RandomGamma(gamma_limit=({max(40, int(100 * min(r, 1 / r) - 10))}, '
                               f'{min(250, int(100 * max(r, 1 / r) + 10))}), p=0.3)'})
    if bad('vignetting') and by['vignetting']['delta'] < 0:
        add('vignetting', f'B\'s corners are darker (corner / centre light {by["vignetting"]["a"]["med"]:.2f} → '
            f'{by["vignetting"]["b"]["med"]:.2f}): lens vignetting or a spot light. Objects near the edges get darker; '
            'enable the lens shading correction or add RandomShadow / RandomToneCurve.',
            {'albumentations': 'RandomShadow(p=0.2)'})
    if bad('uniformity') and by['uniformity']['delta'] > 0 and not bad('vignetting'):
        add('uniformity', f'B\'s light is less even across the frame (grid variation {by["uniformity"]["a"]["med"]:.0f}% → '
            f'{by["uniformity"]["b"]["med"]:.0f}%): shadows, a single lamp or a bright window. Diffuse the light or add '
            'RandomShadow; see the light map.', {'albumentations': 'RandomShadow(p=0.3)'})
    for k, side in (('light_dx', ('left', 'right')), ('light_dy', ('top', 'bottom'))):
        if bad(k):
            d = by[k]['b']['med']
            add(k, f'The light comes from another side in B (brighter {side[1] if d > 0 else side[0]}, {d:+.0f}% across '
                f'the frame vs {by[k]["a"]["med"]:+.0f}% in A). Train with fliplr / RandomShadow, or move the light.',
                {'albumentations': 'RandomShadow(p=0.3)'})
    if bad('haze') and by['haze']['delta'] > 0:
        add('haze', f'B is hazier / has more glare (dark-channel {by["haze"]["a"]["med"]:.0f} → {by["haze"]["b"]["med"]:.0f}): '
            'fog, a dirty window or lens flare lifts the blacks and lowers contrast. Clean the lens / add a hood, or '
            'train with RandomFog.', {'albumentations': 'RandomFog(p=0.2)'})
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
    tint = [k for k in ('cast_a', 'cast_b', 'wb_rg', 'wb_bg', 'cct') if bad(k)]
    if tint:
        da = by['cast_a']['delta'] if 'cast_a' in by else 0
        db = by['cast_b']['delta'] if 'cast_b' in by else 0
        words = [w for w, on in ((('more magenta' if da > 0 else 'greener'), abs(da) >= 1.5),
                                 (('yellower / warmer' if db > 0 else 'bluer / cooler'), abs(db) >= 1.5)) if on]
        hyp['hsv_h'] = max(hyp['hsv_h'], min(0.05, _round_up(0.015 + (abs(da) + abs(db)) / 600, 0.005)))
        if 'cct' in by and abs(by['cct']['delta']) >= 300:
            words.append(f'{by["cct"]["a"]["med"]:.0f} K → {by["cct"]["b"]["med"]:.0f} K')
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
    if bad('blur_aniso') and by['blur_aniso']['delta'] > 0:
        add('blur_aniso', f'B\'s blur has a direction (blur direction {by["blur_aniso"]["a"]["med"]:.2f} → '
            f'{by["blur_aniso"]["b"]["med"]:.2f}): motion blur from a slow shutter or a moving camera / object. Use a '
            'faster shutter (more light / gain); train with MotionBlur.',
            {'albumentations': 'MotionBlur(blur_limit=(3, 9), p=0.3)'})
    if bad('chroma_noise') and by['chroma_noise']['delta'] > 0:
        add('chroma_noise', f'B has more colour noise (σ {by["chroma_noise"]["a"]["med"]:.1f} → '
            f'{by["chroma_noise"]["b"]["med"]:.1f}): a smaller sensor or a weaker noise filter in the camera. Add ISONoise.',
            {'albumentations': 'ISONoise(color_shift=(0.01, 0.05), intensity=(0.1, 0.5), p=0.3)'})
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
    if bad('blockiness') and by['blockiness']['delta'] > 0 and not (bad('jpeg_q') and by['jpeg_q']['delta'] < 0):
        add('blockiness', f'B shows more 8×8 JPEG blocks (blocking {by["blockiness"]["a"]["med"]:.2f} → '
            f'{by["blockiness"]["b"]["med"]:.2f}): heavier compression or a video stream. Raise the stream bitrate / '
            'quality, or add ImageCompression.', {'albumentations': 'ImageCompression(quality_range=(30, 100), p=0.3)'})
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
           'jpeg_q': ('more compressed', 'less compressed'), 'midtone': ('darker midtones', 'brighter midtones'),
           'local_contrast': ('flatter detail', 'crisper local contrast'),
           'vignetting': ('darker corners', 'brighter corners'), 'uniformity': ('more even light', 'less even light'),
           'haze': ('clearer', 'hazier / more glare'), 'blur_aniso': ('less motion blur', 'more motion blur'),
           'chroma_noise': ('less colour noise', 'more colour noise'), 'blockiness': ('less blocky', 'blockier')}.get(key)
    if lbl and r is not None:
        w = lbl[0] if r < 1 else lbl[1]
        amt = (f'×{r:.1f}' if r >= 2 else f'÷{1 / r:.1f}' if r > 0.01 else '÷100+') if r >= 2 or r <= 0.5 \
            else _pct((r - 1) * 100)
        return f'{w} ({amt})'
    if key == 'cast_a':
        return 'more magenta' if m['delta'] > 0 else 'greener'
    if key == 'cast_b':
        return 'yellower / warmer' if m['delta'] > 0 else 'bluer / cooler'
    if key == 'cct':
        return f'{"cooler / bluer" if m["delta"] > 0 else "warmer / yellower"} light ({m["delta"]:+.0f} K)'
    if key in ('light_dx', 'light_dy'):
        side = ('right', 'left') if key == 'light_dx' else ('bottom', 'top')
        return f'light more from the {side[0] if m["delta"] > 0 else side[1]}'
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

    avail = [r for r in metrics if r['available']]
    if avail:
        for r, q in zip(avail, bh([r['p'] for r in avail])):
            r['q'] = float(q)
            r['significant'] = bool(q < 0.05)
            by[r['key']]['q'] = r['q']

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
        sc[side] = {k: _finite_list(t.vec[ix, imgstats.COL[k]])
                    for k in ('brightness', 'contrast', 'sharpness', 'noise', 'saturation', 'cast_b', 'cct', 'vignetting',
                              'uniformity', 'hour')}
        sc[side]['i'] = ix.tolist()
    hc = imgstats.COL['hour']
    day = any(len(t.names) and np.isfinite(t.vec[:, hc]).mean() >= 0.3 for t in (ta, tb))

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
              'formats': formats, 'separability': sep, 'recommendations': recs, 'hyp': hyp, 'day': bool(day),
              'light_map': light_map(ta, tb), 'exposure_mix': exposure_mix(ta, tb),
              'correction': correction(curves),
              'hyp_yaml': hyp_yaml(hyp, recs), 'models': []}
    report['verdict'] = verdict(report)
    arrays = {'names_a': ta.names, 'names_b': tb.names, 'vec_a': ta.vec, 'vec_b': tb.vec,
              'p_b': p_b[len(ta.names):] if p_b is not None else None}
    return report, arrays


def _finite_list(v, digits=3):
    """A JSON-safe list: NaN -> None (the scatter skips it)."""
    v = np.round(np.asarray(v, np.float64), digits)
    return [None if not math.isfinite(x) else float(x) for x in v.tolist()]


def light_map(ta, tb):
    """{a, b, diff}: the mean GRIDxGRID luma (0-255) of A's and B's images, None when the tables carry no grid."""
    g, lo = imgstats.GRID, 4 * imgstats.NHIST
    out = {'grid': g}
    for side, t in (('a', ta), ('b', tb)):
        if not len(t.names) or t.hist.shape[1] < lo + g * g:
            return None
        out[side] = np.round(t.hist[:, lo:lo + g * g].mean(0).reshape(g, g) * 255.0, 2).tolist()
    out['diff'] = np.round(np.asarray(out['b']) - np.asarray(out['a']), 2).tolist()
    return out


def exposure_mix(ta, tb):
    """Share of images per exposure class (by mean luma) + the share of flat (low contrast) images, per side."""
    jb, jc = imgstats.COL['brightness'], imgstats.COL['contrast']
    out = {'classes': [c for c, _ in EXPOSURE], 'edges': [e for _, e in EXPOSURE[:-1]], 'low_contrast': LOW_CONTRAST}
    for side, t in (('a', ta), ('b', tb)):
        v = t.vec[:, jb]
        v = v[np.isfinite(v)]
        k = np.searchsorted(np.array(out['edges'], np.float64), v, 'right')
        cnt = np.bincount(k, minlength=len(EXPOSURE))[:len(EXPOSURE)]
        c = t.vec[:, jc]
        c = c[np.isfinite(c)]
        out[side] = {'share': np.round(cnt / max(1, len(v)), 4).tolist(), 'n': len(v),
                     'flat': round(float((c < LOW_CONTRAST).mean()) if len(c) else 0.0, 4)}
    return out


def _lut_channel(ha, hb):
    """256-entry LUT mapping B's tone distribution (32-bin shares hb) onto A's (ha) by CDF matching."""
    nb = len(ha)
    w = 256 / nb
    x = np.arange(nb + 1) * w                                 # bin edges in pixel values
    ramp = np.arange(nb + 1) * 1e-7                           # strictly increasing even across empty bins
    ca = np.concatenate([[0.0], np.cumsum(np.asarray(ha, np.float64))]) + ramp
    cb = np.concatenate([[0.0], np.cumsum(np.asarray(hb, np.float64))]) + ramp
    ca, cb = ca / ca[-1], cb / cb[-1]
    v = np.arange(256) + 0.5
    q = np.interp(v, x, cb)                                   # where each B value sits in B's distribution
    return np.clip(np.round(np.interp(q, ca, x) - 0.5), 0, 255).astype(np.uint8)


def correction(curves):
    """{lut: [[256] R, G, B], residual: {r, g, b, luma}, gain}: per-channel LUT that matches B's mean R / G / B curves
    to A's (apply it to B's images at inference: cv2.LUT). residual = |mean of A - mean of B after the LUT| per
    channel, in 0-255 (what a global curve cannot fix, e.g. noise or blur, is not here)."""
    ch = ('r', 'g', 'b')
    if any(not curves.get(c, {}).get('a') or not curves.get(c, {}).get('b') for c in ch):
        return None
    nb = len(curves['r']['a'])
    centres = (np.arange(nb) + 0.5) * 256 / nb
    luts, res, before = [], {}, {}
    for c in ch:
        ha, hb = np.asarray(curves[c]['a']), np.asarray(curves[c]['b'])
        lut = _lut_channel(ha, hb)
        luts.append(lut.tolist())
        ma = float((ha * centres).sum() / max(ha.sum(), 1e-9))
        mb = float((hb * centres).sum() / max(hb.sum(), 1e-9))
        mb2 = float((hb * lut[np.clip(centres.astype(int), 0, 255)]).sum() / max(hb.sum(), 1e-9))
        before[c], res[c] = round(abs(ma - mb), 2), round(abs(ma - mb2), 2)
    return {'lut': luts, 'residual': res, 'before': before}


def apply_lut(rgb, lut):
    """`rgb` uint8 (H, W, 3) through the per-channel LUT [[256] x 3] of correction()."""
    import cv2
    t = np.stack([np.asarray(x, np.uint8) for x in lut], 1).reshape(256, 1, 3)
    return cv2.LUT(np.ascontiguousarray(rgb, np.uint8), t)


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
        # is the drop real? top confidence per image A vs B (KS + Mann-Whitney), and the share of images with
        # nothing found (two proportions)
        from ..ml import stats
        ma, mb = side_of_img == a, side_of_img == b
        res['tests'] = {'top_conf': stats.two_sample(top[ma], top[mb]),
                        'none_found': stats.two_prop(int((nhit[ma] == 0).sum()), int(ma.sum()),
                                                     int((nhit[mb] == 0).sum()), int(mb.sum()))}
    if by_name_vec and metric_keys:
        idx = {n: i for i, n in enumerate(d.images)}
        rows = [(idx[n], v) for n, v in by_name_vec.items() if n in idx]
        if len(rows) >= bins * 2:
            ii = np.array([r[0] for r in rows])
            V = np.stack([r[1] for r in rows])
            sides = side_of_img[ii]
            labelled = any(len(d.gt_by_img[i]) for i in ii)
            cv, mv = {}, {}
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
                if labelled:                      # mAP50 per bin, A and B pooled (the same images per bin)
                    mp, nl = [], []
                    for t in range(len(edges) - 1):
                        mask = np.zeros(n_img, bool)
                        mask[ii[ok & (bi == t)]] = True
                        k_lab = int(sum(len(d.gt_by_img[i]) for i in np.nonzero(mask)[0]))
                        nl.append(k_lab)
                        mp.append(float(ap_fn(mask)['map50']) if k_lab else None)
                    mv[key] = {'x': curve['x'], 'map50': mp, 'labels': nl}
            res['conf_vs'] = cv
            if mv:
                res['map_vs'] = mv
    return res


# ---------------------------------------------------------------- "as B would shoot it" preview

def simulate(rgb, a_med, b_med, strength=1.0, img_vec=None):
    """`rgb` (an A image, uint8) moved toward B's conditions by the A -> B change of the medians: luma gain and
    contrast, the gamma left in the midtones, a radial vignette, saturation gain, Lab tint shift, a Gaussian (or, when
    B's blur has a direction, a motion) blur fitted to the sharpness ratio, added luma and colour noise, and a JPEG
    round trip at B's quality. a_med / b_med: {metric: median}. Returns (uint8 image, applied params)."""
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
    # gamma: the part of B's midtone change the gain did not make
    gam = 1.0
    ma_mid, mb_mid = a_med.get('midtone'), b_med.get('midtone')
    if ma_mid and mb_mid and all(map(math.isfinite, (ma_mid, mb_mid))) and abs(mb_mid / ma_mid - 1) > 0.03:
        x0 = min(0.98, max(0.02, ma_mid * g / 255.0))
        x1 = min(0.98, max(0.02, ma_mid * (mb_mid / ma_mid) ** s / 255.0))
        gam = float(min(2.0, max(0.5, math.log(x1) / math.log(x0))))
        if abs(gam - 1) > 0.03:
            out = 255.0 * np.power(out / 255.0, gam)
    prm['gamma'] = round(gam, 3)
    # vignette: corners / centre moved by B's / A's vignetting ratio (1 at the centre, the full factor at the corners)
    va, vb = a_med.get('vignetting'), b_med.get('vignetting')
    vf = 1.0
    if va and vb and math.isfinite(va) and math.isfinite(vb) and va > 0.05:
        vf = float(min(2.0, max(0.3, vb / va))) ** s
        if abs(vf - 1) > 0.02:
            h0, w0 = out.shape[:2]
            yy, xx = np.ogrid[0:h0, 0:w0]
            r2 = ((xx - w0 / 2) / (0.375 * w0)) ** 2 + ((yy - h0 / 2) / (0.375 * h0)) ** 2   # corner cell centres: 2
            out = np.clip(out * (1.0 + (vf - 1.0) * np.minimum(r2 / 2.0, 1.6))[..., None], 0, 255)
    prm['vignette'] = round(vf, 3)
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
        target = float(cv2.Laplacian(g0, cv2.CV_32F).var()) * sr
        da_ = b_med.get('blur_aniso', 0) - a_med.get('blur_aniso', 0)
        lap = lambda im: float(cv2.Laplacian(im, cv2.CV_32F).var())
        if math.isfinite(da_) and da_ > 0.05:
            # B's blur has a direction: a horizontal line kernel, its length fitted to the sharpness ratio
            line = lambda L: np.full((1, L), 1.0 / L, np.float32)
            lo, hi = 1, 61
            while hi - lo > 1:                  # sharpness falls monotonically with the length
                mid = (lo + hi) // 2
                lo, hi = (mid, hi) if lap(cv2.filter2D(g0, -1, line(mid))) > target else (lo, mid)
            L = max(2, round(hi * k))      # in the full image's pixels
            out = cv2.filter2D(out, -1, line(L), borderType=cv2.BORDER_REFLECT)
            prm['motion_px'] = L
            sigma = L / math.sqrt(12.0)         # the line's own std, for the blur figure
        else:
            lo, hi = 0.0, 8.0
            for _ in range(10):                 # sharpness falls monotonically with the blur sigma
                mid = (lo + hi) / 2
                v = lap(cv2.GaussianBlur(g0, (0, 0), max(mid, 0.05)))
                lo, hi = (mid, hi) if v > target else (lo, mid)
            sigma = (lo + hi) / 2 * k          # in the full image's pixels
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
    ca_, cb_ = a_med.get('chroma_noise'), b_med.get('chroma_noise')
    addc = 0.0
    if ca_ is not None and cb_ is not None and math.isfinite(ca_) and math.isfinite(cb_) and cb_ > ca_:
        addc = math.sqrt(max(0.0, cb_ * cb_ - ca_ * ca_)) * s
        if addc > 0.1:
            lab = cv2.cvtColor(np.clip(out, 0, 255).astype(np.uint8), cv2.COLOR_RGB2LAB).astype(np.float32)
            lab[..., 1:] += np.random.default_rng(1).normal(0.0, addc, lab.shape[:2] + (2,)).astype(np.float32)
            out = cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2RGB).astype(np.float32)
    prm['chroma_noise'] = round(addc, 2)
    res = np.clip(out, 0, 255).astype(np.uint8)
    qa, qb = a_med.get('jpeg_q'), b_med.get('jpeg_q')
    q = None
    if qb is not None and math.isfinite(qb):
        q0 = qa if qa is not None and math.isfinite(qa) else 100.0
        if qb < q0 - 2:
            q = round(min(100.0, max(5.0, q0 + (qb - q0) * min(s, 1.0))))
            ok, buf = cv2.imencode('.jpg', res[..., ::-1], [cv2.IMWRITE_JPEG_QUALITY, q])
            if ok:
                res = np.ascontiguousarray(cv2.imdecode(buf, cv2.IMREAD_COLOR)[..., ::-1])
    prm['jpeg_quality'] = q
    return res, prm
