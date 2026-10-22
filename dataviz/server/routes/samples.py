"""
Per-box endpoints: crop thumbnails, the full image with boxes drawn (small LRU), sample details,
nearest neighbours, near-duplicates, distances between picked boxes, CSV export and tags.
"""

import io
import csv
import hashlib
import threading
from collections import OrderedDict

import numpy as np
from PIL import Image, ImageDraw
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, Response

from ...data import sources
from ...data.curation import parse_label

from ...auth import deps
from ...ml import analysis
from ..deps import guards, owned, _r
from ..models import IdsRequest, DistancesRequest, TagRequest, TextSearchRequest


def draw_preds(draw, preds, palette, lw):
    """Predictions as dashed boxes with a score tag, so they read apart from the solid label boxes."""
    w = max(1, lw // 2 + 1)
    step = max(6, lw * 4)
    for (x1, y1, x2, y2), score, c, _ in preds:
        col = palette[c % len(palette)] if c >= 0 else '#ffffff'
        for x in range(int(x1), int(x2), step * 2):
            draw.line([x, y1, min(x + step, x2), y1], fill=col, width=w)
            draw.line([x, y2, min(x + step, x2), y2], fill=col, width=w)
        for y in range(int(y1), int(y2), step * 2):
            draw.line([x1, y, x1, min(y + step, y2)], fill=col, width=w)
            draw.line([x2, y, x2, min(y + step, y2)], fill=col, width=w)
        txt = f'{score:.2f}'
        tw = draw.textlength(txt)
        draw.rectangle([x1, y1 - 12, x1 + tw + 4, y1], fill=col)
        draw.text((x1 + 2, y1 - 12), txt, fill='#000000')


def open_image_rgb(state, name):
    """One dataset image as PIL (not converted: callers may draft-decode JPEGs); 502 when it cannot be read."""
    try:
        return state.source.open_image(name)
    except Exception as e:  # noqa: BLE001 - missing file, S3 error
        raise HTTPException(502, f'cannot load {name}: {e}') from e


def _label_name(state, name):
    ids = state.by_image.get(name)
    if ids:
        return state.samples[ids[0]]['label']
    return f'{name.rsplit(".", 1)[0]}.txt'


def image_boxes_px(state, name, w, h):
    """[(cls, x1, y1, x2, y2)] of one image in pixels of a w x h rendering. The index's boxes, unless the image
    editor changed this image since (then its label file, the truth)."""
    if name in state.pending_new or any(i in state.stale_ids for i in state.by_image.get(name, ())):
        src = state.source
        lbl = _label_name(state, name)
        try:
            data = src.read_label_bytes(lbl) if hasattr(src, 'read_label_bytes') else src.read_label(lbl).encode()
        except FileNotFoundError:
            data = b''
        return [(b[1], (b[2] - b[4] / 2) * w, (b[3] - b[5] / 2) * h, (b[2] + b[4] / 2) * w, (b[3] + b[5] / 2) * h)
                for b in parse_label(data.decode('utf-8', 'replace'))[0]]
    dm = state.deleted_mask()
    out = []
    for i in state.by_image.get(name, ()):
        if dm is not None and dm[i]:
            continue
        o = state.samples[i]
        sx, sy = w / o['img_w'], h / o['img_h']
        x1, y1, x2, y2 = o['bbox']
        out.append((int(state.labels[i]), x1 * sx, y1 * sy, x2 * sx, y2 * sy))
    return out


def router(state, ctx):
    r = APIRouter(dependencies=[Depends(deps.require_bound('viewer'))])     # every route: the open session
    g = guards(state)
    need_index, sample_or_404, feature_or_400, scores_or_409 = g.need_index, g.sample_or_404, g.feature_or_400, g.scores_or_409

    @r.get('/api/crop/{sid}')
    def crop(sid: int):
        s = sample_or_404(sid)
        path = state.cache_dir / 'crops' / f'{sid:06d}.jpg'
        if not path.is_file():                    # an embeddings row without an image preview
            raise HTTPException(404, f'sample {sid} has no image')
        cut = None
        if s.get('img_w') and s.get('img_h'):     # a box at the image edge: its crop is part black, send that part transparent
            from ...data.indexer import crop_outside_transparent
            with Image.open(path) as im:
                cut = crop_outside_transparent(im.convert('RGB'), s['bbox'], s['img_w'], s['img_h'],
                                               float(state.cfg['index']['crop_pad']))
        if cut is None:
            return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control': 'max-age=86400'})
        buf = io.BytesIO()
        cut.save(buf, 'WEBP', quality=90)
        return Response(buf.getvalue(), media_type='image/webp', headers={'Cache-Control': 'max-age=86400'})

    @r.get('/api/preview/{sid}/{kind}')
    def preview(sid: int, kind: str):
        """An embeddings row's own audio / video / image file (Range requests work, so media can seek), or a redirect
        to its URL. The path was checked to stay inside the session folder when indexed and is checked again here."""
        from ...data.embedding_source import AUDIO_TYPES, IMAGE_TYPES, VIDEO_TYPES, is_url
        s = sample_or_404(sid)
        types = {'audio': AUDIO_TYPES, 'video': VIDEO_TYPES, 'image': IMAGE_TYPES}.get(kind)
        ref = (s.get('preview') or {}).get(kind)
        if types is None or not ref:
            raise HTTPException(404, f'sample {sid} has no {kind} preview')
        if is_url(ref):
            if not (state.cfg.get('embeddings') or {}).get('allow_url_previews'):
                raise HTTPException(404, 'URL previews are off (config embeddings.allow_url_previews)')
            return RedirectResponse(ref)
        try:
            path = state.source.preview_path(ref)
        except (AttributeError, ValueError) as e:
            raise HTTPException(404, str(e)) from e
        if not path.is_file():
            raise HTTPException(404, f'{ref} is missing')
        return FileResponse(path, media_type=types.get(path.suffix.lower(), 'application/octet-stream'),
                            headers={'Cache-Control': 'private, max-age=3600'})

    image_cache = OrderedDict()
    image_lock = threading.Lock()

    @r.get('/api/image/{sid}')
    def full_image(sid: int, max_w: int = 1280, all_boxes: bool = True, model: str | None = None, conf: float = 0.25):
        """The image with its label boxes (this one outlined white); with `model`, its predictions dashed on top."""
        g.boxes_only()
        s = sample_or_404(sid)
        m = None
        if model and ctx.models is not None:
            try:
                m = owned(ctx.models.model, model)
            except KeyError:
                raise HTTPException(404, f'no model {model!r}')
        ckey = (state.tag, sid, max_w, all_boxes, model, round(conf, 3) if model else None)
        with image_lock:
            if ckey in image_cache:
                image_cache.move_to_end(ckey)
                return Response(image_cache[ckey], media_type='image/jpeg')
        try:
            img = sources.cached_rgb(state.source, s['image']).copy()     # shared cache: draw on a copy
        except Exception as e:  # noqa: BLE001 - missing file, S3 error
            raise HTTPException(502, f'cannot load {s["image"]}: {e}') from e
        draw = ImageDraw.Draw(img)
        lw = max(2, img.width // 500)
        others = state.by_image[s['image']] if all_boxes else [sid]
        for oid in others:
            o = state.samples[oid]
            if oid == sid:
                continue
            draw.rectangle(o['bbox'], outline=state.palette[o['cls'] % len(state.palette)], width=lw)
        x1, y1, x2, y2 = s['bbox']
        draw.rectangle([x1 - lw, y1 - lw, x2 + lw, y2 + lw], outline='white', width=lw * 3)
        draw.rectangle(s['bbox'], outline=state.palette[s['cls'] % len(state.palette)], width=lw * 2)
        if m is not None:
            draw_preds(draw, ctx.model_service.image_preds(ctx.current(), m, s['image'], conf), state.palette, lw)
        if img.width > max_w:
            img = img.resize((max_w, round(img.height * max_w / img.width)), Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=88)
        with image_lock:
            image_cache[ckey] = buf.getvalue()
            while len(image_cache) > 64:
                image_cache.popitem(last=False)
        return Response(buf.getvalue(), media_type='image/jpeg')

    # ------------------------------------------------------------ whole images (the image browser / viewer)
    def image_or_404(name):
        need_index()
        if not name or not state.has_image(name):
            raise HTTPException(404, f'no image {name!r}')
        return name

    def open_rgb(name):
        return open_image_rgb(state, name)

    def boxes_px(name, w, h):
        return image_boxes_px(state, name, w, h)

    @r.get('/api/images')
    def images():
        """Every image name, for the image browser: the page knows the ones with boxes from the points payload, so
        only the background ones (no live box) are listed by name."""
        need_index()
        names = state.image_names()
        dm = state.deleted_mask()

        def has_box(n):
            ids = state.by_image.get(n)
            return bool(ids) and (dm is None or not all(dm[i] for i in ids))
        return {'total': len(names), 'bg': [n for n in names if not has_box(n)],
                'pending': {k: v for k, v in state.pending_new.items()}}

    raw_cache = OrderedDict()

    @r.get('/api/img/raw')
    def image_raw(name: str, max_w: int = 1600):
        """The image as it is (no boxes drawn): the viewer draws the boxes itself, so they can be toggled."""
        image_or_404(name)
        max_w = max(64, min(int(max_w), 8192))
        ckey = (state.tag, name, max_w)
        with image_lock:
            if ckey in raw_cache:
                raw_cache.move_to_end(ckey)
                return Response(raw_cache[ckey], media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=300'})
        img = open_rgb(name)
        if img.format == 'JPEG':
            img.draft('RGB', (max_w, max_w * 4))          # decode at a smaller scale when that is enough
        img = img.convert('RGB')
        if img.width > max_w:
            img = img.resize((max_w, round(img.height * max_w / img.width)), Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=90)
        with image_lock:
            raw_cache[ckey] = buf.getvalue()
            while len(raw_cache) > 32:
                raw_cache.popitem(last=False)
        return Response(buf.getvalue(), media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=300'})

    @r.get('/api/img/thumb')
    def image_thumb(name: str, size: int = 220):
        """A thumbnail with the boxes drawn, cached on disk under a key of the boxes (an edit makes a new one)."""
        image_or_404(name)
        size = max(48, min(int(size), 512))
        size = 160 if size <= 160 else 256 if size <= 256 else 512    # few sizes: the slider never misses the cache
        bkey = repr(boxes_px(name, 10000, 10000))
        key = hashlib.sha1(f'{name}\0{size}\0{bkey}\0{state.palette}'.encode()).hexdigest()
        path = state.cache_dir / 'img_thumbs' / f'{key}.jpg'
        if not path.is_file():
            img = open_rgb(name)
            if img.format == 'JPEG':
                img.draft('RGB', (size, size))               # 4-10x faster on big JPEGs
            img = img.convert('RGB')
            img.thumbnail((size, size), Image.BILINEAR)
            draw = ImageDraw.Draw(img)
            lw = max(1, size // 160)
            for c, x1, y1, x2, y2 in boxes_px(name, img.width, img.height):
                draw.rectangle([x1, y1, x2, y2], outline=state.palette[c % len(state.palette)], width=lw)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f'{path.name}.{threading.get_ident()}.part')
            img.save(tmp, 'JPEG', quality=82)
            tmp.replace(path)
        return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=86400'})

    @r.get('/api/sample/{sid}')
    def sample(sid: int, feature: str | None = None):
        s = dict(sample_or_404(sid))
        key = feature_or_400(feature)
        s['same_image'] = [i for i in state.by_image[s['image']] if i != sid]
        sc = state.scores.get(key)
        if sc is not None:
            s['feature'] = key
            s['suspicion'] = float(sc['suspicion'][sid])
            s['suggested'] = int(sc['suggested'][sid])
            s['suggested_name'] = state.classes[int(sc['suggested'][sid])]
            s['outlier'] = float(sc['outlier'][sid])
        return s

    @r.get('/api/similar/{sid}')
    def similar(sid: int, k: int = 24, feature: str | None = None):
        sample_or_404(sid)
        key = feature_or_400(feature)
        sc = scores_or_409(key)
        n = len(state.samples)
        dm = state.deleted_mask()                  # boxes a curation fix removed are never neighbours
        k = max(1, min(k, n - 1 - (0 if dm is None else int(dm.sum()))))
        idx = None
        if k <= sc['nbr_idx'].shape[1]:
            idx, sim = sc['nbr_idx'][sid], sc['nbr_sim'][sid]
            if dm is not None:
                live = ~dm[idx]
                idx, sim = idx[live], sim[live]
            if len(idx) >= k:
                idx, sim = idx[:k], sim[:k]
            else:
                idx = None                         # too many neighbours removed: the exact search below
        if idx is None:
            emb = state.get_features(key)          # memory, else the workspace folder
            if emb is None:
                raise HTTPException(409, f'features {key!r} not computed yet')
            d = ((emb - emb[sid]) ** 2).sum(1)
            d[sid] = np.inf
            if dm is not None:
                d[dm] = np.inf
            idx = np.argsort(d)[:k]
            idx = idx[np.isfinite(d[idx])]
            sim = 1.0 - d[idx] / 2.0
        return {'id': [int(i) for i in idx], 'sim': _r(sim)}

    @r.post('/api/text-search')
    def text_search(req: TextSearchRequest):
        need_index()
        key = feature_or_400(req.feature)
        q = req.query.strip()
        if not q:
            raise HTTPException(400, 'empty query')
        if not state.registry[key].text_model:
            raise HTTPException(400, f'{state.registry[key].label} has no text encoder: pick an OpenCLIP / SigLIP feature')
        if not state.text_ready(key, q):        # loading the text tower or computing features: a job
            job = state.request_text_search(key, q, req.k)
            return {'ready': False, 'job_id': job.id}
        res = state.text_search(key, q, req.k)
        return {'ready': True, 'id': res['id'], 'score': _r(res['score'])}

    @r.get('/api/duplicates')
    def duplicates(threshold: float | None = None, cross_image_only: bool = True, limit: int = 500,
                   feature: str | None = None):
        key = feature_or_400(feature)
        sc = scores_or_409(key)
        acfg = state.cfg['analysis']
        if threshold is None:
            threshold = float(acfg['duplicate_threshold'])
        # a cosine threshold means nothing for unnormalized vectors: those spaces cut by rank (analysis.duplicate_cutoff)
        metric = getattr(state.registry.get(key), 'metric', 'cosine')
        quantile = float(acfg.get('duplicate_quantile', 0.005))
        cut, how = analysis.duplicate_cutoff(sc['nbr_sim'], metric, threshold, quantile)
        # boxes a curation fix removed are dropped before the limit
        pairs = analysis.near_duplicates(sc['nbr_idx'], sc['nbr_sim'], cut, limit * 4, state.deleted_mask())
        if cross_image_only:
            pairs = [p for p in pairs if state.samples[p[0]]['image'] != state.samples[p[1]]['image']]
        cutoff = {'by': how, 'sim': round(cut, 6)}
        if how == 'rank':
            cutoff.update(quantile=quantile, distance=round(float(np.sqrt(max(0.0, 2.0 * (1.0 - cut)))), 6))
        return {'pairs': [{'a': a, 'b': b, 'sim': round(s, 4)} for a, b, s in pairs[:limit]], 'cutoff': cutoff}

    @r.post('/api/export')
    def export(req: IdsRequest):
        need_index()
        key = feature_or_400(req.feature)
        sc = state.scores.get(key)
        buf = io.StringIO()
        w = csv.writer(buf)
        if state.source.kind == 'embeddings':          # the row's own id, label, previews and metadata columns
            cols = list((state.index or {}).get('meta_types') or {})
            prev = [k for k in ('image', 'audio', 'video', 'text') if any(k in state.samples[i].get('preview', {}) for i in req.ids)]
            w.writerow(['id', 'row_id', 'class_id', 'class_name', *prev, *cols, 'feature', 'suspicion', 'suggested_class',
                        'outlier'])
            for sid in req.ids:
                s = sample_or_404(sid)
                extra = [key, round(float(sc['suspicion'][sid]), 3), state.classes[int(sc['suggested'][sid])],
                         round(float(sc['outlier'][sid]), 4)] if sc else [key, '', '', '']
                w.writerow([sid, s['row_id'], s['cls'], s['cls_name'], *[s['preview'].get(k, '') for k in prev],
                            *['' if s['meta'].get(c) is None else s['meta'][c] for c in cols], *extra])
            return Response(buf.getvalue(), media_type='text/csv',
                            headers={'Content-Disposition': 'attachment; filename="selection.csv"'})
        w.writerow(['id', 'class_id', 'class_name', 'image', 'label', 'box_idx', 'x1', 'y1', 'x2', 'y2',
                    'feature', 'suspicion', 'suggested_class', 'outlier'])
        for sid in req.ids:
            s = sample_or_404(sid)
            extra = [key, round(float(sc['suspicion'][sid]), 3), state.classes[int(sc['suggested'][sid])],
                     round(float(sc['outlier'][sid]), 4)] if sc else [key, '', '', '']
            w.writerow([sid, s['cls'], s['cls_name'],
                        state.source.image_ref(s['image']), state.source.label_ref(s['label']),
                        s['box_idx'], *s['bbox'], *extra])
        return Response(buf.getvalue(), media_type='text/csv',
                        headers={'Content-Disposition': 'attachment; filename="selection.csv"'})

    @r.get('/api/tags')
    def get_tags():
        return state.load_tags()

    @r.post('/api/tags', dependencies=[Depends(deps.require_bound('labeler'))])
    def set_tags(req: TagRequest):
        tag = req.tag.strip()
        if not tag:
            raise HTTPException(400, 'empty tag')
        for sid in req.ids:
            sample_or_404(sid)
        return state.update_tag(tag, req.ids, req.remove)

    # ------------------------------------------------------------ distances between picked boxes
    @r.post('/api/distances')
    def distances(req: DistancesRequest):
        need_index()
        key = feature_or_400(req.feature)
        ids = list(dict.fromkeys(req.ids))[:12]
        for sid in ids:
            sample_or_404(sid)
        emb = state.get_features(key)
        if emb is None:
            raise HTTPException(409, f'features {key!r} not computed yet')
        e = np.asarray(emb[ids], dtype=np.float64)
        metric = state.registry[key].metric
        if metric == 'cosine':                   # rows are L2-normalized
            d = np.clip(1.0 - e @ e.T, 0.0, 2.0)
        else:
            d = np.sqrt(np.maximum(((e[:, None, :] - e[None, :, :]) ** 2).sum(-1), 0.0))
        np.fill_diagonal(d, 0.0)
        return {'ids': ids, 'feature': key, 'metric': 'cosine distance' if metric == 'cosine' else 'euclidean',
                'd': np.round(d, 4).tolist()}

    return r
