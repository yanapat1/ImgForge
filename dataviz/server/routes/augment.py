"""
The visualizer's Augment tab (/api/augment/*): what an augmentation recipe does to this dataset's images.

    GET  /api/augment/catalog          the transforms (groups, help, what each simulates, arguments) and the presets
    POST /api/augment/validate         {recipe} -> the recipe cleaned, or 400 saying what is wrong
    GET  /api/augment/recipes          the signed-in user's saved recipes for this session
    PUT  /api/augment/recipes          {recipes: [{name, recipe}]} (at most 50)
    POST /api/augment/preview          {recipe, pick: random | per_class | names, names?, sids?, n_images, variants, seed, size}
                                       -> {items: [{name, w, h, orig, variants: [{img, boxes, dropped, applied}]}], classes}
    POST /api/augment/one              {recipe, name, seed, variant} -> image/jpeg at full size; with json: true and size
                                       -> {w, h, orig: {img, boxes}, img, iw, ih, boxes, dropped, applied} (the compare viewer)
    POST /api/augment/export           {recipe, format: albumentations | python | yaml} -> the text
    POST /api/augment/boxes            {recipe, m, k, seed, min_px, train_size} -> {ready, result} | {ready: false, job_id, key}
    GET  /api/augment/boxes/result     ?key= -> the box check (lost / tiny / at the edge, per class and per step)
    GET  /api/augment/targets          the image sets uploaded to compare against (this session)
    POST /api/augment/targets          multipart files (+ name): a new target set -> {id, name, n}
    DELETE /api/augment/targets/{id}
    POST /api/augment/autotune         {recipe (start), target, quality, m, seed} -> a job: steps added while they raise the
                                        target's coverage -> {recipe, history, before, after} (GET .../coverage/result?key=)
    POST /api/augment/model            {recipe, model, n, conf, seed} -> a job: the model's mAP50 / mAP / P / R with each step at
                                        full chance, every step together and the whole recipe (rows 'all', 'recipe'), against the labels the steps moved
    POST /api/augment/ghost            {recipe, feature, n, k, seed} -> a job: augmented box crops with their 10 nearest session
                                        boxes + weights in that feature (the page places them on the current map)
    POST /api/augment/curve            {recipe, target, quality, m, seed} -> a job: coverage + unreal variants at strengths
                                        0.25 … 2 -> {points, best} (GET .../coverage/result?key=)
    POST /api/augment/coverage         {recipe, target: {kind: upload, id} | {kind: dc, bucket, path} | {kind: session, id}, quality,
                                        m, k, seed} -> {ready, result} | {ready: false, job_id, key}  (data/augment_run.py)
    GET  /api/augment/coverage/result  ?key= | latest=1 -> the coverage report
    GET  /api/augment/thumb            ?part=&i= -> one 224 px thumbnail of a coverage part (the page's galleries)

Images are read once each, shrunk to `size` before the recipe runs (a preview, not training data), and sent as JPEG data
URLs. Everything runs in the request (the worker thread pool): 12 images x 8 variants at 384 px take well under a second.
"""

import base64
import io
import json
import os
import random
import re
import shutil
import time
import uuid

import numpy as np
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from ...auth import deps
from ...data import augment as ag
from ..deps import guards, owned
from .samples import image_boxes_px, open_image_rgb

MAX_IMAGES, MAX_VARIANTS, MAX_RECIPES = 12, 8, 50
MAX_TARGET_FILES, MAX_FILE_MB, MAX_DC_FILES = 1000, 50, 2000
TARGET_RE = re.compile(r'^[0-9a-f]{16}$')
MEM = 8                                            # results kept in memory (also saved under <cache>/augment/checks)


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


def _bx(boxes):
    """Boxes [(cls, x1, y1, x2, y2)] as JSON rows, pixels rounded to 0.1."""
    return [[int(c), round(a, 1), round(b, 1), round(x, 1), round(y, 1)] for c, a, b, x, y in boxes]


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

    mem = {}                                       # key -> result (the newest MEM)

    def check_file(st, key):
        d = st.cache_dir / 'augment' / 'checks'
        d.mkdir(parents=True, exist_ok=True)
        return d / f'{key.replace(":", "_")}.json'

    def remember(key, value):
        mem[key] = value
        while len(mem) > MEM:
            mem.pop(next(iter(mem)))

    def cached(st, key):
        if key in mem:
            return mem[key]
        try:
            v = json.loads(check_file(st, key).read_text())
        except (FileNotFoundError, ValueError):
            return None
        remember(key, v)
        return v

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
                                 'boxes': _bx(bx),
                                 'dropped': dropped, 'applied': applied})
            items.append({'name': name, 'w': w, 'h': h, 'w0': w0, 'h0': h0,
                          'orig': {'img': _data_url(rgb), 'boxes': _bx(boxes)},
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
        seed = _clamp(body.get('seed'), 0, 2**31 - 1, 0)
        if body.get('json'):                          # the compare viewer: both sides with their boxes, at one size
            rgb, boxes, _, _ = load_small(st, name, _clamp(body.get('size'), 256, 2048, 1600))
            im, bx, dropped, applied = ag.apply(rgb, boxes, recipe, seed)
            return {'w': int(rgb.shape[1]), 'h': int(rgb.shape[0]), 'orig': {'img': _data_url(rgb), 'boxes': _bx(boxes)},
                    'img': _data_url(im), 'iw': int(im.shape[1]), 'ih': int(im.shape[0]), 'boxes': _bx(bx), 'dropped': dropped, 'applied': applied}
        rgb, boxes, _, _ = load_small(st, name, 2048)
        im, _, _, _ = ag.apply(rgb, boxes, recipe, seed)
        return Response(_jpeg(im, 90), media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=300'})

    @r.post('/api/augment/boxes')
    def boxes(body: dict = Body(...)):
        g.need_index()
        g.boxes_only()
        _albu()
        st = ctx.current()
        recipe = _recipe(body)
        m = _clamp(body.get('m'), 10, 500, 200)
        k = _clamp(body.get('k'), 1, 4, 2)
        seed = _clamp(body.get('seed'), 0, 2**31 - 1, 0)
        min_px = _clamp(body.get('min_px'), 2, 64, 8)
        size = _clamp(body.get('train_size'), 160, 2048, 640)
        key = f'box:{str(st.fingerprint or "")[:12]}:{ag.recipe_hash(recipe)}:{m}:{k}:{seed}:{min_px}:{size}'
        hit = cached(st, key)
        if hit is not None:
            return {'ready': True, 'key': key, 'result': hit}
        names = pick_images(st, 'per_class', m, seed)

        def run(job):
            res = ag.box_check(lambda nm: load_small(st, nm, size)[:2], names, recipe, k, seed, min_px, size, list(st.classes),
                               progress=job.progress, cancelled=job.cancelled.is_set)
            check_file(st, key).write_text(json.dumps(res))
            remember(key, res)
        job = st.submit(f'augbox:{key}', 'Augment · box check', run, lane='io')
        return {'ready': False, 'job_id': job.id, 'key': key}

    @r.get('/api/augment/boxes/result')
    def boxes_result(key: str):
        hit = cached(ctx.current(), key)
        if hit is None:
            raise HTTPException(404, 'no such box check (run it again)')
        return {'key': key, 'result': hit}

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
        if fmt == 'ultralytics':
            return {'text': ag.to_ultralytics(recipe), 'file': 'train_augmented.py'}
        raise HTTPException(400, 'format: albumentations, python, yaml or ultralytics')

    @r.post('/api/augment/import')
    def import_(body: dict = Body(...)):
        _albu()
        try:
            recipe, skipped = ag.from_albu_json(body.get('data') or {})
        except (ValueError, TypeError, AttributeError) as e:
            raise HTTPException(400, f'not an albumentations pipeline: {e}') from None
        return {'recipe': recipe, 'skipped': skipped}

    @r.post('/api/augment/copy')
    def make_copy(body: dict = Body(...), user=Depends(deps.current_user)):
        """A new managed Data Center bucket holding k augmented variants of every image (+ the originals), YOLO labels."""
        g.need_index()
        g.boxes_only()
        _albu()
        from ...data import augment_run as ar
        from ...datacenter import service as dc
        from ...datacenter.store import DCError
        st = ctx.current()
        recipe = _recipe(body)
        if not any(s['on'] for s in recipe['steps']):
            raise HTTPException(400, 'the recipe has no step to apply')
        k = _clamp(body.get('k'), 1, 10, 2)
        originals = bool(body.get('originals', True))
        names = pick_images(st, 'random', len(st.by_image), 0)
        names.sort()
        name = str(body.get('name') or '').strip()[:80] or 'augmented copy'
        try:
            b = dc.create_bucket(user, name, kind='managed')
        except DCError as e:
            raise HTTPException(e.status, e.message) from None

        def run(job):
            res = ar.make_copy(names, lambda nm: load_small(st, nm, 0)[:2], recipe, k, originals, b['root'], list(st.classes),
                               progress=job.progress, cancelled=job.cancelled.is_set)
            job.progress(len(names), len(names), f'{res["images"]} images written')
        job = st.submit(f'augcopy:{b["id"]}', f'Augment · copy to “{b["name"]}”', run, lane='io')
        return {'job_id': job.id, 'bucket': {'id': b['id'], 'name': b['name']}, 'images': len(names) * (k + int(originals))}

    # ------------------------------------------------------------ coverage: targets, the job, its report
    def aug_root(st):
        d = st.cache_dir / 'augment'
        d.mkdir(parents=True, exist_ok=True)
        return d

    def targets_dir(st):
        d = aug_root(st) / 'targets'
        d.mkdir(exist_ok=True)
        return d

    def target_meta(st, tid):
        if not TARGET_RE.match(tid or ''):
            raise HTTPException(404, 'no such target set')
        try:
            return json.loads((targets_dir(st) / tid / 'meta.json').read_text())
        except (FileNotFoundError, ValueError):
            raise HTTPException(404, 'no such target set') from None

    @r.get('/api/augment/targets')
    def targets():
        st = ctx.current()
        out = []
        for d in sorted(targets_dir(st).iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                out.append(json.loads((d / 'meta.json').read_text()))
            except (FileNotFoundError, ValueError, NotADirectoryError):
                continue
        return {'targets': out}

    @r.post('/api/augment/targets')
    def upload_targets(files: list[UploadFile] = File(...), name: str = Form('')):
        st = ctx.current()
        if not files or len(files) > MAX_TARGET_FILES:
            raise HTTPException(400, f'give 1 to {MAX_TARGET_FILES} images')
        tid = uuid.uuid4().hex[:16]
        d = targets_dir(st) / tid
        (d / 'images').mkdir(parents=True)
        kept, skipped = 0, []
        try:
            for i, f in enumerate(files):
                base = os.path.basename(f.filename or f'image{i}')
                if not base.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp', '.webp', '.tif', '.tiff')):
                    skipped.append(base)
                    continue
                dest, size = d / 'images' / f'{i:05d}_{base}', 0
                with open(dest, 'wb') as out:
                    while chunk := f.file.read(1 << 20):
                        size += len(chunk)
                        if size > MAX_FILE_MB << 20:
                            break
                        out.write(chunk)
                if size > MAX_FILE_MB << 20:
                    dest.unlink(missing_ok=True)
                    skipped.append(base)
                    continue
                kept += 1
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise
        if not kept:
            shutil.rmtree(d, ignore_errors=True)
            raise HTTPException(400, 'none of these files is an image')
        meta = {'id': tid, 'name': (name or f'{kept} images').strip()[:80], 'n': kept, 'skipped': skipped[:50],
                'created': time.time()}
        (d / 'meta.json').write_text(json.dumps(meta))
        return meta

    @r.delete('/api/augment/targets/{tid}')
    def delete_target(tid: str):
        st = ctx.current()
        target_meta(st, tid)
        shutil.rmtree(targets_dir(st) / tid, ignore_errors=True)
        return {'ok': True}

    def target_files(st, tgt, user):
        """-> (cache key part, [images], label, read) of a target spec; read(image) -> PIL, None = local files."""
        from ...data import augment_run as ar
        kind = (tgt or {}).get('kind')
        if kind == 'upload':
            m = target_meta(st, str(tgt.get('id') or ''))
            files = sorted((targets_dir(st) / m['id'] / 'images').iterdir())
            return ar.h16('upload', m['id'], len(files)), files, m['name'], None
        if kind == 'dc':
            from ...datacenter import service as dc
            from ...datacenter.store import DCError
            try:
                files = dc.image_files(user, str(tgt.get('bucket') or ''), str(tgt.get('path') or ''), MAX_DC_FILES)
            except DCError as e:
                raise HTTPException(e.status, e.message) from None
            if not files:
                raise HTTPException(400, 'that folder holds no images')
            sig = []
            for f in files:
                try:
                    stt = os.stat(f)
                    sig.append(f'{f}:{stt.st_size}:{int(stt.st_mtime)}')
                except OSError:
                    continue
            return ar.h16('dc', *sig), files, f'{tgt.get("path") or "/"} ({len(files)} images)', None
        if kind == 'session':
            other = str(tgt.get('id') or '')
            seen = {d['id']: d for d in ctx.datasets.datasets(user)}
            if other not in seen:
                raise HTTPException(404, 'no such session (or you cannot open it)')
            if other == getattr(st, 'dataset_id', None):
                raise HTTPException(400, 'pick another session than this one')
            try:
                src = ctx.datasets.source(ctx.datasets.dataset(other))
                names = list(src.list_images())
            except Exception as e:  # noqa: BLE001 - its folder or bucket cannot be read now
                raise HTTPException(409, f'that session cannot be read: {e}') from None
            if not names:
                raise HTTPException(400, 'that session has no images')
            step = max(1, len(names) // MAX_DC_FILES)          # evenly spread when it is bigger than the cap
            pick = names[::step][:MAX_DC_FILES]
            return ar.h16('session', other, *pick), pick, f'session “{seen[other].get("name")}” ({len(pick)} images)', src.open_image
        raise HTTPException(400, 'target: {kind: upload, id}, {kind: dc, bucket, path} or {kind: session, id}')

    def feature_of(st, quality):
        if quality != 'deep':
            return 'quick'
        reg = st.registry
        key = st.default_feature
        if key in reg and reg[key].group == 'deep':
            return key
        deep = [k for k in reg if reg[k].group == 'deep']
        if not deep:
            raise HTTPException(400, 'no deep feature is installed here: use Quick')
        return deep[0]

    @r.post('/api/augment/coverage')
    def coverage(body: dict = Body(...), user=Depends(deps.current_user)):
        g.need_index()
        g.boxes_only()
        _albu()
        from ...data import augment_run as ar
        st = ctx.current()
        recipe = _recipe(body)
        m = _clamp(body.get('m'), 20, 1000, 300)
        k = _clamp(body.get('k'), 1, 4, 2)
        seed = _clamp(body.get('seed'), 0, 2**31 - 1, 0)
        feature = feature_of(st, body.get('quality') or 'quick')
        tkey, files, label, read = target_files(st, body.get('target'), user)
        fp = str(st.fingerprint or '')
        trk = ar.h16(fp, m, seed)
        auk = ar.h16(trk, ag.recipe_hash(recipe), k)
        key = f'cov:{ar.h16(trk, auk, tkey, feature, seed)}'
        hit = cached(st, key)
        if hit is not None:
            return {'ready': True, 'key': key, 'result': hit}
        names = pick_images(st, 'random', m, seed)
        root = aug_root(st) / 'emb'

        def run(job):
            def load(nm):
                rgb, boxes, _, _ = load_small(st, nm, 448)
                return rgb, boxes
            job.progress(0, 4, 'this dataset\'s images')
            trd, trm = ar.ensure_train(root, trk, names, load)
            job.progress(1, 4, 'augmenting them')
            aud, aum = ar.ensure_aug(root, auk, names, load, recipe, k, seed, cancelled=job.cancelled.is_set)
            job.progress(2, 4, 'the target images')
            tgd, tgm = ar.ensure_target(root, tkey, files[:MAX_DC_FILES], read)
            if tgm['n'] < 1:
                raise ValueError('none of the target images could be read')
            vecs = []
            for i, (d, mm) in enumerate(((trd, trm), (aud, aum), (tgd, tgm))):
                if job.cancelled.is_set():
                    return
                vecs.append(ar.embed(d, mm['n'], feature, st.registry,
                                     lambda done, total=None, stage=None, *a, _i=i: job.progress(done, total, f'embedding {("dataset", "augmented", "target")[_i]} images')))
            rep = ar.report(*vecs, trm, aum, tgm, seed=seed)
            rep.update(key=key, feature=feature, target_label=label, parts={'train': trd.name, 'aug': aud.name, 'target': tgd.name},
                       recipe=recipe, aug_src=aum.get('src') or [])
            check_file(st, key).write_text(json.dumps(rep))
            (aug_root(st) / 'checks' / 'latest_coverage.json').write_text(json.dumps({'key': key}))
            remember(key, rep)
        job = st.submit(f'augcov:{key}', 'Augment · coverage', run, lane='main')
        return {'ready': False, 'job_id': job.id, 'key': key}

    @r.post('/api/augment/model', dependencies=[Depends(deps.require_bound('labeler'))])
    def model_check(body: dict = Body(...)):
        """A detection model on each recipe step at full chance, scored against the moved labels (a job)."""
        g.need_index()
        g.boxes_only()
        _albu()
        from ...data import augment_model as am
        from ...data import augment_run as ar
        st = ctx.current()
        reg, ms = getattr(ctx, 'models', None), getattr(ctx, 'model_service', None)
        if reg is None or ms is None:
            raise HTTPException(409, 'models are not available on this server')
        mid = str(body.get('model') or '')
        m = next((x for x in owned(reg.models) if x['id'] == mid and x.get('family') != 'hf'), None)
        if m is None:
            raise HTTPException(404, f'no detection model {mid!r}')
        recipe = _recipe(body)
        if not any(x['on'] for x in recipe['steps']):
            raise HTTPException(400, 'the recipe has no step to apply')
        n = _clamp(body.get('n'), 10, 300, 100)
        seed = _clamp(body.get('seed'), 0, 2**31 - 1, 0)
        try:
            conf = float(body['conf']) if body.get('conf') is not None else float(m.get('conf') or ms.cfg.get('default_conf', 0.25))
        except (TypeError, ValueError):
            raise HTTPException(400, 'conf: a number') from None
        fp = ms.fingerprint(m)
        key = f'model:{ar.h16('v2', str(st.fingerprint or ""), m["id"], fp, ag.recipe_hash(recipe), n, seed, round(conf, 3))}'
        hit = cached(st, key)
        if hit is not None:
            return {'ready': True, 'key': key, 'result': hit}
        names = pick_images(st, 'per_class', n, seed)

        def run(job):
            from ...models import predict, runners, schema
            job.progress(stage=f'loading {m["name"]}')
            runner = runners.get_runner(m, fp, ms.workdir)
            target = int(getattr(runner, 'draft_size', None) or getattr(runner, 'imgsz', None) or 640)
            model_names = list(runner.names or [])

            def pred(imgs):
                out = []
                for im, d in zip(imgs, predict.predict_batch(m, fp, ms.workdir, imgs), strict=False):
                    d = schema.clean(d, *im.size)
                    out.append(d[d[:, 4] >= runners.LOW_CONF])
                return out
            res = am.run(lambda nm: load_small(st, nm, target)[:2], names, recipe, pred, model_names, list(st.classes), conf,
                         predict.pick_batch_size(runner, m, None), job.progress, job.cancelled.is_set, seed)
            res.update(key=key, model={'id': m['id'], 'name': m['name']}, recipe_used=recipe)   # not 'recipe': that is a row
            check_file(st, key).write_text(json.dumps(res))
            remember(key, res)
        job = st.submit(f'augmodel:{key}', f'Augment · {m["name"]} under the recipe', run, lane='io' if m.get('format') == 'api' else 'main')
        return {'ready': False, 'job_id': job.id, 'key': key}

    @r.post('/api/augment/ghost')
    def ghost(body: dict = Body(...)):
        """The recipe's augmented boxes placed on this session's map: crops embedded with the map's feature, each with
        its nearest session boxes and weights (the page puts it at their weighted position on the current map). A job."""
        g.need_index()
        g.boxes_only()
        _albu()
        from ...data import augment_run as ar
        st = ctx.current()
        recipe = _recipe(body)
        feature = str(body.get('feature') or '')
        if feature not in st.registry:
            raise HTTPException(400, f'unknown feature {feature!r}')
        if st.registry[feature].group != 'deep' and feature != 'color-hist' and feature != 'hog':
            raise HTTPException(400, 'this map is made from box geometry: augmented boxes cannot be placed on it')
        if not st.feature_cached(feature):
            raise HTTPException(409, 'generate the map with this feature first')
        n = _clamp(body.get('n'), 10, ar.GHOST_IMAGES, 60)
        k = _clamp(body.get('k'), 1, ar.GHOST_K, 1)
        seed = _clamp(body.get('seed'), 0, 2**31 - 1, 0)
        key = f'ghost:{ar.h16(str(st.fingerprint or ""), feature, ag.recipe_hash(recipe), n, k, seed)}'
        hit = cached(st, key)
        if hit is not None:
            return {'ready': True, 'key': key, 'result': hit}
        names = pick_images(st, 'per_class', n, seed)

        def run(job):
            def load(nm):
                rgb, boxes, _, _ = load_small(st, nm, 640)
                return rgb, boxes
            job.progress(0, 3, 'augmenting the boxes')
            d, m = ar.ghost_crops(aug_root(st) / 'emb', key.split(':')[1], names, load, recipe, k, seed, job.cancelled.is_set)
            if not m['n']:
                raise ValueError('the recipe left no box to place')
            job.progress(1, 3, 'embedding them')
            q = ar.embed(d, m['n'], feature, st.registry, lambda *a: None)
            job.progress(2, 3, 'placing them on the map')
            ids, w, sim = ar.place(np.asarray(st.get_features(feature), np.float32), q)
            res = {'key': key, 'feature': feature, 'n': m['n'], 'part': d.name, 'cls': m['cls'], 'image': m['image'],
                   'applied': m['applied'], 'nbr': ids.tolist(), 'w': np.round(w, 4).tolist(), 'sim': np.round(sim, 4).tolist()}
            check_file(st, key).write_text(json.dumps(res))
            remember(key, res)
        job = st.submit(f'augghost:{key}', 'Augment · boxes on the map', run, lane='main')
        return {'ready': False, 'job_id': job.id, 'key': key}

    @r.post('/api/augment/curve')
    def curve(body: dict = Body(...), user=Depends(deps.current_user)):
        """The recipe at strengths 0.25 … 2: the target's coverage and the unreal variants at each (a job)."""
        g.need_index()
        g.boxes_only()
        _albu()
        from ...data import augment_run as ar
        st = ctx.current()
        recipe = _recipe(body)
        if not any(x['on'] for x in recipe['steps']):
            raise HTTPException(400, 'the recipe has no step to apply')
        m = _clamp(body.get('m'), 20, 1000, 300)
        seed = _clamp(body.get('seed'), 0, 2**31 - 1, 0)
        feature = feature_of(st, body.get('quality') or 'quick')
        tkey, files, _, read = target_files(st, body.get('target'), user)
        trk = ar.h16(str(st.fingerprint or ''), m, seed)
        key = f'curve:{ar.h16(trk, ag.recipe_hash({**recipe, "strength": 1}), tkey, feature, seed)}'
        hit = cached(st, key)
        if hit is not None:
            return {'ready': True, 'key': key, 'result': hit}
        names = pick_images(st, 'random', m, seed)
        root = aug_root(st) / 'emb'

        def run(job):
            def load(nm):
                rgb, boxes, _, _ = load_small(st, nm, 448)
                return rgb, boxes
            trd, trm = ar.ensure_train(root, trk, names, load)
            tgd, tgm = ar.ensure_target(root, tkey, files[:MAX_DC_FILES], read)
            trv = ar.embed(trd, trm['n'], feature, st.registry, lambda *a: None)
            tgv = ar.embed(tgd, tgm['n'], feature, st.registry, lambda *a: None)
            trial = ar.trial_fn(root, trk, names, load, trv, tgv, feature, st.registry, seed, job.cancelled.is_set)
            res = ar.strength_curve(trial, recipe, job.progress)
            res['key'] = key
            check_file(st, key).write_text(json.dumps(res))
            remember(key, res)
        job = st.submit(f'augcurve:{key}', 'Augment · strength curve', run, lane='main')
        return {'ready': False, 'job_id': job.id, 'key': key}

    @r.post('/api/augment/autotune')
    def autotune(body: dict = Body(...), user=Depends(deps.current_user)):
        """Find a recipe for the target images: steps added one by one while they raise its coverage (a job)."""
        g.need_index()
        g.boxes_only()
        _albu()
        from ...data import augment_run as ar
        st = ctx.current()
        base = _recipe(body)
        m = _clamp(body.get('m'), 20, 1000, 300)
        seed = _clamp(body.get('seed'), 0, 2**31 - 1, 0)
        feature = feature_of(st, body.get('quality') or 'quick')
        tkey, files, label, read = target_files(st, body.get('target'), user)
        trk = ar.h16(str(st.fingerprint or ''), m, seed)
        key = f'tune:{ar.h16(trk, ag.recipe_hash(base), tkey, feature, seed)}'
        hit = cached(st, key)
        if hit is not None:
            return {'ready': True, 'key': key, 'result': hit}
        names = pick_images(st, 'random', m, seed)
        root = aug_root(st) / 'emb'

        def run(job):
            def load(nm):
                rgb, boxes, _, _ = load_small(st, nm, 448)
                return rgb, boxes
            job.progress(0, 1, 'this dataset\'s and the target images')
            trd, trm = ar.ensure_train(root, trk, names, load)
            tgd, tgm = ar.ensure_target(root, tkey, files[:MAX_DC_FILES], read)
            if tgm['n'] < 1:
                raise ValueError('none of the target images could be read')
            trv = ar.embed(trd, trm['n'], feature, st.registry, lambda *a: None)
            tgv = ar.embed(tgd, tgm['n'], feature, st.registry, lambda *a: None)
            res = ar.autotune(root, trk, names, load, base, trv, tgv, feature, st.registry, seed, job.progress,
                              job.cancelled.is_set, trm.get('stats'), tgm.get('stats'))
            res.update(key=key, target_label=label, feature=feature)
            check_file(st, key).write_text(json.dumps(res))
            remember(key, res)
        job = st.submit(f'augtune:{key}', 'Augment · find a recipe', run, lane='main')
        return {'ready': False, 'job_id': job.id, 'key': key}

    @r.get('/api/augment/coverage/result')
    def coverage_result(key: str = '', latest: int = 0):
        st = ctx.current()
        if latest:
            try:
                key = json.loads((aug_root(st) / 'checks' / 'latest_coverage.json').read_text())['key']
            except (FileNotFoundError, ValueError, KeyError):
                raise HTTPException(404, 'no coverage run yet') from None
        hit = cached(st, key)
        if hit is None:
            raise HTTPException(404, 'no such coverage run (run it again)')
        return {'key': key, 'result': hit}

    @r.get('/api/augment/thumb')
    def thumb(part: str, i: int):
        from ...data import augment_run as ar
        if not ar.PART_RE.match(part or '') or i < 0:
            raise HTTPException(404, 'no such image')
        f = aug_root(ctx.current()) / 'emb' / part / 'imgs' / f'{int(i):05d}.jpg'
        if not f.exists():
            raise HTTPException(404, 'no such image')
        return FileResponse(f, media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=3600'})

    return r
