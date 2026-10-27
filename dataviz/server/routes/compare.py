"""
The Compare tab of a combined dataset (data/compare.py over data/diff.py), under /s/<id>/ like every session route:

    POST /api/compare/run       {a, b, params?} -> {ready, key, summary} (computed inline: ~1 s at 500k boxes)
    GET  /api/compare/result    ?key= -> summary + the image names of every image category
    GET  /api/compare/columns   ?key= -> binary columns status (u1), pair (i4), iou (f2) per box (server/binary.py)
    GET  /api/compare/image     ?key=&name= -> both sides of one image: boxes with status / partner
    POST /api/compare/quickmap  {ref, feature} -> {ready, key, id, x, y, info} + novel (similarity to ref) or a job
    GET  /api/union/status      the members: fingerprint the union was built from vs now (stale members)
"""

import time

import numpy as np
import orjson
from fastapi import APIRouter, Depends, HTTPException, Request

from ...auth import deps
from ...data import compare
from ..binary import columns_response, etag_for
from ..deps import guards, json_bytes as _send


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
