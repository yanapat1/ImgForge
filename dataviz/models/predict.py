"""
Run a model over every image of the open dataset (a background job) and cache the detections.

    <workspace>/predictions/<model id>_<model fingerprint>.npz
        images  (I,)   image names (all images, also those without labels)
        img     (M,)   image index of each detection
        det     (M, 6) x1 y1 x2 y2 score class_id   (original pixels, model class ids, score >= runners.LOW_CONF)
        names   json   the model's class names (or [])
        ms      float  mean inference time per image

Class ids are mapped to dataset class ids by name (mapping()), so a model whose class order differs from
the dataset's (e.g. lb_dataset swaps 13/14) is still scored correctly.
"""

import re
import json
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from . import runners, schema


def cache_path(state, m, fp):
    return Path(state.cache_dir) / 'predictions' / f'{m["id"]}_{fp}.npz'


def load(path):
    if not path.exists():
        return None
    z = np.load(path, allow_pickle=False)
    return {'images': [str(s) for s in z['images']], 'img': z['img'], 'det': z['det'],
            'names': json.loads(str(z['names'])), 'ms': float(z['ms'])}


def _norm(name):
    return str(name).strip().lower().replace('_', '-').replace(' ', '-')


def mapping(model_names, ds_classes):
    """Model class id -> dataset class id (-1 = no such class in the dataset), and the unmatched names."""
    if not model_names or all(re.fullmatch(r'(class[_-]?)?\d+', str(n).strip().lower()) for n in model_names):
        return None, []                  # no real names (e.g. '0', '1', ... or class_3): ids are dataset ids
    ds = {_norm(c): i for i, c in enumerate(ds_classes)}
    out, unmatched = [], []
    for n in model_names:
        out.append(ds.get(_norm(n), -1))
        if out[-1] < 0:
            unmatched.append(n)
    return np.asarray(out, np.int64), unmatched


def map_classes(det_cls, model_names, ds_classes):
    mp, _ = mapping(model_names, ds_classes)
    c = det_cls.astype(np.int64)
    if mp is None:                       # no names: ids are dataset ids
        return np.where(c < len(ds_classes), c, -1)
    return np.where((c >= 0) & (c < len(mp)), mp[np.clip(c, 0, len(mp) - 1)], -1)


READ_AHEAD = 3            # batches decoded in advance while the model runs
DRAFT_FACTOR = 2          # JPEG draft decode keeps the long side >= DRAFT_FACTOR x the model input size


def _per_sample_bytes(runner):
    size = int(getattr(runner, 'imgsz', None) or getattr(runner, 'size', None) or 640)
    return size * size * 3 * 4 * 60          # input + rough activation footprint of a detector, fp32


def pick_batch_size(runner, m, configured=None):
    """Images per model call. A number in the config is a cap (and the exported graph's fixed batch is a hard limit);
    'auto' / None sizes the batch from free VRAM on a GPU, or uses the runner's default on CPU / for API models."""
    from ..core import hardware
    auto = configured in (None, '', 0, 'auto')
    if auto:
        bs = int(runner.batch_size or 8)
        if m.get('format') != 'api' and hardware.device() == 'cuda':
            bs = hardware.pick_batch(_per_sample_bytes(runner), cap=max(bs, 32))
    else:
        bs = int(configured)
    if runner.max_batch:
        bs = min(bs, runner.max_batch)
    return max(1, bs)


def _decode(source, name, target, draft):
    """-> (RGB image, (w0, h0) original size). With `target` (the model input size) a big JPEG is decoded at 1/2..1/8
    scale (Image.draft) while its long side stays >= DRAFT_FACTOR x target; the caller maps boxes back to w0 x h0."""
    im = source.open_image(name)
    size0 = im.size
    if draft and target and im.format == 'JPEG' and max(size0) >= 2 * DRAFT_FACTOR * target:
        k = DRAFT_FACTOR * target / max(size0)
        im.draft('RGB', (max(1, int(size0[0] * k)), max(1, int(size0[1] * k))))
    return im.convert('RGB'), size0


def predict_batch(m, fp, workdir, batch):
    """One model call over `batch` (PIL images) with the hardware fallbacks: CUDA OOM halves the batch, any other CUDA
    failure demotes the process to CPU (the runners then move their model, see Runner.sync_device)."""
    from ..core import hardware
    with runners._lock:
        runner = runners.get_runner(m, fp, workdir)
        parts = hardware.with_fallback(runner.predict, batch)
    return [d for part in parts for d in part]


def run(state, m, fp, workdir, job, batch_size=None, draft=True):
    """Predict every image; returns the cached dict (see module doc)."""
    from ..core import hardware
    state.wait_index(job)
    path = cache_path(state, m, fp)
    cached = load(path)
    if cached is not None:
        return cached
    job.progress(stage=f'loading {m["name"]}')
    runner = runners.get_runner(m, fp, workdir)
    images = sorted(state.source.list_images())
    bs = pick_batch_size(runner, m, batch_size)
    target = int(getattr(runner, 'draft_size', None) or 0) if draft else 0
    job.progress(0, len(images), f'predicting · {m["name"]}')
    img_idx, dets = [], []
    t_model = 0.0
    with ThreadPoolExecutor(hardware.workers('io')) as io_pool:      # decode / download ahead while the model runs
        def submit(start):
            return [io_pool.submit(_decode, state.source, n, target, draft) for n in images[start:start + bs]]
        starts = list(range(0, len(images), bs))
        pending = deque((st, submit(st)) for st in starts[:READ_AHEAD])
        nxt = READ_AHEAD
        while pending:
            start, futs = pending.popleft()
            if nxt < len(starts):
                pending.append((starts[nxt], submit(starts[nxt])))
                nxt += 1
            decoded = [f.result() for f in futs]
            batch = [d[0] for d in decoded]
            t0 = time.perf_counter()
            # one loaded runner is shared with the labeling page (models/hub.py): hold it for the batch, and get it
            # again in case a label-page call swapped models in between (a reload, never a runner closed mid-batch)
            res = predict_batch(m, fp, workdir, batch)
            t_model += time.perf_counter() - t0
            for k, (im, d) in enumerate(zip(batch, res)):
                w0, h0 = decoded[k][1]
                if (w0, h0) != im.size:                          # drafted decode: back to original pixels
                    d = np.asarray(d, np.float32).copy()
                    d[:, [0, 2]] *= w0 / im.width
                    d[:, [1, 3]] *= h0 / im.height
                d = schema.clean(d, w0, h0)
                d = d[d[:, 4] >= runners.LOW_CONF]
                img_idx.append(np.full(len(d), start + k, np.int32))
                dets.append(d)
            job.progress(min(start + bs, len(images)), len(images), extra=f'{t_model / (start + len(batch)) * 1000:.0f} ms/img')
        runner = runners.get_runner(m, fp, workdir)
    out = {'images': images, 'img': np.concatenate(img_idx) if img_idx else np.zeros(0, np.int32),
           'det': np.concatenate(dets).astype(np.float32) if dets else np.zeros((0, 6), np.float32),
           'names': list(runner.names or []), 'ms': t_model / max(1, len(images)) * 1000}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp.npz')
    np.savez_compressed(tmp, images=np.asarray(images), img=out['img'], det=out['det'],
                        names=np.asarray(json.dumps(out['names'])), ms=np.asarray(out['ms']))
    tmp.replace(path)
    return out
