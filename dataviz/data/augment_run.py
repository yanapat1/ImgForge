"""
Augment › Coverage, the work behind the job: turn the three image sets into embeddings, each part cached on its own,
then data/augment_cover.run.

    <cache>/augment/emb/<part>/imgs/00000.jpg   224 px thumbnails (what the features read, and what the page shows)
    <cache>/augment/emb/<part>/meta.json         {names, stats, steps (aug only)}
    <cache>/augment/emb/<part>/vec_<feature>.npy the embeddings of one feature

    part train_<h>: images of this session (fingerprint, how many, seed)
    part aug_<h>:   those images through the recipe, k variants each (train part + recipe + k)
    part tgt_<h>:   the target images (an upload, or a Data Center folder with its files' sizes and times)

A recipe change re-makes only the aug part; another target only the tgt part. Features: 'quick' = colour histogram +
HOG (CPU, seconds), else a deep feature key of the registry (DINOv2 / CLIP ...). Pure functions of their inputs, so
tests drive them without a server.
"""

import hashlib
import json
import re
from pathlib import Path

import numpy as np

from . import augment as ag
from . import augment_cover as cov

THUMB = 224
PART_RE = re.compile(r'^(train|aug|tgt)_[0-9a-f]{16}$')


def h16(*parts):
    return hashlib.sha1('|'.join(map(str, parts)).encode()).hexdigest()[:16]


def thumb(rgb):
    from PIL import Image
    return np.asarray(Image.fromarray(rgb).resize((THUMB, THUMB), Image.BICUBIC))


def _write(d, rgb_list_iter, extra=None):
    """Write the thumbnails of an iterator of uint8 images into d/imgs; meta (names, stats, + extra) into d/meta.json."""
    from PIL import Image
    imgs = d / 'imgs'
    imgs.mkdir(parents=True, exist_ok=True)
    stats = []
    for i, rgb in enumerate(rgb_list_iter):
        t = thumb(rgb)
        Image.fromarray(t).save(imgs / f'{i:05d}.jpg', 'JPEG', quality=90)
        stats.append(cov.image_stats(t))
    meta = {'n': len(stats), 'stats': stats, **(extra or {})}
    (d / 'meta.json').write_text(json.dumps(meta))
    return meta


def meta_of(d):
    try:
        return json.loads((d / 'meta.json').read_text())
    except (FileNotFoundError, ValueError):
        return None


def paths_of(d, n):
    return [d / 'imgs' / f'{i:05d}.jpg' for i in range(n)]


def embed(d, n, feature, registry, progress):
    """The embeddings of part d with `feature` (cached as vec_<feature>.npy)."""
    safe = re.sub(r'[^a-zA-Z0-9_.-]', '_', feature)
    f = d / f'vec_{safe}.npy'
    if f.exists():
        v = np.load(f)
        if len(v) == n:
            return v
    paths = paths_of(d, n)
    if feature == 'quick':
        from ..ml import features as feats
        v = np.hstack([feats.color_hist(paths, None, progress), feats.hog(paths, None, progress)])
        v = feats.l2_normalize(v.astype(np.float32))
    else:
        stub = [{'bbox': [0, 0, THUMB, THUMB], 'img_w': THUMB, 'img_h': THUMB, 'cls': 0, 'area': THUMB * THUMB} for _ in range(n)]
        v = np.asarray(registry[feature].fn(paths, stub, progress), np.float32)
    np.save(f, v)
    return v


def ensure_train(root, key, names, load):
    d = root / f'train_{key}'
    m = meta_of(d)
    if m and m.get('n') == len(names):
        return d, m
    return d, _write(d, (load(n)[0] for n in names), {'names': list(names)})


def ensure_aug(root, key, names, load, recipe, k, seed, cancelled=None):
    d = root / f'aug_{key}'
    m = meta_of(d)
    if m and m.get('n') == len(names) * k:
        return d, m
    compose = ag.build(recipe, seed)
    steps, src = [], []

    def gen():
        for i, name in enumerate(names):
            if cancelled is not None and cancelled():
                raise RuntimeError('cancelled')
            rgb, boxes = load(name)
            for j in range(k):
                im, _, _, applied = ag.apply(rgb, boxes, recipe, seed * 1000 + i * 10 + j, compose=compose)
                steps.append(applied)
                src.append(i)
                yield im
    meta = _write(d, gen())
    meta.update(steps=steps, src=src)
    (d / 'meta.json').write_text(json.dumps(meta))
    return d, meta


def ensure_target(root, key, files):
    """files: local paths of the target images (unreadable ones are skipped and counted)."""
    from PIL import Image, ImageOps
    d = root / f'tgt_{key}'
    m = meta_of(d)
    if m:
        return d, m
    names, bad = [], []

    def gen():
        for p in files:
            try:
                with Image.open(p) as im:
                    rgb = np.asarray(ImageOps.exif_transpose(im).convert('RGB'))
            except Exception:  # noqa: BLE001 - not an image, truncated: left out and said
                bad.append(Path(p).name)
                continue
            names.append(str(p))
            yield rgb
    meta = _write(d, gen())
    meta.update(names=names, skipped=bad)
    (d / 'meta.json').write_text(json.dumps(meta))
    return d, meta


def report(train_v, aug_v, tgt_v, tr_meta, au_meta, tg_meta, seed=0):
    rep = cov.run(train_v, aug_v, tgt_v, au_meta.get('steps') or [], tr_meta.get('stats'), tg_meta.get('stats'), seed=seed)
    names = tg_meta.get('names') or []
    rep['target_names'] = {str(i): Path(names[i]).name for i in rep['uncovered'] if i < len(names)}
    rep['skipped'] = tg_meta.get('skipped') or []
    for c in rep['credit']:
        c['label'] = ag.BY_NAME.get(c['t'], {}).get('label', c['t'])
    return rep
