"""
The embedding plot's data: k-NN scores per feature, 2D projections (computed as background jobs,
answered with {ready: false, job_id} until done), density groups (bubbles), and the saved-views list.
"""

import numpy as np
from fastapi import APIRouter, Depends, HTTPException

from ...auth import deps
from ...ml import analysis
from ...state import normalize_params, projection_key
from ..deps import guards, json_bytes
from ..models import ProjectionRequest, GroupsRequest


def router(state, ctx):
    r = APIRouter(dependencies=[Depends(deps.require_bound('viewer'))])     # every route: the open session
    g = guards(state)
    need_index, feature_or_400, scores_or_409 = g.need_index, g.feature_or_400, g.scores_or_409

    def view_label(e):
        m = analysis.METHODS.get(e['method'], {'label': e['method'], 'params': {}})
        spec = state.registry.get(e['feature'])
        parts = [m['label'], spec.label if spec else e['feature']]
        cls = e.get('classes')
        if cls:
            names = [state.classes[c] if c < len(state.classes) else str(c) for c in cls]
            parts.append(' vs '.join(names) if len(names) <= 2 else f'{len(names)} classes')
        else:
            parts.append('all classes')
        changed = {k: v for k, v in e['params'].items() if m['params'].get(k) != v}
        if changed:
            parts.append(', '.join(f'{k}={v}' for k, v in changed.items()))
        return ' · '.join(parts)

    @r.get('/api/projections')
    def saved_views():
        need_index()
        entries = [e for e in state.saved_projections().values() if e['feature'] in state.registry]
        entries.sort(key=lambda e: e.get('last_used') or e.get('created') or '', reverse=True)
        return {'views': [{**e, 'label': view_label(e)} for e in entries]}

    @r.get('/api/scores')
    def scores(feature: str | None = None):
        key = feature_or_400(feature)
        need_index()
        try:
            sc = scores_or_409(key)
        except HTTPException:
            return {'ready': False, 'job_id': state.request_scores(key).id}
        return json_bytes(state.payload(f'scores|{key}', lambda: {
            'ready': True, 'feature': key, 'suspicion': np.round(sc['suspicion'], 3),
            'suggested': sc['suggested'], 'outlier': np.round(sc['outlier'], 4)}))

    @r.post('/api/projection')
    def projection(req: ProjectionRequest):
        feature_or_400(req.feature)
        if req.classes is not None and any(c < 0 or (state.ready and c >= len(state.classes)) for c in req.classes):
            raise HTTPException(400, f'invalid classes: {req.classes}')
        try:
            proj, key, job = state.request_projection(req.feature, req.method, req.params, req.classes or None)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if proj is None:
            return {'ready': False, 'key': key, 'job_id': job.id}
        return json_bytes(state.payload(f'proj|{key}', lambda: {
            'ready': True, 'key': key, 'id': proj['id'], 'x': np.round(proj['xy'][:, 0], 4),
            'y': np.round(proj['xy'][:, 1], 4), 'info': proj['info']}))

    @r.post('/api/groups')
    async def groups(req: GroupsRequest):
        try:
            params = normalize_params(req.method, req.params)
        except ValueError as e:
            raise HTTPException(400, str(e))
        key = projection_key(req.feature, req.method, params, req.classes or None)
        res = await state.groups_async(key, req.min_cluster_size, req.per_class)
        if res is None:
            raise HTTPException(409, 'projection not computed yet')
        group, groups = res
        # -2 = a box a curation fix removed (not an outlier); the edit counter keys the payload
        return json_bytes(state.payload(f'groups|{key}|{req.min_cluster_size}|{req.per_class}|{state.edit_seq}', lambda: {
            'key': key, 'group': group, 'groups': groups, 'n_outliers': int((group == -1).sum())}))

    return r
