"""
The crops of an image-prep variant (ml/prep.py, the Map's Preprocess › Image): one JPEG per box in
<workspace>/crops~<tag>/<id>.jpg, written once per index version (done.json holds the fingerprint), then read by the
feature extractor like the index's own crops/.

Each crop is the region `context` asks for around the box, made square by `fit`, with the `tone` and `mask` applied.
It is cut from the index's crop (crops/, a square of side max(w, h) * (1 + 2 * crop_pad) around the box) when the
region lies inside that square (box, +5 %, the default +10 %), so no image is read again; a wider context (+25 %,
+50 %, the whole image) decodes each source image once (grouped by image, a thread pool) — for a streamed S3
dataset that reads every image again. `tta` hflip also writes the mirror images (crops~<tag>/f/), whose features
are averaged with the others.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from ..ml import prep as mprep
from . import indexer, sources

GREY = (114, 114, 114)              # the letterbox / mask fill (ultralytics' pad colour)
WORKERS = min(16, (os.cpu_count() or 4) * 2)


def _region(s, context, fit):
    """(x1, y1, x2, y2) in image pixels of the region the variant crops for sample `s` (may extend past the image)."""
    x1, y1, x2, y2 = s['bbox']
    pad = mprep.CONTEXT_PAD[context]
    if pad is None:                                   # the whole image
        x1, y1, x2, y2 = 0, 0, s['img_w'], s['img_h']
        pad = 0.0
    w, h = (x2 - x1) * (1 + 2 * pad), (y2 - y1) * (1 + 2 * pad)
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    if fit == 'square':
        w = h = max(w, h)
    elif fit == 'center':
        w = h = min(w, h)
    return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2


def _from_crop(region, s, crop_pad):
    """The region in the index crop's pixel units (fractions 0..1 of its side), or None when it leaves the crop."""
    X1, Y1, X2, Y2 = indexer.square_crop_box(*s['bbox'], crop_pad)
    side = max(X2 - X1, 1)
    r = ((region[0] - X1) / side, (region[1] - Y1) / side, (region[2] - X1) / side, (region[3] - Y1) / side)
    return r if r[0] >= -1e-6 and r[1] >= -1e-6 and r[2] <= 1 + 1e-6 and r[3] <= 1 + 1e-6 else None


def _square(im, fit, size):
    """A crop of the region -> size x size: letterbox pads (grey), the others resize (the region is square already,
    except stretch)."""
    if fit == 'letterbox':
        return ImageOps.pad(im, (size, size), Image.BICUBIC, color=GREY)
    return im.resize((size, size), Image.BICUBIC)


def _tone(im, tone):
    if tone == 'gray':
        return im.convert('L').convert('RGB')
    if tone == 'clahe':
        import cv2
        lab = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2LAB)
        lab[:, :, 0] = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(lab[:, :, 0])
        return Image.fromarray(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB))
    return im


def _polygon(text, box_idx, img_w, img_h):
    """Pixel polygon of line `box_idx` of a YOLO segmentation label text, or None (a box line, no such line)."""
    lines = (text or '').splitlines()
    if box_idx >= len(lines):
        return None
    parts = lines[box_idx].split()
    if len(parts) < 7 or len(parts) % 2 == 0:
        return None
    try:
        v = [float(p) for p in parts[1:]]
    except ValueError:
        return None
    return [(v[i] * img_w, v[i + 1] * img_h) for i in range(0, len(v), 2)]


def _mask(im, poly, region, fit, size):
    """Grey outside the polygon (image pixels), drawn in the output square's coordinates."""
    rx1, ry1, rx2, ry2 = region
    rw, rh = max(rx2 - rx1, 1e-6), max(ry2 - ry1, 1e-6)
    if fit == 'letterbox':
        k = size / max(rw, rh)
        ox, oy = (size - rw * k) / 2, (size - rh * k) / 2
        pts = [((x - rx1) * k + ox, (y - ry1) * k + oy) for x, y in poly]
    else:
        pts = [((x - rx1) * size / rw, (y - ry1) * size / rh) for x, y in poly]
    m = Image.new('L', im.size, 0)
    ImageDraw.Draw(m).polygon(pts, fill=255)
    return Image.composite(im, Image.new('RGB', im.size, GREY), m)


def has_polygons(source, samples, n=50):
    """Whether the dataset's labels are segmentation polygons (a sample of label files is read)."""
    seen = set()
    for s in samples:
        if s['label'] in seen:
            continue
        seen.add(s['label'])
        try:
            if _polygon(source.read_label(s['label']), s['box_idx'], 1, 1):
                return True
        except Exception:  # noqa: BLE001 - an unreadable label file is no polygon
            pass
        if len(seen) >= n:
            break
    return False


def build(state, image_part, progress=None, cancelled=None):
    """Write (or reuse) the crops of image prep `image_part` for every sample of `state` -> (paths, flip paths or
    None). Raises ValueError when the source cannot give what it needs (no polygons for a mask)."""
    progress = progress or (lambda *a, **k: None)
    samples, size = state.samples, int(state.cfg['index']['thumb_size'])
    crop_pad = float(state.cfg['index']['crop_pad'])
    t = mprep.tag(image_part)
    out = Path(state.cache_dir) / f'crops~{t}'
    flip = out / 'f' if image_part.get('tta') else None
    names = [f'{s["id"]:06d}.jpg' for s in samples]
    paths = [out / n for n in names]
    flips = [flip / n for n in names] if flip else None
    done = out / 'done.json'
    try:
        if json.loads(done.read_text()) == {'fingerprint': state.fingerprint, 'n': len(samples), 'spec': image_part}:
            return paths, flips
    except (OSError, ValueError):
        pass
    out.mkdir(parents=True, exist_ok=True)
    if flip:
        flip.mkdir(exist_ok=True)
    context, fit = image_part.get('context', 'square10'), image_part.get('fit', 'square')
    tone, mask = image_part.get('tone', 'none'), image_part.get('mask')
    own = Path(state.cache_dir) / 'crops'
    labels = {}
    if mask:
        progress(stage='reading the polygons')
        for s in samples:
            if s['label'] not in labels:
                try:
                    labels[s['label']] = state.source.read_label(s['label'])
                except Exception:  # noqa: BLE001 - its boxes keep their background
                    labels[s['label']] = ''

    def one(i, region, im):
        s = samples[i]
        im = _square(im, fit, size)
        im = _tone(im, tone)
        if mask:
            poly = _polygon(labels.get(s['label'], ''), s['box_idx'], s['img_w'], s['img_h'])
            if poly:
                im = _mask(im, poly, region, fit, size)
        im.save(paths[i], 'JPEG', quality=90)
        if flips:
            ImageOps.mirror(im).save(flips[i], 'JPEG', quality=90)

    # boxes whose region lies in their index crop: cut from it; the others: from the decoded source image
    from_crop, from_image = [], {}
    for i, s in enumerate(samples):
        region = _region(s, context, fit)
        frac = _from_crop(region, s, crop_pad) if context != 'image' else None
        if frac is not None and (own / names[i]).exists():
            from_crop.append((i, region, frac))
        else:
            from_image.setdefault(s['image'], []).append((i, region))
    total, n_done = len(samples), [0]

    def tick():
        n_done[0] += 1
        if n_done[0] % 200 == 0 or n_done[0] == total:
            progress(n_done[0], total, 'preprocessing the crops')

    def cut(item):
        i, region, frac = item
        with Image.open(own / names[i]) as c:
            c = c.convert('RGB')
            w, h = c.size
            one(i, region, c.crop((round(frac[0] * w), round(frac[1] * h), round(frac[2] * w), round(frac[3] * h))))
        tick()

    def decode(item):
        name, rows = item
        img = sources.cached_rgb(state.source, name)
        for i, region in rows:
            one(i, region, img.crop(tuple(int(round(v)) for v in region)))
            tick()

    progress(0, total, 'preprocessing the crops')
    with ThreadPoolExecutor(WORKERS, thread_name_prefix='prep') as ex:
        for _ in ex.map(cut, from_crop):
            if cancelled and cancelled():
                raise InterruptedError('stopped')
        for _ in ex.map(decode, from_image.items()):
            if cancelled and cancelled():
                raise InterruptedError('stopped')
    done.write_text(json.dumps({'fingerprint': state.fingerprint, 'n': len(samples), 'spec': image_part}))
    return paths, flips
