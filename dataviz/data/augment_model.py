"""
Augment › Model: how a detection model holds up under the recipe. Each step of the recipe is applied at full chance
(and the whole recipe as it is) to the same sample of this session's images, the model predicts every version, and
each is scored against the labels moved by that step (geometry steps move the boxes, so the score stays honest).
A step that costs the model a lot is one to train with; a step it already handles adds little.

    run(load, names, recipe, predict, model_names, ds_classes, target, conf, progress, cancelled) -> report

load(name) -> (uint8 image with long side <= target, boxes [(cls, x1, y1, x2, y2)] in its pixels);
predict(list of PIL images) -> list of (N, 6) [x1 y1 x2 y2 score cls] in those pixels, model class ids.
Scores come from data/stress._scores (models/evaluate: mAP50, mAP, P and R at `conf`).
"""

import numpy as np

from . import augment as ag
from . import stress

KEEP = 0.95                                       # a step "handled" keeps at least this share of the clean mAP50


def run(load, names, recipe, predict, model_names, ds_classes, conf=0.25, batch=8, progress=None, cancelled=None, seed=0):
    from PIL import Image
    from ..models import predict as pmod
    r = ag.validate(recipe)
    on = [s for s in r['steps'] if s['on']]
    if not on:
        raise ValueError('the recipe has no step to apply')
    data = []
    for nm in names:
        try:
            data.append((nm, *load(nm)))
        except Exception:  # noqa: BLE001 - an unreadable image is left out
            continue
    if not data:
        raise ValueError('none of the sampled images could be read')
    variants = [('clean', 'No augmentation', None)] + \
        [(s['t'], ag.BY_NAME[s['t']]['label'], {'version': 1, 'mode': 'all', 'n': 1, 'strength': r['strength'], 'steps': [{**s, 'p': 1.0}]}) for s in on] + \
        ([('recipe', 'The whole recipe', r)] if len(on) > 1 else [])
    total, done = len(data) * len(variants), 0
    nc = len(ds_classes)
    rows = []
    for key, label, rec in variants:
        compose = ag.build(rec, seed) if rec else None
        ims, gt, dets, vnames = [], [], [], []
        for i, (nm, rgb, boxes) in enumerate(data):
            if rec:
                im, bx, _, _ = ag.apply(rgb, boxes, rec, seed * 1000 + i, compose=compose)
            else:
                im, bx = rgb, [(int(c), *b) for c, *b in boxes]
            vn = f'{i:05d}'
            vnames.append(vn)
            ims.append(im)
            gt += [(vn, [float(x1), float(y1), float(x2), float(y2)], int(c)) for c, x1, y1, x2, y2 in bx]
        for a in range(0, len(ims), batch):
            if cancelled is not None and cancelled():
                raise InterruptedError('cancelled')
            chunk = ims[a:a + batch]
            for im, d in zip(chunk, predict([Image.fromarray(x) for x in chunk]), strict=False):
                d = np.asarray(d, np.float32).reshape(-1, 6).copy()
                if len(d):
                    d[:, 5] = pmod.map_classes(d[:, 5], model_names, ds_classes)
                dets.append(d)
            done += len(chunk)
            if progress is not None:
                progress(done, total, f'model on: {label}')
        rows.append({'key': key, 'label': label, **stress._scores(vnames, gt, nc, dets, None, conf)})
    clean = rows[0]
    base = clean.get('map50') or 0.0
    for row in rows[1:]:
        row['drop'] = round(base - (row.get('map50') or 0.0), 4)
        row['kept'] = round((row.get('map50') or 0.0) / base, 4) if base else None
        row['handled'] = bool(base) and (row.get('map50') or 0.0) >= KEEP * base
    steps = sorted((x for x in rows[1:] if x['key'] != 'recipe'), key=lambda x: -x['drop'])
    hurts = [x for x in steps if not x['handled']]
    if not base:
        verdict = 'The model finds almost nothing on these images even without augmentation: check that it fits this dataset.'
    elif hurts:
        verdict = (f'The model handles {len(steps) - len(hurts)} of {len(steps)} steps. It loses most on '
                   + ', '.join(f'{x["label"]} (mAP50 −{x["drop"] * 100:.0f} points)' for x in hurts[:3])
                   + ': train with these, or collect images like them.')
    else:
        verdict = 'The model already handles every step of this recipe (each keeps 95 % of its clean mAP50): training with it adds robustness it mostly has.'
    return {'n': len(data), 'conf': conf, 'clean': clean, 'steps': steps, 'recipe': next((x for x in rows if x['key'] == 'recipe'), None),
            'verdict': verdict}
