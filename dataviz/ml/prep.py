"""
Preprocessing of a projection (the Map's Preprocess section): what happens to the boxes before the feature extractor
(image prep) and to the feature vectors before the method (feature prep). Pure: no state, importable in a worker.

    spec = {'image': {...}, 'feature': {...}}       only the keys that differ from the default; {} = Default (today)

Image prep (a cached feature variant `<feature>~<tag>`: image/prepcrops.py writes the crops, the feature runs on them)
    context   box | pad5 | square10 (default: the index's crop) | pad25 | pad50 | image   the region around the box
    fit       square (default: a square around the box) | letterbox | stretch | center     how it becomes a square
    mask      '' | polygon: outside a segmentation polygon filled with grey (the object only)
    tone      none | gray | clahe: colour / lighting evened out
    tta       '' | hflip: the features of the crop and its mirror image averaged

Feature prep (in the projection worker, no re-extraction; part of the projection key `|f:<tag>`)
    fuse      {feature, weight 0.05-0.95}: concatenated with another feature space, weighted
    debias    '' | folder | session: each group's mean vector subtracted (camera / folder bias)
    scale     none | standardize | whiten
    pca       50 (default: what the methods did) | 0 (off) | 20 | 100 | 200 dims before the method
    filter    {min_px, min_blur, brightness: [lo, hi]}: boxes left out of the map ...
    show      hide (default) | grey: ... or placed by their neighbours and drawn faint
"""

import hashlib
import json

import numpy as np

IMAGE = {'context': ('square10', 'box', 'pad5', 'pad25', 'pad50', 'image'),
         'fit': ('square', 'letterbox', 'stretch', 'center'),
         'mask': ('', 'polygon'),
         'tone': ('none', 'gray', 'clahe'),
         'tta': ('', 'hflip')}
CONTEXT_PAD = {'box': 0.0, 'pad5': 0.05, 'square10': 0.1, 'pad25': 0.25, 'pad50': 0.5, 'image': None}
SCALES = ('none', 'standardize', 'whiten')
PCA_DIMS = (50, 0, 20, 100, 200)
DEBIAS = ('', 'folder', 'session')
SHOW = ('hide', 'grey')
FEATURE_DEFAULTS = {'scale': 'none', 'pca': 50, 'debias': '', 'show': 'hide'}
FUSE_MAX_BYTES = 6 << 30            # the fused matrix of one projection (float32): refused above this


class PrepError(ValueError):
    """A preprocessing spec the user can fix (an unknown option, a value out of range)."""


def _num(v, name, lo, hi):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise PrepError(f'{name} must be a number') from None
    if not lo <= x <= hi:
        raise PrepError(f'{name} must be between {lo} and {hi}')
    return x


def validate(spec):
    """The canonical spec: unknown keys / values refused (PrepError), defaults dropped -> {'image'?, 'feature'?}."""
    if spec in (None, {}):
        return {}
    if not isinstance(spec, dict) or set(spec) - {'image', 'feature'}:
        raise PrepError('prep has two parts: image and feature')
    out = {}
    im = spec.get('image') or {}
    if not isinstance(im, dict) or set(im) - set(IMAGE):
        raise PrepError(f'image prep keys: {", ".join(IMAGE)}')
    img = {}
    for k, allowed in IMAGE.items():
        v = im.get(k, allowed[0])
        v = '' if v is None or v is False else ('polygon' if v is True and k == 'mask' else 'hflip' if v is True else v)
        if v not in allowed:
            raise PrepError(f'image.{k} must be one of {", ".join(x or "off" for x in allowed)}')
        if v != allowed[0]:
            img[k] = v
    if img.get('context') == 'image' and img.get('mask'):
        raise PrepError('a polygon mask needs a box context, not the whole image')
    if img:
        out['image'] = img
    fe = spec.get('feature') or {}
    if not isinstance(fe, dict) or set(fe) - {'fuse', 'debias', 'scale', 'pca', 'filter', 'show'}:
        raise PrepError('feature prep keys: fuse, debias, scale, pca, filter, show')
    feat = {}
    if fe.get('scale', 'none') not in SCALES:
        raise PrepError(f'feature.scale must be one of {", ".join(SCALES)}')
    try:
        pca = int(fe.get('pca', 50))
    except (TypeError, ValueError):
        raise PrepError('feature.pca must be a whole number') from None
    if pca not in PCA_DIMS:
        raise PrepError(f'feature.pca must be one of {", ".join(map(str, PCA_DIMS))}')
    if (fe.get('debias') or '') not in DEBIAS:
        raise PrepError(f'feature.debias must be one of {", ".join(x or "off" for x in DEBIAS)}')
    if fe.get('show', 'hide') not in SHOW:
        raise PrepError('feature.show must be hide or grey')
    for k, v in (('scale', fe.get('scale', 'none')), ('pca', pca), ('debias', fe.get('debias') or ''),
                 ('show', fe.get('show', 'hide'))):
        if v != FEATURE_DEFAULTS[k]:
            feat[k] = v
    fu = fe.get('fuse')
    if fu:
        if not isinstance(fu, dict) or not fu.get('feature'):
            raise PrepError('feature.fuse needs {feature, weight}')
        feat['fuse'] = {'feature': str(fu['feature']), 'weight': round(_num(fu.get('weight', 0.3), 'fuse weight', 0.05, 0.95), 3)}
    fl = fe.get('filter') or {}
    if not isinstance(fl, dict) or set(fl) - {'min_px', 'min_blur', 'brightness'}:
        raise PrepError('feature.filter keys: min_px, min_blur, brightness')
    filt = {}
    if fl.get('min_px'):
        filt['min_px'] = int(_num(fl['min_px'], 'min_px', 1, 10000))
    if fl.get('min_blur'):
        filt['min_blur'] = round(_num(fl['min_blur'], 'min_blur', 0, 1e7), 3)
    br = fl.get('brightness')
    if br:
        if not isinstance(br, (list, tuple)) or len(br) != 2:
            raise PrepError('brightness is [lowest, highest] (0-255)')
        lo, hi = (_num(x, 'brightness', 0, 255) for x in br)
        if lo > hi:
            raise PrepError('brightness: the lowest is above the highest')
        if (lo, hi) != (0, 255):
            filt['brightness'] = [round(lo, 1), round(hi, 1)]
    if filt:
        feat['filter'] = filt
    if 'show' in feat and 'filter' not in feat:
        del feat['show']                    # nothing is filtered: hide / grey changes nothing
    if feat:
        out['feature'] = feat
    return out


def canon(part):
    return json.dumps(part or {}, sort_keys=True, separators=(',', ':'))


def tag(part):
    """8 hex chars naming a part of a spec ('' for an empty part: the Default keeps today's cache keys)."""
    return hashlib.sha1(canon(part).encode()).hexdigest()[:8] if part else ''


def variant_key(feature, image_part):
    """The feature key the image prep makes: `<feature>~<tag>` (the feature itself without image prep)."""
    return f'{feature}~{tag(image_part)}' if image_part else feature


def base_feature(key):
    return key.split('~', 1)[0]


def summary(spec):
    """A short human line of a spec ('' for the Default), for the map's label and the assistant."""
    im, fe = spec.get('image') or {}, spec.get('feature') or {}
    words = []
    if im.get('context'):
        words.append({'box': 'box only', 'image': 'whole image'}.get(im['context'], im['context'].replace('pad', '+') + '%'))
    if im.get('fit'):
        words.append(im['fit'])
    if im.get('mask'):
        words.append('masked')
    if im.get('tone'):
        words.append(im['tone'])
    if im.get('tta'):
        words.append('flip-avg')
    if fe.get('fuse'):
        words.append(f'+ {fe["fuse"]["feature"]} ×{fe["fuse"]["weight"]:g}')
    if fe.get('debias'):
        words.append(f'− {fe["debias"]} bias')
    if fe.get('scale'):
        words.append(fe['scale'])
    if 'pca' in fe:
        words.append(f'PCA {fe["pca"]}' if fe['pca'] else 'no PCA')
    if fe.get('filter'):
        words.append('filtered' + (' (grey)' if fe.get('show') == 'grey' else ''))
    return ', '.join(words)


# ---------------------------------------------------------------- feature prep (the worker)
def keep_mask(samples, ids, filt):
    """Bool per id: the boxes the filter keeps (all True without one)."""
    keep = np.ones(len(ids), bool)
    if not filt:
        return keep
    if filt.get('min_px'):
        side = np.fromiter((min(samples[i]['bbox'][2] - samples[i]['bbox'][0], samples[i]['bbox'][3] - samples[i]['bbox'][1])
                            for i in ids), np.float32, len(ids))
        keep &= side >= filt['min_px']
    if filt.get('min_blur'):
        keep &= np.fromiter((samples[i].get('blur') or 0 for i in ids), np.float32, len(ids)) >= filt['min_blur']
    if filt.get('brightness'):
        lo, hi = filt['brightness']
        b = np.fromiter((samples[i].get('brightness') or 0 for i in ids), np.float32, len(ids))
        keep &= (b >= lo) & (b <= hi)
    return keep


def group_codes(samples, ids, how):
    """int32 group code per id for debias (folder of the image, or its session / camera), or None."""
    if not how:
        return None
    if how == 'folder':
        names = [samples[i]['image'].rsplit('/', 1)[0] if '/' in samples[i]['image'] else '' for i in ids]
    else:
        names = [str(samples[i].get('session') or '') for i in ids]
    _, codes = np.unique(np.asarray(names, dtype=object).astype(str), return_inverse=True)
    return codes.astype(np.int32)


def _l2(x):
    n = np.linalg.norm(x, axis=1, keepdims=True)
    return x / np.maximum(n, 1e-12)


def apply_features(emb, fe, metric='cosine', groups=None, extra=None):
    """The feature prep of one projection, in the worker -> (matrix, notes): fuse, debias, scale (pca is the
    method's own step: analysis.project(pre_pca=...))."""
    x = np.asarray(emb, np.float32)
    notes = []
    fu = fe.get('fuse')
    if fu and extra is not None:
        w = float(fu['weight'])
        b = _l2(np.asarray(extra, np.float32))
        a = _l2(x) if metric == 'cosine' else x
        x = np.hstack([np.sqrt(1 - w) * a, np.sqrt(w) * b]).astype(np.float32)
    if fe.get('debias') and groups is not None:
        k = int(groups.max()) + 1 if len(groups) else 0
        if k < 2:
            notes.append(f'one {fe["debias"]} only: nothing to remove')
        else:
            sums = np.zeros((k, x.shape[1]), np.float64)
            np.add.at(sums, groups, x)
            cnt = np.bincount(groups, minlength=k).astype(np.float64)[:, None]
            x = (x - (sums / np.maximum(cnt, 1))[groups]).astype(np.float32)
            notes.append(f'{k} {fe["debias"]} groups centred')
    if fe.get('scale') == 'standardize':
        sd = x.std(0)
        x = ((x - x.mean(0)) / np.maximum(sd, 1e-6)).astype(np.float32)
    elif fe.get('scale') == 'whiten':
        from sklearn.decomposition import PCA
        nc = min(int(fe.get('pca') or 50), x.shape[1], len(x))
        x = PCA(n_components=nc, whiten=True, random_state=0).fit_transform(x).astype(np.float32)
    if metric == 'cosine' and (fe.get('debias') or fe.get('scale') or fu):
        x = _l2(x)                          # cosine maps (UMAP / k-NN) expect unit rows again
    return np.ascontiguousarray(x), notes
