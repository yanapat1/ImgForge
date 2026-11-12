"""
The Compare tab of a combined dataset (data/compare.py over data/diff.py), under /s/<id>/ like every session route:

    POST /api/compare/run       {a, b, params?} -> {ready, key, summary} (computed inline: ~1 s at 500k boxes)
    GET  /api/compare/result    ?key= -> summary + the image names of every image category
    GET  /api/compare/columns   ?key= -> binary columns status (u1), pair (i4), iou (f2) per box (server/binary.py)
    GET  /api/compare/image     ?key=&name= -> both sides of one image: boxes with status / partner
    POST /api/compare/quickmap  {ref, feature} -> {ready, key, id, x, y, info} + novel (similarity to ref) or a job
    POST /api/compare/shift     {a, b, sample?, feature?} -> {ready, key, report} or {ready: false, job_id}: the
                                "Image conditions" report (data/shift.py); measuring images is an io-lane job
    GET  /api/compare/shift/images   ?key=&which=outside|low|high|most_b|uncovered&metric=&side=a|b -> {names}
    GET  /api/compare/shift/lut      ?key= -> the B -> A correction LUT (JSON download, with a cv2.LUT snippet)
    GET  /api/compare/shift/preview  ?key=&name=&strength=&mode= -> image/jpeg: as_b = an A image as B's conditions
                                     would make it, correct = a B image through the LUT; X-Shift-Params = the params
    GET  /api/compare/stress/axes    the stress-test axes and levels (data/stress.py)
    POST /api/compare/stress    {member, model, n?, axes?, conf?, key? (a shift report: B's position)} -> {ready, key,
                                result, summary} or {ready: false, job_id}: a model under graded light / blur / noise /
                                colour / JPEG / resolution changes (main lane; io for API models)
    GET  /api/compare/stress/preview ?name=&axis=&level= -> image/jpeg of one image at that level
    GET  /api/union/status      the members: fingerprint the union was built from vs now (stale members)
"""

import io
import json
import time

import numpy as np
import orjson
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from ...auth import deps
from ...image import compare
from ...image import stress as stresslib
from ..binary import columns_response, etag_for
from ..deps import guards, owned
from ..deps import json_bytes as _send


def json_bytes(obj):
    return _send(orjson.dumps(obj, option=orjson.OPT_SERIALIZE_NUMPY))


def router(state, ctx):
    r = APIRouter(dependencies=[Depends(deps.require_bound('viewer'))])
    g = guards(state)
    quick = {}                    # (state tag, ref, feature, fp) -> projection dict (the newest 8)
    status_memo = {}              # state tag -> (time, answer)

    def svc():
        g.need_index()
        st = ctx.current()
        if getattr(st.source, 'kind', None) != 'union' or not st.members:
            raise HTTPException(409, 'compare needs a combined dataset (pick 2+ datasets on the home page)')
        return compare.service(st)

    def result_or_404(s, key):
        try:
            return s.by_key(key)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))

    @r.post('/api/compare/run')
    def run(body: dict):
        s = svc()
        try:
            key, res = s.run(str(body.get('a') or ''), str(body.get('b') or ''), body.get('params') or {})
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e).strip("'\""))
        return {'ready': True, 'key': key, 'summary': res.summary}

    @r.get('/api/compare/result')
    def result(key: str):
        s = svc()
        res = result_or_404(s, key)
        lists, trunc = s.image_lists(res)
        return json_bytes({'key': key, 'summary': res.summary, 'image_lists': lists, 'truncated': trunc})

    @r.get('/api/compare/columns')
    def columns(request: Request, key: str):
        s = svc()
        res = result_or_404(s, key)
        return columns_response(request, {'status': res.status, 'pair': res.pair.astype(np.int32),
                                          'iou': res.iou.astype(np.float16)},
                                meta={'key': key, 'a': res.a, 'b': res.b}, etag=etag_for('cmp', key))

    @r.get('/api/compare/tests')
    def tests(key: str, feature: str | None = None):
        """Statistical tests of the comparison `key` for the Compare panels (CompareService.tests): class mix,
        class changes, box statistics and, on a computed feature, the embedding (MMD)."""
        s = svc()
        res = result_or_404(s, key)
        st = ctx.current()
        if feature and feature not in st.registry:
            raise HTTPException(400, f'unknown feature {feature!r}')
        return json_bytes(s.tests(res, feature))

    @r.get('/api/compare/image')
    def image(key: str, name: str):
        s = svc()
        res = result_or_404(s, key)
        try:
            return s.image_detail(res, name)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))

    @r.post('/api/compare/quickmap')
    def quickmap(body: dict):
        s = svc()
        st = ctx.current()
        ref, feature = str(body.get('ref') or ''), str(body.get('feature') or st.default_feature)
        if feature not in st.registry:
            raise HTTPException(400, f'unknown feature {feature!r}')
        k = (st.tag, ref, feature, st.fingerprint)
        hit = quick.get(k)
        if hit is None and (feature in st.features or st.feature_cached(feature)):
            try:
                hit = s.quick_map(ref, feature)
            except (KeyError, ValueError) as e:
                raise HTTPException(400, str(e).strip("'\""))
            quick[k] = hit
            while len(quick) > 8:
                quick.pop(next(iter(quick)))
        if hit is None:                                      # features of the union not assembled yet: a job
            job = st.submit(f'quickmap:{st.tag}:{ref}:{feature}', f'Shared map · {ref}',
                            lambda job: s.quick_map(ref, feature, job) and None)
            return {'ready': False, 'job_id': job.id}
        return json_bytes({'ready': True, 'key': f'quick|{feature}|{ref}', 'id': hit['id'],
                           'x': np.round(hit['xy'][:, 0], 4), 'y': np.round(hit['xy'][:, 1], 4),
                           'info': hit['info'], 'novel': np.round(hit['novel_sim'], 3)})

    def predicted_models(st):
        """[(model, evaluate.Data, conf)] of the caller's models that already predicted this dataset, + a signature."""
        reg, ms = getattr(ctx, 'models', None), getattr(ctx, 'model_service', None)
        if reg is None or ms is None or getattr(st, 'unit', 'box') == 'image':
            return [], ''
        out, sig = [], []
        try:
            models = [m for m in owned(reg.models) if m.get('family') != 'hf']
        except Exception:              # noqa: BLE001 - no database / no user: the report without models
            return [], ''
        for m in models:
            try:
                got = ms.data(st, m) if ms.is_predicted(st, m) else None
            except Exception:          # noqa: BLE001 - a broken cache: skip that model
                got = None
            if got is None:
                continue
            conf = float(m.get('conf') or ms.cfg.get('default_conf', 0.25))
            out.append((m, got[1], conf))
            sig.append(f'{m["id"]}:{ms.fingerprint(m)}:{conf}')
        return out, '|'.join(sig)

    @r.post('/api/compare/shift')
    def shift(body: dict):
        s = svc()
        st = ctx.current()
        a, b = str(body.get('a') or ''), str(body.get('b') or '')
        try:
            sample = max(0, int(body.get('sample', compare.SHIFT_SAMPLE) or 0))
            s.member_index(a), s.member_index(b)
        except (KeyError, ValueError, TypeError) as e:
            raise HTTPException(400, str(e).strip("'\""))
        if a == b:
            raise HTTPException(400, 'pick two different members to compare')
        feature = str(body.get('feature') or '') or None
        if feature is not None and feature not in st.registry:
            feature = None
        models, msig = predicted_models(st)
        key = s.shift_key(a, b, sample, feature, msig)
        hit = s.shift_cached(key)
        if hit is not None:
            return json_bytes({'ready': True, 'key': key, 'report': hit['report']})
        job = st.submit(f'shift:{key}', f'Image conditions · {a} vs {b}',
                        lambda job: s.shift(a, b, sample, feature, models, key, job) and None, lane='io')
        return {'ready': False, 'job_id': job.id, 'key': key}

    @r.get('/api/compare/shift/images')
    def shift_images(key: str, which: str, metric: str = '', side: str = 'b'):
        s = svc()
        try:
            entry = s.shift_by_key(key)
            names = s.shift_images(entry, which, metric or None, 'a' if side == 'a' else 'b')
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))
        return {'key': key, 'names': names}

    @r.get('/api/compare/shift/preview')
    def shift_preview(key: str, name: str, strength: float = 1.0, mode: str = 'as_b'):
        s = svc()
        try:
            entry = s.shift_by_key(key)
            data, prm = s.shift_preview(entry, name, strength, mode='correct' if mode == 'correct' else 'as_b')
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))
        except (OSError, ValueError) as e:
            raise HTTPException(400, f'cannot read {name}: {e}')
        return Response(data, media_type='image/jpeg', headers={'X-Shift-Params': json.dumps(prm),
                                                                'Cache-Control': 'private, max-age=600'})

    def _model(mid):
        reg, ms = getattr(ctx, 'models', None), getattr(ctx, 'model_service', None)
        if reg is None or ms is None:
            raise HTTPException(409, 'models are not available on this server')
        m = next((x for x in owned(reg.models) if x['id'] == mid and x.get('family') != 'hf'), None)
        if m is None:
            raise HTTPException(404, f'no detection model {mid!r}')
        return m, ms

    @r.get('/api/compare/stress/axes')
    def stress_axes():
        return {'axes': stresslib.axes_info(), 'sample': stresslib.SAMPLE, 'max': stresslib.SAMPLE_MAX}

    @r.post('/api/compare/stress', dependencies=[Depends(deps.require_bound('labeler')), Depends(g.boxes_only)])
    def stress(body: dict):
        s = svc()
        st = ctx.current()
        member = str(body.get('member') or '')
        try:
            s.member_index(member)
            n = int(body.get('n') or stresslib.SAMPLE)
            conf = float(body['conf']) if body.get('conf') is not None else None
        except (KeyError, ValueError, TypeError) as e:
            raise HTTPException(400, str(e).strip("'\""))
        m, ms = _model(str(body.get('model') or ''))
        conf = conf if conf is not None else float(m.get('conf') or ms.cfg.get('default_conf', 0.25))
        fp = ms.fingerprint(m)
        axes = [str(a) for a in (body.get('axes') or [])]
        key, names, labelled, axes = compare._stress_plan(s, member, m, fp, n, axes, conf)
        report = None
        if body.get('key'):
            hit = s.shift_cached(str(body['key']))
            report = hit['report'] if hit else None
        res = compare.stress_cached(s, key)
        if res is not None:
            return json_bytes({'ready': True, 'key': key, 'result': res, 'summary': stresslib.summarise(res, report)})
        job = st.submit(f'stress:{key}', f'Stress test · {m["name"]} on {member}',
                        lambda job: compare.stress_run(s, key, member, m, fp, ms.workdir, names, labelled, axes, conf,
                                                       job) and None,
                        lane='io' if m.get('format') == 'api' else 'main')
        return {'ready': False, 'job_id': job.id, 'key': key}

    @r.get('/api/compare/stress/preview')
    def stress_preview(name: str, axis: str, level: float):
        st = ctx.current()
        if axis not in stresslib.AXES:
            raise HTTPException(400, f'unknown axis {axis!r}')
        try:
            from PIL import Image, ImageOps
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(st.source.read_image_bytes(name)))).convert('RGB')
            im.thumbnail((stresslib.BASE, stresslib.BASE), Image.BILINEAR)
            out = stresslib.apply(np.asarray(im), axis, level)
        except (OSError, ValueError, KeyError) as e:
            raise HTTPException(400, f'cannot read {name}: {e}')
        buf = io.BytesIO()
        Image.fromarray(out).save(buf, 'JPEG', quality=88)
        return Response(buf.getvalue(), media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=600'})

    @r.get('/api/compare/shift/lut')
    def shift_lut(key: str):
        s = svc()
        try:
            entry = s.shift_by_key(key)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\""))
        cor = entry['report'].get('correction')
        if not cor:
            raise HTTPException(400, 'this report has no correction (no colour curves on one side)')
        body = {'from': entry['b'], 'to': entry['a'], 'channels': 'RGB', 'lut': cor['lut'], 'residual': cor['residual'],
                'usage': ['import cv2, json, numpy as np',
                          "lut = np.array(json.load(open('correction-lut.json'))['lut'], np.uint8).T.reshape(256, 1, 3)",
                          'rgb = cv2.LUT(rgb, lut)  # an RGB uint8 image of B, before the model']}
        return Response(json.dumps(body), media_type='application/json', headers={
            'Content-Disposition': f'attachment; filename="correction-lut-{entry["b"]}-to-{entry["a"]}.json"'})

    @r.get('/api/union/status')
    def union_status():
        g.need_index()
        st = ctx.current()
        if getattr(st.source, 'kind', None) != 'union':
            return {'union': False}
        hit = status_memo.get(st.tag)
        if hit is not None and time.time() - hit[0] < 30:
            return hit[1]
        now = st.source.member_fingerprints()
        members = [{'alias': m['alias'], 'name': m['name'], 'sid': m['sid'], 'n': m['n'],
                    'fp_used': m['fp'], 'fp_now': now.get(m['alias']),
                    'stale': now.get(m['alias']) != m['fp']} for m in st.members]
        out = {'union': True, 'stale': any(m['stale'] for m in members), 'members': members}
        status_memo[st.tag] = (time.time(), out)
        return out

    return r
