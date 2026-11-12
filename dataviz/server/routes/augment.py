"""
The visualizer's Augment tab (/api/augment/*): what an augmentation recipe does to this dataset's images.

    GET  /api/augment/catalog          the transforms (groups, help, what each simulates, arguments) and the presets
    POST /api/augment/validate         {recipe} -> the recipe cleaned, or 400 saying what is wrong
    GET  /api/augment/recipes          the signed-in user's saved recipes for this session
    PUT  /api/augment/recipes          {recipes: [{name, recipe}]} (at most 50)
    POST /api/augment/preview          {recipe, pick: random | per_class | names, names?, sids?, n_images, variants, seed, size}
                                       -> {items: [{name, w, h, orig, variants: [{img, boxes, dropped, applied}]}], classes}
    POST /api/augment/one              {recipe, name, seed, variant} -> image/jpeg at full size (the lightbox)
    POST /api/augment/export           {recipe, format: albumentations | python | yaml} -> the text

Images are read once each, shrunk to `size` before the recipe runs (a preview, not training data), and sent as JPEG data
URLs. Everything runs in the request (the worker thread pool): 12 images x 8 variants at 384 px take well under a second.
"""

import base64
import io
import json
import os
import random

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import Response

from ...auth import deps
from ...data import augment as ag
from ..deps import guards
from .samples import image_boxes_px, open_image_rgb

MAX_IMAGES, MAX_VARIANTS, MAX_RECIPES = 12, 8, 50


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _recipe(body):
    try:
        return ag.validate((body or {}).get('recipe') or {})
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


def _albu():
    try:
        import albumentations  # noqa: F401
    except ImportError:
        raise HTTPException(503, 'Install albumentations (pip install -r requirements/visualizer.txt) to use the Augment tab') from None


def _jpeg(arr, quality=85):
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, 'JPEG', quality=quality)
    return buf.getvalue()


def _data_url(arr):
    return 'data:image/jpeg;base64,' + base64.b64encode(_jpeg(arr)).decode()


def pick_images(st, mode, n, seed, names=None, sids=None):
    """The images to preview: the ones asked for, one per class (cycling), or a seeded random draw; never deleted ones."""
    dm = st.deleted_mask()
    live = [nm for nm, ids in st.by_image.items() if not ids or dm is None or not all(dm[i] for i in ids)]
    if sids:
        names = [st.samples[i]['image'] for i in sids if isinstance(i, int) and 0 <= i < len(st.samples)]
    if names:
        ok = set(live)
        got = list(dict.fromkeys(x for x in names if x in ok))
        if got:
            return got[:n]
    rng = random.Random(seed)
    if mode == 'per_class':
        by_cls = {}
        for nm in live:
            for i in st.by_image.get(nm, ()):
                if dm is None or not dm[i]:
                    by_cls.setdefault(int(st.labels[i]), []).append(nm)
        pools = [rng.sample(v, len(v)) for _, v in sorted(by_cls.items())]
        out, seen = [], set()
        while len(out) < n and any(pools):
            for p in pools:
                while p and p[-1] in seen:
                    p.pop()
                if p and len(out) < n:
                    seen.add(p[-1])
                    out.append(p.pop())
        if out:
            return out
    return rng.sample(live, min(n, len(live)))


def load_small(st, name, size):
    """-> (uint8 array no larger than size on its long side, boxes in its pixels, original w, h)."""
    import numpy as np
    from PIL import ImageOps
    im = ImageOps.exif_transpose(open_image_rgb(st, name)).convert('RGB')
    w0, h0 = im.size
    if size and max(w0, h0) > size:
        f = size / max(w0, h0)
        im = im.resize((max(1, round(w0 * f)), max(1, round(h0 * f))))
    w, h = im.size
    return np.asarray(im), image_boxes_px(st, name, w, h), w0, h0


def router(state, ctx):
    r = APIRouter(dependencies=[Depends(deps.require_bound('viewer'))])
    g = guards(state)

    def recipes_file(user):
        d = ctx.current().cache_dir / 'augment' / 'recipes'
        d.mkdir(parents=True, exist_ok=True)
        return d / f'{user["id"]}.json'

    @r.get('/api/augment/catalog')
    def catalog():
        _albu()
        return ag.catalog()

    @r.post('/api/augment/validate')
    def validate(body: dict = Body(...)):
        return {'recipe': _recipe(body), 'hash': ag.recipe_hash(_recipe(body))}

    @r.get('/api/augment/recipes')
    def get_recipes(user=Depends(deps.current_user)):
        f = recipes_file(user)
        try:
            return {'recipes': json.loads(f.read_text())}
        except (FileNotFoundError, ValueError):
            return {'recipes': []}

    @r.put('/api/augment/recipes')
    def put_recipes(body: dict = Body(...), user=Depends(deps.current_user)):
        items = body.get('recipes') or []
        if not isinstance(items, list) or len(items) > MAX_RECIPES:
            raise HTTPException(400, f'at most {MAX_RECIPES} saved recipes')
        out = []
        for it in items:
            name = str((it or {}).get('name') or '').strip()[:80]
            if not name:
                raise HTTPException(400, 'a saved recipe needs a name')
            out.append({'name': name, 'recipe': _recipe(it)})
        f = recipes_file(user)
        tmp = f.with_suffix('.tmp')
        tmp.write_text(json.dumps(out))
        os.replace(tmp, f)                                 # whole file or nothing
        return {'recipes': out}

    @r.post('/api/augment/preview')
    def preview(body: dict = Body(...)):
        g.need_index()
        g.boxes_only()
        _albu()
        st = ctx.current()
        recipe = _recipe(body)
        n = _clamp(body.get('n_images'), 1, MAX_IMAGES, 6)
        k = _clamp(body.get('variants'), 1, MAX_VARIANTS, 4)
        size = _clamp(body.get('size'), 160, 640, 384)
        seed = _clamp(body.get('seed'), 0, 2**31 - 1, 0)
        names = pick_images(st, body.get('pick') or 'random', n, seed, body.get('names'), body.get('sids'))
        try:
            compose = ag.build(recipe, seed)
        except (ValueError, TypeError) as e:
            raise HTTPException(400, f'this recipe cannot run: {e}') from None
        items = []
        for ii, name in enumerate(names):
            rgb, boxes, w0, h0 = load_small(st, name, size)
            h, w = rgb.shape[:2]
            variants = []
            for j in range(k):
                im, bx, dropped, applied = ag.apply(rgb, boxes, recipe, seed * 1000 + ii * 10 + j, compose=compose)
                variants.append({'img': _data_url(im), 'w': int(im.shape[1]), 'h': int(im.shape[0]),
                                 'boxes': [[c, round(a, 1), round(b, 1), round(x, 1), round(y, 1)] for c, a, b, x, y in bx],
                                 'dropped': dropped, 'applied': applied})
            items.append({'name': name, 'w': w, 'h': h, 'w0': w0, 'h0': h0,
                          'orig': {'img': _data_url(rgb), 'boxes': [[int(c), round(a, 1), round(b, 1), round(x, 1), round(y, 1)] for c, a, b, x, y in boxes]},
                          'variants': variants})
        return {'items': items, 'classes': list(st.classes), 'recipe': recipe}

    @r.post('/api/augment/one')
    def one(body: dict = Body(...)):
        g.need_index()
        _albu()
        st = ctx.current()
        name = str(body.get('name') or '')
        if name not in st.by_image:
            raise HTTPException(404, f'no image {name!r} in this session')
        recipe = _recipe(body)
        rgb, boxes, _, _ = load_small(st, name, 2048)
        im, _, _, _ = ag.apply(rgb, boxes, recipe, _clamp(body.get('seed'), 0, 2**31 - 1, 0))
        return Response(_jpeg(im, 90), media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=300'})

    @r.post('/api/augment/export')
    def export(body: dict = Body(...)):
        _albu()
        recipe, fmt = _recipe(body), body.get('format') or 'albumentations'
        if fmt == 'albumentations':
            return {'text': json.dumps(ag.to_albu_json(recipe), indent=2, default=str), 'file': 'augment.json'}
        if fmt == 'python':
            return {'text': ag.to_python(recipe), 'file': 'augment.py'}
        if fmt == 'yaml':
            return {'text': ag.to_yaml(recipe), 'file': 'augment.yaml'}
        raise HTTPException(400, 'format: albumentations, python or yaml')

    @r.post('/api/augment/import')
    def import_(body: dict = Body(...)):
        _albu()
        try:
            recipe, skipped = ag.from_albu_json(body.get('data') or {})
        except (ValueError, TypeError, AttributeError) as e:
            raise HTTPException(400, f'not an albumentations pipeline: {e}') from None
        return {'recipe': recipe, 'skipped': skipped}

    return r
