"""
Augment › Coverage, the work behind the job: turn the three image sets into embeddings, each part cached on its own,
then image/augment_cover.run.

    <cache>/augment/emb/<part>/imgs/00000.jpg   224 px thumbnails (what the features read, and what the page shows)
    <cache>/augment/emb/<part>/meta.json         {names, stats, steps (aug only)}
    <cache>/augment/emb/<part>/vec_<feature>.npy the embeddings of one feature

    part train_<h>: images of this session (fingerprint, how many, seed)
    part aug_<h>:   those images through the recipe, k variants each (train part + recipe + k)
    part tgt_<h>:   the target images (an upload, or a Bucket folder with its files' sizes and times)

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
PART_RE = re.compile(r'^(train|aug|tgt|ghost)_[0-9a-f]{16}$')


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


def ensure_target(root, key, files, read=None):
    """files: the target images, local paths, or names read with read(name) -> PIL image (another session's source);
    unreadable ones are skipped and counted."""
    from PIL import Image, ImageOps
    d = root / f'tgt_{key}'
    m = meta_of(d)
    if m:
        return d, m
    names, bad = [], []

    def gen():
        for p in files:
            try:
                im = read(p) if read is not None else Image.open(p)
                with im:
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


def make_copy(names, load, recipe, k, originals, out_root, classes, progress=None, cancelled=None, seed=0):
    """An augmented copy as a YOLO folder: images/<name>_aug<j>.jpg + labels/<name>_aug<j>.txt for k variants of each
    image (+ the originals when asked), classes.txt and recipe.json (what made it). Boxes the recipe lost are left out
    of the labels. load(name) -> (full-size uint8 image, boxes px). -> {images, boxes, lost}."""
    from PIL import Image
    out_root = Path(out_root)
    (out_root / 'images').mkdir(parents=True, exist_ok=True)
    (out_root / 'labels').mkdir(exist_ok=True)
    (out_root / 'classes.txt').write_text(''.join(f'{c}\n' for c in classes))
    (out_root / 'recipe.json').write_text(json.dumps({'recipe': ag.validate(recipe), 'variants': k, 'originals': bool(originals),
                                                      'source_images': len(names)}, indent=2))
    compose = ag.build(recipe, seed)
    done = {'images': 0, 'boxes': 0, 'lost': 0}

    def save(stem, im, boxes):
        h, w = im.shape[:2]
        Image.fromarray(im).save(out_root / 'images' / f'{stem}.jpg', 'JPEG', quality=92)
        lines = [f'{c} {(x1 + x2) / 2 / w:.6f} {(y1 + y2) / 2 / h:.6f} {(x2 - x1) / w:.6f} {(y2 - y1) / h:.6f}'
                 for c, x1, y1, x2, y2 in boxes]
        (out_root / 'labels' / f'{stem}.txt').write_text('\n'.join(lines) + ('\n' if lines else ''))
        done['images'] += 1
        done['boxes'] += len(boxes)
    for i, name in enumerate(names):
        if cancelled is not None and cancelled():
            break
        rgb, boxes = load(name)
        stem = re.sub(r'[^A-Za-z0-9._-]+', '_', Path(name).with_suffix('').as_posix())
        if originals:
            save(stem, rgb, [(int(c), *b) for c, *b in boxes])
        for j in range(k):
            im, out, lost, _ = ag.apply(rgb, boxes, recipe, seed * 1000 + i * 10 + j, compose=compose)
            save(f'{stem}_aug{j + 1}', im, out)
            done['lost'] += lost
        if progress is not None:
            progress(i + 1, len(names), 'writing the augmented copy')
    return done


# ---------------------------------------------------------------- the search: a recipe for these target images
CANDIDATES = ['RandomBrightnessContrast', 'RandomGamma', 'HueSaturationValue', 'PlanckianJitter', 'RandomToneCurve',
              'GaussNoise', 'ISONoise', 'GaussianBlur', 'MotionBlur', 'Defocus', 'Downscale', 'ImageCompression',
              'RandomFog', 'RandomShadow', 'ToGray', 'Sharpen', 'CLAHE']
SEARCH_IMAGES, ROUNDS, MIN_GAIN = 200, 4, 0.02


def score(rep):
    """Coverage after, less a penalty for augmented images like neither set (too strong to be real)."""
    off = rep.get('off_domain') or 0.0
    return rep['coverage']['after'] - 0.5 * max(0.0, off - 0.2)


def trial_fn(root, trk, names, load, train_v, tgt_v, feature, registry, seed=0, cancelled=None):
    """-> trial(recipe): its coverage report on SEARCH_IMAGES images, one variant each (cached per recipe, no map)."""
    sub = list(names[:SEARCH_IMAGES])
    sk = h16(trk, 'search', len(sub))

    def trial(recipe):
        r = ag.validate(recipe)
        d, m = ensure_aug(root, h16(sk, ag.recipe_hash(r), 1), sub, load, r, 1, seed, cancelled)
        v = embed(d, m['n'], feature, registry, lambda *a: None)
        return cov.run(train_v, v, tgt_v, m.get('steps') or [], with_map=False)
    return trial


STRENGTHS = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0)


def strength_curve(trial, recipe, progress=None):
    """Coverage and unreal variants of `recipe` at each strength -> {points, best}: best = the highest score."""
    pts = []
    for i, st in enumerate(STRENGTHS):
        rep = trial({**ag.validate(recipe), 'strength': st})
        pts.append({'strength': st, 'coverage': rep['coverage']['after'], 'off_domain': rep.get('off_domain') or 0.0, 'score': round(score(rep), 4)})
        if progress is not None:
            progress(i + 1, len(STRENGTHS), f'strength {st}')
    best = max(pts, key=lambda p: p['score'])
    return {'points': pts, 'best': best['strength'], 'before': rep['coverage']['before']}


def autotune(root, trk, names, load, base, train_v, tgt_v, feature, registry, seed=0, progress=None, cancelled=None,
             train_stats=None, tgt_stats=None):
    """Greedy: start from `base` (a recipe), add the candidate step that raises the score most, tune its chance, repeat
    while a round gains at least MIN_GAIN (at most ROUNDS). Each trial augments SEARCH_IMAGES images once and embeds
    them (cached per recipe). -> {recipe, history: [{step, label, p, coverage}], before, after}."""
    trial = trial_fn(root, trk, names, load, train_v, tgt_v, feature, registry, seed, cancelled)
    cur = ag.validate(base)
    cur_rep = trial(cur) if cur['steps'] else cov.run(train_v, np.zeros((0, train_v.shape[1])), tgt_v, with_map=False)
    start = cur_rep['coverage']
    best_score, history = score(cur_rep), []
    gap_steps = {g['step']['t']: g['step'] for g in (cov.gap(train_stats, tgt_stats) if train_stats and tgt_stats else [])}
    pool = list(dict.fromkeys([*gap_steps, *CANDIDATES]))                      # what the images differ in, first
    total = ROUNDS * (len(pool) + 3)
    done = 0
    for _ in range(ROUNDS):
        have = {s['t'] for s in cur['steps']}
        best = None
        for t in pool:
            if t in have:
                continue
            if cancelled is not None and cancelled():
                raise RuntimeError('cancelled')
            gap_step = gap_steps.get(t)
            step = {**ag._step(t), **({'args': gap_step['args']} if gap_step else {}), 'p': 0.5}
            cand = {**cur, 'steps': [*cur['steps'], step]}
            rep = trial(cand)
            done += 1
            if progress is not None:
                progress(done, total, f'trying {ag.BY_NAME[t]["label"]}')
            if best is None or score(rep) > best[0]:
                best = (score(rep), cand, rep)
        if best is None or best[0] - best_score < MIN_GAIN:
            break
        # its chance: a little more or less may do better
        top = best
        for p in (0.25, 0.8):
            c2 = {**best[1], 'steps': [*best[1]['steps'][:-1], {**best[1]['steps'][-1], 'p': p}]}
            rep = trial(c2)
            done += 1
            if score(rep) > top[0]:
                top = (score(rep), c2, rep)
        best_score, cur = top[0], ag.validate(top[1])
        last = cur['steps'][-1]
        history.append({'step': last['t'], 'label': ag.BY_NAME[last['t']]['label'], 'p': last['p'], 'coverage': top[2]['coverage']['after']})
    after = history[-1]['coverage'] if history else start['after']
    return {'recipe': cur, 'history': history, 'before': start['before'], 'start': start['after'], 'after': after}


# ---------------------------------------------------------------- augmented boxes on the session's own map
GHOST_IMAGES, GHOST_K, NEIGHBOURS = 200, 3, 10


def ghost_crops(root, key, names, load, recipe, k=1, seed=0, cancelled=None, pad=0.1):
    """Crops of the boxes of `names` after the recipe (k variants each), cut like the session's own crops (a padded
    square, image/indexer.square_crop_box) -> (part dir, meta {n, cls, image, applied})."""
    from PIL import Image
    from .indexer import square_crop_box
    d = root / f'ghost_{key}'
    m = meta_of(d)
    if m:
        return d, m
    imgs = d / 'imgs'
    imgs.mkdir(parents=True, exist_ok=True)
    compose = ag.build(recipe, seed)
    cls, image, applied, n = [], [], [], 0
    for i, name in enumerate(names):
        if cancelled is not None and cancelled():
            raise RuntimeError('cancelled')
        rgb, boxes = load(name)
        for j in range(k):
            im, out, _, steps = ag.apply(rgb, boxes, recipe, seed * 1000 + i * 10 + j, compose=compose)
            pil = Image.fromarray(im)
            for c, x1, y1, x2, y2 in out:
                if x2 - x1 < 2 or y2 - y1 < 2:
                    continue
                pil.crop(square_crop_box(x1, y1, x2, y2, pad)).resize((THUMB, THUMB), Image.BICUBIC).save(imgs / f'{n:05d}.jpg', 'JPEG', quality=90)
                cls.append(int(c))
                image.append(name)
                applied.append(steps)
                n += 1
    meta = {'n': n, 'cls': cls, 'image': image, 'applied': applied}
    (d / 'meta.json').write_text(json.dumps(meta))
    return d, meta


def place(X, Q, k=NEIGHBOURS):
    """Each row of Q among the rows of X (both embeddings, L2-normalised here): its k nearest rows and inverse-distance
    weights (summing to 1), and the cosine similarity to the nearest one -> (ids (q, k), weights (q, k), sim (q,))."""
    from sklearn.neighbors import NearestNeighbors
    X = X / np.maximum(np.linalg.norm(X, axis=1, keepdims=True), 1e-12)
    Q = Q / np.maximum(np.linalg.norm(Q, axis=1, keepdims=True), 1e-12)
    k = min(k, len(X))
    dist, ids = NearestNeighbors(n_neighbors=k).fit(X).kneighbors(Q, k)
    w = 1.0 / (dist + 1e-6)
    w /= w.sum(axis=1, keepdims=True)
    return ids, w, 1.0 - dist[:, 0] ** 2 / 2
