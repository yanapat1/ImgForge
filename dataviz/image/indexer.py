"""
YOLO dataset indexer for the dataset visualizer.

Turns a YOLO folder (images/, labels/, classes.txt) into one record per
bounding box ("patch"), saves a square thumbnail of each box and computes
simple per-box quality metrics (size, brightness, blur).
"""

import os
import sys
import json
import io
import math
import threading
import time
from collections import deque
from contextlib import contextmanager
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from ..core import logging as vlog
from ..paths import DATA_PREP_DIR

try:
    # inside the YOLO project: share the splitter's session rules (scripts/data_prep/split_dataset.py)
    sys.path.insert(0, str(DATA_PREP_DIR))
    from split_dataset import parse_session_and_frame  # noqa: E402
except ImportError:
    # this folder used on its own (e.g. cloned from git): same rules, local copy
    import re

    _SESSION_PATTERNS = [
        re.compile(r'^wd_(\d+)_image_(\d+)\.'),
        re.compile(r'^frame_(\d+)_([\d.]+)s\.'),
        re.compile(r'^(\d+)image_(\d+)\.'),
    ]

    def parse_session_and_frame(filename):
        """(session_id, frame_rank) for an image filename; unknown names get a per-file session."""
        name = os.path.basename(filename)
        m = _SESSION_PATTERNS[0].match(name)
        if m:
            return f'wd_{m.group(1)}', int(m.group(2))
        m = _SESSION_PATTERNS[1].match(name)
        if m:
            return 'video1', int(m.group(1))
        m = _SESSION_PATTERNS[2].match(name)
        if m:
            return f'burst_{m.group(1)}', int(m.group(2))
        return f'__singleton__{name}', 0

from . import imagehash  # noqa: E402
from .sources import IMAGE_EXTS, INDEX_VERSION, LocalSource  # noqa: E402,F401

INDEX_WORKERS = 4      # legacy default; build_index(workers=) / config index.workers ('auto') replaces it

MAX_AUTO_WORKERS = 16  # measured on 4000x3000 JPEGs: 16 processes 14.9s, 32 = 26.7s, 64 = 44.6s (memory bandwidth / shared host)


def load_class_names(dataset_dir):
    """Class names of a local dataset folder (classes.txt, else data.yaml names)."""
    return LocalSource(dataset_dir).read_classes()


def find_image(image_names, stem):
    """Image file name for a label stem (from the source's image listing), or None."""
    for ext in IMAGE_EXTS:
        for e in (ext, ext.upper()):
            if f'{stem}{e}' in image_names:
                return f'{stem}{e}'
    return None


def parse_label_file(text):
    """Parse the text of a YOLO label file.

    Returns (boxes, n_bad) where boxes is a list of (cls, cx, cy, w, h, line) (line = 0-based line index in
    the file, the sample's box_idx) and n_bad counts malformed or out-of-range lines.
    """
    boxes, bad = [], 0
    for ln, line in enumerate(text.splitlines()):
        parts = line.split()
        if not parts:
            continue
        if len(parts) < 5:
            bad += 1
            continue
        try:
            cls = int(float(parts[0]))
            cx, cy, w, h = (float(v) for v in parts[1:5])
        except ValueError:
            bad += 1
            continue
        if cls < 0 or w <= 0 or h <= 0:
            bad += 1
            continue
        boxes.append((cls, cx, cy, w, h, ln))
    return boxes, bad


def yolo_to_pixel(cx, cy, w, h, img_w, img_h):
    """Normalized YOLO xywh -> clipped integer pixel (x1, y1, x2, y2)."""
    x1 = max(0, int(round((cx - w / 2) * img_w)))
    y1 = max(0, int(round((cy - h / 2) * img_h)))
    x2 = min(img_w, int(round((cx + w / 2) * img_w)))
    y2 = min(img_h, int(round((cy + h / 2) * img_h)))
    return x1, y1, max(x2, x1 + 1), max(y2, y1 + 1)


def square_crop_box(x1, y1, x2, y2, pad=0.1):
    """Square region centred on the box with relative padding.

    The region may extend past the image; PIL fills the outside with black.
    """
    side = max(x2 - x1, y2 - y1) * (1 + 2 * pad)
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    half = side / 2
    return int(round(cx - half)), int(round(cy - half)), int(round(cx + half)), int(round(cy + half))


def crop_outside_transparent(crop, box, img_w, img_h, pad=0.1):
    """The square crop of `box` with the part that lies outside the image made transparent (RGBA), or None when the
    square is inside the image. PIL fills that part with black; transparent, the page's own (themed) background shows
    through instead. Rounded 1 px inward, so the dark JPEG fringe at the image edge goes too."""
    X1, Y1, X2, Y2 = square_crop_box(*box, pad)
    if X1 >= 0 and Y1 >= 0 and X2 <= img_w and Y2 <= img_h:
        return None
    sx, sy = crop.width / max(X2 - X1, 1), crop.height / max(Y2 - Y1, 1)
    left = math.ceil((max(0, X1) - X1) * sx) + (X1 < 0)
    top = math.ceil((max(0, Y1) - Y1) * sy) + (Y1 < 0)
    right = math.floor((min(img_w, X2) - X1) * sx) - (X2 > img_w)
    bottom = math.floor((min(img_h, Y2) - Y1) * sy) - (Y2 > img_h)
    alpha = Image.new('L', crop.size, 0)
    if right > left and bottom > top:
        alpha.paste(255, (left, top, right, bottom))
    out = crop.convert('RGBA')
    out.putalpha(alpha)
    return out


def box_quality(gray_crop):
    """(brightness, blur) for a grayscale uint8 crop. Lower blur = blurrier."""
    if gray_crop.size == 0:
        return 0.0, 0.0
    brightness = float(gray_crop.mean())
    blur = float(cv2.Laplacian(gray_crop, cv2.CV_64F).var())
    return brightness, blur


def dataset_fingerprint(dataset_dir):
    """Hash of label/image names, sizes and mtimes; changes when data changes."""
    return LocalSource(dataset_dir).fingerprint()


def _crop_boxes(img, boxes, img_name, label_name, thumb_size, pad):
    """(samples without id, crop JPEG bytes) for the boxes of one decoded RGB image."""
    img_w, img_h = img.size
    arr = np.asarray(img)
    session, _ = parse_session_and_frame(img_name)
    samples, crops = [], []
    for cls, cx, cy, w, h, box_idx in boxes:
        x1, y1, x2, y2 = yolo_to_pixel(cx, cy, w, h, img_w, img_h)
        crop = img.crop(square_crop_box(x1, y1, x2, y2, pad)).resize((thumb_size, thumb_size), Image.BICUBIC)
        buf = io.BytesIO()
        crop.save(buf, 'JPEG', quality=90)
        crops.append(buf.getvalue())
        # grayscale is per-pixel, so converting just the box gives the same values as slicing a full-image gray
        region = arr[y1:y2, x1:x2]
        gray = cv2.cvtColor(region, cv2.COLOR_RGB2GRAY) if region.size else region[:, :, 0]
        brightness, blur = box_quality(gray)
        samples.append({
            'image': img_name,
            'label': label_name,
            'box_idx': box_idx,
            'cls': cls,
            'bbox': [x1, y1, x2, y2],
            'img_w': img_w,
            'img_h': img_h,
            'area': round(w * h, 6),
            'area_px': (x2 - x1) * (y2 - y1),
            'aspect': round((x2 - x1) / max(1, y2 - y1), 4),
            'brightness': round(brightness, 2),
            'blur': round(blur, 2),
            'session': session,
            'yolo': [round(cx, 6), round(cy, 6), round(w, 6), round(h, 6)],   # the line's geometry (data/reuse.py)
        })
    return samples, crops


def _decode(im, draft):
    """(RGB image, scale to the original pixels). draft (a pixel size, 0 = off): a JPEG is decoded at 1/2..1/8 scale
    while its long side stays >= 2 x draft (the whole-image unit: its crop is draft/2 px, decoding is most of the cost)."""
    w0 = im.size[0]
    if draft and im.format == 'JPEG' and max(im.size) >= 4 * draft:
        im.draft('RGB', (max(1, im.size[0] * 2 * draft // max(im.size)), max(1, im.size[1] * 2 * draft // max(im.size))))
    img = im.convert('RGB')
    return img, w0 / img.size[0]


def _to_original(samples, scale, size0):
    """Box fields of samples cropped from a draft-decoded image, in original-image pixels."""
    if scale == 1:
        return samples
    for s in samples:
        x1, y1, x2, y2 = (int(round(v * scale)) for v in s['bbox'])
        s['bbox'] = [x1, y1, min(x2, size0[0]), min(y2, size0[1])]
        s['img_w'], s['img_h'] = size0
        s['area_px'] = (s['bbox'][2] - x1) * (s['bbox'][3] - y1)
    return samples


def _hash_meta(data, size0, sig=None):
    """images.json record (data/imagehash.py) of an image whose bytes the indexer holds anyway: ~1 ms / MB."""
    return {'q': imagehash.quick_bytes(data), 'h': imagehash.full(data), 'w': size0[0], 'hh': size0[1], 'sig': sig}


def _index_one(source, label_name, label_text, img_name, thumb_size, pad, data=None, draft=0):
    """Boxes of one image: (samples without id, crop JPEG bytes, n_bad, hash record or None). Runs on a worker
    thread.

    data: a future with the image bytes (S3 stream mode downloads ahead on its own pool; those are hashed too -
    other sources are hashed by the backfill, State.ensure_hashes)."""
    boxes, bad = parse_label_file(label_text)
    if not boxes:
        return [], [], bad, None
    meta = None
    if data is not None:
        raw = data.result()
        im = Image.open(io.BytesIO(raw))
        meta = _hash_meta(raw, im.size)
    else:
        im = source.open_image(img_name)
    size0 = im.size
    img, scale = _decode(im, draft)
    samples, crops = _crop_boxes(img, boxes, img_name, label_name, thumb_size, pad)
    return _to_original(samples, scale, size0), crops, bad, meta


def _index_one_path(path, label_name, label_text, img_name, thumb_size, pad, draft=0):
    """Same as _index_one for a local image file; module-level and picklable, runs in a worker process.
    The file is read once into memory: decoded from those bytes and hashed (+ its size:mtime sig)."""
    boxes, bad = parse_label_file(label_text)
    if not boxes:
        return [], [], bad, None
    with open(path, 'rb') as f:
        st = os.fstat(f.fileno())
        data = f.read()
    with Image.open(io.BytesIO(data)) as im:
        size0 = im.size
        meta = _hash_meta(data, size0, f'{st.st_size}:{st.st_mtime_ns}')
        img, scale = _decode(im, draft)
    del data
    samples, crops = _crop_boxes(img, boxes, img_name, label_name, thumb_size, pad)
    return _to_original(samples, scale, size0), crops, bad, meta


def _save_hashes(source, cache_dir, fingerprint, image_names, img_meta):
    """images.json after a build: the hashes computed now + still-valid ones of the previous file (same sig)."""
    old = (imagehash.load(cache_dir) or {}).get('images') or {}
    sig = getattr(source, 'image_sig', None)
    out = {}
    for name, e in old.items():
        if name in image_names and name not in img_meta:
            out[name] = e                          # re-validated by sig in the backfill (State.ensure_hashes)
    for name, e in img_meta.items():
        if e.get('sig') is None and sig is not None:
            try:
                e['sig'] = sig(name)
            except (OSError, KeyError):
                pass
        out[name] = e
    try:
        imagehash.save(cache_dir, fingerprint, out, complete=False)
    except OSError as e:
        vlog.get_logger('index').warning(f'could not write {imagehash.FILE}: {e!r}')


def resolve_workers(value='auto', kind='io'):
    """index.workers config value ('auto' or an int) -> worker count."""
    try:
        n = int(value)
        if n > 0:
            return n
    except (TypeError, ValueError):
        pass
    from ..core import hardware
    return min(hardware.workers(kind), MAX_AUTO_WORKERS)


_held = threading.local()


@contextmanager
def index_lock(cache_dir):
    """Exclusive lock on a workspace while its index is built or read for assembly (data/union.py): a build
    deletes the crops first, so two builds (or a build and a union linking the crops) must not overlap.
    flock on <ws>/.index.lock: across processes and threads; re-entrant on one thread."""
    import fcntl
    ws = Path(cache_dir)
    ws.mkdir(parents=True, exist_ok=True)
    key = str(ws.resolve())
    held = getattr(_held, 'paths', None)
    if held is None:
        held = _held.paths = set()
    if key in held:
        yield
        return
    with open(ws / '.index.lock', 'a+') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        held.add(key)
        try:
            yield
        finally:
            held.discard(key)
            fcntl.flock(f, fcntl.LOCK_UN)


def build_index(source, cache_dir, thumb_size=224, pad=0.1, rebuild=False, log=None, progress=None,
                workers='auto', donor=None):
    """Index every box (under index_lock(cache_dir); see _build_index)."""
    with index_lock(cache_dir):
        return _build_index(source, cache_dir, thumb_size, pad, rebuild, log, progress, workers, donor)


def _build_index(source, cache_dir, thumb_size=224, pad=0.1, rebuild=False, log=None, progress=None,
                 workers='auto', donor=None):
    """Index every box in the dataset and write thumbnails.

    `source` is a sources.LocalSource / S3Source (a plain folder path is accepted too).
    Returns dict with keys: classes, samples, fingerprint, stats.
    Cached in <cache_dir>/samples.json and reused while the fingerprint matches.
    Images are loaded, decoded and cropped on a thread pool (S3: downloaded in parallel);
    results are consumed in label-file order, so sample ids are the same on every run.
    progress(done, total, stage) is called while indexing (optional).
    workers: config index.workers ('auto' or int). Local image files are decoded in worker processes (no GIL);
    S3 stream mode and non-file sources use threads (network bound), as does any process-pool failure.
    Images whose bytes are read are hashed on the way (<cache_dir>/images.json, data/imagehash.py).
    donor: a data/reuse.Donor - images unchanged since an earlier index take their boxes from it (no decode).
    """
    if not hasattr(source, 'list_labels'):
        source = LocalSource(source)
    lg = vlog.get_logger('index')
    progress = progress or (lambda *a, **k: None)
    cache_dir = Path(cache_dir)
    crops_dir = cache_dir / 'crops'
    index_path = cache_dir / 'samples.json'
    progress(stage='checking dataset fingerprint')
    fingerprint = source.fingerprint()

    if not rebuild and index_path.exists():
        try:
            cached = json.loads(index_path.read_text())
        except ValueError:
            lg.warning(f'{index_path.name} is corrupt; rebuilding')
        else:
            if cached.get('fingerprint') == fingerprint:
                lg.info(f'cache hit: {len(cached["samples"]):,} boxes')
                return cached

    if callable(donor):                     # data/reuse.previous_donor: read only on a cache miss
        donor = donor()
    if donor is not None and Path(donor.ws).resolve() == cache_dir.resolve():
        donor = None                        # its crops are about to be deleted
    crops_dir.mkdir(parents=True, exist_ok=True)
    index_path.unlink(missing_ok=True)      # the old index must not outlive its crops if this build is interrupted
    (cache_dir / 'reuse.npz').unlink(missing_ok=True)
    for old in crops_dir.glob('*.jpg'):
        old.unlink()

    t0 = time.time()
    classes = source.read_classes()
    image_names = set(source.list_images())
    label_names = source.list_labels()
    lg.info(f'indexing {vlog.esc(source.uri)}: {len(label_names):,} label files, {len(image_names):,} images')
    stats = {'label_files': len(label_names), 'missing_images': 0, 'bad_lines': 0, 'bad_images': 0, 'images': 0}
    if hasattr(source, 'read_labels'):
        progress(0, len(label_names), 'reading label files')
        texts = source.read_labels(label_names)
    else:
        texts = [source.read_label(n) for n in label_names]
    jobs = []
    # a view (data/views.py) names each label's image itself and may crop without padding / from a draft decode
    image_for = getattr(source, 'image_for_label', None)
    pad = getattr(source, 'index_pad', pad)
    draft = thumb_size if getattr(source, 'index_draft', False) else 0
    image_key = getattr(source, 'image_key', lambda n: f'images/{n}')
    params = {'thumb_size': int(thumb_size), 'pad': round(float(pad), 6), 'draft': int(draft), 'version': INDEX_VERSION}
    if donor is not None and donor.params != params:
        lg.info('donor index has other params: not reused')
        donor = None
    own_hashes = ((imagehash.load(cache_dir) or {}).get('images') or {}) if donor is not None else None
    n_reused = 0
    sig_of = getattr(source, 'image_sig', None)
    sigs = {}                               # image -> image_sig of every image read: what a later donor checks
    for name, text in zip(label_names, texts):
        img = image_for(name) if image_for else find_image(image_names, Path(name).stem)
        if img not in image_names:
            img = None
        if img is None:
            stats['missing_images'] += 1
            continue
        reuse = None
        if sig_of is not None:
            try:
                sigs[img] = sig_of(img)
            except (OSError, KeyError):
                pass
        if donor is not None:
            reuse = donor.match(img, parse_label_file(text)[0], sigs.get(img), own_hashes)
            n_reused += reuse is not None
        jobs.append((name, text, img, reuse))
    if donor is not None:
        lg.info(f'reusing {n_reused:,} of {len(jobs):,} images from {vlog.esc(Path(donor.ws).name)}')

    progress(0, len(jobs), 'loading images + cropping boxes')
    samples = []
    # S3 stream mode: images are downloaded `concurrency` at a time on the source's pool, a bounded window
    # ahead of the decode threads (memory stays flat however big the bucket is)
    stream = source.kind == 's3' and source.root is None
    nworkers = resolve_workers(workers, 'proc')
    paths = None
    if not stream and jobs:
        ps = [source.image_ref(j[2]) for j in jobs[:1]]
        if ps and os.path.isfile(ps[0]):
            paths = lambda name: source.image_ref(name)  # noqa: E731
    window = 2 * int(source.s3cfg['concurrency']) if stream else 4 * nworkers
    done = {'n': 0}
    img_meta = {}
    reuse_src = []                          # donor sample id per sample (-1: decoded now): reuse.npz

    def take(j):
        """(samples, None crops, n_bad, hash record) of a job taken from the donor; None when a donor crop is gone."""
        name, text, img, sids = j
        boxes, bad = parse_label_file(text)
        session, _ = parse_session_and_frame(img)
        out = []
        for (cls, *_rest, box_idx), dsid in zip(boxes, sids):
            d = donor.samples[dsid]
            out.append({'image': img, 'label': name, 'box_idx': box_idx, 'cls': cls,
                        **{k: d[k] for k in ('bbox', 'img_w', 'img_h', 'area', 'area_px', 'aspect', 'brightness',
                                             'blur')},
                        'session': session, 'yolo': d['yolo'], '_donor': dsid})
        for s_ in out:
            if not (Path(donor.ws) / 'crops' / f'{s_["_donor"]:06d}.jpg').exists():
                return None
        return out, None, bad, donor.hash_entry(img, sigs.get(img), len(out))

    def run(make_executor, use_paths, start):
        """Index jobs[start:] with the executor; results consumed in order. Returns the next job index."""
        pending, it = deque(), iter(jobs[start:])
        pos = start

        def submit_next(ex):
            j = next(it, None)
            if j is None:
                return
            if j[3] is not None:
                pending.append((j[2], None, j))         # from the donor: handled in order, inline
            elif use_paths:
                pending.append((j[2], ex.submit(_index_one_path, paths(j[2]), j[0], j[1], j[2], thumb_size, pad,
                                                draft), j))
            else:
                data = source.pool.submit(source.get, image_key(j[2])) if stream else None
                pending.append((j[2], ex.submit(_index_one, source, j[0], j[1], j[2], thumb_size, pad, data, draft),
                                j))

        with make_executor() as ex:
            for _ in range(window):
                submit_next(ex)
            while pending:
                img_name, fut, j = pending.popleft()
                try:
                    got = take(j) if fut is None else None
                    if fut is None and got is None:      # a donor crop went missing: decode this one here
                        got = _index_one(source, j[0], j[1], j[2], thumb_size, pad, None, draft)
                    boxes, crops, bad, meta = got if got is not None else fut.result()
                except (OSError, ValueError, SyntaxError) as e:     # one unreadable / truncated image: skip it
                    stats['bad_images'] += 1
                    lg.warning(f'skipping unreadable image: {e!r}')
                    boxes, crops, bad, meta = [], [], 0, None
                if meta is not None:
                    meta['n'] = len(boxes)
                    img_meta[img_name] = meta
                submit_next(ex)
                pos += 1
                stats['bad_lines'] += bad
                if boxes:
                    stats['images'] += 1
                for k, s in enumerate(boxes):
                    sid = len(samples)
                    dsid = s.pop('_donor', None)
                    if dsid is None:
                        (crops_dir / f'{sid:06d}.jpg').write_bytes(crops[k])
                    else:
                        from .reuse import link
                        link(Path(donor.ws) / 'crops' / f'{dsid:06d}.jpg', crops_dir / f'{sid:06d}.jpg')
                    reuse_src.append(-1 if dsid is None else dsid)
                    s['id'] = sid
                    s['cls_name'] = classes[s['cls']] if s['cls'] < len(classes) else f'class_{s["cls"]}'
                    samples.append(s)
                done['n'] += 1
                n = done['n']
                if n % 10 == 0 or n == len(jobs):
                    el = time.time() - t0
                    progress(n, len(jobs), extra=f'{len(samples):,} boxes · {n / max(el, 1e-3):.0f} img/s')
        return pos

    threads = lambda: ThreadPoolExecutor(min(nworkers, 16) if not stream else INDEX_WORKERS,  # noqa: E731
                                         thread_name_prefix='index')
    if paths is not None and nworkers > 1:
        import multiprocessing as mp
        started = len(samples)
        try:
            run(lambda: ProcessPoolExecutor(nworkers, mp_context=mp.get_context('spawn')), True, 0)
        except Exception as e:                      # broken pool / pickling / spawn problem: redo with threads
            if len(samples) != started or done['n']:
                lg.warning(f'process pool failed ({e!r}); restarting with threads')
                samples.clear()
                img_meta.clear()
                reuse_src.clear()
                stats.update(bad_lines=0, bad_images=0, images=0)
                done['n'] = 0
                for old in crops_dir.glob('*.jpg'):
                    old.unlink()
            else:
                lg.warning(f'process pool failed ({e!r}); using threads')
            run(threads, False, 0)
    else:
        run(threads, False, 0)

    n_cls = max([s['cls'] for s in samples], default=-1) + 1
    while len(classes) < n_cls:
        classes.append(f'class_{len(classes)}')

    result = {'fingerprint': fingerprint, 'classes': classes, 'samples': samples, 'stats': stats, 'params': params,
              'sigs': sigs}
    if donor is not None and any(v >= 0 for v in reuse_src):
        from .reuse import save_map
        save_map(cache_dir, reuse_src, donor)       # State.get_features takes the donor's rows (reuse.py)
    tmp = index_path.with_name(f'{index_path.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(result))
    os.replace(tmp, index_path)             # atomic: a crash never leaves a truncated samples.json
    _save_hashes(source, cache_dir, fingerprint, image_names, img_meta)
    lg.info(f'built {len(samples):,} boxes from {stats["images"]:,} images in {time.time() - t0:.1f}s '
            f'(missing images: {stats["missing_images"]}, bad lines: {stats["bad_lines"]}, '
            f'bad images: {stats["bad_images"]})')
    return result
