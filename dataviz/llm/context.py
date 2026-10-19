"""
What the assistant is shown: compact text tables and one-image mosaics instead of raw JSON and many images.

dataset_summary   name, counts, per-class table (count / images / median area / aspect), quality quantiles, curation
                  issue counts, tags                                   (class table cached per state + boxes)
eval_summary      leaderboard + findings of the Evaluation tab (models/insights via routes/models.eval_overview)
selection_mosaic  up to 24 box crops in one numbered grid JPEG + a legend (#k -> box id, class, image)
image_mosaic      up to 8 whole images in one numbered grid (auto-tag)
class_exemplars   contact sheets, one row per class (<= 16 rows a sheet), k crops each, captioned: from the open
                  dataset, or from a labeling task's own boxes (cached per task + annotation count)
prepare_image     a label-page image for the model: <= 1024 px long side, JPEG
"""

import io
import math
import threading
from collections import OrderedDict

import numpy as np
from PIL import Image, ImageDraw

from ..core import logging as vlog

_log = vlog.get_logger('llm')
_cache = OrderedDict()
_cache_lock = threading.Lock()
CACHE_N = 32


def _cached(key, build):
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    val = build()
    with _cache_lock:
        _cache[key] = val
        while len(_cache) > CACHE_N:
            _cache.popitem(last=False)
    return val


def clear_cache():
    with _cache_lock:
        _cache.clear()


def _jpeg(img, quality=85):
    buf = io.BytesIO()
    img.convert('RGB').save(buf, 'JPEG', quality=quality)
    return buf.getvalue()


def _q(a, qs=(10, 50, 90)):
    return '/'.join(f'{v:.1f}' for v in np.percentile(a, qs)) if len(a) else '-'


# ---------------------------------------------------------------- dataset
def _live_ids(state):
    dm = state.deleted_mask()
    n = len(state.samples)
    return np.arange(n) if dm is None else np.flatnonzero(~dm)


def _class_table(state):
    ids = _live_ids(state)
    s = state.samples
    labels = np.asarray(state.labels)[ids] if len(ids) else np.zeros(0, np.int64)
    area = np.array([s[i]['area'] for i in ids], dtype=float) * 100
    aspect = np.array([s[i]['aspect'] for i in ids], dtype=float)
    imgs = np.array([s[i]['image'] for i in ids], dtype=object)
    bright = np.array([s[i].get('brightness', 0) for i in ids], dtype=float)
    blur = np.array([s[i].get('blur', 0) for i in ids], dtype=float)
    lines = ['| id | class | boxes | images | median area % | median w/h |', '|---|---|---|---|---|---|']
    for c, name in enumerate(state.classes):
        m = labels == c
        n = int(m.sum())
        if not n:
            lines.append(f'| {c} | {name} | 0 | 0 | - | - |')
            continue
        lines.append(f'| {c} | {name} | {n} | {len(set(imgs[m]))} | {np.median(area[m]):.2f} | {np.median(aspect[m]):.2f} |')
    extra = [f'box brightness p10/p50/p90 (0-255): {_q(bright)}',
             f'box blur (laplacian variance, low = blurry) p10/p50/p90: {_q(blur)}',
             f'box area % of image p10/p50/p90: {_q(area)}']
    return len(ids), len({x for x in imgs}), '\n'.join(lines), '\n'.join(extra)


def dataset_summary(state, curation=None):
    """A ~1-3k token text summary of the open dataset."""
    n_live = len(state.samples) - (0 if state.deleted_mask() is None else int(state.deleted_mask().sum()))
    key = ('ds', id(state), state.tag, len(state.samples), n_live)
    n_boxes, n_img_boxes, table, extra = _cached(key, lambda: _class_table(state))
    try:
        n_images = len(state.image_names())
    except Exception:  # noqa: BLE001 - a remote listing failing must not break the chat
        n_images = len(state.by_image)
    name = getattr(state.source, 'name', '') or 'dataset'
    out = [f'Dataset "{name}": {n_images} images ({max(0, n_images - n_img_boxes)} without boxes), {n_boxes} boxes, '
           f'{len(state.classes)} classes.', '', 'Classes:', table, '', extra]
    stats = (state.index or {}).get('stats') or {}
    if stats:
        out.append('index: ' + ', '.join(f'{k.replace("_", " ")} {v}' for k, v in stats.items()))
    if curation is not None:
        try:
            summ = curation.summary()
        except Exception:  # noqa: BLE001
            summ = {}
        if summ:
            out += ['', 'Curation scan (issues found):']
            out += [f'- {v["label"]} ({k}, {v["severity"]}): {v["n"]}' for k, v in summ.items()]
    tags = _tags(state)
    if tags:
        top = sorted(tags.items(), key=lambda kv: -len(kv[1]))[:20]
        out += ['', 'Tags (boxes): ' + ', '.join(f'{k} {len(v)}' for k, v in top)]
    return '\n'.join(out)


def _tags(state):
    try:
        return state.load_tags()
    except Exception:  # noqa: BLE001 - no workspace yet
        return {}


def eval_summary(ctx):
    """The Evaluation tab as short text, or None when there is nothing to evaluate (no models / predictions)."""
    if getattr(ctx, 'models', None) is None or getattr(ctx, 'model_service', None) is None:
        return None
    try:
        from ..server.routes.models import eval_overview
        ov = eval_overview(ctx)
    except Exception as e:  # noqa: BLE001 - optional context: the chat works without it
        _log.debug(f'eval summary: {vlog.esc(e)}')
        return None
    lb = ov.get('leaderboard') or {}
    rows = lb.get('rows') or []
    if not rows:
        return None
    f = lambda v: '-' if v is None else f'{v:.3f}' if isinstance(v, float) else str(v)  # noqa: E731
    out = [f'Models evaluated on the {lb.get("split")} split (each at its best-F1 confidence):',
           '| model | mAP50 | mAP50-95 | P | R | F1 | conf | ms/img | missed | wrong class | FP background |',
           '|---|---|---|---|---|---|---|---|---|---|---|']
    for r in rows:
        out.append(f'| {r["name"]} | {f(r["map50"])} | {f(r["map"])} | {f(r["p"])} | {f(r["r"])} | {f(r["f1"])} | '
                   f'{f(r["conf"])} | {f(r["ms"])} | {r["missed"]} | {r["wrong_cls"]} | {r["fp_bg"]} |')
    pc = lb.get('per_class') or {}
    best = lb.get('best')
    if best in (pc.get('models') or []):
        ap = pc['ap50'][pc['models'].index(best)]
        name = next((r['name'] for r in rows if r['id'] == best), best)
        out.append(f'Per-class AP50 of {name}: ' + ', '.join(
            f'{c} {f(a)}' for c, a in zip(pc.get('classes') or [], ap)))
    finds = ov.get('findings') or []
    if finds:
        out += ['', 'Findings:'] + [f'- [{x.get("severity")}] {x.get("title")}: {x.get("detail")}' for x in finds[:15]]
    return '\n'.join(out)


# ---------------------------------------------------------------- mosaics
def _label(draw, x, y, text, size=12):
    tw = draw.textlength(text)
    draw.rectangle([x, y, x + tw + 6, y + size + 4], fill='#000000')
    draw.text((x + 3, y + 2), text, fill='#ffffff')


def _crop(state, sid, cell):
    p = state.cache_dir / 'crops' / f'{sid:06d}.jpg' if state.cache_dir is not None else None
    try:
        if p is not None and p.is_file():
            return Image.open(p).convert('RGB').resize((cell, cell), Image.BILINEAR)
    except OSError:
        pass
    s = state.samples[sid]
    from ..data import sources
    img = sources.cached_rgb(state.source, s['image'])      # shared and read-only: crop() makes a new image
    x1, y1, x2, y2 = s['bbox']
    side = max(x2 - x1, y2 - y1) * 1.2
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    return img.crop((int(cx - side / 2), int(cy - side / 2), int(cx + side / 2), int(cy + side / 2))).resize(
        (cell, cell), Image.BILINEAR)


def sample_even(ids, n):
    ids = list(ids)
    if len(ids) <= n:
        return ids
    return [ids[int(round(i))] for i in np.linspace(0, len(ids) - 1, n)]


def _grid(tiles, cell, cols=None):
    n = len(tiles)
    cols = cols or max(1, math.ceil(math.sqrt(n)))
    rows = math.ceil(n / cols)
    sheet = Image.new('RGB', (cols * cell + (cols - 1) * 4, rows * cell + (rows - 1) * 4), (40, 40, 40))
    draw = ImageDraw.Draw(sheet)
    for k, t in enumerate(tiles):
        x, y = (k % cols) * (cell + 4), (k // cols) * (cell + 4)
        sheet.paste(t, (x, y))
        _label(draw, x, y, f'#{k + 1}')
    return sheet


def selection_mosaic(state, ids, max_n=24, cell=112):
    """(jpeg, legend text, the ids shown) of box crops in one numbered grid; an even sample of a big selection."""
    ids = [int(i) for i in ids if 0 <= int(i) < len(state.samples)]
    shown = sample_even(ids, max_n)
    if not shown:
        return None, '', []
    tiles = [_crop(state, i, cell) for i in shown]
    legend = [f'Box crops ({len(shown)} shown' + (f' of {len(ids)} selected' if len(ids) > len(shown) else '') + '):']
    for k, i in enumerate(shown):
        s = state.samples[i]
        c = int(state.labels[i])
        w, h = s['bbox'][2] - s['bbox'][0], s['bbox'][3] - s['bbox'][1]
        legend.append(f'#{k + 1} → box {i}, class "{state.classes[c] if 0 <= c < len(state.classes) else c}", '
                      f'image {s["image"]}, {w}x{h} px')
    return _jpeg(_grid(tiles, cell)), '\n'.join(legend), shown


def image_mosaic(state, names, cell=256):
    """(jpeg, names) of whole images (labels not drawn), letterboxed in numbered cells."""
    from ..server.routes.samples import open_image_rgb
    tiles = []
    for n in names:
        try:
            img = open_image_rgb(state, n)
            if img.format == 'JPEG':
                img.draft('RGB', (cell, cell))
            img = img.convert('RGB')
        except Exception:  # noqa: BLE001 - a broken image is a grey cell
            img = Image.new('RGB', (cell, cell), (90, 90, 90))
        img.thumbnail((cell, cell), Image.BILINEAR)
        t = Image.new('RGB', (cell, cell), (0, 0, 0))
        t.paste(img, ((cell - img.width) // 2, (cell - img.height) // 2))
        tiles.append(t)
    return _jpeg(_grid(tiles, cell, cols=min(4, len(tiles)))), list(names)


def image_with_boxes(state, name, max_side=1024):
    """(jpeg, (w, h)) of a dataset image with its label boxes drawn, numbered, <= max_side px."""
    from ..server.routes.samples import image_boxes_px
    from ..data import sources
    img = sources.cached_rgb(state.source, name).copy()     # the cache is shared: thumbnail / draw on a copy
    img.thumbnail((max_side, max_side), Image.BILINEAR)
    draw = ImageDraw.Draw(img)
    lw = max(2, img.width // 400)
    for k, (c, x1, y1, x2, y2) in enumerate(image_boxes_px(state, name, img.width, img.height)):
        draw.rectangle([x1, y1, x2, y2], outline=state.palette[c % len(state.palette)], width=lw)
        _label(draw, x1, max(0, y1 - 16), str(k + 1))
    return _jpeg(img), img.size


def prepare_image(data, max_side=1024):
    """(jpeg, (w, h)) of an image's bytes scaled to <= max_side px long side (the coordinates the model sees)."""
    img = Image.open(io.BytesIO(data))
    if img.format == 'JPEG':
        img.draft('RGB', (max_side, max_side))
    img = img.convert('RGB')
    img.thumbnail((max_side, max_side), Image.BILINEAR)
    return _jpeg(img, 88), img.size


# ---------------------------------------------------------------- class exemplars
def _sheets(rows, k, cell):
    """rows: [(class name, [PIL crops])] -> [jpeg] contact sheets of <= 16 rows: name column + k crops."""
    name_w = 150
    out = []
    for i in range(0, len(rows), 16):
        chunk = rows[i:i + 16]
        sheet = Image.new('RGB', (name_w + k * (cell + 4), len(chunk) * (cell + 4)), (32, 32, 32))
        draw = ImageDraw.Draw(sheet)
        for r, (name, crops) in enumerate(chunk):
            y = r * (cell + 4)
            draw.text((6, y + cell // 2 - 6), str(name)[:22], fill='#ffffff')
            if not crops:
                draw.text((name_w + 6, y + cell // 2 - 6), '(no example yet)', fill='#aaaaaa')
            for j, c in enumerate(crops[:k]):
                sheet.paste(c, (name_w + j * (cell + 4), y))
        out.append(_jpeg(sheet))
    return out


def _square(img, x1, y1, x2, y2, cell):
    side = max(x2 - x1, y2 - y1) * 1.15
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    return img.crop((int(cx - side / 2), int(cy - side / 2), int(math.ceil(cx + side / 2)),
                     int(math.ceil(cy + side / 2)))).resize((cell, cell), Image.BILINEAR)


def state_exemplars(state, k=3, cell=96):
    """[jpeg] contact sheets of the open dataset's classes (k crops each: the largest boxes, from distinct images)."""
    def build():
        ids = _live_ids(state)
        rows = []
        for c, name in enumerate(state.classes):
            mine = [int(i) for i in ids if int(state.labels[i]) == c]
            mine.sort(key=lambda i: -state.samples[i]['area_px'])
            pick, seen = [], set()
            for i in mine:
                if state.samples[i]['image'] not in seen:
                    pick.append(i)
                    seen.add(state.samples[i]['image'])
                if len(pick) == k:
                    break
            crops = []
            for i in pick:
                try:
                    crops.append(_crop(state, i, cell))
                except Exception:  # noqa: BLE001 - a missing image just leaves a gap
                    pass
            rows.append((name, crops))
        return _sheets(rows, k, cell)
    return _cached(('ex', id(state), state.tag, len(state.samples), k, cell), build)


def task_exemplars(pid, st, task, image_bytes, k=3, cell=96):
    """[jpeg] contact sheets of a labeling task's classes, from its own boxes (manual ones first, distinct
    images). image_bytes(media_row) -> bytes. Cached per (project, task, annotation count + last change)."""
    sig = st.box_signature(task['id'])
    names = tuple((x['id'], x['name']) for x in task['classes'])
    key = ('task', pid, task['id'], tuple(sig), names, k, cell)

    def build():
        rows = []
        for cid, name in names:
            got = st.class_boxes(task['id'], cid, 200)      # manual boxes first, biggest first
            pick, seen = [], set()
            for mid, g in got:
                if mid not in seen:
                    pick.append((mid, g))
                    seen.add(mid)
                if len(pick) == k:
                    break
            crops = []
            for mid, g in pick:
                try:
                    m = st.media_row(mid)
                    img = Image.open(io.BytesIO(image_bytes(m)))
                    img = img.convert('RGB')
                    W, H = img.size
                    crops.append(_square(img, g['x'] * W, g['y'] * H, (g['x'] + g['w']) * W, (g['y'] + g['h']) * H, cell))
                except Exception as e:  # noqa: BLE001 - a missing image just leaves a gap
                    _log.debug(f'exemplar {vlog.esc(mid)}: {vlog.esc(e)}')
            rows.append((name, crops))
        return _sheets(rows, k, cell)
    return _cached(key, build)
