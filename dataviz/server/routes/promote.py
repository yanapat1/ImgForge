"""
"Promote to labeling" of a combined dataset (data/promote.py), under /s/<union id>/ like every session route:

    POST /api/promote   {target, name, project_id, task_id, dedup, labels, priority, include_background, create_viz}
                        -> {job_id}; the job's result: {project_id, task_id, viz_session_id, n_media, n_skipped_dup,
                        n_existing, n_annotations, classes}

The owner of the combined dataset only; target 'existing' also needs labeler on that labeling session (checked in
the job's first step, before anything is copied).
"""

import orjson
from fastapi import APIRouter, Depends, HTTPException

from ...auth import context, deps
from ...data import promote
from ..deps import json_bytes as _send


def json_bytes(obj):
    return _send(orjson.dumps(obj, option=orjson.OPT_SERIALIZE_NUMPY))


def router(state, ctx):
    r = APIRouter(dependencies=[Depends(deps.require_bound('viewer'))])

    @r.post('/api/promote')
    def start_promote(body: dict | None = None, user=Depends(deps.current_user)):
        b = context.bound()
        if b is None or b.role != 'owner':
            raise HTTPException(403, 'only the owner of the combined dataset can promote it')
        if ctx.labeling is None:
            raise HTTPException(501, 'labeling sessions are not enabled in this server')
        st = ctx.current()
        if getattr(st.source, 'kind', None) != 'union':
            raise HTTPException(409, 'promote needs a combined dataset (pick 2+ datasets on the home page)')
        opts = dict(body or {})
        if opts.get('target', 'new') not in ('new', 'existing') or \
                opts.get('dedup', 'hash') not in ('hash', 'none') or \
                opts.get('labels', 'prefer') not in ('prefer', 'merge', 'all'):
            raise HTTPException(400, 'target: new | existing, dedup: hash | none, labels: prefer | merge | all')
        if opts.get('target') == 'existing' and not (opts.get('project_id') and opts.get('task_id')):
            raise HTTPException(400, 'pick the labeling session and its task')

        def fn(job):
            try:
                return promote.run(st, ctx, user, opts, job)
            except HTTPException as e:
                raise ValueError(str(e.detail)) from None

        job = st.submit(f'promote:{st.tag}', f'Promote to labeling · {st.source.name}', fn, lane='io',
                        public_result=True)
        return json_bytes({'job_id': job.id})

    return r
