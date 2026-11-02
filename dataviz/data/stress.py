"""
Stress test: how a model's score falls as the light, blur, noise, colour, compression or resolution of the images it
sees moves away from what it was trained on. The Compare tab's "Image conditions" section runs it on a sample of
member A's images (the training-like data) and marks where member B (the new camera / lighting) sits on each axis, so
"will the old model fit the new data" gets a number before B is labelled.

    AXES        key -> (label, levels, neutral, unit, metric, kind): each level is a pure, deterministic
                perturbation (apply()); blur / motion lengths are in pixels of a 640 px long side, so they mean the
                same at any image size
    run()       decodes each sampled image once (long side <= the model's input size: nothing the model would see is
                lost), predicts the clean images, then every axis x level (predict.predict_batch: the runner lock and
                the CUDA fallbacks), scores each level with evaluate.ap_stats on the sample's labels (mAP50 / mAP /
                P / R) or, without labels, against the clean predictions (consistency: how much of what the model
                found it still finds), and measures what each level does to the image-condition metric of its axis
                on a few images (curve), so B's measured change maps onto the level axis
    summarise() per axis: the safe window (levels scoring >= 95 % of clean), where B sits (from the shift report's
                medians, through the measured curve), the expected score there, a status; overall the worst axis
                and what to do

Results are cached in memory and in <cache_dir>/compare/stress-<key>.json.
"""

import hashlib
import json
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from . import imgstats

BASE = 640              # blur / motion levels are pixels at this long side
KEEP = 0.95             # the safe window keeps >= this share of the clean score
SAMPLE = 150            # images per run by default
SAMPLE_MAX = 500        # decoded images are held in memory: ~1 MB each at 640 px
CURVE_N = 16            # images the per-level metric curve is measured on

# key: (label, levels, neutral, unit, metric of the shift report it moves, kind of change: ratio | delta | level)
AXES = {
    'brightness': ('brightness', [0.3, 0.45, 0.6, 0.8, 1.0, 1.25, 1.5, 1.75], 1.0, '×', 'brightness', 'ratio'),
    'gamma': ('midtones (gamma)', [0.5, 0.67, 0.8, 1.0, 1.25, 1.5, 2.0], 1.0, 'γ', 'midtone', 'ratio'),
    'contrast': ('contrast', [0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6], 1.0, '×', 'contrast', 'ratio'),
    'blur': ('defocus blur', [0, 0.5, 1, 1.5, 2, 3, 4], 0, 'σ px', 'sharpness', 'ratio'),
    'motion': ('motion blur', [0, 3, 5, 7, 9, 12, 15], 0, 'px', 'blur_aniso', 'delta'),
    'noise': ('noise', [0, 3, 6, 10, 15, 20, 25], 0, 'σ', 'noise', 'delta'),
    'temperature': ('colour temperature', [-3000, -2000, -1000, 0, 1000, 2000, 3000], 0, 'K', 'cct', 'delta'),
    'saturation': ('saturation', [0, 0.4, 0.7, 1.0, 1.3, 1.6, 2.0], 1.0, '×', 'saturation', 'ratio'),
    'jpeg': ('JPEG quality', [10, 15, 25, 35, 50, 70, 100], 100, 'q', 'jpeg_q', 'level'),
    'resolution': ('resolution', [0.25, 0.35, 0.5, 0.75, 1.0], 1.0, '×', 'width', 'ratio'),
}

# what to do when B falls outside the window, per axis
ADVICE = {
    'brightness': 'add light / raise exposure or gain, and train with hsv_v ≥ {m:.2f}',
    'gamma': 'match the camera tone curve, or train with RandomGamma',
    'contrast': 'train with RandomBrightnessContrast / CLAHE',
    'blur': 'check the focus / lens; train with Blur',
    'motion': 'use a faster shutter (more light); train with MotionBlur',
    'noise': 'add light or lower the gain / ISO; train with GaussNoise',
    'temperature': 'fix the camera white balance, or apply the correction LUT; train with hsv_h / RGBShift',
    'saturation': 'match the camera colour profile; train with hsv_s ≥ {m:.2f}',
    'jpeg': 'raise the stream bitrate / JPEG quality; train with ImageCompression',
    'resolution': 'move the camera closer / use a higher resolution, or train at that scale (scale aug)',
}


def axes_info():
    return [{'key': k, 'label': lb, 'levels': lv, 'neutral': nt, 'unit': u, 'metric': mt}
            for k, (lb, lv, nt, u, mt, _) in AXES.items()]


# ---------------------------------------------------------------- perturbations

def _wb_gains(dk):
    """Von Kries R, G, B gains that move a ~6500 K white by dk kelvin (negative = warmer / yellower)."""
    def rgb_of(t):                       # Planckian locus approximation (Tanner Helland), normalised to G
        t = min(40000.0, max(1000.0, t)) / 100.0
        r = 255.0 if t <= 66 else 329.698727446 * (t - 60) ** -0.1332047592
        g = 99.4708025861 * math.log(t) - 161.1195681661 if t <= 66 else 288.1221695283 * (t - 60) ** -0.0755148492
        b = 255.0 if t >= 66 else (0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307)
        return np.array([r, g, b], np.float64).clip(1, 255) / max(g, 1.0)
    base, tgt = rgb_of(6500.0), rgb_of(6500.0 + dk)
    gain = tgt / base
    return (gain / gain[1]).astype(np.float32)


def apply(rgb, axis, level, seed=0):
    """`rgb` uint8 (H, W, 3) perturbed by `level` of `axis` (AXES). The neutral level returns the input."""
    import cv2
    if axis not in AXES:
        raise KeyError(f'unknown axis {axis!r}')
    if float(level) == float(AXES[axis][2]):
        return rgb
    lv = float(level)
    h, w = rgb.shape[:2]
    px = max(h, w) / BASE
    f = rgb.astype(np.float32)
    if axis == 'brightness':
        out = f * lv
    elif axis == 'gamma':
        out = 255.0 * np.power(f / 255.0, lv)
    elif axis == 'contrast':
        m = float(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).mean())
        out = (f - m) * lv + m
    elif axis == 'blur':
        out = cv2.GaussianBlur(f, (0, 0), lv * px)
    elif axis == 'motion':
        L = max(2, round(lv * px))
        out = cv2.filter2D(f, -1, np.full((1, L), 1.0 / L, np.float32), borderType=cv2.BORDER_REFLECT)
    elif axis == 'noise':
        out = f + np.random.default_rng(seed).normal(0.0, lv, (h, w, 1)).astype(np.float32)
    elif axis == 'temperature':
        out = f * _wb_gains(lv)
    elif axis == 'saturation':
        g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)[..., None]
        out = g + (f - g) * lv
    elif axis == 'jpeg':
        ok, buf = cv2.imencode('.jpg', np.ascontiguousarray(rgb[..., ::-1]), [cv2.IMWRITE_JPEG_QUALITY, int(lv)])
        return np.ascontiguousarray(cv2.imdecode(buf, cv2.IMREAD_COLOR)[..., ::-1]) if ok else rgb
    else:                                # resolution: a lower-resolution camera seen at the same model input size
        sw, sh = max(1, round(w * lv)), max(1, round(h * lv))
        small = cv2.resize(rgb, (sw, sh), interpolation=cv2.INTER_AREA)
        return cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)
    return np.clip(out, 0, 255).astype(np.uint8)


def _metric_of(rgb, key):
    """One image-condition metric of `rgb` (the shift report's units)."""
    if key == 'jpeg_q':
        return math.nan
    if key == 'width':
        return float(rgb.shape[1])
    v, _ = imgstats.measure_array(rgb)
    return float(v[imgstats.COL[key]])


def measure_curve(images, axis, workers=4):
    """{levels, y}: the median change (ratio or delta vs the clean image, by the axis kind) of the axis' metric over
    `images` at each level: what each level does in the units of the shift report."""
    _, levels, neutral, _, key, kind = AXES[axis]
    if kind == 'level':
        return {'levels': list(levels), 'y': list(levels)}
    ims = images[:CURVE_N]

    def one(im):
        base = _metric_of(im, key)
        row = []
        for lv in levels:
            if key == 'width':          # the resolution axis keeps the size: its change IS the level
                row.append(float(lv) if kind == 'ratio' else 0.0)
                continue
            v = base if lv == neutral else _metric_of(apply(im, axis, lv, seed=1), key)
            if kind == 'ratio':
                row.append(v / base if base > 1e-9 and math.isfinite(v) else math.nan)
            else:
                row.append(v - base if math.isfinite(v) and math.isfinite(base) else math.nan)
        return row
    with ThreadPoolExecutor(max(1, workers), thread_name_prefix='stress-curve') as ex:
        rows = np.array(list(ex.map(one, ims)), np.float64) if ims else np.zeros((0, len(levels)))
    y = np.nanmedian(rows, axis=0) if len(rows) else np.full(len(levels), np.nan)
    return {'levels': list(levels), 'y': [None if not math.isfinite(v) else round(float(v), 4) for v in y]}


# ---------------------------------------------------------------- the run

def _fit(im, target):
    """PIL image -> RGB uint8 array with its long side <= target (the model letterboxes to target anyway)."""
    from PIL import Image
    im = im.convert('RGB')
    if target and max(im.size) > target:
        im.thumbnail((target, target), Image.BILINEAR)
    return np.asarray(im)


def _scores(names, gt, nc, dets, sizes, conf):
    """Score of one level: dets[i] = (N, 6) [x1 y1 x2 y2 score cls] in original pixels (dataset class ids, -1 =
    not a dataset class) of names[i]; gt = [(name, bbox, cls)]."""
    from ..models import evaluate
    samples = [{'id': j, 'image': nm, 'bbox': b, 'cls': c} for j, (nm, b, c) in enumerate(gt)]
    img = np.concatenate([np.full(len(d), i, np.int32) for i, d in enumerate(dets)]) if dets else np.zeros(0, np.int32)
    det = np.concatenate(dets).astype(np.float32) if dets else np.zeros((0, 6), np.float32)
    d = evaluate.Data(samples, nc, {'images': names, 'img': img, 'det': det}, det[:, 5].astype(np.int64), {})
    top = np.array([float(x[:, 4].max()) if len(x) else 0.0 for x in dets])
    found = np.array([int((x[:, 4] >= conf).sum()) for x in dets])
    out = {'mean_top': round(float(top.mean()) if len(top) else 0.0, 4),
           'none_found': round(float((found == 0).mean()) if len(found) else 0.0, 4),
           'per_image': round(float(found.mean()) if len(found) else 0.0, 3)}
    if gt:
        ap = evaluate.ap_stats(d, 'all')
        ci = min(100, max(0, round(conf * 100)))
        out.update(map50=round(ap['map50'], 4), map=round(ap['map'], 4), p=round(ap['p_conf_all'][ci], 4),
                   r=round(ap['r_conf_all'][ci], 4))
    return out


def run(state, m, fp, workdir, names, axes, conf, job=None, labelled=True):
    """The stress result of model `m` on images `names` (union names) over `axes`. labelled: score against the
    labels (else against the clean predictions). Raises InterruptedError when the job is cancelled."""
    from PIL import Image, ImageOps

    from ..core import hardware
    from ..models import predict, runners, schema
    axes = [a for a in axes if a in AXES] or list(AXES)
    if job is not None:
        job.progress(stage=f'loading {m["name"]}')
    runner = runners.get_runner(m, fp, workdir)
    target = int(getattr(runner, 'draft_size', None) or getattr(runner, 'imgsz', None) or BASE)
    bs = predict.pick_batch_size(runner, m, None)
    src = state.source
    cancelled = getattr(job, 'cancelled', None)

    def decode(nm):
        im = src.open_image(nm)
        size0 = im.size
        if im.format == 'JPEG' and max(size0) >= 2 * target:
            k = target / max(size0)
            im.draft('RGB', (max(1, int(size0[0] * k)), max(1, int(size0[1] * k))))
        return _fit(ImageOps.exif_transpose(im), target), size0

    ims, sizes, keep = [], [], []
    with ThreadPoolExecutor(hardware.workers('io'), thread_name_prefix='stress-io') as ex:
        for nm, r in zip(names, ex.map(lambda n: _safe(decode, n), names)):
            if r is not None:
                ims.append(r[0])
                sizes.append(r[1])
                keep.append(nm)
    names = keep
    if not names:
        raise ValueError('none of the sampled images could be read')
    levels = {a: [lv for lv in AXES[a][1] if lv != AXES[a][2]] for a in axes}
    total = len(names) * (1 + sum(len(v) for v in levels.values()))
    done = [0]
    model_names = []

    def predict_level(axis, level):
        out = []
        with ThreadPoolExecutor(hardware.workers('omp'), thread_name_prefix='stress-aug') as ex:
            batches = [list(range(i, min(i + bs, len(ims)))) for i in range(0, len(ims), bs)]
            prep = lambda ix: [ex.submit(apply, ims[i], axis, level, i) if axis else None for i in ix]
            fut = prep(batches[0]) if axis else None
            for b, ix in enumerate(batches):
                if cancelled is not None and cancelled.is_set():
                    raise InterruptedError('cancelled')
                cur = [f.result() for f in fut] if axis else [ims[i] for i in ix]
                if axis and b + 1 < len(batches):
                    fut = prep(batches[b + 1])
                res = predict.predict_batch(m, fp, workdir, [Image.fromarray(x) for x in cur])
                for i, d in zip(ix, res):
                    h, w = ims[i].shape[:2]
                    w0, h0 = sizes[i]
                    d = np.asarray(d, np.float32).reshape(-1, 6).copy()
                    if (w0, h0) != (w, h):
                        d[:, [0, 2]] *= w0 / w
                        d[:, [1, 3]] *= h0 / h
                    d = schema.clean(d, w0, h0)
                    out.append(d[d[:, 4] >= runners.LOW_CONF])
                done[0] += len(ix)
                if job is not None:
                    job.progress(done[0], total, f'stress test · {AXES[axis][0] + f" {level}" if axis else "clean"}')
        if not model_names:
            model_names.extend(list(runners.get_runner(m, fp, workdir).names or []))
        return out

    raw_clean = predict_level(None, None)
    # class ids: the dataset's (labels) or the model's own (consistency)
    if labelled:
        nc = len(state.classes)
        to_ds = lambda d: _to_ds(d, model_names, state.classes)
        gt = []
        for nm in names:
            for sid in state.by_image.get(nm, []):
                s = state.samples[sid]
                gt.append((nm, list(map(float, s['bbox'])), int(s['cls'])))
    else:
        to_ds = lambda d: d
        clean_ref = [d[d[:, 4] >= conf] for d in raw_clean]
        nc = int(max([int(d[:, 5].max()) + 1 for d in raw_clean if len(d)] or [1]))
        gt = [(nm, d[k, :4].tolist(), int(d[k, 5])) for nm, d in zip(names, clean_ref) for k in range(len(d))]
    clean = _scores(names, gt, nc, [to_ds(d) for d in raw_clean], sizes, conf)
    result = {'model': {'id': m['id'], 'name': m['name']}, 'n': len(names), 'labelled': bool(labelled),
              'n_labels': len(gt), 'conf': conf, 'clean': clean,
              'score_key': 'map50' if gt else 'mean_top', 'axes': []}
    for a in axes:
        lab, lv_all, neutral, unit, key, _ = AXES[a]
        rows = []
        for lv in lv_all:
            if lv == neutral:
                rows.append({'level': lv, **clean})
                continue
            rows.append({'level': lv, **_scores(names, gt, nc, [to_ds(d) for d in predict_level(a, lv)], sizes, conf)})
        curve = measure_curve(ims, a)
        result['axes'].append({'key': a, 'label': lab, 'unit': unit, 'neutral': neutral, 'metric': key,
                               'rows': rows, 'curve': curve})
    return result


def _safe(fn, *a):
    try:
        return fn(*a)
    except (OSError, ValueError, KeyError):
        return None


def _to_ds(d, model_names, ds_classes):
    from ..models import predict
    if not len(d):
        return d
    d = d.copy()
    d[:, 5] = predict.map_classes(d[:, 5], model_names, ds_classes)
    return d


# ---------------------------------------------------------------- where B sits, the safe window

def _interp_level(y_target, levels, ys):
    """The level at which the measured change `ys` reaches y_target (linear, ys need not be monotone: the run of
    levels through the neutral one is used), (level, beyond) - beyond = outside the tested range."""
    pts = [(float(lv), float(y)) for lv, y in zip(levels, ys) if y is not None and math.isfinite(y)]
    if len(pts) < 2:
        return None, False
    pts.sort(key=lambda p: p[1])
    yv = np.array([p[1] for p in pts])
    xv = np.array([p[0] for p in pts])
    beyond = bool(y_target < yv[0] or y_target > yv[-1])
    return float(np.interp(y_target, yv, xv)), beyond


def b_point(axis, row):
    """Where B sits on `axis`, from the shift report row of its metric (a / b medians, a p5 / p95) through the
    measured curve: {level, beyond, band: [lo, hi] (A's own p5..p95 on the level scale)}. None without the metric."""
    if not row or not row.get('available'):
        return None
    kind = AXES[axis['key']][5]
    a, b = row['a'], row['b']

    def change(v):
        if kind == 'ratio':
            return v / a['med'] if a['med'] > 1e-9 else None
        if kind == 'delta':
            return v - a['med']
        return v
    yb = change(b['med'])
    if yb is None:
        return None
    lvls, ys = axis['curve']['levels'], axis['curve']['y']
    lb, beyond = _interp_level(yb, lvls, ys)
    if lb is None:
        return None
    band = [_interp_level(change(a['p5']), lvls, ys)[0], _interp_level(change(a['p95']), lvls, ys)[0]]
    band = sorted(x for x in band if x is not None)
    return {'level': round(lb, 4), 'beyond': beyond, 'band': band if len(band) == 2 else None}


def _window(levels, scores, neutral, thr):
    """[lo, hi] levels around the neutral one where the score stays >= thr (linear between levels); a side that
    never drops is the last tested level, open[side] True."""
    lv = np.array(levels, np.float64)
    sc = np.array([np.nan if s is None else s for s in scores], np.float64)
    o = np.argsort(lv)
    lv, sc = lv[o], sc[o]
    i0 = int(np.argmin(np.abs(lv - neutral)))
    edges, open_ = [], []
    for step in (-1, 1):
        i = i0
        while 0 <= i + step < len(lv) and sc[i + step] >= thr:
            i += step
        j = i + step
        if 0 <= j < len(lv) and np.isfinite(sc[j]) and sc[i] != sc[j]:
            t = (sc[i] - thr) / (sc[i] - sc[j])
            edges.append(float(lv[i] + t * (lv[j] - lv[i])))
            open_.append(False)
        else:
            edges.append(float(lv[i]))
            open_.append(True)
    return [min(edges), max(edges)], open_


def summarise(result, report=None):
    """Per axis: window, B's point and expected score, status (ok < 5 % drop <= moderate < 15 % <= major); overall
    the expected score on B (the worst axis), the limiting axis, advice. report: the shift report (B's medians)."""
    sk = result['score_key']
    clean = float(result['clean'].get(sk) or 0.0)
    thr = KEEP * clean
    rows_by = {m['key']: m for m in (report or {}).get('metrics', [])}
    out = []
    for ax in result['axes']:
        levels = [r['level'] for r in ax['rows']]
        scores = [r.get(sk) for r in ax['rows']]
        win, open_ = _window(levels, scores, ax['neutral'], thr)
        bp = b_point(ax, rows_by.get(ax['metric'])) if report else None
        item = {'key': ax['key'], 'label': ax['label'], 'unit': ax['unit'], 'window': [round(w, 4) for w in win],
                'open': open_, 'worst': round(float(np.nanmin([np.nan if s is None else s for s in scores])), 4),
                'b': bp}
        if bp is not None:
            o = np.argsort(levels)
            exp = float(np.interp(bp['level'], np.array(levels, np.float64)[o], np.array(scores, np.float64)[o]))
            drop = 1.0 - exp / clean if clean > 1e-9 else 0.0
            item.update(expected=round(exp, 4), drop=round(drop, 4),
                        status='ok' if drop < 0.05 else 'moderate' if drop < 0.15 else 'major')
        out.append(item)
    scored = [x for x in out if 'expected' in x]
    worst = min(scored, key=lambda x: x['expected']) if scored else None
    advice = []
    for x in sorted(scored, key=lambda x: -x['drop']):
        if x['status'] == 'ok':
            continue
        lvl = x['b']['level']
        tip = ADVICE[x['key']].format(m=min(0.9, abs(lvl - 1) + 0.1))
        advice.append({'key': x['key'], 'severity': x['status'],
                       'text': f'{sk_label(sk)} falls {x["drop"] * 100:.0f}% at B\'s {x["label"]} '
                               f'({fmt_level(lvl, x["unit"])}{", beyond the tested range" if x["b"]["beyond"] else ""}): '
                               f'{tip}.'})
    head = None
    if worst is not None:
        head = {'expected': worst['expected'], 'clean': round(clean, 4), 'drop': worst['drop'], 'axis': worst['key'],
                'label': worst['label'], 'status': worst['status']}
    return {'axes': out, 'headline': head, 'advice': advice, 'score_key': sk, 'clean': round(clean, 4),
            'threshold': round(thr, 4)}


def sk_label(sk):
    return {'map50': 'mAP50', 'mean_top': 'confidence'}.get(sk, sk)


def fmt_level(v, unit):
    if unit in ('×', 'γ'):
        return f'{v:.2f}{unit}'
    if unit == 'K':
        return f'{v:+.0f} K'
    return f'{v:.1f} {unit}'


# ---------------------------------------------------------------- cache

def key_of(m_id, fp, names, axes, conf, edit_seq=0, labelled=True):
    h = hashlib.sha1('\n'.join(names).encode())
    h.update(f'|{m_id}|{fp}|{",".join(sorted(axes))}|{conf}|{edit_seq}|{labelled}|v1'.encode())
    return 'st' + h.hexdigest()[:18]


def load(cache_dir, key):
    p = Path(cache_dir) / 'compare' / f'stress-{key}.json'
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def save(cache_dir, key, result):
    d = Path(cache_dir) / 'compare'
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f'stress-{key}.json.tmp'
    tmp.write_text(json.dumps(result))
    tmp.replace(d / f'stress-{key}.json')
