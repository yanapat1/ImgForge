"""
Data curation (Curation view): scan the open dataset for label / image problems (a background job),
page through the issues, fix them in the label files (backup + undo), ignore the ones that are fine.

    POST /api/curation/scan        {force, feature, model, conf, iou} -> {ready: true, summary} | {ready: false, job_id}
    GET  /api/curation/summary     counts per issue type, fixes since the index, history
    GET  /api/curation/issues      ?type=&offset=&limit=&ignored=0|1 -> {summary, total, items}
    GET  /api/curation/ids         ?types=a,b -> {images, boxes} (the page's filter "has issue")
    POST /api/curation/fix         {items: [{label|image, expect_sha1, ops}]} | {type}
                                   -> {applied, conflicts, errors: [{file, error}], undo_id, patch} (200 on partial)
    POST /api/curation/undo        {undo_id} -> {restored, conflicts, errors, patch, stale}
    POST /api/curation/ignore      {ids, ignore}
    GET  /api/curation/patch       in-memory edits since the index ({cls, deleted}), re-applied after a page reload
    GET  /api/curation/image       ?name=&max_w= one image, scaled (issue cards)

The image editor (viewer in the Explore view) goes through the same backup / undo / sha1 path:
    GET  /api/img/boxes            ?name= -> {label, sha1, writable, img_w, img_h, boxes:[{line, cls, cx, cy, w, h, sid}]}
    POST /api/edits                {image, expect_sha1, ops:[{op: set_box|add|set_class|delete, line?, cls?, cx, cy, w, h}]}
                                   -> {applied, undo_id, patch, label, sha1, boxes, status}; 409 {detail, boxes, sha1}
    GET  /api/edits/status         {pending_new, moved, images}: what a re-index would place in the plot

Every handler is a plain `def`: FastAPI runs it on its thread pool, the event loop never waits on disk.
Viewers read; fix / undo / ignore / edits need labeler. The ignore list is shared by the session
(State.update_ignored). Per-State caches (scans, thumbnails, image name sets) are kept for every loaded session,
bounded, and dropped when the session is unloaded (Ctx.close_hooks).
"""

import threading
from collections import OrderedDict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from ...auth import access, context, deps
from ...core import config as vconfig
from ...data import curation as cur
from ...data.sources import Conflict
from ...models.splits import load_splits
from ..deps import guards, owned

IGNORE_KEY = 'curation_ignored'


def router(state, ctx):
    # every route: the open session, which must have boxes
    r = APIRouter(dependencies=[Depends(deps.require_bound('viewer')), Depends(guards(state).boxes_only)])
    labeler = [Depends(deps.require_bound('labeler'))]
    need_index = guards(state).need_index
    stores = OrderedDict()          # State.tag -> Curation, one per loaded session
    lock = threading.Lock()
    thumbs = OrderedDict()          # (tag, name, max_w) -> jpeg bytes
    image_sets = OrderedDict()      # tag -> set of image names

    def keep():                     # a few more than the loaded sessions: an evicted one may come back
        return ctx.max_loaded + 2

    def forget(tag):
        with lock:
            stores.pop(tag, None)
            image_sets.pop(tag, None)
            for k in [k for k in thumbs if k[0] == tag]:
                del thumbs[k]
    ctx.close_hooks.append(forget)

    def scanned(st):
        c = stores.get(st.tag)
        return c if c is not None and c.ready else None
    ctx.curation = scanned          # the Evaluation tab reads split_leak from here

    def store():
        need_index()
        st = ctx.current()
        with lock:
            c = stores.get(st.tag)
            if c is None:
                c = stores[st.tag] = cur.Curation(st, (st.cfg or {}).get('curation'))
                while len(stores) > keep():
                    stores.popitem(last=False)
            stores.move_to_end(st.tag)
        if not c.ready and not c.scanning:
            c.load_cached()
        return c

    def ignored_ids():
        return state.ignored_ids()

    def summary_payload(c):
        return {'ready': c.ready, 'scanning': c.scanning, 'scanned_at': c.scanned_at, 'scan_s': c.scan_s,
                'summary': c.summary(ignored_ids()) if c.ready else {},
                'n_fixes': c.n_fixes(), 'writable': bool(getattr(state.source, 'label_writable', False)),
                'scores': c.scores[0] if c.scores else None,
                'model': c.model_raw[0] if c.model_raw else None,
                'types': {t: {'group': g, 'severity': s, 'label': lbl, 'bulk': b} for t, (g, s, lbl, b) in cur.TYPES.items()}}

    def split_map(st):
        """{image name: split} of the split the models were trained on (split_leak), {} when there is none."""
        try:
            if ctx.model_service is not None:
                return ctx.model_service.splits(st)[0]
            src = ((st.cfg or {}).get('models') or {}).get('split_source')
            return load_splits(st.dataset_dir, vconfig.resolve(src) if src else None)[0]
        except Exception:  # noqa: BLE001 - the other rules still run
            return {}

    @r.post('/api/curation/scan')
    def scan(body: dict | None = None):
        body = body or {}
        if body.get('force') or body.get('model'):     # replaces the scan every member sees: not for viewers
            b = context.bound()
            if b is None or not access.allows(b.role, 'labeler'):
                raise HTTPException(403, 'you can only view this session')
        c = store()
        if c.ready and not body.get('force'):
            return {**summary_payload(c), 'ready': True}
        model = None
        mid = body.get('model')
        if mid and ctx.models is not None and ctx.model_service is not None:
            try:
                model = owned(ctx.models.model, mid)
            except KeyError:
                raise HTTPException(404, f'no model {mid!r}')
        st = ctx.current()

        def run(job):
            raw = None
            if model is not None:
                job.progress(stage='scoring the model')
                try:
                    raw = cur.model_issues(ctx.model_service, st, model, body.get('conf'), body.get('iou'),
                                           c.cfg['model_min_score'], c.cfg['limit_per_type'])
                except Exception:  # noqa: BLE001 - the label rules still run without the model ones
                    raw = None
            res = c.scan(job.progress, feature=body.get('feature'), model=raw, splits=split_map(st))
            return {'n': sum(v['n'] for v in res.values())}

        job = st.submit(f'curation:{st.tag}', f'Curation scan · {st.source.name}', run, lane='io')
        return {'ready': False, 'job_id': job.id}

    @r.get('/api/curation/summary')
    def summary():
        c = store()
        return {**summary_payload(c), 'history': c.history()[:50]}

    @r.get('/api/curation/issues')
    def issues(type: str = '', offset: int = 0, limit: int = 200, ignored: int = 0):
        c = store()
        if not c.ready:
            raise HTTPException(409, 'not scanned yet')
        if type and type not in cur.TYPES:
            raise HTTPException(400, f'unknown issue type {type!r}')
        ign = ignored_ids()
        total, items = c.items(type or None, max(0, offset), max(1, min(limit, 1000)), ign, bool(ignored))
        return {'summary': c.summary(ign), 'total': total, 'offset': offset, 'items': items}

    @r.get('/api/curation/ids')
    def ids(types: str = ''):
        c = store()
        if not c.ready:
            return {'images': [], 'boxes': []}
        return c.ids_for([t for t in types.split(',') if t], ignored_ids())

    @r.post('/api/curation/fix', dependencies=labeler)
    def fix(body: dict):
        c = store()
        if not c.ready:
            raise HTTPException(409, 'not scanned yet')
        if body.get('type'):
            typ = body['type']
            if typ not in cur.TYPES:
                raise HTTPException(400, f'unknown issue type {typ!r}')
            items = c.default_fix_items(typ, ignored_ids())
            text = f'Fix all · {cur.TYPES[typ][2]}'
        else:
            items = body.get('items') or []
            text = body.get('summary') or ''
        if not items:
            return {'applied': 0, 'files': 0, 'conflicts': [], 'errors': [], 'undo_id': None,
                    'patch': {'cls': {}, 'deleted': []},
                    'summary': c.summary(ignored_ids()), 'n_fixes': c.n_fixes()}
        try:
            res = c.fix(items, text)
        except PermissionError as e:
            raise HTTPException(403, str(e))
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        except (ValueError, Conflict) as e:
            raise HTTPException(400, str(e))
        return {**res, 'summary': c.summary(ignored_ids()), 'n_fixes': c.n_fixes()}

    @r.post('/api/curation/fix-boxes', dependencies=labeler)
    def fix_boxes(body: dict):
        """Fix chosen boxes in one batch (with undo): op 'suggested' sets each to the class cleanlab suggests (label
        quality of `feature`), 'delete' removes them. Scans first when the dataset was not scanned yet."""
        st = ctx.current()
        op = body.get('op')
        if op not in ('suggested', 'delete'):
            raise HTTPException(400, "op must be 'suggested' or 'delete'")
        try:
            ids = sorted({int(i) for i in body.get('ids') or []})[:50000]
        except (TypeError, ValueError):
            raise HTTPException(400, 'ids must be box ids')
        if not st.ready:
            raise HTTPException(503, 'dataset index is still being built')
        ids = [i for i in ids if 0 <= i < len(st.samples)]
        if op == 'suggested':
            feature = body.get('feature') or st.default_feature
            lq = st.cached_label_quality(feature) if feature in st.registry else None
            if lq is None:
                raise HTTPException(409, 'run the label-quality check first (Find issues)')
            per = lq[0]
            ops = {i: {'op': 'set_class', 'cls': int(per['cl_suggested'][i])} for i in ids
                   if per['cl_scored'][i] and int(per['cl_suggested'][i]) != int(st.labels[i])}
            text = f'Change {len(ops)} box{"es" if len(ops) != 1 else ""} to cleanlab\'s class'
        else:
            ops = {i: {'op': 'delete'} for i in ids}
            text = f'Delete {len(ops)} box{"es" if len(ops) != 1 else ""}'
        c = store()
        if not c.ready:
            c.scan(None, feature=body.get('feature'), splits=split_map(st))
        items, missing = c.box_fix_items(ops)
        if not items:
            return {'applied': 0, 'files': 0, 'conflicts': [], 'errors': [], 'undo_id': None, 'missing': missing,
                    'patch': {'cls': {}, 'deleted': []}, 'summary': c.summary(ignored_ids()), 'n_fixes': c.n_fixes()}
        try:
            res = c.fix(items, text)
        except PermissionError as e:
            raise HTTPException(403, str(e))
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        except (ValueError, Conflict) as e:
            raise HTTPException(400, str(e))
        return {**res, 'missing': missing, 'summary': c.summary(ignored_ids()), 'n_fixes': c.n_fixes()}

    @r.post('/api/curation/undo', dependencies=labeler)
    def undo(body: dict):
        c = store()
        try:
            res = c.undo(str(body.get('undo_id', '')))
        except KeyError:
            raise HTTPException(404, 'nothing to undo (already undone, or pruned)')
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        except (ValueError, OSError) as e:             # an unreadable backup (per-file failures come back in errors)
            raise HTTPException(400, f'cannot undo: {e}')
        return {**res, 'summary': c.summary(ignored_ids()) if c.ready else {}, 'n_fixes': c.n_fixes()}

    @r.post('/api/curation/ignore', dependencies=labeler)
    def ignore(body: dict):
        need_index()
        ids_ = [str(i) for i in body.get('ids') or []]
        return {'ignored': ctx.current().update_ignored(ids_, bool(body.get('ignore', True)))}

    @r.get('/api/curation/patch')
    def patch():
        c = store()
        return c.state_patch()

    @r.get('/api/curation/history')
    def history():
        return {'history': store().history()}

    @r.get('/api/curation/image')
    def image(name: str, max_w: int = 360):
        need_index()
        st = ctx.current()
        max_w = max(64, min(int(max_w), 1280))
        key = (st.tag, name, max_w)
        with lock:
            if key in thumbs:
                thumbs.move_to_end(key)
                return Response(thumbs[key], media_type='image/jpeg', headers={'Cache-Control': 'max-age=600'})
        if '/' in name or '\\' in name or name not in set_of_images(st):
            raise HTTPException(404, f'no image {name!r}')
        try:
            data = cur.thumbnail(st.source, name, max_w)
        except Exception as e:  # noqa: BLE001 - unreadable file, S3 error
            raise HTTPException(502, f'cannot load {name}: {e}') from e
        with lock:
            thumbs[key] = data
            while len(thumbs) > 256:
                thumbs.popitem(last=False)
        return Response(data, media_type='image/jpeg', headers={'Cache-Control': 'max-age=600'})

    # ------------------------------------------------------------ the image editor
    def image_size(st, name):
        ids = st.by_image.get(name)
        if ids:
            s0 = st.samples[ids[0]]
            return s0['img_w'], s0['img_h']
        try:
            with st.source.open_image(name) as im:     # header only: PIL decodes lazily
                return im.size
        except Exception as e:  # noqa: BLE001 - missing file, S3 error
            raise HTTPException(502, f'cannot load {name}: {e}') from e

    def boxes_payload(c, st, name):
        out = c.image_boxes(name)
        out['img_w'], out['img_h'] = image_size(st, name)
        out['writable'] = bool(getattr(st.source, 'label_writable', False))
        return out

    @r.get('/api/img/boxes')
    def img_boxes(name: str):
        c = store()
        st = ctx.current()
        if not name or not st.has_image(name):
            raise HTTPException(404, f'no image {name!r}')
        return boxes_payload(c, st, name)

    @r.post('/api/edits', dependencies=labeler)
    def edits(body: dict):
        c = store()
        st = ctx.current()
        name = str(body.get('image') or '')
        if not name or not st.has_image(name):
            raise HTTPException(404, f'no image {name!r}')
        ops = body.get('ops') or []
        if not isinstance(ops, list) or not all(isinstance(o, dict) for o in ops):
            raise HTTPException(400, 'ops must be a list of objects')
        try:
            res = c.edit(name, body.get('expect_sha1'), ops)
        except PermissionError as e:
            raise HTTPException(403, str(e))
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        except (ValueError, Conflict) as e:
            raise HTTPException(400, str(e))
        if res['conflicts']:
            cur_boxes = boxes_payload(c, st, name)
            raise HTTPException(409, {'detail': 'the label file changed since it was opened: reloaded, your edits are '
                                                'kept to re-apply', 'boxes': cur_boxes['boxes'], 'sha1': cur_boxes['sha1']})
        if res['errors']:
            raise HTTPException(400, '; '.join(e['error'] for e in res['errors']))
        res['img_w'], res['img_h'] = image_size(st, name)
        res['writable'] = True
        res['status'] = c.edit_status()
        res['summary'] = c.summary(ignored_ids()) if c.ready else {}
        res['n_fixes'] = c.n_fixes()
        return res

    @r.get('/api/edits/status')
    def edits_status():
        return store().edit_status()

    def set_of_images(st):
        with lock:
            s = image_sets.get(st.tag)
        if s is None:
            s = set(st.source.list_images())
            with lock:
                image_sets[st.tag] = s
                while len(image_sets) > keep():
                    image_sets.popitem(last=False)
        return s

    return r
