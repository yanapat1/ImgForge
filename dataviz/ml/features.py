"""
Feature spaces for the box crops: deep embeddings (DINOv2, CLIP, your YOLO weights)
and simple hand-crafted features (colour histogram, HOG, box geometry).

Every extractor has the signature  fn(crop_paths, samples, progress) -> (N, D) float32
where progress(done, total, stage) reports to the job manager.
"""

import json
import os
import queue
import hashlib
import threading
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import cv2
import numpy as np
from PIL import Image

from ..core import hardware
from ..core import logging as vlog

lg = vlog.get_logger('features')

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
CLIP_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
CLIP_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
SIGLIP_MEAN = np.array([0.5, 0.5, 0.5], dtype=np.float32)
SIGLIP_STD = np.array([0.5, 0.5, 0.5], dtype=np.float32)
DEFAULT_FEATURE = 'dinov2-base'   # used when the config does not name one


@dataclass
class FeatureSpec:
    key: str
    label: str
    group: str                  # 'deep' | 'simple'
    fn: Callable
    metric: str = 'cosine'      # metric for UMAP / k-NN ('cosine' => rows are L2-normalized)
    note: str = ''
    kind: str = ''              # 'clip' | 'siglip' | 'dinov2' for config models, '' otherwise
    text_model: str | None = None   # HF id whose text tower shares this image space (text search)


def l2_normalize(x):
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    return (x / np.maximum(norms, 1e-12)).astype(np.float32)


def _load(path, size, mean=IMAGENET_MEAN, std=IMAGENET_STD):
    with Image.open(path) as im:
        img = im.convert('RGB')
    if img.size != (size, size):
        img = img.resize((size, size), Image.BICUBIC)
    arr = np.asarray(img, dtype=np.float32) / 255.0
    return ((arr - mean) / std).transpose(2, 0, 1)


# ---------------------------------------------------------------- shared decoded-crop cache
_CROP_LOCK = threading.Lock()


def _load_u8(path, size):
    """Decoded crop as uint8 (size, size, 3); a corrupt crop file becomes a black square instead of failing the job."""
    try:
        with Image.open(path) as im:
            img = im.convert('RGB')
    except (OSError, ValueError, SyntaxError):
        lg.warning(f'unreadable crop {path}; using a black square')
        return np.zeros((size, size, 3), np.uint8)
    if img.size != (size, size):
        img = img.resize((size, size), Image.BICUBIC)
    return np.asarray(img, dtype=np.uint8)


def _crops_fingerprint(paths, size):
    h = hashlib.sha1(f'{size}:{len(paths)}'.encode())
    for p in paths:
        try:
            st = os.stat(p)
            h.update(f'{Path(p).name}:{st.st_size}:{st.st_mtime_ns};'.encode())
        except OSError:
            h.update(b'?;')
    return h.hexdigest()[:16]


def crop_cache(paths, size=224, progress=None):
    """Decoded crops as a read-only uint8 memmap (N, size, size, 3), RGB, bicubic-resized like `_load`.

    Built once (parallel decode) as `features/_crops<size>_<fingerprint>.npy` next to the crops and shared by every
    model; the fingerprint covers the crop files' names, sizes and mtimes, so a changed dataset rebuilds it."""
    n = len(paths)
    if n == 0:
        return np.zeros((0, size, size, 3), np.uint8)
    fdir = Path(paths[0]).parent.parent / 'features'
    fp = _crops_fingerprint(paths, size)
    target = fdir / f'_crops{size}_{fp}.npy'
    with _CROP_LOCK:
        if target.exists():
            try:
                mm = np.load(target, mmap_mode='r')
                if mm.shape == (n, size, size, 3):
                    return mm
            except Exception as e:                  # damaged cache file: rebuild it below
                lg.warning(f'crop cache {target.name} unusable ({e!r}); rebuilding')
        fdir.mkdir(parents=True, exist_ok=True)
        tmp = fdir / f'{target.name}.{os.getpid()}.tmp'
        try:
            mm = np.lib.format.open_memmap(tmp, mode='w+', dtype=np.uint8, shape=(n, size, size, 3))
            done = 0
            if progress:
                progress(0, n, 'decoding crops')

            def put(i):
                mm[i] = _load_u8(paths[i], size)
            with ThreadPoolExecutor(hardware.workers('io')) as pool:
                for _ in pool.map(put, range(n)):
                    done += 1
                    if progress and done % 512 == 0:
                        progress(done, n)
            mm.flush()
            mm = None                               # release the memmap before the rename (put() is done with it)
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink()
        for old in fdir.glob(f'_crops{size}_*.npy'):
            if old != target:
                try:
                    old.unlink()
                except OSError:
                    pass
        return np.load(target, mmap_mode='r')


# ---------------------------------------------------------------- resident model LRU
_MODELS = OrderedDict()       # key -> model; the most recent MODEL_KEEP stay on the device between runs
_MODELS_LOCK = threading.Lock()
MODEL_KEEP = 2


def _release_models(keep=None):
    with _MODELS_LOCK:
        for k in [k for k in _MODELS if k != keep]:
            del _MODELS[k]
    _free_gpu()


def _free_gpu():
    try:
        import gc
        import torch
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _resident(key, build):
    """Model from the LRU (moved to the current device / precision), or build() it and keep it."""
    with _MODELS_LOCK:
        model = _MODELS.get(key)
        if model is not None:
            _MODELS.move_to_end(key)
    if model is None:
        model = build()
        with _MODELS_LOCK:
            _MODELS[key] = model
            _MODELS.move_to_end(key)
            evict = [k for k in _MODELS if k != key][:max(0, len(_MODELS) - MODEL_KEEP)]
            for k in evict:
                del _MODELS[k]
        if evict:
            _free_gpu()
    return model


def _to_current(model):
    """Move a (possibly demoted-to-CPU) model to hardware.device() with the matching dtype."""
    import torch
    dev = hardware.device()
    dtype = torch.float16 if dev == 'cuda' else torch.float32     # fp16 on GPU keeps the cache compatible
    p = next(model.parameters())
    if p.device.type != dev or p.dtype != dtype:
        model.to(dev, dtype)
    return dev, dtype


def _from_pretrained(name):
    import torch
    from transformers import AutoModel
    dev = hardware.device()
    dtype = torch.float16 if dev == 'cuda' else torch.float32
    try:
        m = AutoModel.from_pretrained(name, dtype=dtype, attn_implementation='sdpa')
    except Exception:                       # architecture without sdpa support
        m = AutoModel.from_pretrained(name, dtype=dtype)
    return m.to(dev).eval()


# ---------------------------------------------------------------- batched GPU loop
def _prefetch(make, count, depth=3):
    """Yield make(0..count-1) in order, produced by a background thread so decode / copy overlaps compute."""
    q = queue.Queue(depth)
    stop = threading.Event()
    END = object()

    def run():
        try:
            for i in range(count):
                if stop.is_set():
                    return
                item = make(i)
                while not stop.is_set():
                    try:
                        q.put(item, timeout=0.2)
                        break
                    except queue.Full:
                        pass
            q.put(END)
        except BaseException as e:          # noqa: BLE001 - re-raised in the consumer
            q.put(e)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        while True:
            item = q.get()
            if item is END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        stop.set()
        while not q.empty():
            try:
                q.get_nowait()
            except queue.Empty:
                break


def _batched_torch(paths, progress, forward, mean, std, batch_size=128, size=224, per_sample_bytes=24 << 20):
    """Shared loop for HF vision models. Crops come from the shared uint8 memmap, are copied to the GPU as uint8
    (pinned, async), normalised there, and the embeddings are collected and L2-normalised on the GPU."""
    n = len(paths)
    progress(0, n, 'embedding crops')
    if n == 0:                                  # nothing to embed: no need for torch (or a GPU) at all
        return np.zeros((0, 1), np.float32)
    import torch
    crops = crop_cache(paths, size, progress)
    batch_size = max(1, hardware.pick_batch(per_sample_bytes, cap=batch_size))
    starts = list(range(0, n, batch_size))
    mean_t, std_t = torch.tensor(mean, dtype=torch.float32), torch.tensor(std, dtype=torch.float32)
    stats = {}

    def stage(i):
        x = torch.from_numpy(np.array(crops[starts[i]:starts[i] + batch_size]))
        if hardware.device() == 'cuda':
            x = x.pin_memory()
        return x

    def run(x):                              # x: uint8 (B, H, W, 3) on the host -> (B, D) float32 on the device
        dev = hardware.device()
        try:
            xd = x.to(dev, non_blocking=True)
            if dev not in stats:
                stats[dev] = (mean_t.to(dev).view(1, 3, 1, 1), std_t.to(dev).view(1, 3, 1, 1))
            m, s = stats[dev]
            xd = (xd.permute(0, 3, 1, 2).float().div_(255.0) - m) / s
            return forward(xd.to(torch.float16 if dev == 'cuda' else torch.float32)).float()
        except Exception as e:
            if hardware.is_oom(e):
                _release_models(keep=None)     # drop the resident models (they reload on the next run)
            raise

    outs, pending, spilled, done = [], 0, [], 0
    with torch.inference_mode():
        for i, x in enumerate(_prefetch(stage, len(starts))):
            outs.extend(hardware.with_fallback(run, x))
            done += len(x)
            pending += sum(o.numel() * 4 for o in outs[-1:])
            if pending > (1 << 30):          # keep the GPU-side result buffer bounded
                spilled.append(_gather(outs))
                outs, pending = [], 0
            if i % 8 == 7 and hardware.device() == 'cuda':
                torch.cuda.synchronize()     # honest progress without a sync per batch
            progress(done, n)
        if outs:
            spilled.append(_gather(outs))
    return np.concatenate(spilled) if spilled else np.zeros((0, 1), np.float32)


def _gather(outs):
    """Concatenate batch outputs, L2-normalise where they live, one device -> host copy."""
    import torch
    dev = outs[-1].device
    x = torch.cat([o.to(dev) for o in outs])
    x = x / x.norm(dim=1, keepdim=True).clamp_min(1e-12)
    return x.float().cpu().numpy()


def hf_dinov2(model_name, batch_size=128):
    def fn(paths, samples, progress):
        progress(stage=f'loading {model_name} (downloads on first use)')
        model = _resident(('hf', model_name), lambda: _from_pretrained(model_name))
        _to_current(model)

        def forward(x):
            _to_current(model)
            return model(pixel_values=x).pooler_output
        return _batched_torch(paths, progress, forward, IMAGENET_MEAN, IMAGENET_STD, batch_size=batch_size)
    return fn


def hf_image_features(model_name, mean, std, batch_size=64):
    """Image-tower embeddings of an HF image-text model (SigLIP 2, OpenCLIP) via get_image_features."""
    def fn(paths, samples, progress):
        progress(stage=f'loading {model_name} (downloads on first use)')
        model = _resident(('hf', model_name), lambda: _from_pretrained(model_name))
        _to_current(model)

        def forward(x):
            import torch
            _to_current(model)
            out = model.get_image_features(pixel_values=x)
            return out if torch.is_tensor(out) else out.pooler_output
        return _batched_torch(paths, progress, forward, mean, std, batch_size=batch_size)
    return fn


def yolo_backbone(weights, batch_size=64, imgsz=224):
    """YOLO backbone embeddings. Crops are letterboxed by ultralytics (not stretched), so this reads the original
    files, but decodes the next batches in a background thread and feeds arrays straight to embed()."""
    def fn(paths, samples, progress):
        import torch
        from ultralytics import YOLO
        progress(stage=f'loading {Path(weights).parent.parent.name}')
        model = _resident(('yolo', str(weights)), lambda: YOLO(str(weights)))
        n = len(paths)
        progress(0, n, 'embedding crops')
        starts = list(range(0, n, batch_size))
        pool = ThreadPoolExecutor(hardware.workers('io'))

        def read(i):
            chunk = paths[starts[i]:starts[i] + batch_size]
            return [a if a is not None else np.zeros((imgsz, imgsz, 3), np.uint8)
                    for a in pool.map(lambda p: cv2.imread(str(p)), chunk)]
        out, done = [], 0
        try:
            for arrays in _prefetch(read, len(starts)):
                emb = model.embed(arrays, imgsz=imgsz, verbose=False)
                out.append(torch.stack([e.flatten() for e in emb]).float())
                done += len(arrays)
                progress(done, n)
            if not out:
                return np.zeros((0, 1), np.float32)
            x = torch.cat([o.cpu() for o in out]).numpy()
        except Exception as e:
            if hardware.is_oom(e):
                _release_models(keep=None)
            raise
        finally:
            pool.shutdown(wait=False)
        return l2_normalize(x)
    return fn


def color_hist(paths, samples, progress, bins=8):
    def one(p):
        bgr = cv2.imread(str(p))
        if bgr is None:                             # unreadable crop: black
            bgr = np.zeros((1, 1, 3), np.uint8)
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        h = cv2.calcHist([hsv], [0, 1, 2], None, [bins] * 3, [0, 180, 0, 256, 0, 256]).ravel()
        return np.sqrt(h / max(h.sum(), 1))
    return l2_normalize(_map_cpu(one, paths, progress, 'colour histograms'))


def _hog_descriptor(gray, cell=8, bins=9):
    """HOG with cv2.HOGDescriptor's default layout: 2×2-cell blocks at a 1-cell stride, unsigned
    gradients over `bins` orientation bins (linearly split between the two nearest), L2-Hys blocks.
    OpenCV 5 moved HOGDescriptor out of the main package (into contrib's xobjdetect), so it is done here."""
    g = gray.astype(np.float32)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=1)    # ksize=1: the [-1, 0, 1] kernel HOG uses
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=1)
    mag, ang = cv2.cartToPolar(gx, gy, angleInDegrees=True)
    b = (ang % 180.0) / (180.0 / bins) - 0.5
    b0 = np.floor(b)
    w1 = b - b0
    b0 = b0.astype(np.int64) % bins
    b1 = (b0 + 1) % bins
    h, w = g.shape
    ch, cw = h // cell, w // cell
    ys, xs = np.mgrid[:h, :w]
    c = (ys // cell) * cw + xs // cell
    hist = (np.bincount((c * bins + b0).ravel(), (mag * (1 - w1)).ravel(), ch * cw * bins)
            + np.bincount((c * bins + b1).ravel(), (mag * w1).ravel(), ch * cw * bins)).reshape(ch, cw, bins)
    blocks = np.concatenate([hist[:-1, :-1], hist[:-1, 1:], hist[1:, :-1], hist[1:, 1:]], axis=-1)
    blocks = blocks.reshape(-1, 4 * bins)
    blocks /= np.sqrt((blocks ** 2).sum(1, keepdims=True)) + 1e-6
    blocks = np.minimum(blocks, 0.2)                # L2-Hys: clip, then renormalise
    blocks /= np.sqrt((blocks ** 2).sum(1, keepdims=True)) + 1e-6
    return blocks.ravel().astype(np.float32)


def hog(paths, samples, progress, size=64):
    def one(p):
        gray = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        if gray is None:                            # unreadable crop: black
            gray = np.zeros((size, size), np.uint8)
        gray = cv2.resize(gray, (size, size), interpolation=cv2.INTER_AREA)
        return _hog_descriptor(gray)
    return l2_normalize(_map_cpu(one, paths, progress, 'HOG descriptors'))


def geometry(paths, samples, progress):
    progress(0, len(samples), 'box geometry')
    rows = []
    for s in samples:
        x1, y1, x2, y2 = s['bbox']
        w, h = (x2 - x1) / s['img_w'], (y2 - y1) / s['img_h']
        rows.append([np.log(max(s['area'], 1e-6)), np.log(max(s['aspect'], 1e-3)),
                     (x1 + x2) / 2 / s['img_w'], (y1 + y2) / 2 / s['img_h'], w, h,
                     s['brightness'] / 255.0, np.log1p(s['blur'])])
    x = np.asarray(rows, np.float32).reshape(len(samples), 8)
    progress(len(samples), len(samples))
    return ((x - x.mean(0)) / np.maximum(x.std(0), 1e-6)).astype(np.float32)


def _map_cpu(one, paths, progress, stage):
    n = len(paths)
    progress(0, n, stage)
    out = []
    with ThreadPoolExecutor(hardware.workers('io')) as pool:
        for i, row in enumerate(pool.map(one, paths), 1):      # all tasks queued up front, results in order
            out.append(row)
            if i % 256 == 0 or i == n:
                progress(i, n)
    return np.asarray(out, np.float32).reshape(n, -1)


def discover_yolo_weights(project_root, limit=5, runs_dir='runs', exclude=('rfdetr',)):
    """Most recent ultralytics best.pt files under runs/ (paths containing an `exclude` word are skipped)."""
    found = [p for p in Path(project_root, runs_dir).glob('**/weights/best.pt')
             if not any(word.lower() in str(p).lower() for word in exclude)]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)[:limit]


_NORM = {'dinov2': (IMAGENET_MEAN, IMAGENET_STD), 'clip': (CLIP_MEAN, CLIP_STD), 'siglip': (SIGLIP_MEAN, SIGLIP_STD)}
SIMPLE = {
    'color-hist': ('Colour histogram (HSV)', color_hist, 'cosine', 'lighting / colour bias'),
    'hog': ('HOG (shape / edges)', hog, 'cosine', ''),
    'geometry': ('Box geometry (size, position, light)', geometry, 'euclidean', 'size / position / brightness bias'),
}


def model_spec(m, default_batch=128):
    """FeatureSpec for one `features.models` entry of the config."""
    kind = m.get('type', 'dinov2')
    if kind not in _NORM:
        raise ValueError(f'features.models[{m.get("key")}]: type must be one of {sorted(_NORM)}, got {kind!r}')
    mean, std = _NORM[kind]
    mean = np.asarray(m.get('mean', mean), np.float32)
    std = np.asarray(m.get('std', std), np.float32)
    batch = int(m.get('batch_size', default_batch))
    fn = hf_dinov2(m['model'], batch_size=batch) if kind == 'dinov2' else \
        hf_image_features(m['model'], mean, std, batch_size=batch)
    return FeatureSpec(m['key'], m.get('label', m['key']), 'deep', fn, note=m.get('note', ''), kind=kind,
                       text_model=m['model'] if kind in ('clip', 'siglip') else None)


# ---------------------------------------------------------------- text tower (text search)
class TextEncoder:
    """Text embeddings in the image space of a CLIP / SigLIP feature. Holds only the last model (the text
    towers are 0.1-0.4 GB, the image tower is not loaded) and an LRU of encoded queries."""

    def __init__(self, cache=256):
        import threading
        from collections import OrderedDict
        self._lock = threading.Lock()
        self._model = None            # (name, model, tokenizer, device)
        self._memo = OrderedDict()
        self._cache = cache

    def loaded(self, name):
        return self._model is not None and self._model[0] == name

    def has(self, name, query):
        return (name, query) in self._memo

    def _load(self, name, progress=None):
        import torch
        from transformers import AutoModel, AutoTokenizer
        if self.loaded(name):
            return self._model
        if progress:
            progress(stage=f'loading {name} text tower (downloads on first use)')
        if self._model is not None:
            self._model = None
            torch.cuda.empty_cache()
        device = hardware.device()
        dtype = torch.float16 if device == 'cuda' else torch.float32
        model = AutoModel.from_pretrained(name, dtype=dtype).to(device).eval()
        if hasattr(model, 'vision_model'):
            del model.vision_model    # only the text side is used; frees most of the GPU memory
            torch.cuda.empty_cache()
        self._model = (name, model, AutoTokenizer.from_pretrained(name), device)
        return self._model

    def encode(self, name, kind, query, progress=None):
        """(D,) unit vector for `query`."""
        import torch
        with self._lock:
            if (name, query) in self._memo:
                self._memo.move_to_end((name, query))
                return self._memo[(name, query)]
            _, model, tok, device = self._load(name, progress)
            # siglip was trained on max_length=64 padding; shorter padding gives poor matches
            pad = {'padding': 'max_length', 'max_length': 64} if kind == 'siglip' else {'padding': True}
            batch = tok([prompt(query, kind)], truncation=True, return_tensors='pt', **pad).to(device)
            with torch.inference_mode():
                out = model.get_text_features(**batch)
            out = out if torch.is_tensor(out) else out.pooler_output
            vec = l2_normalize(out.float().cpu().numpy())[0]
            self._memo[(name, query)] = vec
            while len(self._memo) > self._cache:
                self._memo.popitem(last=False)
            return vec


def prompt(query, kind):
    """CLIP-style models match 'a photo of a dog' much better than a bare 'dog'; sentences are left as-is."""
    q = query.strip()
    if len(q.split()) <= 3 and not q.lower().startswith(('a ', 'an ', 'the ', 'photo')):
        return f'a photo of {q}'
    return q


TEXT = TextEncoder()


def build_registry(project_root, extra_yolo=(), cfg=None):
    """Feature spaces offered in the page, from the `features` section of the config."""
    if cfg is None:
        from ..core.config import DEFAULTS
        cfg = DEFAULTS['features']
    specs = [model_spec(m, cfg.get('batch_size', 128)) for m in cfg.get('models', [])]
    y = cfg.get('yolo', {})
    weights = [Path(w).resolve() for w in list(y.get('extra_weights', [])) + list(extra_yolo)]
    if y.get('auto_discover', True):
        weights += discover_yolo_weights(project_root, y.get('limit', 5), y.get('runs_dir', 'runs'),
                                         y.get('exclude', ['rfdetr']))
    for w in dict.fromkeys(weights):
        run = w.parent.parent.name if w.parent.name == 'weights' else w.stem
        specs.append(FeatureSpec(f'yolo:{run}', f'YOLO backbone · {run}', 'deep',
                                 yolo_backbone(w, y.get('batch_size', 64), y.get('imgsz', 224)), note=str(w)))
    for key in cfg.get('simple', list(SIMPLE)):
        if key not in SIMPLE:
            raise ValueError(f'features.simple: unknown feature {key!r} (choose from {list(SIMPLE)})')
        label, fn, metric, note = SIMPLE[key]
        specs.append(FeatureSpec(key, label, 'simple', fn, metric=metric, note=note))
    return {s.key: s for s in specs}


class FeatureRegistry(Mapping):
    """The config's feature spaces (`base`, shared by every session) plus extra ones resolved on use, each from a
    provider callable -> {key: FeatureSpec}: the session owner's own models (ml/user_features.OwnerModels) and the
    embeddings imported into the session (State.imported_features). A plain dict everywhere else: registry[key],
    `in`, .get, .values, .items."""

    def __init__(self, base, extra=None):
        self.base = base
        self.extras = [extra] if extra else []

    def add(self, provider):
        self.extras.append(provider)

    def extra(self):
        out = {}
        for p in self.extras:
            for k, v in p().items():
                out.setdefault(k, v)
        return out

    def __getitem__(self, key):
        if key in self.base:
            return self.base[key]
        return self.extra()[key]

    def __iter__(self):
        yield from self.base
        yield from (k for k in self.extra() if k not in self.base)

    def __len__(self):
        return len(self.base) + sum(1 for k in self.extra() if k not in self.base)

    def __contains__(self, key):
        return key in self.base or key in self.extra()

    def refresh(self):
        for p in self.extras:
            if hasattr(p, 'refresh'):
                p.refresh()


def cache_name(key):
    safe = ''.join(c if c.isalnum() or c in '-_' else '_' for c in key)
    return f'{safe}_{hashlib.sha1(key.encode()).hexdigest()[:6]}' if safe != key else safe


def load_cached(features_dir, key, fingerprint, n):
    path, meta = features_dir / f'{cache_name(key)}.npy', features_dir / f'{cache_name(key)}.json'
    if path.exists() and meta.exists():
        m = json.loads(meta.read_text())
        if m.get('fingerprint') == fingerprint and m.get('n') == n:
            return np.load(path, mmap_mode='r')     # read-only memmap: pages in on demand, shared by readers
    return None


def save_cached(features_dir, key, fingerprint, emb):
    features_dir.mkdir(parents=True, exist_ok=True)
    final = features_dir / f'{cache_name(key)}.npy'
    tmp = features_dir / f'{cache_name(key)}.tmp.npy'
    np.save(tmp, emb)
    os.replace(tmp, final)          # atomic: an open memmap of the previous file stays valid
    (features_dir / f'{cache_name(key)}.json').write_text(
        json.dumps({'fingerprint': fingerprint, 'n': len(emb), 'dim': int(emb.shape[1]) if emb.ndim == 2 else 0}))


def is_cached(features_dir, key, fingerprint, n):
    meta = features_dir / f'{cache_name(key)}.json'
    if not meta.exists():
        return False
    m = json.loads(meta.read_text())
    return m.get('fingerprint') == fingerprint and m.get('n') == n


# ------------------------------------------------------------ pgvector copy (ml/vectors.py)
# The .npy above stays the fast cache; PostgreSQL keeps the durable copy so a lost / pruned workspace folder
# is rebuilt without the GPU. Every call is best effort: no database, pgvector missing, a broken connection
# only costs a warning, never the feature job.
_backfilling = set()
_backfill_lock = threading.Lock()


def _vectors_on(cfg):
    if not ((cfg or {}).get('vectors') or {}).get('enabled', True):
        return False
    try:
        from .. import db
        db.engine()
    except RuntimeError:                               # no database configured (scripts, the file-only tests)
        return False
    return True


def vectors_store(fingerprint, key, emb, cfg):
    """Save a freshly computed feature matrix to pgvector."""
    if not _vectors_on(cfg) or getattr(emb, 'ndim', 0) != 2 or not emb.shape[1]:
        return
    try:
        from . import vectors
        vectors.save(fingerprint, key, np.asarray(emb), hnsw_min_rows=int((cfg.get('vectors') or {}).get('hnsw_min_rows', 50_000)))
    except Exception as e:  # noqa: BLE001 - the .npy is written; the database copy is a backup
        lg.warning(f'{key}: could not store the vectors in PostgreSQL ({type(e).__name__}: {e})')


def vectors_restore(features_dir, key, fingerprint, n, cfg):
    """The .npy is missing: rebuild it from pgvector (returns the memmap) instead of recomputing; None if not stored."""
    if not _vectors_on(cfg):
        return None
    try:
        from . import vectors
        if not vectors.exists(fingerprint, key):
            return None
        X = vectors.load(fingerprint, key)
    except Exception as e:  # noqa: BLE001
        lg.warning(f'{key}: could not read the vectors from PostgreSQL ({type(e).__name__}: {e})')
        return None
    if X is None or len(X) != n:
        return None
    save_cached(features_dir, key, fingerprint, X)
    lg.info(f'{key}: {n:,} vectors restored from PostgreSQL')
    return load_cached(features_dir, key, fingerprint, n)


def vectors_backfill(fingerprint, key, emb, cfg):
    """The .npy exists but PostgreSQL has no copy (computed before it was there): store it in the background."""
    if not _vectors_on(cfg) or getattr(emb, 'ndim', 0) != 2 or not emb.shape[1]:
        return
    k = (fingerprint, key)
    with _backfill_lock:
        if k in _backfilling:
            return
        _backfilling.add(k)

    def run():
        try:
            from . import vectors
            if not vectors.exists(fingerprint, key):
                vectors_store(fingerprint, key, emb, cfg)
        except Exception as e:  # noqa: BLE001
            lg.debug(f'{key}: vectors backfill skipped ({e})')
    threading.Thread(target=run, daemon=True, name=f'vectors-{key}').start()
